"""Request/response models for the new-api admin API.

Mirrors ``rust/crates/corlinman-newapi-client/src/types.rs``. Field names
match new-api's JSON wire shape; pydantic aliases handle the one slot
(``type`` -> ``channel_type``) where the wire name collides with a Python
keyword.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ChannelType(StrEnum):
    """High-level channel-type categories surfaced to corlinman admin /
    onboard UIs. Mapped to the integer codes new-api uses on its
    ``/api/channel/?type=`` endpoint.
    """

    LLM = "llm"
    EMBEDDING = "embedding"
    TTS = "tts"

    def as_int(self) -> int:
        """Integer code expected by new-api's ``?type=`` query."""
        return _CHANNEL_TYPE_INT[self]


_CHANNEL_TYPE_INT: dict[ChannelType, int] = {
    ChannelType.LLM: 1,
    ChannelType.EMBEDDING: 2,
    ChannelType.TTS: 8,
}


class Channel(BaseModel):
    """One channel row as returned by ``GET /api/channel/``."""

    model_config = ConfigDict(populate_by_name=True)

    id: int
    name: str
    # Wire name is ``type``; rename for Python.
    channel_type: int = Field(alias="type")
    status: int
    models: str
    group: str = ""
    priority: int | None = None
    used_quota: int | None = None
    remain_quota: int | None = None
    test_time: int | None = None
    response_time: int | None = None


class User(BaseModel):
    """User record returned by ``GET /api/user/self``."""

    id: int
    username: str
    display_name: str | None = None
    role: int
    status: int


class ProbeResult(BaseModel):
    """Successful probe — user + resolved server version."""

    base_url: str
    user: User
    server_version: str | None = None


class TestResult(BaseModel):
    """Outcome of a 1-token chat round-trip."""

    status: int
    latency_ms: int
    model: str | None = None


__all__ = [
    "Channel",
    "ChannelType",
    "ProbeResult",
    "TestResult",
    "User",
]
