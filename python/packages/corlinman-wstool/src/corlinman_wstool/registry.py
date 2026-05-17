"""Tool registry + per-connection bookkeeping for the WS tool bus server.

Python port of the shared-state portions of
``rust/crates/corlinman-wstool/src/server.rs`` — specifically
``ServerState``, ``ConnHandle`` and ``InvokeReply`` plumbing.

The server module imports from here so the dispatch and timeout machinery
can sit on top of a thin, testable bookkeeping core.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from corlinman_wstool.protocol import ToolAdvert, WsToolMessage
from corlinman_wstool.types import InvokeOutcome, WsToolConfig

if TYPE_CHECKING:
    from corlinman_hooks.bus import HookBus

__all__ = ["ConnHandle", "ServerState"]


@dataclass
class ConnHandle:
    """Everything one connection exposes to the rest of the server.

    ``outbox`` is the write-side queue (consumed by the per-connection
    writer task). ``pending`` correlates request_ids with their
    :class:`asyncio.Future` waiters; both ``result`` and ``error`` frames
    for that id complete the waiter.
    """

    runner_id: str
    tools: list[ToolAdvert]
    outbox: asyncio.Queue[object]  # WsToolMessage variants or _SENTINEL
    pending: dict[str, asyncio.Future[InvokeOutcome]] = field(default_factory=dict)

    async def send(self, msg: object) -> bool:
        """Push ``msg`` onto the outbox. Returns False if the outbox is closed."""
        try:
            await self.outbox.put(msg)
            return True
        except Exception:  # pragma: no cover — closed queue is rare
            return False

    def fail_pending(self) -> None:
        """Resolve every pending waiter with ``InvokeOutcome(kind='disconnected')``.

        Called on socket teardown so callers stop blocking.
        """
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_result(InvokeOutcome(kind="disconnected"))
        self.pending.clear()


class ServerState:
    """Shared server state.

    Exposed publicly so adjacent modules in this package (e.g.
    ``file_fetcher``, the future :class:`WsToolRuntime` plug-in adapter)
    can accept a :class:`ServerState` through their constructors. Most
    fields are package-internal; external callers should interact with
    the state only via the methods on :class:`~corlinman_wstool.server.WsToolServer`.
    """

    __slots__ = (
        "cfg",
        "hook_bus",
        "runners",
        "tool_index",
        "_seq_iter",
    )

    def __init__(self, cfg: WsToolConfig, hook_bus: HookBus | None) -> None:
        self.cfg = cfg
        self.hook_bus = hook_bus
        self.runners: dict[str, ConnHandle] = {}
        # tool name -> runner_id that advertised it. First runner to
        # advertise wins on contention; purged on disconnect.
        self.tool_index: dict[str, str] = {}
        # Monotonic request_id allocator; ``itertools.count`` is atomic
        # under asyncio's single-threaded scheduler.
        self._seq_iter = itertools.count(0)

    # ----- request_id ----------------------------------------------------
    def next_request_id(self) -> str:
        return f"req-{next(self._seq_iter)}"

    # ----- tool routing --------------------------------------------------
    def resolve_tool(self, tool: str) -> ConnHandle | None:
        runner_id = self.tool_index.get(tool)
        if runner_id is None:
            return None
        return self.runners.get(runner_id)

    def runner_for_tool(self, tool: str) -> str | None:
        return self.tool_index.get(tool)

    # ----- connection lifecycle -----------------------------------------
    def register_runner(self, conn: ConnHandle) -> None:
        """Insert the runner + its tool advertisements into shared state.

        First runner to advertise a given tool wins; we do not overwrite.
        """
        self.runners[conn.runner_id] = conn
        for tool in conn.tools:
            self.tool_index.setdefault(tool.name, conn.runner_id)

    def deregister_runner(self, runner_id: str) -> bool:
        """Remove the runner + every tool entry that pointed at it.

        Returns ``True`` iff we actually had this runner registered. The
        Rust crate uses this signal to avoid double-decrementing the
        ``WSTOOL_RUNNERS_CONNECTED`` gauge.
        """
        removed = self.runners.pop(runner_id, None) is not None
        if removed:
            stale = [t for t, r in self.tool_index.items() if r == runner_id]
            for t in stale:
                del self.tool_index[t]
        return removed

    def advertised_tools(self) -> dict[str, str]:
        """Snapshot of currently-advertised tools (tool name -> runner id)."""
        return dict(self.tool_index)

    def runner_count(self) -> int:
        return len(self.runners)

    # ----- pending waiter helpers ---------------------------------------
    def take_waiter(
        self, runner_id: str, request_id: str
    ) -> asyncio.Future[InvokeOutcome] | None:
        conn = self.runners.get(runner_id)
        if conn is None:
            return None
        return conn.pending.pop(request_id, None)


# Sentinel pushed onto an outbox to ask the writer task to exit cleanly.
class _OutboxClose:
    """Singleton-style marker used by the server's writer task."""

    __slots__ = ()


OUTBOX_CLOSE: object = _OutboxClose()

# Re-export so ``server.py`` doesn't have to know the implementation.
__all__ += ["OUTBOX_CLOSE"]

# Silence "imported but unused" for the protocol re-export used in type hints
# below. (Hatchling/ruff configs may strip unused names otherwise.)
_ = WsToolMessage
