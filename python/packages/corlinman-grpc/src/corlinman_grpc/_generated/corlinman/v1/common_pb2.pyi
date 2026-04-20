from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Role(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ROLE_UNSPECIFIED: _ClassVar[Role]
    USER: _ClassVar[Role]
    ASSISTANT: _ClassVar[Role]
    SYSTEM: _ClassVar[Role]
    TOOL: _ClassVar[Role]

class FailoverReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FAILOVER_REASON_UNSPECIFIED: _ClassVar[FailoverReason]
    BILLING: _ClassVar[FailoverReason]
    RATE_LIMIT: _ClassVar[FailoverReason]
    AUTH: _ClassVar[FailoverReason]
    AUTH_PERMANENT: _ClassVar[FailoverReason]
    TIMEOUT: _ClassVar[FailoverReason]
    MODEL_NOT_FOUND: _ClassVar[FailoverReason]
    FORMAT: _ClassVar[FailoverReason]
    CONTEXT_OVERFLOW: _ClassVar[FailoverReason]
    OVERLOADED: _ClassVar[FailoverReason]
    UNKNOWN: _ClassVar[FailoverReason]

ROLE_UNSPECIFIED: Role
USER: Role
ASSISTANT: Role
SYSTEM: Role
TOOL: Role
FAILOVER_REASON_UNSPECIFIED: FailoverReason
BILLING: FailoverReason
RATE_LIMIT: FailoverReason
AUTH: FailoverReason
AUTH_PERMANENT: FailoverReason
TIMEOUT: FailoverReason
MODEL_NOT_FOUND: FailoverReason
FORMAT: FailoverReason
CONTEXT_OVERFLOW: FailoverReason
OVERLOADED: FailoverReason
UNKNOWN: FailoverReason

class Message(_message.Message):
    __slots__ = ("role", "content", "name", "tool_call_id", "content_json")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_JSON_FIELD_NUMBER: _ClassVar[int]
    role: Role
    content: str
    name: str
    tool_call_id: str
    content_json: bytes
    def __init__(
        self,
        role: _Optional[_Union[Role, str]] = ...,
        content: _Optional[str] = ...,
        name: _Optional[str] = ...,
        tool_call_id: _Optional[str] = ...,
        content_json: _Optional[bytes] = ...,
    ) -> None: ...

class Usage(_message.Message):
    __slots__ = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
    )
    PROMPT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_TOKENS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TOKENS_FIELD_NUMBER: _ClassVar[int]
    CACHED_TOKENS_FIELD_NUMBER: _ClassVar[int]
    REASONING_TOKENS_FIELD_NUMBER: _ClassVar[int]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    def __init__(
        self,
        prompt_tokens: _Optional[int] = ...,
        completion_tokens: _Optional[int] = ...,
        total_tokens: _Optional[int] = ...,
        cached_tokens: _Optional[int] = ...,
        reasoning_tokens: _Optional[int] = ...,
    ) -> None: ...

class TraceContext(_message.Message):
    __slots__ = ("traceparent", "tracestate", "request_id")
    TRACEPARENT_FIELD_NUMBER: _ClassVar[int]
    TRACESTATE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    traceparent: str
    tracestate: str
    request_id: str
    def __init__(
        self,
        traceparent: _Optional[str] = ...,
        tracestate: _Optional[str] = ...,
        request_id: _Optional[str] = ...,
    ) -> None: ...

class ErrorInfo(_message.Message):
    __slots__ = ("reason", "message", "retryable", "upstream_code")
    REASON_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    UPSTREAM_CODE_FIELD_NUMBER: _ClassVar[int]
    reason: FailoverReason
    message: str
    retryable: bool
    upstream_code: str
    def __init__(
        self,
        reason: _Optional[_Union[FailoverReason, str]] = ...,
        message: _Optional[str] = ...,
        retryable: bool = ...,
        upstream_code: _Optional[str] = ...,
    ) -> None: ...

class ChannelBinding(_message.Message):
    __slots__ = ("channel", "account", "thread", "sender", "session_key")
    CHANNEL_FIELD_NUMBER: _ClassVar[int]
    ACCOUNT_FIELD_NUMBER: _ClassVar[int]
    THREAD_FIELD_NUMBER: _ClassVar[int]
    SENDER_FIELD_NUMBER: _ClassVar[int]
    SESSION_KEY_FIELD_NUMBER: _ClassVar[int]
    channel: str
    account: str
    thread: str
    sender: str
    session_key: str
    def __init__(
        self,
        channel: _Optional[str] = ...,
        account: _Optional[str] = ...,
        thread: _Optional[str] = ...,
        sender: _Optional[str] = ...,
        session_key: _Optional[str] = ...,
    ) -> None: ...
