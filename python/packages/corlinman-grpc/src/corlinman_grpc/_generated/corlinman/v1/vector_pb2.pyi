from corlinman_grpc._generated.corlinman.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RagQuery(_message.Message):
    __slots__ = (
        "query_vector",
        "top_k",
        "overfetch",
        "scope",
        "session_key",
        "required_tags",
        "excluded_tags",
        "rag_params_json",
        "trace",
    )
    QUERY_VECTOR_FIELD_NUMBER: _ClassVar[int]
    TOP_K_FIELD_NUMBER: _ClassVar[int]
    OVERFETCH_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    SESSION_KEY_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_TAGS_FIELD_NUMBER: _ClassVar[int]
    EXCLUDED_TAGS_FIELD_NUMBER: _ClassVar[int]
    RAG_PARAMS_JSON_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    query_vector: bytes
    top_k: int
    overfetch: int
    scope: str
    session_key: str
    required_tags: _containers.RepeatedScalarFieldContainer[str]
    excluded_tags: _containers.RepeatedScalarFieldContainer[str]
    rag_params_json: bytes
    trace: _common_pb2.TraceContext
    def __init__(
        self,
        query_vector: _Optional[bytes] = ...,
        top_k: _Optional[int] = ...,
        overfetch: _Optional[int] = ...,
        scope: _Optional[str] = ...,
        session_key: _Optional[str] = ...,
        required_tags: _Optional[_Iterable[str]] = ...,
        excluded_tags: _Optional[_Iterable[str]] = ...,
        rag_params_json: _Optional[bytes] = ...,
        trace: _Optional[_Union[_common_pb2.TraceContext, _Mapping]] = ...,
    ) -> None: ...

class RagHits(_message.Message):
    __slots__ = ("hits", "error", "candidate_count")
    HITS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_COUNT_FIELD_NUMBER: _ClassVar[int]
    hits: _containers.RepeatedCompositeFieldContainer[RagHit]
    error: _common_pb2.ErrorInfo
    candidate_count: int
    def __init__(
        self,
        hits: _Optional[_Iterable[_Union[RagHit, _Mapping]]] = ...,
        error: _Optional[_Union[_common_pb2.ErrorInfo, _Mapping]] = ...,
        candidate_count: _Optional[int] = ...,
    ) -> None: ...

class RagHit(_message.Message):
    __slots__ = (
        "chunk_id",
        "content",
        "score",
        "tags",
        "path",
        "offset_start",
        "offset_end",
        "meta_json",
    )
    CHUNK_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    OFFSET_START_FIELD_NUMBER: _ClassVar[int]
    OFFSET_END_FIELD_NUMBER: _ClassVar[int]
    META_JSON_FIELD_NUMBER: _ClassVar[int]
    chunk_id: str
    content: str
    score: float
    tags: _containers.RepeatedScalarFieldContainer[str]
    path: str
    offset_start: int
    offset_end: int
    meta_json: bytes
    def __init__(
        self,
        chunk_id: _Optional[str] = ...,
        content: _Optional[str] = ...,
        score: _Optional[float] = ...,
        tags: _Optional[_Iterable[str]] = ...,
        path: _Optional[str] = ...,
        offset_start: _Optional[int] = ...,
        offset_end: _Optional[int] = ...,
        meta_json: _Optional[bytes] = ...,
    ) -> None: ...

class Chunk(_message.Message):
    __slots__ = (
        "chunk_id",
        "content",
        "embedding",
        "tags",
        "path",
        "offset_start",
        "offset_end",
        "scope",
        "last_touched_at",
        "meta_json",
    )
    CHUNK_ID_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    EMBEDDING_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    OFFSET_START_FIELD_NUMBER: _ClassVar[int]
    OFFSET_END_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    LAST_TOUCHED_AT_FIELD_NUMBER: _ClassVar[int]
    META_JSON_FIELD_NUMBER: _ClassVar[int]
    chunk_id: str
    content: str
    embedding: bytes
    tags: _containers.RepeatedScalarFieldContainer[str]
    path: str
    offset_start: int
    offset_end: int
    scope: str
    last_touched_at: int
    meta_json: bytes
    def __init__(
        self,
        chunk_id: _Optional[str] = ...,
        content: _Optional[str] = ...,
        embedding: _Optional[bytes] = ...,
        tags: _Optional[_Iterable[str]] = ...,
        path: _Optional[str] = ...,
        offset_start: _Optional[int] = ...,
        offset_end: _Optional[int] = ...,
        scope: _Optional[str] = ...,
        last_touched_at: _Optional[int] = ...,
        meta_json: _Optional[bytes] = ...,
    ) -> None: ...

class UpsertAck(_message.Message):
    __slots__ = ("accepted", "replaced", "rejected", "rejected_ids", "error")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REPLACED_FIELD_NUMBER: _ClassVar[int]
    REJECTED_FIELD_NUMBER: _ClassVar[int]
    REJECTED_IDS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    accepted: int
    replaced: int
    rejected: int
    rejected_ids: _containers.RepeatedScalarFieldContainer[str]
    error: _common_pb2.ErrorInfo
    def __init__(
        self,
        accepted: _Optional[int] = ...,
        replaced: _Optional[int] = ...,
        rejected: _Optional[int] = ...,
        rejected_ids: _Optional[_Iterable[str]] = ...,
        error: _Optional[_Union[_common_pb2.ErrorInfo, _Mapping]] = ...,
    ) -> None: ...
