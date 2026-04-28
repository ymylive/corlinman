"""corlinman-user-model — distilled per-user traits + ``{{user.*}}`` placeholders."""

from corlinman_user_model.distiller import (
    DISTILL_SYSTEM_PROMPT,
    DistillerConfig,
    LLMCaller,
    SessionTurn,
    default_llm_caller,
    distill_session,
    read_session_turns,
    redact_text,
)
from corlinman_user_model.placeholders import UserModelResolver
from corlinman_user_model.store import UserModelStore
from corlinman_user_model.traits import TraitKind, UserTrait

__all__ = [
    "DISTILL_SYSTEM_PROMPT",
    "DistillerConfig",
    "LLMCaller",
    "SessionTurn",
    "TraitKind",
    "UserModelResolver",
    "UserModelStore",
    "UserTrait",
    "default_llm_caller",
    "distill_session",
    "read_session_turns",
    "redact_text",
]
