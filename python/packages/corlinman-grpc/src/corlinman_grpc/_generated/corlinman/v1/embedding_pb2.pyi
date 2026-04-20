from corlinman_grpc._generated.corlinman.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EmbedRequest(_message.Message):
    __slots__ = ("model", "texts", "route", "request_id", "trace")
    MODEL_FIELD_NUMBER: _ClassVar[int]
    TEXTS_FIELD_NUMBER: _ClassVar[int]
    ROUTE_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    model: str
    texts: _containers.RepeatedScalarFieldContainer[str]
    route: str
    request_id: str
    trace: _common_pb2.TraceContext
    def __init__(
        self,
        model: _Optional[str] = ...,
        texts: _Optional[_Iterable[str]] = ...,
        route: _Optional[str] = ...,
        request_id: _Optional[str] = ...,
        trace: _Optional[_Union[_common_pb2.TraceContext, _Mapping]] = ...,
    ) -> None: ...

class EmbedResponse(_message.Message):
    __slots__ = ("request_id", "vectors", "dim", "model", "error")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    VECTORS_FIELD_NUMBER: _ClassVar[int]
    DIM_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    vectors: _containers.RepeatedCompositeFieldContainer[EmbeddingVector]
    dim: int
    model: str
    error: _common_pb2.ErrorInfo
    def __init__(
        self,
        request_id: _Optional[str] = ...,
        vectors: _Optional[_Iterable[_Union[EmbeddingVector, _Mapping]]] = ...,
        dim: _Optional[int] = ...,
        model: _Optional[str] = ...,
        error: _Optional[_Union[_common_pb2.ErrorInfo, _Mapping]] = ...,
    ) -> None: ...

class EmbeddingVector(_message.Message):
    __slots__ = ("data",)
    DATA_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    def __init__(self, data: _Optional[bytes] = ...) -> None: ...
