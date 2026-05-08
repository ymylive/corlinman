"""In-memory agent-card registry, loaded from a directory of yaml files.

The registry is intentionally minimal: it maps ``name -> AgentCard``
and exposes a read-only lookup surface. Hot-reload, file watching, and
validation reporting belong to an operator-facing admin layer that is
out of scope for this workstream.
"""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

from corlinman_agent.agents.card import AgentCard


class AgentCardLoadError(RuntimeError):
    """Raised when a yaml file under the agents dir is unparseable or
    missing required fields. The file path is included so operators can
    locate the offender without re-running the loader."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _as_str_list(value: object, field_name: str, path: Path) -> list[str]:
    """Coerce an optional yaml list-of-strings field. ``None`` / missing
    is the empty list; anything else must be a list of strings or the
    file is rejected."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise AgentCardLoadError(path, f"{field_name} must be a list of strings")
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise AgentCardLoadError(path, f"{field_name} entries must be strings")
        out.append(entry)
    return out


def _as_str_dict(value: object, field_name: str, path: Path) -> dict[str, str]:
    """Coerce the ``variables:`` mapping. Values are stringified
    (yaml may parse ``"15"`` as an int) to keep the expander's
    substitution step type-safe."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentCardLoadError(path, f"{field_name} must be a mapping")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str):
            raise AgentCardLoadError(path, f"{field_name} keys must be strings")
        out[k] = str(v)
    return out


def _load_card(path: Path) -> AgentCard:
    """Parse one ``<name>.yaml`` file into an :class:`AgentCard`.

    The filename stem is authoritative for ``name`` — if the yaml body
    also carries a ``name:`` key it must agree, otherwise the file is
    rejected (protects against copy-paste mistakes where a file was
    renamed without updating its body).
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AgentCardLoadError(path, f"yaml parse error: {exc}") from exc

    if raw is None:
        raise AgentCardLoadError(path, "file is empty")
    if not isinstance(raw, dict):
        raise AgentCardLoadError(path, "top-level yaml must be a mapping")

    stem = path.stem
    declared_name = raw.get("name")
    if declared_name is not None:
        if not isinstance(declared_name, str):
            raise AgentCardLoadError(path, "name must be a string")
        if declared_name != stem:
            raise AgentCardLoadError(
                path,
                f"declared name {declared_name!r} does not match filename stem {stem!r}",
            )
    name = stem

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise AgentCardLoadError(path, "description must be a string")

    system_prompt = raw.get("system_prompt")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise AgentCardLoadError(path, "system_prompt is required and must be a non-empty string")

    variables = _as_str_dict(raw.get("variables"), "variables", path)
    tools_allowed = _as_str_list(raw.get("tools_allowed"), "tools_allowed", path)
    skill_refs = _as_str_list(raw.get("skill_refs"), "skill_refs", path)

    return AgentCard(
        name=name,
        description=description,
        system_prompt=system_prompt,
        variables=variables,
        tools_allowed=tools_allowed,
        skill_refs=skill_refs,
        source_path=path,
    )


class AgentCardRegistry:
    """Read-only lookup over agent cards loaded from disk.

    The registry is built via :meth:`load_from_dir`, which scans a
    directory for ``*.yaml`` / ``*.yml`` files and parses each into an
    :class:`AgentCard`. Failed files raise :class:`AgentCardLoadError`
    immediately rather than being silently skipped — silent skips cause
    hard-to-debug "why won't my agent expand" tickets.
    """

    def __init__(self, cards: dict[str, AgentCard]) -> None:
        self._cards = cards

    @classmethod
    def load_from_dir(cls, root: Path) -> AgentCardRegistry:
        """Load every ``*.yaml`` / ``*.yml`` file under ``root``.

        Non-existent roots yield an empty registry (lets operators run
        with no agents configured yet). A path that exists but isn't a
        directory is a configuration error and raises.
        """
        cards: dict[str, AgentCard] = {}
        if not root.exists():
            return cls(cards)
        if not root.is_dir():
            raise AgentCardLoadError(root, "agents root must be a directory")

        # Sorted so load order is deterministic across platforms — matters
        # when two files declare the same name and we want a stable
        # "first wins / last wins" story (we reject duplicates below,
        # but the sorted scan keeps error messages predictable).
        for path in sorted(root.iterdir()):
            if path.suffix.lower() not in (".yaml", ".yml"):
                continue
            if not path.is_file():
                continue
            card = _load_card(path)
            if card.name in cards:
                raise AgentCardLoadError(
                    path,
                    f"duplicate agent name {card.name!r} "
                    f"(also defined in {cards[card.name].source_path})",
                )
            cards[card.name] = card
        return cls(cards)

    def get(self, name: str) -> AgentCard | None:
        """Return the card for ``name`` or ``None`` if not registered."""
        return self._cards.get(name)

    def names(self) -> list[str]:
        """Return all registered agent names, sorted."""
        return sorted(self._cards.keys())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._cards

    def __len__(self) -> int:
        return len(self._cards)


__all__ = ["AgentCardLoadError", "AgentCardRegistry"]
