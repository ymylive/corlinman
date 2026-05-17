"""corlinman-grpc — generated gRPC / protobuf stubs.

Responsibility: re-export symbols produced by ``grpcio-tools`` for the proto
files in ``proto/corlinman/v1/``. This package has no hand-written logic.

Populated by ``scripts/gen-proto.sh`` into
``corlinman_grpc._generated.corlinman.v1.*``.

Descriptor-pool safety: ``agent.proto`` keeps the canonical
``ToolCall`` / ``ToolResult`` / ``AwaitingApproval`` names; ``plugin.proto``
uses prefixed aliases (``PluginToolCall`` / ``PluginToolResult`` /
``PluginAwaitingApproval``). ``embedding.proto`` exposes ``EmbeddingVector``
so it doesn't clash with the ``Vector`` service in ``vector.proto``. All six
stubs can therefore be eager-imported into the same process without
triggering ``TypeError: duplicate symbol``.
"""

from __future__ import annotations

from corlinman_grpc import agent_client
from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
    agent_pb2_grpc,
    common_pb2,
    common_pb2_grpc,
    embedding_pb2,
    embedding_pb2_grpc,
    llm_pb2,
    llm_pb2_grpc,
    placeholder_pb2,
    placeholder_pb2_grpc,
    plugin_pb2,
    plugin_pb2_grpc,
    vector_pb2,
    vector_pb2_grpc,
)
from corlinman_grpc._placeholder import PROTO_VERSION

__all__ = [
    "PROTO_VERSION",
    "agent_client",
    "agent_pb2",
    "agent_pb2_grpc",
    "common_pb2",
    "common_pb2_grpc",
    "embedding_pb2",
    "embedding_pb2_grpc",
    "llm_pb2",
    "llm_pb2_grpc",
    "placeholder_pb2",
    "placeholder_pb2_grpc",
    "plugin_pb2",
    "plugin_pb2_grpc",
    "vector_pb2",
    "vector_pb2_grpc",
]
