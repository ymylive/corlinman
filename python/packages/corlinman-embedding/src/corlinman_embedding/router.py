"""Embedding router — pick local pool or remote client per config.

Responsibility: read ``EmbeddingConfig`` (source = ``"local" | "remote"``,
model name, dim assertion) and route ``embed(texts)`` accordingly. Emits
metrics ``corlinman_embedding_batch_size`` (plan §9).

TODO(M4): implement config-driven selection and dim assertion (default 3072
for the RAG pipeline).
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)
