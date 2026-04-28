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

from corlinman_user_model.store import UserModelStore
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
# ---------------------------------------------------------------------------


_REDACTION_TOKEN = "[REDACTED]"

# Order matters: URLs before emails (URL can contain '@' in userinfo),
# emails before phone (some emails contain digit runs that would otherwise
# match the phone pattern).
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://\S+"), _REDACTION_TOKEN),
    (re.compile(r"\S+@\S+"), _REDACTION_TOKEN),
    (re.compile(r"\b\d{18}\b"), _REDACTION_TOKEN),  # Chinese ID number
    (re.compile(r"\b\d{11}\b"), _REDACTION_TOKEN),  # Chinese mobile number
)


def redact_text(content: str) -> str:
    """Strip the obvious PII shapes before anything leaves the box.

    Intentionally narrow: we don't try to be a NER. The goal is to
    catch the four shapes that show up most often in real chats and
    that the LLM is likely to echo back. Anything subtler is the LLM's
    own "no PII" instruction's job to refuse.
    """
    redacted = content
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


# ---------------------------------------------------------------------------
# Prompt — single source of truth.
# ---------------------------------------------------------------------------


DISTILL_SYSTEM_PROMPT = """你是一个用户画像分析师。读下面这段对话，抽取**关于用户**的稳定特征
（不是临时状态，不是事实陈述）。每条 trait 有 4 个字段：

  kind: interest | tone | topic | preference
  value: <30字以内的中文短语>
  confidence: 0.0–1.0（这条 trait 的置信度，0.4 以下不要输出）
  evidence: <从对话里摘取的一句话>

输出严格 JSON：[{kind, value, confidence, evidence}, ...]
绝对不要包含用户的姓名 / 联系方式 / 地址 / 其他 PII。"""


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
) -> list[UserTrait]:
    """Distil one session into traits and persist them.

    Returns the list of traits that were actually upserted (i.e. passed
    the confidence floor and parsed cleanly). An empty list is a normal
    return value when the LLM finds nothing stable enough.

    ``user_id`` defaults to the session's ``session_key`` — that is the
    gateway's stable identifier and already follows the
    ``"<channel>:<sender_id>"`` convention from hermes-agent. Callers
    can override when they need to attribute traits to something other
    than the session originator.
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
