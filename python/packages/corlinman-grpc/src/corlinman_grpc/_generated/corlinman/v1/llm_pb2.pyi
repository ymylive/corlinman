from corlinman_grpc._generated.corlinman.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FinishReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FINISH_REASON_UNSPECIFIED: _ClassVar[FinishReason]
    FINISH_STOP: _ClassVar[FinishReason]
    FINISH_LENGTH: _ClassVar[FinishReason]
    FINISH_TOOL_CALL: _ClassVar[FinishReason]
    FINISH_CONTENT_FILTER: _ClassVar[FinishReason]
    FINISH_ERROR: _ClassVar[FinishReason]

FINISH_REASON_UNSPECIFIED: FinishReason
FINISH_STOP: FinishReason
FINISH_LENGTH: FinishReason
FINISH_TOOL_CALL: FinishReason
FINISH_CONTENT_FILTER: FinishReason
FINISH_ERROR: FinishReason

class ChatRequest(_message.Message):
    __slots__ = (
        "model",
        "messages",
        "temperature",
        "max_tokens",
        "top_p",
        "stop",
        "stream",
        "tools_json",
        "provider_config_json",
        "trace",
    )
    MODEL_FIELD_NUMBER: _ClassVar[int]
    MESSAGES_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    MAX_TOKENS_FIELD_NUMBER: _ClassVar[int]
    TOP_P_FIELD_NUMBER: _ClassVar[int]
    STOP_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    TOOLS_JSON_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_CONFIG_JSON_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    model: str
    messages: _containers.RepeatedCompositeFieldContainer[_common_pb2.Message]
    temperature: float
    max_tokens: int
    top_p: float
    stop: _containers.RepeatedScalarFieldContainer[str]
    stream: bool
    tools_json: bytes
    provider_config_json: bytes
    trace: _common_pb2.TraceContext
    def __init__(
        self,
        model: _Optional[str] = ...,
        messages: _Optional[_Iterable[_Union[_common_pb2.Message, _Mapping]]] = ...,
        temperature: _Optional[float] = ...,
        max_tokens: _Optional[int] = ...,
        top_p: _Optional[float] = ...,
        stop: _Optional[_Iterable[str]] = ...,
        stream: bool = ...,
        tools_json: _Optional[bytes] = ...,
        provider_config_json: _Optional[bytes] = ...,
        trace: _Optional[_Union[_common_pb2.TraceContext, _Mapping]] = ...,
    ) -> None: ...

class ChatChunk(_message.Message):
    __slots__ = ("token", "usage", "finish", "error")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    FINISH_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    token: str
    usage: _common_pb2.Usage
    finish: FinishReason
    error: _common_pb2.ErrorInfo
    def __init__(
        self,
        token: _Optional[str] = ...,
        usage: _Optional[_Union[_common_pb2.Usage, _Mapping]] = ...,
        finish: _Optional[_Union[FinishReason, str]] = ...,
        error: _Optional[_Union[_common_pb2.ErrorInfo, _Mapping]] = ...,
    ) -> None: ...

class CompleteRequest(_message.Message):
    __slots__ = ("model", "prompt", "temperature", "max_tokens", "trace")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    MAX_TOKENS_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    model: str
    prompt: str
    temperature: float
    max_tokens: int
    trace: _common_pb2.TraceContext
    def __init__(
        self,
        model: _Optional[str] = ...,
        prompt: _Optional[str] = ...,
        temperature: _Optional[float] = ...,
        max_tokens: _Optional[int] = ...,
        trace: _Optional[_Union[_common_pb2.TraceContext, _Mapping]] = ...,
    ) -> None: ...

class CompleteResponse(_message.Message):
    __slots__ = ("text", "usage", "finish")
    TEXT_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    FINISH_FIELD_NUMBER: _ClassVar[int]
    text: str
    usage: _common_pb2.Usage
    finish: FinishReason
    def __init__(
        self,
        text: _Optional[str] = ...,
        usage: _Optional[_Union[_common_pb2.Usage, _Mapping]] = ...,
        finish: _Optional[_Union[FinishReason, str]] = ...,
    ) -> None: ...
