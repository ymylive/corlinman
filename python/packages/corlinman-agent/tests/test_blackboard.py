"""Tests for the v0.7 shared blackboard (sibling-agent scratchpad)."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from corlinman_agent.subagent.blackboard import (
    BLACKBOARD_ARGS_INVALID_ERROR,
    BLACKBOARD_READ_TOOL,
    BLACKBOARD_WRITE_TOOL,
    BlackboardStore,
    blackboard_read_tool_schema,
    blackboard_write_tool_schema,
    dispatch_blackboard_read,
    dispatch_blackboard_write,
)


@pytest.fixture
def store(tmp_path: Path) -> BlackboardStore:
    return BlackboardStore(tmp_path / "kb.sqlite")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def test_read_schema_shape() -> None:
    schema = blackboard_read_tool_schema()
    assert schema["function"]["name"] == BLACKBOARD_READ_TOOL
    assert schema["function"]["name"] == "blackboard.read"
    assert schema["function"]["parameters"]["required"] == ["key"]


def test_write_schema_shape() -> None:
    schema = blackboard_write_tool_schema()
    assert schema["function"]["name"] == BLACKBOARD_WRITE_TOOL
    assert schema["function"]["name"] == "blackboard.write"
    assert set(schema["function"]["parameters"]["required"]) == {"key", "value"}


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


def test_write_then_read_latest_returns_last_value(store: BlackboardStore) -> None:
    store.write(trace_id="t", key="k", value="v1", written_by="agent-a")
    store.write(trace_id="t", key="k", value="v2", written_by="agent-b")
    assert store.read_latest(trace_id="t", key="k") == "v2"


def test_read_missing_key_returns_none(store: BlackboardStore) -> None:
    assert store.read_latest(trace_id="t", key="missing") is None


def test_writes_are_trace_scoped(store: BlackboardStore) -> None:
    """A read in trace A must never see a write from trace B even if
    the key matches. Trace isolation is the security boundary."""
    store.write(trace_id="trace-a", key="shared", value="from-a", written_by="x")
    store.write(trace_id="trace-b", key="shared", value="from-b", written_by="y")
    assert store.read_latest(trace_id="trace-a", key="shared") == "from-a"
    assert store.read_latest(trace_id="trace-b", key="shared") == "from-b"


def test_history_is_append_only(store: BlackboardStore) -> None:
    """Multiple writes to the same key store separately — the row
    count grows. Forensic queries should be able to see the history."""
    import sqlite3

    for i in range(3):
        store.write(trace_id="t", key="k", value=f"v{i}", written_by="a")
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT value FROM blackboard WHERE trace_id='t' AND key='k' "
            "ORDER BY written_at ASC"
        ).fetchall()
    assert [r[0] for r in rows] == ["v0", "v1", "v2"]


def test_concurrent_writes_do_not_lose_data(store: BlackboardStore) -> None:
    """Two threads racing to write the same key shouldn't either crash
    or lose a row to PK collision. The store's same-ms IntegrityError
    recovery is what we're exercising here.

    We use threads (not async) because sqlite3.connect releases the GIL
    on commit, so two threads can genuinely race on the file."""
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        store.write(trace_id="t", key="race", value=f"v{i}", written_by=f"w{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    import sqlite3
    with sqlite3.connect(store.db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM blackboard WHERE trace_id='t' AND key='race'"
        ).fetchone()[0]
    assert n == 8, "every concurrent write must persist a row"


# ---------------------------------------------------------------------------
# Dispatchers
# ---------------------------------------------------------------------------


def test_dispatch_write_returns_receipt(store: BlackboardStore) -> None:
    body = json.dumps({"key": "k", "value": "hello"})
    content = dispatch_blackboard_write(
        args_json=body,
        store=store,
        trace_id="t",
        written_by="agent-a",
    )
    payload = json.loads(content)
    assert payload["key"] == "k"
    assert payload["written_by"] == "agent-a"
    assert isinstance(payload["written_at"], int)
    assert "error" not in payload


def test_dispatch_read_returns_value_and_present_flag(store: BlackboardStore) -> None:
    store.write(trace_id="t", key="k", value="hello", written_by="x")
    body = json.dumps({"key": "k"})
    content = dispatch_blackboard_read(args_json=body, store=store, trace_id="t")
    payload = json.loads(content)
    assert payload == {"key": "k", "value": "hello", "present": True}


def test_dispatch_read_missing_key_returns_present_false(
    store: BlackboardStore,
) -> None:
    content = dispatch_blackboard_read(
        args_json='{"key": "no-such-key"}',
        store=store,
        trace_id="t",
    )
    assert json.loads(content) == {
        "key": "no-such-key",
        "value": None,
        "present": False,
    }


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("not-json", "args_json not JSON"),
        ('{"key": ""}', "missing or empty 'key'"),
        ('{"key": 123}', "missing or empty 'key'"),
    ],
)
def test_dispatch_read_args_invalid(
    store: BlackboardStore, body: str, fragment: str
) -> None:
    content = dispatch_blackboard_read(args_json=body, store=store, trace_id="t")
    payload = json.loads(content)
    assert payload["error"].startswith(BLACKBOARD_ARGS_INVALID_ERROR)
    assert fragment in payload["error"]


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ('{"key": "k"}', "'value' must be a string"),
        ('{"value": "v"}', "missing or empty 'key'"),
        ('{"key": "k", "value": 5}', "'value' must be a string"),
    ],
)
def test_dispatch_write_args_invalid(
    store: BlackboardStore, body: str, fragment: str
) -> None:
    content = dispatch_blackboard_write(
        args_json=body, store=store, trace_id="t", written_by="x"
    )
    payload = json.loads(content)
    assert payload["error"].startswith(BLACKBOARD_ARGS_INVALID_ERROR)
    assert fragment in payload["error"]


# ---------------------------------------------------------------------------
# End-to-end coordination simulation
# ---------------------------------------------------------------------------


async def test_two_async_writers_then_reader_sees_latest(store: BlackboardStore) -> None:
    """Simulates two sibling agents calling blackboard.write
    concurrently and a third one reading. Last write wins on read."""

    async def writer(value: str, written_by: str) -> None:
        # Each sibling has its own awaitable; the dispatcher is sync,
        # so we run it via asyncio.to_thread to get true parallel
        # file-locks under the hood.
        await asyncio.to_thread(
            store.write,
            trace_id="t",
            key="shared",
            value=value,
            written_by=written_by,
        )

    await asyncio.gather(
        writer("alpha", "agent-a"),
        writer("beta", "agent-b"),
    )
    # Both wrote; the read sees one of them — the contract is
    # "latest wins", not which one wrote last. Either is acceptable.
    value = store.read_latest(trace_id="t", key="shared")
    assert value in ("alpha", "beta")
