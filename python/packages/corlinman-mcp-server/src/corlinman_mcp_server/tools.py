"""``tools`` capability adapter — bridges :class:`PluginRegistry` and
:class:`PluginRuntime` onto MCP's ``tools/list`` + ``tools/call``
methods.

Mirrors the Rust ``adapters::tools`` module 1:1: tool naming uses the
``<plugin>:<tool>`` form, runtime errors are surfaced via
``CallResult { isError: true }`` (not as JSON-RPC errors), and
protocol failures (unknown plugin / tool / allowlist denial) come back
as :class:`McpError` subclasses.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

import structlog

from .adapters import CapabilityAdapter, SessionContext
from .bridges import (
    CancellationToken,
    PluginInput,
    PluginOutputAcceptedForLater,
    PluginOutputError,
    PluginOutputSuccess,
    PluginRegistry,
    PluginRuntime,
    ProgressSink,
)
from .errors import (
    McpError,
    McpInternalError,
    McpInvalidParamsError,
    McpMethodNotFoundError,
    McpToolNotAllowedError,
)
from .types import (
    JsonValue,
    ToolDescriptor,
    ToolsCallParams,
    ToolsCallResult,
    ToolsListResult,
    text_content,
)

log = structlog.get_logger(__name__)

METHOD_LIST: str = "tools/list"
METHOD_CALL: str = "tools/call"

MCP_PROGRESS_NOTIFICATION: str = "notifications/progress"
"""Method name for outbound progress notifications. Per spec §progress."""

DEFAULT_DEADLINE_MS: int = 30_000
"""Default deadline for a single MCP-driven tool call."""


# ---------------------------------------------------------------------
# Progress bridge
# ---------------------------------------------------------------------


@dataclass
class ProgressEvent:
    """One progress event emitted by an in-flight tool. Mirrors the
    Rust ``ProgressEvent``."""

    progress_token: str
    message: str
    fraction: float | None = None

    def to_progress_params(self) -> dict[str, JsonValue]:
        """Render to a JSON-RPC notification body —
        ``notifications/progress`` with the spec-shaped params."""
        out: dict[str, JsonValue] = {
            "progressToken": self.progress_token,
            "progress": self.fraction if self.fraction is not None else 0.0,
        }
        if self.message:
            out["message"] = self.message
        return out


class ProgressBridge(Protocol):
    """Bridges plugin-runtime :class:`ProgressSink` events onto MCP
    ``notifications/progress`` frames."""

    def forward(self, event: ProgressEvent) -> None: ...


class NullProgressBridge:
    """No-op bridge — used when the caller didn't supply a
    ``progressToken``."""

    def forward(self, event: ProgressEvent) -> None:  # noqa: ARG002
        pass


class CollectingProgressBridge:
    """Test bridge that records forwarded events in a list. Exposed
    publicly because integration tests outside this package may want to
    assert on the progress channel."""

    __slots__ = ("events",)

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def forward(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def drain(self) -> list[ProgressEvent]:
        out = list(self.events)
        self.events.clear()
        return out


class _ProgressSinkAdapter:
    """Bridge from a runtime :class:`ProgressSink` callback onto the
    adapter's :class:`ProgressBridge`."""

    __slots__ = ("_token", "_bridge")

    def __init__(self, token: str, bridge: ProgressBridge) -> None:
        self._token = token
        self._bridge = bridge

    async def emit(self, message: str, fraction: float | None) -> None:
        self._bridge.forward(
            ProgressEvent(
                progress_token=self._token,
                message=message,
                fraction=fraction,
            )
        )


# ---------------------------------------------------------------------
# Tool name encoding
# ---------------------------------------------------------------------


def encode_tool_name(plugin: str, tool: str) -> str:
    """``<plugin>:<tool>`` per design Open question §2."""
    return f"{plugin}:{tool}"


def decode_tool_name(qualified: str) -> tuple[str, str] | None:
    """Inverse of :func:`encode_tool_name`. Returns ``None`` when the
    input doesn't contain the separator or either side is empty."""
    if ":" not in qualified:
        return None
    plugin, tool = qualified.split(":", 1)
    if not plugin or not tool:
        return None
    return plugin, tool


# ---------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------


class ToolsAdapter:
    """Adapter that maps a :class:`PluginRegistry` + a
    :class:`PluginRuntime` onto MCP's ``tools/*`` surface.

    Mirrors the Rust ``ToolsAdapter`` 1:1.
    """

    def __init__(
        self,
        registry: PluginRegistry,
        runtime: PluginRuntime,
        progress: ProgressBridge | None = None,
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self._progress: ProgressBridge = progress or NullProgressBridge()
        self._cancel_root: CancellationToken = CancellationToken()

    @classmethod
    def with_runtime(
        cls,
        registry: PluginRegistry,
        runtime: PluginRuntime,
    ) -> ToolsAdapter:
        """Convenience — build with the no-op progress bridge."""
        return cls(registry, runtime, NullProgressBridge())

    def cancel_all(self) -> None:
        """Cancel every in-flight call on this session."""
        self._cancel_root.cancel()

    # ------------------------------------------------------------------
    # CapabilityAdapter protocol
    # ------------------------------------------------------------------

    def capability_name(self) -> str:
        return "tools"

    async def handle(
        self,
        method: str,
        params: JsonValue,
        ctx: SessionContext,
    ) -> JsonValue:
        if method == METHOD_LIST:
            return self.list_tools(ctx).model_dump()
        if method == METHOD_CALL:
            # Pull progressToken out of `_meta` if present.
            progress_token: str | None = None
            if isinstance(params, dict):
                meta = params.get("_meta")
                if isinstance(meta, dict):
                    tok = meta.get("progressToken")
                    if isinstance(tok, str):
                        progress_token = tok
            try:
                parsed = ToolsCallParams.model_validate(params or {})
            except Exception as e:
                raise McpInvalidParamsError(f"tools/call: bad params: {e}") from e
            result = await self.call_tool(parsed, ctx, progress_token)
            return result.model_dump()
        raise McpMethodNotFoundError(method)

    # ------------------------------------------------------------------
    # tools/list
    # ------------------------------------------------------------------

    def list_tools(self, ctx: SessionContext) -> ToolsListResult:
        """Build the ``tools/list`` response, filtered by
        ``ctx.tools_allowlist``."""
        out: list[ToolDescriptor] = []
        for entry in self._registry.list():
            for tool in entry.manifest.capabilities.tools:
                name = encode_tool_name(entry.manifest.name, tool.name)
                if not ctx.allows_tool(name):
                    continue
                params = tool.parameters
                if isinstance(params, dict):
                    input_schema = params
                else:
                    input_schema = {"type": "object", "additionalProperties": True}
                description = tool.description if tool.description else None
                out.append(
                    ToolDescriptor(
                        name=name,
                        description=description,
                        inputSchema=input_schema,
                    )
                )
        # Stable ordering for snapshot tests.
        out.sort(key=lambda t: t.name)
        return ToolsListResult(tools=out, nextCursor=None)

    # ------------------------------------------------------------------
    # tools/call
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        params: ToolsCallParams,
        ctx: SessionContext,
        progress_token: str | None,
    ) -> ToolsCallResult:
        """Execute one ``tools/call``."""
        decoded = decode_tool_name(params.name)
        if decoded is None:
            raise McpMethodNotFoundError(f"tools/call: {params.name}")
        plugin_name, tool_name = decoded

        qualified = encode_tool_name(plugin_name, tool_name)
        if not ctx.allows_tool(qualified):
            raise McpToolNotAllowedError(qualified)

        entry = self._registry.get(plugin_name)
        if entry is None:
            raise McpMethodNotFoundError(
                f"tools/call: unknown plugin {plugin_name}"
            )

        tool = next(
            (t for t in entry.manifest.capabilities.tools if t.name == tool_name),
            None,
        )
        if tool is None:
            raise McpMethodNotFoundError(
                f"tools/call: plugin '{plugin_name}' has no tool '{tool_name}'"
            )

        # Serialise arguments; MCP allows omitted args (we map to `{}`).
        args = params.arguments if params.arguments is not None else {}
        try:
            args_bytes = json.dumps(args, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            raise McpInternalError(f"tools/call: serialize args: {e}") from e

        cwd = entry.plugin_dir()
        env_map = dict(entry.manifest.entry_point.env)
        plugin_input = PluginInput(
            plugin=plugin_name,
            tool=tool_name,
            args_json=args_bytes,
            call_id=f"mcp-{_uuid_like()}",
            session_key="mcp",
            trace_id=f"mcp-{_uuid_like()}",
            cwd=cwd,
            env=env_map,
            deadline_ms=entry.manifest.communication.timeout_ms or DEFAULT_DEADLINE_MS,
        )

        cancel = self._cancel_root.child_token()
        sink: ProgressSink | None = None
        if progress_token is not None:
            sink = _ProgressSinkAdapter(progress_token, self._progress)

        timeout_ms = entry.manifest.communication.timeout_ms or DEFAULT_DEADLINE_MS
        # Hard wall-clock guard (mirrors the Rust 500ms buffer).
        timeout_s = (timeout_ms + 500) / 1000.0

        try:
            outcome = await asyncio.wait_for(
                self._runtime.execute(plugin_input, sink, cancel),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            cancel.cancel()
            log.warning(
                "tools/call: deadline exceeded",
                plugin=plugin_name,
                tool=tool_name,
            )
            return ToolsCallResult(
                content=[
                    text_content(
                        f"tools/call: deadline exceeded after {timeout_ms}ms"
                    )
                ],
                isError=True,
            )
        except McpError:
            raise
        except Exception as e:
            # Runtime infrastructure failure (sandbox, transport) —
            # propagate as JSON-RPC -32603.
            raise McpInternalError(f"tools/call: runtime failure: {e}") from e

        if isinstance(outcome, PluginOutputSuccess):
            try:
                text = outcome.content.decode("utf-8")
            except UnicodeDecodeError as e:
                text = f"<non-utf8 plugin output: {e}>"
            return ToolsCallResult(
                content=[text_content(text)],
                isError=False,
            )
        if isinstance(outcome, PluginOutputError):
            log.debug(
                "tools/call: runtime error",
                plugin=plugin_name,
                tool=tool_name,
                code=outcome.code,
            )
            return ToolsCallResult(
                content=[text_content(f"[code {outcome.code}] {outcome.message}")],
                isError=True,
            )
        if isinstance(outcome, PluginOutputAcceptedForLater):
            return ToolsCallResult(
                content=[
                    text_content(
                        f"accepted-for-later (task_id={outcome.task_id}); "
                        "polling not supported in MCP C1"
                    )
                ],
                isError=False,
            )
        # Defensive: unknown variant
        raise McpInternalError(
            f"tools/call: runtime returned unknown variant {type(outcome).__name__}"
        )


def _uuid_like() -> str:
    """Pseudo-uuid for per-call correlation in logs; not a security
    primitive. Mirrors the Rust ``uuid_like`` helper."""
    now_ns = time.time_ns()
    entropy = uuid.uuid4().hex[:8]
    return f"{now_ns:x}-{entropy}-{os.getpid():x}"


__all__ = [
    "CollectingProgressBridge",
    "DEFAULT_DEADLINE_MS",
    "MCP_PROGRESS_NOTIFICATION",
    "METHOD_CALL",
    "METHOD_LIST",
    "NullProgressBridge",
    "ProgressBridge",
    "ProgressEvent",
    "ToolsAdapter",
    "decode_tool_name",
    "encode_tool_name",
]
