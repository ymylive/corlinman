from corlinman_grpc._generated.corlinman.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AttachmentKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ATTACHMENT_KIND_UNSPECIFIED: _ClassVar[AttachmentKind]
    ATTACHMENT_KIND_IMAGE: _ClassVar[AttachmentKind]
    ATTACHMENT_KIND_AUDIO: _ClassVar[AttachmentKind]
    ATTACHMENT_KIND_VIDEO: _ClassVar[AttachmentKind]
    ATTACHMENT_KIND_FILE: _ClassVar[AttachmentKind]

ATTACHMENT_KIND_UNSPECIFIED: AttachmentKind
ATTACHMENT_KIND_IMAGE: AttachmentKind
ATTACHMENT_KIND_AUDIO: AttachmentKind
ATTACHMENT_KIND_VIDEO: AttachmentKind
ATTACHMENT_KIND_FILE: AttachmentKind

class ClientFrame(_message.Message):
    __slots__ = ("start", "tool_result", "cancel", "approval")
    START_FIELD_NUMBER: _ClassVar[int]
    TOOL_RESULT_FIELD_NUMBER: _ClassVar[int]
    CANCEL_FIELD_NUMBER: _ClassVar[int]
    APPROVAL_FIELD_NUMBER: _ClassVar[int]
    start: ChatStart
    tool_result: ToolResult
    cancel: Cancel
    approval: ApprovalDecision
    def __init__(
        self,
        start: _Optional[_Union[ChatStart, _Mapping]] = ...,
        tool_result: _Optional[_Union[ToolResult, _Mapping]] = ...,
        cancel: _Optional[_Union[Cancel, _Mapping]] = ...,
        approval: _Optional[_Union[ApprovalDecision, _Mapping]] = ...,
    ) -> None: ...

class ChatStart(_message.Message):
    __slots__ = (
        "model",
        "messages",
        "tools_json",
        "session_key",
        "binding",
        "placeholders",
        "temperature",
        "max_tokens",
        "stream",
        "trace",
        "provider_config_json",
        "attachments",
    )
    class PlaceholdersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: bytes
        def __init__(
            self, key: _Optional[str] = ..., value: _Optional[bytes] = ...
        ) -> None: ...

    MODEL_FIELD_NUMBER: _ClassVar[int]
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    TOOLS_JSON_FIELD_NUMBER: _ClassVar[int]
    SESSION_KEY_FIELD_NUMBER: _ClassVar[int]
    BINDING_FIELD_NUMBER: _ClassVar[int]
    PLACEHOLDERS_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    MAX_TOKENS_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_CONFIG_JSON_FIELD_NUMBER: _ClassVar[int]
    ATTACHMENTS_FIELD_NUMBER: _ClassVar[int]
    model: str
    messages: _containers.RepeatedCompositeFieldContainer[_common_pb2.Message]
    tools_json: bytes
    session_key: str
    binding: _common_pb2.ChannelBinding
    placeholders: _containers.ScalarMap[str, bytes]
    temperature: float
    max_tokens: int
    stream: bool
    trace: _common_pb2.TraceContext
    provider_config_json: bytes
    attachments: _containers.RepeatedCompositeFieldContainer[Attachment]
    def __init__(
        self,
        model: _Optional[str] = ...,
        messages: _Optional[_Iterable[_Union[_common_pb2.Message, _Mapping]]] = ...,
        tools_json: _Optional[bytes] = ...,
        session_key: _Optional[str] = ...,
        binding: _Optional[_Union[_common_pb2.ChannelBinding, _Mapping]] = ...,
        placeholders: _Optional[_Mapping[str, bytes]] = ...,
        temperature: _Optional[float] = ...,
        max_tokens: _Optional[int] = ...,
        stream: bool = ...,
        trace: _Optional[_Union[_common_pb2.TraceContext, _Mapping]] = ...,
        provider_config_json: _Optional[bytes] = ...,
        attachments: _Optional[_Iterable[_Union[Attachment, _Mapping]]] = ...,
    ) -> None: ...

class Attachment(_message.Message):
    __slots__ = ("kind", "url", "bytes", "mime", "file_name")
    KIND_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    BYTES_FIELD_NUMBER: _ClassVar[int]
    MIME_FIELD_NUMBER: _ClassVar[int]
    FILE_NAME_FIELD_NUMBER: _ClassVar[int]
    kind: AttachmentKind
    url: str
    bytes: bytes
    mime: str
    file_name: str
    def __init__(
        self,
        kind: _Optional[_Union[AttachmentKind, str]] = ...,
        url: _Optional[str] = ...,
        bytes: _Optional[bytes] = ...,
        mime: _Optional[str] = ...,
        file_name: _Optional[str] = ...,
    ) -> None: ...

class ToolResult(_message.Message):
    __slots__ = ("call_id", "result_json", "is_error", "duration_ms")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_JSON_FIELD_NUMBER: _ClassVar[int]
    IS_ERROR_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    result_json: bytes
    is_error: bool
    duration_ms: int
    def __init__(
        self,
        call_id: _Optional[str] = ...,
        result_json: _Optional[bytes] = ...,
        is_error: bool = ...,
        duration_ms: _Optional[int] = ...,
    ) -> None: ...

class Cancel(_message.Message):
    __slots__ = ("reason",)
    REASON_FIELD_NUMBER: _ClassVar[int]
    reason: str
    def __init__(self, reason: _Optional[str] = ...) -> None: ...

class ApprovalDecision(_message.Message):
    __slots__ = ("call_id", "approved", "scope", "deny_message")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    APPROVED_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    DENY_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    approved: bool
    scope: str
    deny_message: str
    def __init__(
        self,
        call_id: _Optional[str] = ...,
        approved: bool = ...,
        scope: _Optional[str] = ...,
        deny_message: _Optional[str] = ...,
    ) -> None: ...

class ServerFrame(_message.Message):
    __slots__ = ("token", "tool_call", "awaiting", "usage", "done", "error")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_FIELD_NUMBER: _ClassVar[int]
    AWAITING_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    token: TokenDelta
    tool_call: ToolCall
    awaiting: AwaitingApproval
    usage: _common_pb2.Usage
    done: Done
    error: _common_pb2.ErrorInfo
    def __init__(
        self,
        token: _Optional[_Union[TokenDelta, _Mapping]] = ...,
        tool_call: _Optional[_Union[ToolCall, _Mapping]] = ...,
        awaiting: _Optional[_Union[AwaitingApproval, _Mapping]] = ...,
        usage: _Optional[_Union[_common_pb2.Usage, _Mapping]] = ...,
        done: _Optional[_Union[Done, _Mapping]] = ...,
        error: _Optional[_Union[_common_pb2.ErrorInfo, _Mapping]] = ...,
    ) -> None: ...

class TokenDelta(_message.Message):
    __slots__ = ("text", "is_reasoning", "seq")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    IS_REASONING_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    text: str
    is_reasoning: bool
    seq: int
    def __init__(
        self,
        text: _Optional[str] = ...,
        is_reasoning: bool = ...,
        seq: _Optional[int] = ...,
    ) -> None: ...

class ToolCall(_message.Message):
    __slots__ = ("call_id", "plugin", "tool", "args_json", "seq")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    ARGS_JSON_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    plugin: str
    tool: str
    args_json: bytes
    seq: int
    def __init__(
        self,
        call_id: _Optional[str] = ...,
        plugin: _Optional[str] = ...,
        tool: _Optional[str] = ...,
        args_json: _Optional[bytes] = ...,
        seq: _Optional[int] = ...,
    ) -> None: ...

class AwaitingApproval(_message.Message):
    __slots__ = ("call_id", "plugin", "tool", "args_preview_json", "reason")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    PLUGIN_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    ARGS_PREVIEW_JSON_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    plugin: str
    tool: str
    args_preview_json: bytes
    reason: str
    def __init__(
        self,
        call_id: _Optional[str] = ...,
        plugin: _Optional[str] = ...,
        tool: _Optional[str] = ...,
        args_preview_json: _Optional[bytes] = ...,
        reason: _Optional[str] = ...,
    ) -> None: ...

class Done(_message.Message):
    __slots__ = ("finish_reason", "usage", "total_tokens_seen", "wall_time_ms")
    FINISH_REASON_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TOKENS_SEEN_FIELD_NUMBER: _ClassVar[int]
    WALL_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    finish_reason: str
    usage: _common_pb2.Usage
    total_tokens_seen: int
    wall_time_ms: int
    def __init__(
        self,
        finish_reason: _Optional[str] = ...,
        usage: _Optional[_Union[_common_pb2.Usage, _Mapping]] = ...,
        total_tokens_seen: _Optional[int] = ...,
        wall_time_ms: _Optional[int] = ...,
    ) -> None: ...
