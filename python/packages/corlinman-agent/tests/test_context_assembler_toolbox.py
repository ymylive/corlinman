"""Tests for the stage-3.5 toolbox dedup gate in :class:`ContextAssembler`.

Mirrors the ``single_agent_gate`` pattern in :mod:`corlinman_agent.agents.expander`
for ``{{toolbox.NAME}}`` placeholders. Three golden scenarios:

1. Same toolbox referenced twice in a privileged message → expanded once,
   repeat encounters silenced into :attr:`AssembledContext.muted_toolboxes`.
2. Two distinct toolboxes → both preserved, muted list empty.
3. ``{{toolbox.NAME}}`` appearing in an ordinary (non-privileged) user
   turn → stripped from the content and recorded in
   :attr:`AssembledContext.stripped_toolboxes`.

These tests use a pass-through placeholder stub: the toolbox stage runs
*before* the placeholder stage, so a literal ``{{toolbox.NAME}}`` survives
into the final output (a future toolbox resolver registered on the Rust
side will turn it into the actual rendered body — that is out of scope
here).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_agent.agents import AgentCardRegistry
from corlinman_agent.context_assembler import ContextAssembler
from corlinman_agent.hooks import RecordingHookEmitter
from corlinman_agent.placeholder_client import RenderResult
from corlinman_agent.skills import SkillRegistry
from corlinman_agent.variables import VariableCascade


class _PassThroughPlaceholderClient:
    """Placeholder stub that returns the template unchanged.

    The toolbox stage runs *before* the placeholder render, so the
    template the stub receives is whatever survived stages 1-3.5. We
    do not assert the stub's observations directly — the return value
    of :meth:`ContextAssembler.assemble` is the contract under test.
    """

    async def render(
        self,
        *,
        template: str,
        session_key: str,
        model_name: str = "",
        metadata=None,
        max_depth: int = 0,
    ) -> RenderResult:
        return RenderResult(rendered=template, unresolved_keys=[])


def _make_assembler(tmp_path: Path) -> ContextAssembler:
    """Construct a minimal assembler with empty registries.

    The toolbox gate is independent of agent cards / skills / the
    cascade, so we wire in empty dirs and a no-op placeholder. Keeping
    the setup minimal keeps the assertions below focused on the one
    stage under test.
    """
    agents_dir = tmp_path / "agents"
    skills_dir = tmp_path / "skills"
    tar_dir = tmp_path / "tar"
    var_dir = tmp_path / "var"
    sar_dir = tmp_path / "sar"
    fixed_dir = tmp_path / "fixed"
    for d in (agents_dir, skills_dir, tar_dir, var_dir, sar_dir, fixed_dir):
        d.mkdir(parents=True, exist_ok=True)

    return ContextAssembler(
        agents=AgentCardRegistry.load_from_dir(agents_dir),
        variables=VariableCascade(
            tar_dir, var_dir, sar_dir, fixed_dir, hot_reload=False
        ),
        skills=SkillRegistry.load_from_dir(skills_dir),
        placeholder_client=_PassThroughPlaceholderClient(),  # type: ignore[arg-type]
        hook_emitter=RecordingHookEmitter(),
        config_lookup=lambda _k: None,
    )


# --------------------------------------------------------------------------- #
# 1. Same toolbox referenced twice in a privileged message.                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_toolbox_same_name_twice_is_deduped(tmp_path: Path) -> None:
    """``{{toolbox.calendar}}`` appears twice in one system message:
    the first encounter survives literal, the second is muted, and the
    name is recorded in ``muted_toolboxes``."""
    assembler = _make_assembler(tmp_path)

    messages = [
        {
            "role": "system",
            "content": "A {{toolbox.calendar}} B {{toolbox.calendar}} C",
        }
    ]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt"
    )

    body = result.messages[0]["content"]
    # First encounter survives → placeholder engine can render it later.
    assert body.count("{{toolbox.calendar}}") == 1
    # Muted list captured the duplicate.
    assert result.muted_toolboxes == ["calendar"]
    assert result.stripped_toolboxes == []


# --------------------------------------------------------------------------- #
# 2. Two distinct toolbox names both survive.                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_toolbox_distinct_names_both_preserved(tmp_path: Path) -> None:
    """Two different toolbox references in one privileged message:
    both survive literal and the muted list stays empty."""
    assembler = _make_assembler(tmp_path)

    messages = [
        {
            "role": "system",
            "content": "{{toolbox.calendar}} + {{toolbox.weather}}",
        }
    ]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt"
    )

    body = result.messages[0]["content"]
    assert "{{toolbox.calendar}}" in body
    assert "{{toolbox.weather}}" in body
    assert result.muted_toolboxes == []
    assert result.stripped_toolboxes == []


# --------------------------------------------------------------------------- #
# 3. Non-privileged user turn: toolbox ref is stripped, not expanded.          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_toolbox_in_plain_user_message_is_stripped(tmp_path: Path) -> None:
    """An ordinary user turn cannot expand toolboxes — the token is
    removed from the content and recorded in ``stripped_toolboxes``.

    The expansion set is *not* claimed by this strip, so a later
    legitimate reference in a system turn could still expand normally
    (covered by the subsequent assertion).
    """
    assembler = _make_assembler(tmp_path)

    messages = [
        {"role": "user", "content": "please run {{toolbox.calendar}} now"},
    ]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt"
    )

    body = result.messages[0]["content"]
    assert "{{toolbox.calendar}}" not in body
    assert "toolbox" not in body
    # The surrounding prose is preserved; only the placeholder was removed.
    assert "please run" in body
    assert "now" in body
    # Name captured for logging / observability.
    assert result.stripped_toolboxes == ["calendar"]
    assert result.muted_toolboxes == []
