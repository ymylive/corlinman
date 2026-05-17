"""``prompts`` capability adapter — exposes skills as parameterised
MCP prompts.

Mirrors the Rust ``adapters::prompts`` module 1:1:

============================  ==========================================
Skill field                   MCP ``Prompt`` field
============================  ==========================================
``name``                      ``name``
``description``               ``description``
``body_markdown``             ``messages[0].content.text`` (one ``user`` turn)
(no params today)             ``arguments = []``
============================  ==========================================
"""

from __future__ import annotations

from typing import Iterable

from .adapters import CapabilityAdapter, SessionContext
from .bridges import SkillRegistry
from .errors import (
    McpInvalidParamsError,
    McpMethodNotFoundError,
)
from .types import (
    JsonValue,
    Prompt,
    PromptMessage,
    PromptRole,
    PromptsGetParams,
    PromptsGetResult,
    PromptsListResult,
    prompt_text_content,
)

METHOD_LIST: str = "prompts/list"
METHOD_GET: str = "prompts/get"


class PromptsAdapter:
    """Adapter that maps a :class:`SkillRegistry` onto MCP's
    ``prompts/*`` surface.

    Mirrors the Rust ``PromptsAdapter`` 1:1.
    """

    def __init__(self, skills: SkillRegistry) -> None:
        self._skills = skills

    # ------------------------------------------------------------------
    # CapabilityAdapter protocol
    # ------------------------------------------------------------------

    def capability_name(self) -> str:
        return "prompts"

    async def handle(
        self,
        method: str,
        params: JsonValue,
        ctx: SessionContext,
    ) -> JsonValue:
        if method == METHOD_LIST:
            return self.list_prompts(ctx).model_dump()
        if method == METHOD_GET:
            try:
                parsed = PromptsGetParams.model_validate(params or {})
            except Exception as e:
                raise McpInvalidParamsError(f"prompts/get: bad params: {e}") from e
            result = self.get_prompt(parsed, ctx)
            return result.model_dump()
        raise McpMethodNotFoundError(method)

    # ------------------------------------------------------------------
    # prompts/list
    # ------------------------------------------------------------------

    def list_prompts(self, ctx: SessionContext) -> PromptsListResult:
        """Build the ``prompts/list`` response, filtered by
        ``ctx.prompts_allowed``."""
        out: list[Prompt] = []
        for skill in self._iter_skills():
            if not ctx.allows_prompt(skill.name):
                continue
            description = skill.description or None
            out.append(
                Prompt(
                    name=skill.name,
                    description=description,
                    arguments=[],
                )
            )
        out.sort(key=lambda p: p.name)
        return PromptsListResult(prompts=out, nextCursor=None)

    # ------------------------------------------------------------------
    # prompts/get
    # ------------------------------------------------------------------

    def get_prompt(
        self,
        params: PromptsGetParams,
        ctx: SessionContext,
    ) -> PromptsGetResult:
        """Build the ``prompts/get`` response. Unknown name → -32602
        with the offending name echoed back via ``data``. Allowlist
        denial → same code, distinct message."""
        if not ctx.allows_prompt(params.name):
            raise McpInvalidParamsError(
                f"prompt '{params.name}' is not allowed by this token",
                data={"name": params.name},
            )

        skill = self._skills.get(params.name)
        if skill is None:
            raise McpInvalidParamsError(
                f"unknown prompt '{params.name}'",
                data={"name": params.name},
            )

        body = skill.body_markdown
        description = skill.description or None
        return PromptsGetResult(
            description=description,
            messages=[
                PromptMessage(
                    role=PromptRole.USER,
                    content=prompt_text_content(body),
                )
            ],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _iter_skills(self) -> Iterable:
        if hasattr(self._skills, "iter") and callable(self._skills.iter):
            return self._skills.iter()
        return iter(self._skills)


__all__ = [
    "METHOD_GET",
    "METHOD_LIST",
    "PromptsAdapter",
]
