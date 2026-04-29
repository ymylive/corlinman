"""LLM-driven session → traits distillation.

Pipeline for one ``session_id``:

1. Pull every turn out of ``sessions.sqlite`` (the read-only Rust-owned
   store; we open with ``mode=ro`` to be safe).
2. Run :func:`redact_text` over each turn's content. **No raw user
   input is sent to the LLM.**
3. Build the canonical extraction prompt and call the configured
   provider via ``corlinman-providers``' ``CorlinmanProvider`` Protocol.
4. Parse the JSON response, drop entries below the configured
   confidence floor, derive a stable ``user_id`` per turn (we expect
   the gateway to write ``"<channel>:<sender_id>"`` into ``session_key``
   already, but if not we synthesise from session_id), and upsert each
   accepted trait into ``user_model.sqlite``.

The LLM call is deliberately abstracted via an injectable
``llm_caller`` parameter so tests can pin a fixed JSON response without
mocking the whole provider stack. Real callers pass
:func:`default_llm_caller`, which uses
``corlinman-providers``' resolver.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from corlinman_user_model.store import DEFAULT_TENANT_ID, UserModelStore
from corlinman_user_model.traits import TraitKind, UserTrait

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DistillerConfig:
    """All knobs the distill loop needs.

    Defaults match ``[user_model]`` in ``docs/design/phase3-roadmap.md``
    §6 — distill after 5 turns, drop traits below 0.4 confidence,
    redaction on. ``deepseek-chat`` is picked as the default for cost;
    operators can swap via ``--llm-model`` on the CLI.
    """

    db_path: Path = Path("/data/user_model.sqlite")
    sessions_db_path: Path = Path("/data/sessions.sqlite")
    distill_after_session_turns: int = 5
    trait_confidence_floor: float = 0.4
    redaction_enabled: bool = True
    llm_model: str = "deepseek-chat"


# ---------------------------------------------------------------------------
# Redaction — regex only by design (no spacy / no model dependency).
#
# Phase 3.1 expansion: the v0 regex set caught only Chinese-domestic ID + mobile
# + bare email/URL shapes. Real chat traffic also carries international phone
# numbers (with separators), 17+X ID numbers, bank card PANs (with Luhn check
# to dodge invoice / order-id false positives), IPv4 / IPv6 addresses, and QQ
# numbers. The set below is still pure-regex by design — adding spaCy /
# Presidio would balloon the cold-start dep tree for marginal recall gains and
# we'd still need belt-and-suspenders LLM output filtering downstream.
# ---------------------------------------------------------------------------


_REDACTION_TOKEN = "[REDACTED]"


def _luhn_ok(number: str) -> bool:
    """Luhn check digit. Accepts digit-only strings; returns False otherwise.

    Bank-card PANs (13-19 digits) are the false-positive nightmare of the
    bare-digit pattern: any long-ish numeric run looks like a card. Luhn
    catches >90% of these collisions for ~10 lines of arithmetic, with no
    network call.
    """
    if not number or not number.isdigit():
        return False
    total = 0
    parity = len(number) % 2
    for idx, ch in enumerate(number):
        digit = ord(ch) - ord("0")
        if idx % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# Each entry: (compiled regex, optional post-match validator, replacement).
# The validator (if present) is called with the full match text and must
# return True for the match to be redacted — used by bank-card to enforce
# the Luhn check.
#
# Order matters and is load-bearing:
# 1. URL first — its body can contain '@' (userinfo), digits (port / IP),
#    and '.' (hostname); we don't want partial redactions inside a URL.
# 2. Email next — '@'-form is a strict subset of "non-URL".
# 3. ID-X (17 digits + X) — most specific, claim before any digit-only
#    pattern can chew the prefix.
# 4. Bank card with Luhn before international phone — long bare digit
#    runs that pass Luhn are PANs; the international-phone regex (which
#    accepts no separators between groups too) would otherwise eat the
#    leading 12-15 digits of a 16-digit PAN and leave a stub. Running
#    Luhn first preserves the "Luhn-invalid runs are passed through"
#    invariant that the test pins.
# 5. International phone (with required separators or '+' prefix) — only
#    matches obviously-phone-shaped strings now that bank cards already
#    ran. The [\s\-] requirement on at least one separator (or a leading
#    '+') keeps it from eating bare 12-digit invoice ids.
# 6. Chinese mobile fallback — narrow `1\d{10}` only.
# 7. IPv6 before IPv4 — IPv6 can contain colons + hex; IPv4 is straight
#    dotted decimal. Running v6 first prevents partial IPv4-in-v6-tail.
# 8. QQ last — narrow keyword anchor.
_REDACTION_PATTERNS: tuple[
    tuple[re.Pattern[str], "Callable[[str], bool] | None", str], ...
] = (
    (re.compile(r"https?://\S+"), None, _REDACTION_TOKEN),
    (re.compile(r"\S+@\S+"), None, _REDACTION_TOKEN),
    # Chinese ID with X check digit (17 digits + X/x).
    (re.compile(r"\b\d{17}[\dXx]\b"), None, _REDACTION_TOKEN),
    # Bank card PAN: 13-19 digit run, Luhn-validated. Luhn keeps
    # order-ids / invoice numbers / catalog SKUs from being flagged.
    (re.compile(r"\b\d{13,19}\b"), _luhn_ok, _REDACTION_TOKEN),
    # International phone: either a leading '+' OR at least one
    # space/hyphen between digit groups. Without one of those signals
    # we'd false-match arbitrary 11-15 digit runs (which the bank-card
    # pass already covers when they're real PANs). The non-capturing
    # alternation pins the "must look phone-shaped" rule.
    (
        re.compile(
            r"(?:"
            r"\+\d{1,3}[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}"
            r"|"
            r"\b\d{1,3}[\s\-]\d{2,4}[\s\-]\d{3,4}[\s\-]\d{3,4}\b"
            r")"
        ),
        None,
        _REDACTION_TOKEN,
    ),
    # Chinese mobile fallback — fewer false positives now that bank-card
    # ran first. Kept for back-compat with the v0 redaction contract.
    (re.compile(r"\b1\d{10}\b"), None, _REDACTION_TOKEN),
    # IPv6 — covers both the long form (`2001:0db8:...:7334`) and the
    # compressed form (`2001:db8::1`). Two alternatives:
    #   * `(?:[hex]{1,4}:){2,7}[hex]{1,4}` for at-least-three-groups
    #     uncompressed addresses.
    #   * a `::`-anchored pattern that allows zero-to-six groups on
    #     each side and a final optional trailing group. The trailing
    #     group is optional because addresses like `fe80::` or
    #     `2001:db8::` are still valid; without `(?:...)?` we'd miss
    #     the head when only the suffix carried digits.
    (
        re.compile(
            r"(?:"
            r"(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}"
            r"|"
            r"(?:[A-Fa-f0-9]{1,4}:){1,6}:[A-Fa-f0-9]{0,4}(?::[A-Fa-f0-9]{1,4}){0,5}"
            r")"
        ),
        None,
        _REDACTION_TOKEN,
    ),
    # IPv4 — strict dotted decimal. No octet-range guard: that's a NER
    # concern; the regex catches 999.999.999.999 too which is fine
    # because a redacted IP is harmless even if synthetic.
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), None, _REDACTION_TOKEN),
    # QQ number: keyword-anchored. Bare 5-12 digit runs are too noisy
    # without the `QQ` prefix, but with it, this is a high-precision hit.
    (
        re.compile(r"\bQQ\s*[:：]?\s*\d{5,12}\b", re.IGNORECASE),
        None,
        _REDACTION_TOKEN,
    ),
)


def redact_text(content: str) -> str:
    """Strip the obvious PII shapes before anything leaves the box.

    Intentionally regex-only: we don't try to be a NER. Each pattern is
    paired with an optional validator (e.g. Luhn for bank cards) so we
    don't trade phone-PII recall for invoice-id false positives. See the
    block comment above for the ordering rationale.
    """
    redacted = content
    for pattern, validator, replacement in _REDACTION_PATTERNS:
        if validator is None:
            redacted = pattern.sub(replacement, redacted)
        else:
            redacted = pattern.sub(
                lambda m: replacement if validator(m.group(0)) else m.group(0),
                redacted,
            )
    return redacted


def _trait_value_has_pii(value: str) -> bool:
    """Belt-and-suspenders check on LLM-emitted trait values.

    The system prompt tells the model not to echo PII, but prompts are
    not security boundaries. After the model returns, we run the same
    redaction pass over each `value` field and drop traits where the
    pre/post differ — that means the regex caught something. Cheaper
    than letting a compromised model populate `user_traits` with an
    email address that survives every future placeholder render.
    """
    return redact_text(value) != value


# ---------------------------------------------------------------------------
# Prompt — single source of truth.
# ---------------------------------------------------------------------------


DISTILL_SYSTEM_PROMPT = """你是一个用户画像分析师。读下面这段对话，抽取**关于用户**的稳定特征
（不是临时状态，不是事实陈述）。每条 trait 有 3 个字段：

  kind: interest | tone | topic | preference
  value: <30字以内的中文短语，描述特征本身，不要原文摘抄>
  confidence: 0.0–1.0（这条 trait 的置信度，0.4 以下不要输出）

输出严格 JSON：[{kind, value, confidence}, ...]
绝对不要在 value 里包含用户的姓名 / 电话 / 邮箱 / 身份证 / 银行卡 / IP /
QQ / 微信 / 地址 或任何其他 PII。也不要输出对话原文摘录字段。"""


# Type alias for the LLM call — narrow on purpose so tests can pass
# ``async def fake(prompt, transcript) -> "[...]"`` without importing
# the provider stack.
LLMCaller = Callable[[str, str], Awaitable[str]]


# ---------------------------------------------------------------------------
# Sessions store — read-only sqlite, schema mirrors
# ``rust/crates/corlinman-core/src/session_sqlite.rs``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionTurn:
    """One row out of ``sessions``."""

    session_key: str
    seq: int
    role: str
    content: str
    ts: str


def read_session_turns(sessions_db_path: Path, session_id: str) -> list[SessionTurn]:
    """Read every turn for ``session_id`` ordered by ``seq``.

    Synchronous — the Rust gateway uses WAL so concurrent open is fine,
    and the call site is already inside an async function so wrapping
    every fetch in ``aiosqlite`` would just add ceremony for no gain
    on a single-shot read.
    """
    if not sessions_db_path.exists():
        return []
    uri = f"file:{sessions_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cursor = conn.execute(
            """SELECT session_key, seq, role, content, ts
               FROM sessions
               WHERE session_key = ?
               ORDER BY seq ASC""",
            (session_id,),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()
    return [
        SessionTurn(
            session_key=str(r[0]),
            seq=int(r[1]),
            role=str(r[2]),
            content=str(r[3]) if r[3] is not None else "",
            ts=str(r[4]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def distill_session(
    config: DistillerConfig,
    session_id: str,
    *,
    llm_caller: LLMCaller,
    user_id: str | None = None,
    now_ms: int | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[UserTrait]:
    """Distil one session into traits and persist them.

    Returns the list of traits that were actually upserted (i.e. passed
    the confidence floor and parsed cleanly). An empty list is a normal
    return value when the LLM finds nothing stable enough.

    ``user_id`` defaults to the session's ``session_key`` — that is the
    gateway's stable identifier and already follows the
    ``"<channel>:<sender_id>"`` convention from hermes-agent. Callers
    can override when they need to attribute traits to something other
    than the session originator. ``tenant_id`` is Phase 3.1 plumbing —
    defaults to ``'default'`` until Phase 4 wires multi-tenant ids.
    """
    turns = read_session_turns(config.sessions_db_path, session_id)
    if len(turns) < config.distill_after_session_turns:
        logger.info(
            "user_model.distill.skipped_short_session",
            extra={"session_id": session_id, "turns": len(turns)},
        )
        return []

    transcript = _build_transcript(
        turns, redaction_enabled=config.redaction_enabled
    )

    raw_response = await llm_caller(DISTILL_SYSTEM_PROMPT, transcript)
    parsed = _parse_llm_response(raw_response, floor=config.trait_confidence_floor)

    if not parsed:
        logger.info(
            "user_model.distill.no_traits",
            extra={"session_id": session_id},
        )
        return []

    resolved_user_id = user_id or _user_id_from_turns(turns, fallback=session_id)
    timestamp_ms = now_ms if now_ms is not None else int(time.time() * 1_000)

    persisted: list[UserTrait] = []
    store = await UserModelStore.open_or_create(config.db_path)
    async with store as s:
        for entry in parsed:
            await s.upsert_trait(
                user_id=resolved_user_id,
                trait_kind=entry.kind,
                trait_value=entry.value,
                confidence=entry.confidence,
                session_id=session_id,
                now_ms=timestamp_ms,
                tenant_id=tenant_id,
            )
            persisted.append(
                UserTrait(
                    user_id=resolved_user_id,
                    trait_kind=entry.kind,
                    trait_value=entry.value,
                    confidence=entry.confidence,
                    first_seen=timestamp_ms,
                    last_seen=timestamp_ms,
                    session_ids=(session_id,),
                )
            )
    return persisted


# ---------------------------------------------------------------------------
# Helpers — kept module-private; tests import the public entrypoint and
# pass a fake ``llm_caller``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ParsedTrait:
    kind: TraitKind
    value: str
    confidence: float


def _build_transcript(
    turns: Iterable[SessionTurn], *, redaction_enabled: bool
) -> str:
    """Render turns as ``role: content`` with optional redaction.

    We only feed user/assistant turns to the LLM — tool calls and
    tool-result frames are noise for trait extraction and would just
    eat context budget.
    """
    lines: list[str] = []
    for turn in turns:
        if turn.role not in ("user", "assistant"):
            continue
        content = turn.content
        if not content:
            continue
        if redaction_enabled:
            content = redact_text(content)
        lines.append(f"{turn.role}: {content}")
    return "\n".join(lines)


def _user_id_from_turns(turns: Iterable[SessionTurn], *, fallback: str) -> str:
    """Pick a stable user_id.

    The gateway is expected to write ``"<channel>:<sender_id>"`` into
    ``session_key`` (per the hermes-agent-inspired convention in the
    task spec). When that fails for any reason we fall back to the
    session_id so traits still land somewhere queryable rather than
    getting silently dropped.
    """
    for turn in turns:
        if turn.session_key:
            return turn.session_key
    return fallback


def _parse_llm_response(raw: str, *, floor: float) -> list[_ParsedTrait]:
    """Coerce a (sometimes-fenced) JSON response into typed traits.

    Robust to:
      * markdown fencing (```json ... ```)
      * leading / trailing whitespace
      * unknown ``kind`` strings (drop the entry, don't crash)
      * confidence below the floor (drop)
      * empty / non-string ``value`` (drop)
      * PII that survived the prompt's "no PII" instruction — the
        regex pass over ``value`` (see :func:`_trait_value_has_pii`)
        drops those traits before they hit disk. The model's output
        is **not** a security boundary; this filter is.
      * an ``evidence`` field if present — silently dropped, never
        persisted, never surfaced. The system prompt no longer asks
        for it (see ``DISTILL_SYSTEM_PROMPT``) but older / fine-tuned
        models can still produce one and we refuse to write it.

    A whole-response parse failure returns an empty list and logs.
    """
    cleaned = _strip_code_fence(raw).strip()
    if not cleaned:
        return []
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("user_model.distill.bad_json", extra={"error": str(exc)})
        return []
    if not isinstance(decoded, list):
        return []
    out: list[_ParsedTrait] = []
    for entry in decoded:
        if not isinstance(entry, dict):
            continue
        kind = TraitKind.parse(str(entry.get("kind", "")))
        if kind is None:
            continue
        value = entry.get("value")
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value:
            continue
        try:
            confidence = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < floor:
            continue
        # Belt-and-suspenders: re-run the redaction regex over the
        # value the LLM produced. If anything matches we drop the
        # trait outright — a partial redaction would leave a
        # half-PII trait on disk and we'd rather lose recall than
        # quietly persist `email_user_at_example_dot_com` shapes.
        if _trait_value_has_pii(value):
            logger.warning(
                "user_model.distill.dropped_pii_trait",
                extra={"kind": kind.value},
            )
            continue
        # ``evidence`` was the largest PII ingress in the v0 prompt
        # (the LLM pasted raw user content back). Even if the model
        # still emits it (older fine-tunes, prompt regression), we
        # never read it and it never reaches the store.
        out.append(_ParsedTrait(kind=kind, value=value, confidence=confidence))
    return out


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _strip_code_fence(raw: str) -> str:
    """Pull the body out of a ``` ```json ... ``` ``` fence if present."""
    m = _FENCE_RE.match(raw.strip())
    if m:
        return m.group(1)
    return raw


# ---------------------------------------------------------------------------
# Default provider-backed LLM caller.
#
# Kept thin: tests don't import this — they pass their own ``llm_caller``.
# We import ``corlinman_providers`` lazily so an environment without the
# providers' optional vendor SDKs can still ``import corlinman_user_model``
# (e.g. the placeholder resolver path).
# ---------------------------------------------------------------------------


async def default_llm_caller(system_prompt: str, transcript: str) -> str:
    """Call the configured provider via the legacy ``resolve()`` shim.

    This is intentionally minimal: one user message containing the
    system prompt + transcript. The reasoning loop's full streaming
    machinery is overkill for a one-shot extraction. We aggregate the
    streamed tokens into a single string and return.

    Operators who need per-spec params should wire their own
    ``LLMCaller`` instead — this default is the "just works on a fresh
    box" path.
    """
    # Local import: keeps the placeholder resolver path importable in
    # contexts where the provider stack isn't fully configured.
    from corlinman_providers import resolve

    provider = resolve("deepseek-chat")
    chunks: list[str] = []

    class _Msg:
        # The Protocol only checks for ``role`` and ``content`` attributes.
        def __init__(self, role: str, content: str) -> None:
            self.role = role
            self.content = content

    messages = [
        _Msg("system", system_prompt),
        _Msg("user", transcript),
    ]
    stream = provider.chat_stream(model="deepseek-chat", messages=messages)
    async for chunk in stream:
        if chunk.kind == "token" and chunk.text:
            chunks.append(chunk.text)
    return "".join(chunks)


__all__ = [
    "DISTILL_SYSTEM_PROMPT",
    "DistillerConfig",
    "LLMCaller",
    "SessionTurn",
    "default_llm_caller",
    "distill_session",
    "read_session_turns",
    "redact_text",
]
