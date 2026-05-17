"""LLM-driven background review — autonomous skill + memory consolidation.

Port of hermes-agent's background review fork
(``/tmp/hermes-agent-shallow/agent/background_review.py``).

Unlike the pure deterministic curator (:mod:`.curator`), this module makes
one LLM call with a strict tool-call schema and writes the resulting
mutations back to disk. The hermes implementation forks a full ``AIAgent``
inside the same Python process; corlinman's analogue is more conservative
— it is a **scoped runner** that:

1. Streams a single chat completion from the parent's provider.
2. Re-assembles ``tool_call_*`` chunks into discrete tool calls.
3. Dispatches the result through a hard-coded **whitelist** of two tools.
4. Writes mutations through :mod:`corlinman_skills_registry`'s existing
   safe-write paths (atomic tempfile + ``os.replace``).

Tool whitelist
--------------

The LLM can call ONLY these (anything else is dropped with a warning):

* ``skill_manage(action: "create"|"edit"|"patch"|"delete", name, ...)``
* ``memory_write(target: "MEMORY"|"USER", action: "append"|"replace", content)``

This guarantees the background review can never escape the profile
directory or execute side-effects (no terminal, no web, no arbitrary
file IO).

Review kinds
------------

* ``"memory"``           — only update ``MEMORY.md`` / ``USER.md``
* ``"skill"``            — only create/patch SKILL.md files
* ``"combined"``         — both, in one prompt
* ``"curator"``          — overlap consolidation (folds duplicate
                            ``agent-created`` skills under one umbrella)
* ``"user-correction"``  — process a specific user-correction signal and
                            patch the implicated skill body

Failure mode
------------

:func:`spawn_background_review` **never raises**. Provider failures,
timeouts, malformed tool calls, and disk write errors all surface as a
:class:`BackgroundReviewReport` whose ``error`` field is populated. The
gateway calls this in a fire-and-forget background task; if the fork
crashes we want a structured artefact, not an unhandled exception
killing the asyncio loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from corlinman_skills_registry import (
    Skill,
    SkillRegistry,
    SkillRequirements,
    bump_patch,
    write_skill_md,
)

logger = structlog.get_logger(__name__)


# ─── Public types ────────────────────────────────────────────────────


ReviewKind = Literal["memory", "skill", "combined", "curator", "user-correction"]


# Hard whitelist; the dispatcher refuses anything else. Kept module-level
# so tests can ``in WHITELISTED_TOOLS`` without re-instantiating anything.
WHITELISTED_TOOLS: frozenset[str] = frozenset({"skill_manage", "memory_write"})


@dataclass(frozen=True)
class ReviewWriteRecord:
    """One mutation the LLM proposed and we performed (or skipped).

    ``applied=False`` always carries a ``skipped_reason``; ``applied=True``
    leaves it as ``None``. The dispatcher stamps both shapes uniformly so
    the gateway / UI can render a single audit-row format.
    """

    tool: str           # "skill_manage" | "memory_write"
    action: str         # "create" | "edit" | "patch" | "append" | "replace" | "delete"
    target: str         # skill name or "MEMORY" / "USER"
    applied: bool       # True if we actually wrote
    skipped_reason: str | None = None  # populated when applied=False


@dataclass(frozen=True)
class BackgroundReviewReport:
    """Audit artefact produced by one :func:`spawn_background_review` call.

    Mirrors the hermes summarisation shape (`agent/background_review.py:218`)
    in spirit, but is structured rather than a flat list of strings — the
    gateway needs both the human summary AND machine-readable records to
    drive the admin UI's curator preview.
    """

    profile_slug: str
    kind: ReviewKind
    started_at: datetime
    finished_at: datetime
    writes: list[ReviewWriteRecord]
    error: str | None = None  # populated on failure

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def applied_count(self) -> int:
        return sum(1 for w in self.writes if w.applied)

    @property
    def skipped_count(self) -> int:
        return sum(1 for w in self.writes if not w.applied)


# ─── Prompt loading ─────────────────────────────────────────────────


_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

# Map kind -> filename. Kept as a const so callers can iterate it for docs.
_PROMPT_FILES: dict[ReviewKind, str] = {
    "memory": "memory_review.md",
    "skill": "skill_review.md",
    "combined": "combined_review.md",
    "curator": "curator_review.md",
    "user-correction": "user_preference_patch.md",
}


def load_prompt(kind: ReviewKind) -> str:
    """Read the markdown prompt template for ``kind``.

    Templates ship inside the package next to this module; we resolve
    relative to ``__file__`` so editable installs and built wheels both
    work without packaging gymnastics.
    """
    filename = _PROMPT_FILES.get(kind)
    if filename is None:
        raise ValueError(f"unknown review kind: {kind!r}")
    return (_PROMPT_DIR / filename).read_text(encoding="utf-8")


# ─── Whitelisted-tool dispatcher ─────────────────────────────────────


# Strict skill-name guard: alphanumeric + dash + underscore. Crucially no
# '/', no '..', no '.', so even if the LLM hallucinates a path-traversal
# attempt, the resulting name fails the regex and the create call is
# dropped before we touch the filesystem.
_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")


def _is_safe_skill_name(name: object) -> bool:
    """Return True only if ``name`` is a non-empty bare identifier-style
    string. Used by the dispatcher to refuse path-traversal attempts and
    other malformed names without ever calling ``Path.resolve``.
    """
    return isinstance(name, str) and bool(_SAFE_SKILL_NAME.match(name))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _atomic_write(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` atomically.

    Mirrors :func:`corlinman_skills_registry.parse.write_skill_md`'s
    tempfile + ``os.replace`` pattern so MEMORY.md / USER.md writes share
    the same crash-safety guarantees the skill writer offers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fp:
            fp.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _bump_patch_version(current: str) -> str:
    """Increment a semver patch level. Tolerates malformed input by
    falling back to ``"1.0.1"`` — the field is best-effort metadata, not
    a contract.
    """
    parts = (current or "").split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2])
    except (IndexError, ValueError):
        return "1.0.1"
    return f"{major}.{minor}.{patch + 1}"


def _skill_dir(profile_root: Path, name: str) -> Path:
    """Resolve the directory for skill ``name`` under ``profile_root``.

    Mirrors hermes' ``skills/<name>/SKILL.md`` layout. The caller has
    already passed ``name`` through :func:`_is_safe_skill_name` so we can
    trust the join here.
    """
    return profile_root / "skills" / name


def _skill_md_path(profile_root: Path, name: str) -> Path:
    return _skill_dir(profile_root, name) / "SKILL.md"


async def _apply_tool_calls(
    *,
    tool_calls: list[dict],
    profile_root: Path,
    registry: SkillRegistry,
    review_origin_tag: str = "background_review",
    now: datetime | None = None,
) -> list[ReviewWriteRecord]:
    """Apply each tool call inside the whitelist; drop the rest.

    Each ``tool_call`` is the OpenAI-shape ``{"id", "type": "function",
    "function": {"name", "arguments"}}`` blob, OR the raw flattened
    ``{"tool", "action", "name", ...}`` shape produced by the mock
    provider's fake tool_call path. We accept both so test harnesses
    don't have to mimic the full OpenAI envelope.

    Path-traversal defence: every disk write resolves through the
    whitelisted skill-name regex + ``profile_root`` join so we can never
    escape the profile root. Skill files go through
    :func:`corlinman_skills_registry.write_skill_md`; MEMORY/USER files
    go through :func:`_atomic_write` (same tempfile pattern).
    """
    now = now or _utc_now()
    records: list[ReviewWriteRecord] = []

    for raw_call in tool_calls or []:
        call = _normalise_tool_call(raw_call)
        if call is None:
            records.append(
                ReviewWriteRecord(
                    tool="unknown",
                    action="unknown",
                    target="",
                    applied=False,
                    skipped_reason="malformed_tool_call",
                )
            )
            continue

        tool = call.get("tool")
        if tool not in WHITELISTED_TOOLS:
            records.append(
                ReviewWriteRecord(
                    tool=str(tool or "unknown"),
                    action=str(call.get("action") or "unknown"),
                    target=str(call.get("name") or call.get("target") or ""),
                    applied=False,
                    skipped_reason="not_whitelisted",
                )
            )
            continue

        if tool == "skill_manage":
            records.append(
                await _apply_skill_manage(
                    call,
                    profile_root=profile_root,
                    registry=registry,
                    now=now,
                )
            )
        elif tool == "memory_write":
            records.append(
                _apply_memory_write(
                    call,
                    profile_root=profile_root,
                    now=now,
                )
            )

    return records


def _normalise_tool_call(raw: Any) -> dict[str, Any] | None:
    """Coerce an OpenAI-shape tool_call OR a flat dict into the flat
    ``{"tool", "action", ...}`` shape the dispatcher consumes.

    Returns ``None`` if the shape is unrecognisable — the dispatcher
    surfaces that as a ``malformed_tool_call`` record.
    """
    if not isinstance(raw, dict):
        return None

    # Already-flat shape: ``{"tool": "skill_manage", "action": "create", ...}``
    if "tool" in raw:
        return dict(raw)

    # OpenAI shape: ``{"type": "function", "function": {"name": "...", "arguments": "..."}}``
    fn = raw.get("function") if isinstance(raw.get("function"), dict) else None
    if fn is None:
        return None
    name = fn.get("name")
    args_raw = fn.get("arguments")
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw)
        except (TypeError, json.JSONDecodeError):
            return None
    elif isinstance(args_raw, dict):
        args = args_raw
    else:
        args = {}
    if not isinstance(args, dict):
        return None
    args = dict(args)
    args["tool"] = name
    return args


async def _apply_skill_manage(
    call: dict[str, Any],
    *,
    profile_root: Path,
    registry: SkillRegistry,
    now: datetime,
) -> ReviewWriteRecord:
    """Handle one ``skill_manage`` tool call.

    Refuses on malformed names, refuses to delete pinned / non-agent
    skills, otherwise writes through :func:`write_skill_md`.
    """
    action = str(call.get("action") or "")
    name = call.get("name")

    if not _is_safe_skill_name(name):
        return ReviewWriteRecord(
            tool="skill_manage",
            action=action or "unknown",
            target=str(name or ""),
            applied=False,
            skipped_reason="unsafe_name",
        )

    name = str(name)

    if action == "create":
        content = call.get("content")
        if not isinstance(content, str):
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason="missing_content",
            )
        md_path = _skill_md_path(profile_root, name)
        if md_path.exists():
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason="already_exists",
            )
        # Build a minimal Skill with the agent-created provenance.
        skill = Skill(
            name=name,
            description=_extract_description(content) or f"Agent-created skill: {name}",
            requires=SkillRequirements(),
            allowed_tools=[],
            body_markdown=content,
            source_path=md_path,
            version="1.0.0",
            origin="agent-created",
            state="active",
            pinned=False,
            created_at=now,
        )
        try:
            write_skill_md(md_path, skill)
        except OSError as err:
            logger.warning(
                "background_review.skill_create.io_error",
                name=name,
                err=str(err),
            )
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason=f"io_error: {err}",
            )
        return ReviewWriteRecord(
            tool="skill_manage",
            action=action,
            target=name,
            applied=True,
        )

    if action in ("edit", "patch"):
        md_path = _skill_md_path(profile_root, name)
        existing = registry.get(name)
        if existing is None:
            # Fall back to a fresh load from disk in case the registry
            # was constructed before this skill existed (tests, multi-
            # process). If still missing, refuse.
            if not md_path.exists():
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason="not_found",
                )
            # Re-load just this one file via parse_skill.
            from corlinman_skills_registry.parse import parse_skill

            try:
                existing = parse_skill(md_path, md_path.read_text(encoding="utf-8"))
            except Exception as err:
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason=f"parse_error: {err}",
                )

        if action == "edit":
            content = call.get("content")
            if not isinstance(content, str):
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason="missing_content",
                )
            new_body = content
        else:  # patch
            find = call.get("find")
            replace = call.get("replace")
            if not isinstance(find, str) or not isinstance(replace, str):
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason="missing_find_or_replace",
                )
            if find not in existing.body_markdown:
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason="find_not_in_body",
                )
            new_body = existing.body_markdown.replace(find, replace)

        existing.body_markdown = new_body
        existing.version = _bump_patch_version(existing.version)
        if existing.state == "archived":
            existing.state = "active"
        try:
            write_skill_md(md_path, existing)
        except OSError as err:
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason=f"io_error: {err}",
            )
        # Bump telemetry for the patch — best-effort.
        with contextlib.suppress(OSError):
            bump_patch(md_path.parent, now=now)
        return ReviewWriteRecord(
            tool="skill_manage",
            action=action,
            target=name,
            applied=True,
        )

    if action == "delete":
        md_path = _skill_md_path(profile_root, name)
        existing = registry.get(name)
        if existing is None and md_path.exists():
            from corlinman_skills_registry.parse import parse_skill

            try:
                existing = parse_skill(md_path, md_path.read_text(encoding="utf-8"))
            except Exception:
                existing = None
        if existing is None:
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason="not_found",
            )
        if existing.pinned or existing.origin != "agent-created":
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason="protected",
            )
        try:
            md_path.unlink(missing_ok=True)
        except OSError as err:
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason=f"io_error: {err}",
            )
        return ReviewWriteRecord(
            tool="skill_manage",
            action=action,
            target=name,
            applied=True,
        )

    return ReviewWriteRecord(
        tool="skill_manage",
        action=action or "unknown",
        target=name,
        applied=False,
        skipped_reason="unknown_action",
    )


def _apply_memory_write(
    call: dict[str, Any],
    *,
    profile_root: Path,
    now: datetime,
) -> ReviewWriteRecord:
    """Handle one ``memory_write`` tool call.

    Writes to ``profile_root/MEMORY.md`` or ``profile_root/USER.md``.
    Anything else is refused.
    """
    target = call.get("target")
    action = str(call.get("action") or "")
    content = call.get("content")

    if target not in ("MEMORY", "USER"):
        return ReviewWriteRecord(
            tool="memory_write",
            action=action or "unknown",
            target=str(target or ""),
            applied=False,
            skipped_reason="invalid_target",
        )
    if action not in ("append", "replace"):
        return ReviewWriteRecord(
            tool="memory_write",
            action=action or "unknown",
            target=target,
            applied=False,
            skipped_reason="invalid_action",
        )
    if not isinstance(content, str) or not content.strip():
        return ReviewWriteRecord(
            tool="memory_write",
            action=action,
            target=target,
            applied=False,
            skipped_reason="missing_content",
        )

    path = profile_root / ("MEMORY.md" if target == "MEMORY" else "USER.md")

    if action == "append":
        # One markdown bullet per append, with a timestamp prefix the
        # hermes UI also uses for memory rows.
        timestamp = now.isoformat(timespec="seconds")
        prefix_existing = ""
        if path.exists():
            try:
                prefix_existing = path.read_text(encoding="utf-8")
            except OSError:
                prefix_existing = ""
            if prefix_existing and not prefix_existing.endswith("\n"):
                prefix_existing += "\n"
        new_line = f"- [{timestamp}] {content.strip()}\n"
        payload = prefix_existing + new_line
    else:  # replace
        payload = content if content.endswith("\n") else content + "\n"

    try:
        _atomic_write(path, payload)
    except OSError as err:
        return ReviewWriteRecord(
            tool="memory_write",
            action=action,
            target=target,
            applied=False,
            skipped_reason=f"io_error: {err}",
        )
    return ReviewWriteRecord(
        tool="memory_write",
        action=action,
        target=target,
        applied=True,
    )


def _extract_description(content: str) -> str | None:
    """Pull a short description from the first non-empty body line.

    Cheap default for ``skill_manage(action="create")`` callers that
    don't bother including ``description:`` frontmatter. We strip ``#``
    so the first ``# Title`` line becomes a sane description.
    """
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:200]
    return None


# ─── Provider invocation ────────────────────────────────────────────


# The schemas we advertise to the model. The model may invent tool calls
# outside this set; the dispatcher drops them. We list them here purely
# so providers that accept a tool schema can be told what we expect.
_TOOL_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "skill_manage",
            "description": (
                "Create, edit, patch, or delete a SKILL.md in the active "
                "profile's skills/ directory."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "name"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "edit", "patch", "delete"],
                    },
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_write",
            "description": (
                "Append or replace memory content scoped to the active profile."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["target", "action", "content"],
                "properties": {
                    "target": {"type": "string", "enum": ["MEMORY", "USER"]},
                    "action": {"type": "string", "enum": ["append", "replace"]},
                    "content": {"type": "string"},
                },
            },
        },
    },
]


def _summarise_skills(registry: SkillRegistry, *, limit: int = 50) -> list[dict[str, Any]]:
    """Build a compact summary of the active registry for the prompt.

    We include only the metadata the model needs to make a routing
    decision — name, origin, state, version, first line of body. The full
    body is left off because (a) it can be large, (b) the curator review
    primarily needs to see the *shape* of the library, not the contents.
    """
    summary: list[dict[str, Any]] = []
    for skill in registry:
        if len(summary) >= limit:
            break
        first_line = ""
        for line in skill.body_markdown.splitlines():
            if line.strip():
                first_line = line.strip()[:200]
                break
        summary.append(
            {
                "name": skill.name,
                "description": skill.description,
                "origin": skill.origin,
                "state": skill.state,
                "version": skill.version,
                "pinned": skill.pinned,
                "first_line": first_line,
            }
        )
    return summary


async def _collect_tool_calls_from_stream(
    stream: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    """Drain an ``AsyncIterator[ProviderChunk]`` into ``(tool_calls, error)``.

    Re-assembles ``tool_call_start`` / ``tool_call_delta`` /
    ``tool_call_end`` chunks into discrete ``{"tool", "id",
    "arguments_json"}`` records, then parses the JSON.

    Returns ``([], "no_chunks")`` if the iterator yielded nothing — useful
    signal for tests that pass a degenerate provider. Provider errors
    (``finish_reason == "error"``) surface as a non-None error string.
    """
    in_progress: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    saw_anything = False
    error: str | None = None

    async for chunk in stream:
        saw_anything = True
        kind = getattr(chunk, "kind", None)
        if kind == "tool_call_start":
            tcid = chunk.tool_call_id or f"call_{len(in_progress)}"
            in_progress[tcid] = {
                "id": tcid,
                "name": chunk.tool_name or "",
                "arguments": chunk.arguments_delta or "",
            }
        elif kind == "tool_call_delta":
            tcid = chunk.tool_call_id or ""
            entry = in_progress.get(tcid)
            if entry is not None and chunk.arguments_delta:
                entry["arguments"] += chunk.arguments_delta
        elif kind == "tool_call_end":
            tcid = chunk.tool_call_id or ""
            entry = in_progress.pop(tcid, None)
            if entry is not None:
                completed.append(entry)
        elif kind == "done":
            if chunk.finish_reason == "error":
                error = "provider_finish_reason_error"
            # Flush anything still in_progress at done.
            for entry in in_progress.values():
                completed.append(entry)
            in_progress.clear()
        # ``token`` chunks are ignored — the model is supposed to emit
        # tool_calls only; any prose it produces is discarded.

    if not saw_anything:
        return [], "no_chunks"

    # Convert the OpenAI-style intermediate records into the dispatcher's
    # flat shape.
    flat: list[dict[str, Any]] = []
    for entry in completed:
        args_raw = entry.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except (TypeError, json.JSONDecodeError):
            # Surface the malformed tool call so the dispatcher records it.
            flat.append({"tool": entry.get("name") or "unknown", "_malformed": True})
            continue
        if not isinstance(args, dict):
            flat.append({"tool": entry.get("name") or "unknown", "_malformed": True})
            continue
        args = dict(args)
        args["tool"] = entry.get("name") or args.get("tool")
        flat.append(args)

    return flat, error


async def spawn_background_review(
    *,
    kind: ReviewKind,
    profile_slug: str,
    profile_root: Path,
    recent_messages: list[dict[str, Any]],
    registry: SkillRegistry,
    provider: Any,
    model: str,
    timeout_seconds: float = 60.0,
    user_correction_text: str | None = None,
    now: datetime | None = None,
) -> BackgroundReviewReport:
    """One-shot LLM call → tool-call dispatcher → write mutations.

    Catches **every** exception — provider errors, timeouts, malformed
    tool calls, disk write failures — and surfaces them as a
    :class:`BackgroundReviewReport` with ``error`` populated. The gateway
    calls this in a fire-and-forget background task; raising would kill
    the asyncio task with no audit row.

    Mirrors hermes' :func:`spawn_background_review_thread` in shape
    (system prompt + conversation snapshot → tool calls → writes), but
    runs as a scoped async function rather than a fork-and-replay of a
    full :class:`AIAgent`.
    """
    started_at = now or _utc_now()
    profile_root = Path(profile_root)

    try:
        system_prompt = load_prompt(kind)
    except (OSError, ValueError) as err:
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error=f"prompt_load_failed: {err}",
        )

    if kind == "user-correction" and user_correction_text:
        system_prompt = (
            system_prompt
            + "\n\n## User correction\n\n"
            + user_correction_text.strip()
            + "\n"
        )

    # Build the user message. We send a structured JSON envelope so the
    # model has clear signal about "this is the snapshot, this is the
    # registry context". Tool-call models tolerate JSON in the user turn
    # without confusing it for tool input.
    try:
        skill_summary = _summarise_skills(registry)
    except Exception as err:
        # Registry inspection is best-effort; failures should not break
        # the review pipeline.
        logger.warning(
            "background_review.skill_summary_failed",
            profile_slug=profile_slug,
            err=str(err),
        )
        skill_summary = []

    user_envelope = {
        "profile_slug": profile_slug,
        "kind": kind,
        "recent_messages": list(recent_messages or []),
        "active_skills": skill_summary,
    }
    if kind == "user-correction" and user_correction_text:
        user_envelope["user_correction"] = user_correction_text

    user_message = (
        "Conversation snapshot + active skill registry follow as JSON. "
        "Emit tool_calls only.\n\n"
        + json.dumps(user_envelope, ensure_ascii=False, default=str)
    )

    chat_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    log = logger.bind(
        profile_slug=profile_slug,
        kind=kind,
        model=model,
    )

    try:
        tool_calls, stream_error = await asyncio.wait_for(
            _invoke_provider(provider=provider, model=model, messages=chat_messages),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        log.warning("background_review.timeout", timeout_seconds=timeout_seconds)
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error="timeout",
        )
    except asyncio.CancelledError:
        # Respect cooperative cancellation — re-raise after stamping the
        # report so the gateway can record what we did before the cancel.
        raise
    except Exception as err:
        log.warning("background_review.provider_failure", err=str(err))
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error=f"provider_failure: {err}",
        )

    if stream_error and not tool_calls:
        # The provider produced no tool calls AND signalled an error;
        # treat as a soft failure so the audit row reflects what
        # happened. ``no_chunks`` collapses to "no writes" which is
        # benign — mock provider returns nothing, that's fine.
        if stream_error == "no_chunks":
            return BackgroundReviewReport(
                profile_slug=profile_slug,
                kind=kind,
                started_at=started_at,
                finished_at=_utc_now(),
                writes=[],
                error=None,
            )
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error=stream_error,
        )

    try:
        writes = await _apply_tool_calls(
            tool_calls=tool_calls,
            profile_root=profile_root,
            registry=registry,
            now=started_at,
        )
    except Exception as err:
        log.warning("background_review.dispatch_failure", err=str(err))
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error=f"dispatch_failure: {err}",
        )

    log.info(
        "background_review.completed",
        applied=sum(1 for w in writes if w.applied),
        skipped=sum(1 for w in writes if not w.applied),
    )
    return BackgroundReviewReport(
        profile_slug=profile_slug,
        kind=kind,
        started_at=started_at,
        finished_at=_utc_now(),
        writes=writes,
        error=None,
    )


async def _invoke_provider(
    *,
    provider: Any,
    model: str,
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Adapter over the provider Protocol.

    Two call paths supported, in priority order:

    1. ``provider.chat(...)`` — a hypothetical non-streaming method that
       returns ``{"tool_calls": [...]}`` directly. Real adapters don't
       expose this today; tests use it because it's the cleanest way
       to feed scripted tool_calls into the dispatcher.
    2. ``provider.chat_stream(...)`` — the canonical Protocol method.
       We drain the iterator and assemble tool_calls from chunks.
    """
    chat = getattr(provider, "chat", None)
    if callable(chat):
        result = chat(model=model, messages=messages, tools=_TOOL_SCHEMA)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, dict):
            return list(result.get("tool_calls") or []), result.get("error")
        # Unknown shape — treat as no calls so the report stays sane.
        return [], None

    stream_fn = getattr(provider, "chat_stream", None)
    if not callable(stream_fn):
        return [], "provider_missing_chat_methods"

    stream = stream_fn(model=model, messages=messages, tools=_TOOL_SCHEMA)
    return await _collect_tool_calls_from_stream(stream)


__all__ = [
    "WHITELISTED_TOOLS",
    "BackgroundReviewReport",
    "ReviewKind",
    "ReviewWriteRecord",
    "load_prompt",
    "spawn_background_review",
]
