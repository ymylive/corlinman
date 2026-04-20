"""corlinman-embedding — local or remote embedding dispatch.

Responsibility: present a single ``embed(texts) -> list[list[float]]`` API
to the agent loop; route to a local sentence-transformers pool or to a
remote provider per config. See plan §5.2 RAG data flow.

TODO(M4): implement the router, local pool, and remote client.
"""

from __future__ import annotations

__all__: list[str] = []
