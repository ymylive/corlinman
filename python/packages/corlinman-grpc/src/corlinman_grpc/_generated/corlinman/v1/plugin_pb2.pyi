from corlinman_grpc._generated.corlinman.v1 import common_pb2 as _common_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PluginToolCall(_message.Message):
    __slots__ = (
        "call_id",
        "plugin",
        "tool",
        "args_json",
        "binding",
        "session_key",
        "approval_preconsented",
        "trace",
    )
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    ARGS_JSON_FIELD_NUMBER: _ClassVar[int]
    BINDING_FIELD_NUMBER: _ClassVar[int]
    SESSION_KEY_FIELD_NUMBER: _ClassVar[int]
    APPROVAL_PRECONSENTED_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    plugin: str
    tool: str
    args_json: bytes
    binding: _common_pb2.ChannelBinding
    session_key: str
    approval_preconsented: bool
    trace: _common_pb2.TraceContext
    def __init__(
        self,
        call_id: _Optional[str] = ...,
        plugin: _Optional[str] = ...,
        tool: _Optional[str] = ...,
        args_json: _Optional[bytes] = ...,
        binding: _Optional[_Union[_common_pb2.ChannelBinding, _Mapping]] = ...,
        session_key: _Optional[str] = ...,
        approval_preconsented: bool = ...,
        trace: _Optional[_Union[_common_pb2.TraceContext, _Mapping]] = ...,
    ) -> None: ...

class ToolEvent(_message.Message):
    __slots__ = ("progress", "result", "error", "awaiting_approval")
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    AWAITING_APPROVAL_FIELD_NUMBER: _ClassVar[int]
    progress: Progress
    result: PluginToolResult
    error: _common_pb2.ErrorInfo
    awaiting_approval: PluginAwaitingApproval
    def __init__(
        self,
        progress: _Optional[_Union[Progress, _Mapping]] = ...,
        result: _Optional[_Union[PluginToolResult, _Mapping]] = ...,
        error: _Optional[_Union[_common_pb2.ErrorInfo, _Mapping]] = ...,
        awaiting_approval: _Optional[_Union[PluginAwaitingApproval, _Mapping]] = ...,
    ) -> None: ...

class Progress(_message.Message):
    __slots__ = ("message", "fraction")
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    FRACTION_FIELD_NUMBER: _ClassVar[int]
    message: str
    fraction: float
    def __init__(
        self, message: _Optional[str] = ..., fraction: _Optional[float] = ...
    ) -> None: ...

class PluginToolResult(_message.Message):
    __slots__ = ("call_id", "result_json", "duration_ms")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_JSON_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    result_json: bytes
    duration_ms: int
    def __init__(
        self,
        call_id: _Optional[str] = ...,
        result_json: _Optional[bytes] = ...,
        duration_ms: _Optional[int] = ...,
    ) -> None: ...

class PluginAwaitingApproval(_message.Message):
    __slots__ = (
        "call_id",
        "plugin",
        "tool",
        "args_preview_json",
        "session_key",
        "reason",
    )
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    ARGS_PREVIEW_JSON_FIELD_NUMBER: _ClassVar[int]
    SESSION_KEY_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    plugin: str
    tool: str
    args_preview_json: bytes
    session_key: str
    reason: str
    def __init__(
        self,
        call_id: _Optional[str] = ...,
        plugin: _Optional[str] = ...,
        tool: _Optional[str] = ...,
        args_preview_json: _Optional[bytes] = ...,
        session_key: _Optional[str] = ...,
        reason: _Optional[str] = ...,
    ) -> None: ...
