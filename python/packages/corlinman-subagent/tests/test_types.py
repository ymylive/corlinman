"""Port of the Rust ``types::tests`` module.

Covers:

* :class:`TaskSpec` round-trips through ``to_dict`` / ``from_dict`` with
  defaults populating from a minimal payload and the ``tool_allowlist``
  ``None`` vs ``[]`` distinction surviving the round-trip.
* :class:`TaskResult` round-trips on the happy path; ``error`` is elided
  when ``None``.
* :class:`FinishReason` serialises as the lowercase snake_case wire
  discriminant the parent loop's LLM branches on.
* :meth:`TaskResult.rejected` is the canonical pre-spawn rejection
  envelope and refuses non-pre-spawn reasons.
* :meth:`ParentContext.child_context` derives ids and increments depth
  with saturation at the Rust ``u8::MAX`` (255) cap.
"""

from __future__ import annotations

import pytest
from corlinman_subagent import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_WALL_SECONDS,
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
    ToolCallSummary,
)


def test_task_spec_round_trip_with_defaults() -> None:
    """Maps to ``task_spec_round_trip_with_defaults`` in Rust."""
    spec = TaskSpec(goal="research transformers")
    payload = spec.to_dict()
    # ``tool_allowlist`` is None and ``extra_context`` is empty; both
    # are elided. ``max_*`` defaults are always emitted so the consumer
    # sees explicit values.
    assert "tool_allowlist" not in payload
    assert "extra_context" not in payload
    assert payload["max_wall_seconds"] == DEFAULT_MAX_WALL_SECONDS
    assert payload["max_tool_calls"] == DEFAULT_MAX_TOOL_CALLS
    back = TaskSpec.from_dict(payload)
    assert back == spec


def test_task_spec_defaults_populate_from_minimal_payload() -> None:
    """Maps to ``task_spec_defaults_populate_from_minimal_json`` in Rust."""
    spec = TaskSpec.from_dict({"goal": "summarise this"})
    assert spec.goal == "summarise this"
    assert spec.tool_allowlist is None
    assert spec.max_wall_seconds == DEFAULT_MAX_WALL_SECONDS
    assert spec.max_tool_calls == DEFAULT_MAX_TOOL_CALLS
    assert spec.extra_context == {}


def test_task_spec_empty_allowlist_distinct_from_none() -> None:
    """Maps to ``task_spec_empty_allowlist_distinct_from_none`` in Rust.

    Conflating ``None`` (inherit) with ``[]`` (no tools) would silently
    widen child permissions.
    """
    inherit = TaskSpec(goal="a")
    empty = TaskSpec(goal="b", tool_allowlist=[])
    assert inherit.to_dict() != empty.to_dict()
    empty_back = TaskSpec.from_dict(empty.to_dict())
    assert empty_back.tool_allowlist == []


def test_task_result_round_trip_happy_path() -> None:
    """Maps to ``task_result_round_trip_happy_path`` in Rust."""
    result = TaskResult(
        output_text="transformers are…",
        tool_calls_made=[
            ToolCallSummary(
                name="web_search",
                args_summary="query=transformers",
                duration_ms=1240,
            )
        ],
        child_session_key="sess_abc::child::0",
        child_agent_id="main::researcher::0",
        elapsed_ms=4180,
        finish_reason=FinishReason.STOP,
    )
    payload = result.to_dict()
    back = TaskResult.from_dict(payload)
    assert back == result
    # ``error`` must be elided on the happy path.
    assert "error" not in payload
    assert payload["finish_reason"] == "stop"


def test_finish_reason_serialises_as_snake_case() -> None:
    """Maps to ``finish_reason_serialises_as_snake_case`` in Rust."""
    cases = [
        (FinishReason.STOP, "stop"),
        (FinishReason.LENGTH, "length"),
        (FinishReason.TIMEOUT, "timeout"),
        (FinishReason.ERROR, "error"),
        (FinishReason.DEPTH_CAPPED, "depth_capped"),
        (FinishReason.REJECTED, "rejected"),
    ]
    for variant, expected in cases:
        assert variant.value == expected, f"{variant!r} should serialise as {expected!r}"
        assert variant.as_str() == expected


def test_pre_spawn_rejections_are_only_depth_and_rejected() -> None:
    """Maps to ``pre_spawn_rejections_are_only_depth_and_rejected`` in Rust."""
    assert FinishReason.DEPTH_CAPPED.is_pre_spawn_rejection()
    assert FinishReason.REJECTED.is_pre_spawn_rejection()
    assert not FinishReason.STOP.is_pre_spawn_rejection()
    assert not FinishReason.LENGTH.is_pre_spawn_rejection()
    assert not FinishReason.TIMEOUT.is_pre_spawn_rejection()
    assert not FinishReason.ERROR.is_pre_spawn_rejection()


def test_rejected_task_result_is_well_formed() -> None:
    """Maps to ``rejected_task_result_is_well_formed`` in Rust."""
    result = TaskResult.rejected(
        FinishReason.DEPTH_CAPPED,
        "sess_xyz",
        "depth>=2 cap reached",
    )
    assert result.finish_reason is FinishReason.DEPTH_CAPPED
    assert result.child_session_key == "sess_xyz::child::-"
    assert result.elapsed_ms == 0
    assert result.tool_calls_made == []
    assert result.output_text == ""
    assert result.error == "depth>=2 cap reached"


def test_rejected_raises_on_non_pre_spawn_reason() -> None:
    """Maps to ``rejected_panics_on_non_pre_spawn_reason`` in Rust.

    Python has no debug/release split so we use a hard ``ValueError``
    rather than a ``debug_assert!``-style panic.
    """
    with pytest.raises(ValueError, match="DEPTH_CAPPED/REJECTED only"):
        TaskResult.rejected(FinishReason.STOP, "sess", "wrong kind")


def test_child_context_derives_ids_and_increments_depth() -> None:
    """Maps to ``child_context_derives_ids_and_increments_depth`` in Rust."""
    parent = ParentContext(
        tenant_id="tenant-a",
        parent_agent_id="main",
        parent_session_key="sess_abc",
        depth=0,
        trace_id="trace-xyz",
    )
    child = parent.child_context("researcher", 0)
    assert child.tenant_id == "tenant-a"
    assert child.parent_agent_id == "main::researcher::0"
    assert child.parent_session_key == "sess_abc::child::0"
    assert child.depth == 1
    # trace_id inherits — required for the join query.
    assert child.trace_id == parent.trace_id


def test_child_context_seqs_disambiguate_siblings() -> None:
    """Maps to ``child_context_seqs_disambiguate_siblings`` in Rust."""
    parent = ParentContext(
        tenant_id="t",
        parent_agent_id="p",
        parent_session_key="s",
        depth=0,
        trace_id="trace",
    )
    a = parent.child_context("card", 0)
    b = parent.child_context("card", 1)
    assert a.parent_agent_id != b.parent_agent_id
    assert a.parent_session_key != b.parent_session_key


def test_child_context_depth_saturates_at_u8_max() -> None:
    """Maps to ``child_context_depth_saturates_at_u8_max`` in Rust."""
    parent = ParentContext(
        tenant_id="t",
        parent_agent_id="p",
        parent_session_key="s",
        depth=255,
        trace_id="trace",
    )
    child = parent.child_context("c", 0)
    assert child.depth == 255


def test_module_defaults_match_design() -> None:
    """Sanity check on the module-level defaults — they pin the design's
    documented values and are read by both the supervisor policy and
    callers that want to plug ``DEFAULT_MAX_WALL_SECONDS`` into their
    own configs."""
    assert DEFAULT_MAX_WALL_SECONDS == 60
    assert DEFAULT_MAX_TOOL_CALLS == 12
    assert DEFAULT_MAX_DEPTH == 2
