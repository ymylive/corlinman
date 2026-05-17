"""`plugin-manifest.toml` schema — human-readable TOML, strict validation.

Python port of ``rust/crates/corlinman-plugins/src/manifest.rs``. Field names,
defaults, and validation rules match the Rust crate 1:1 so a manifest written
for one runtime loads cleanly in the other.

Per the task spec, manifests are parsed via PyYAML *or* TOML. The Rust crate
is TOML-only; the Python port accepts TOML (via ``tomllib`` from the stdlib)
to stay byte-compatible, and additionally honours ``.yaml`` / ``.yml`` for
authors who prefer YAML — the structural shape is identical.

Taxonomy (plan §7.1):
  - ``sync``    — JSON-RPC 2.0 over stdio, spawn-per-call, blocks until result.
  - ``async``   — JSON-RPC 2.0 over stdio; response may carry ``task_id`` for
    later callback via ``/plugin-callback``.
  - ``service`` — long-lived gRPC server; gateway launches once and reuses.
  - ``mcp``     — MCP stdio server consumed as a corlinman tool source (v3+).
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------- Constants (parity with Rust) ----------

#: Canonical filename the discovery layer looks for.
MANIFEST_FILENAME = "plugin-manifest.toml"

#: Protocols we currently accept.
KNOWN_PROTOCOLS: tuple[str, ...] = ("openai_function", "block")

#: Hook event kinds known as of the B1 plan. Unknown names emit a warning,
#: not an error — manifests may reference hooks that ship in a later gateway.
KNOWN_HOOK_EVENTS: tuple[str, ...] = (
    "message.received",
    "message.sent",
    "message.transcribed",
    "message.preprocessed",
    "session.patch",
    "agent.bootstrap",
    "gateway.startup",
    "config.changed",
)

#: Highest schema version this gateway understands.
MAX_SUPPORTED_MANIFEST_VERSION = 3

#: First manifest version that recognises ``plugin_type = "mcp"`` and the
#: ``[mcp]`` table.
MCP_MIN_MANIFEST_VERSION = 3


# ---------- Errors ----------


class ManifestParseError(Exception):
    """Top-level error raised when a manifest fails to load.

    Mirrors the Rust ``ManifestParseError`` enum: the concrete subclass
    (``ManifestIoError`` / ``ManifestTomlError`` / ``ManifestValidationError``)
    distinguishes the failure mode while remaining ``isinstance``-compatible
    with this base type for catch-all handlers.
    """

    def __init__(self, path: Path | str, message: str) -> None:
        self.path = Path(path)
        self.message = message
        super().__init__(f"{type(self).__name__}: {self.path}: {message}")


class ManifestIoError(ManifestParseError):
    """``parse_manifest_file`` failed to read the manifest off disk."""


class ManifestTomlError(ManifestParseError):
    """Manifest file is not valid TOML (or YAML, for ``.yaml``/``.yml`` paths)."""


class ManifestValidationError(ManifestParseError):
    """Manifest parsed but failed structural / cross-field validation."""


# ---------- Enums ----------


class PluginType(StrEnum):
    """Plugin runtime taxonomy."""

    SYNC = "sync"
    ASYNC = "async"
    SERVICE = "service"
    MCP = "mcp"

    @classmethod
    def _missing_(cls, value: object) -> PluginType | None:  # type: ignore[override]
        if isinstance(value, str):
            for member in cls:
                if member.value == value:
                    return member
        return None


class RestartPolicy(StrEnum):
    """What to do when the MCP child exits."""

    NEVER = "never"
    ON_CRASH = "on_crash"
    ALWAYS = "always"


class AllowlistMode(StrEnum):
    """Allowlist mode for MCP tool names."""

    ALLOW = "allow"
    DENY = "deny"
    ALL = "all"


# ---------- Sub-tables ----------


class EntryPoint(BaseModel):
    """How to launch the plugin process. ``cwd`` is always the manifest dir."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class Communication(BaseModel):
    """Transport parameters."""

    model_config = ConfigDict(extra="forbid")

    #: Hard deadline for a single invocation in milliseconds.
    timeout_ms: int | None = None


class Tool(BaseModel):
    """A single tool exposed by the plugin."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class Capabilities(BaseModel):
    """Capability advertisement."""

    model_config = ConfigDict(extra="forbid")

    tools: list[Tool] = Field(default_factory=list)
    disable_model_invocation: bool = False


class SandboxConfig(BaseModel):
    """Sandbox config (plan §8). All fields are optional."""

    model_config = ConfigDict(extra="forbid")

    #: Memory cap as a docker-style string (``"256m"``, ``"1g"``).
    memory: str | None = None
    #: CPU fraction (e.g. ``0.5``) or integer count.
    cpus: float | None = None
    read_only_root: bool = False
    cap_drop: list[str] = Field(default_factory=list)
    #: Network mode: ``"none"``, ``"bridge"``, etc. Defaults to ``"none"`` if unset.
    network: str | None = None
    binds: list[str] = Field(default_factory=list)


class EnvPassthrough(BaseModel):
    """Env-var passthrough rules for MCP children."""

    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class ToolsAllowlist(BaseModel):
    """Filter applied to upstream ``tools/list``."""

    model_config = ConfigDict(extra="forbid")

    mode: AllowlistMode = AllowlistMode.ALLOW
    names: list[str] = Field(default_factory=list)


class ResourcesAllowlist(BaseModel):
    """Filter applied to upstream ``resources/*``. Reserved."""

    model_config = ConfigDict(extra="forbid")

    mode: AllowlistMode = AllowlistMode.ALLOW
    patterns: list[str] = Field(default_factory=list)


class McpConfig(BaseModel):
    """``[mcp]`` block — only honoured when ``plugin_type = "mcp"`` and
    ``manifest_version >= 3``."""

    model_config = ConfigDict(extra="forbid")

    autostart: bool = False
    restart_policy: RestartPolicy = RestartPolicy.ON_CRASH
    crash_loop_max: int = 3
    crash_loop_window_secs: int = 60
    handshake_timeout_ms: int = 5_000
    idle_shutdown_secs: int = 0
    env_passthrough: EnvPassthrough = Field(default_factory=EnvPassthrough)
    tools_allowlist: ToolsAllowlist = Field(default_factory=ToolsAllowlist)
    resources_allowlist: ResourcesAllowlist = Field(default_factory=ResourcesAllowlist)


class Meta(BaseModel):
    """UI 'last touched' metadata (shared with core)."""

    model_config = ConfigDict(extra="allow")


# ---------- Top-level manifest ----------


class PluginManifest(BaseModel):
    """Top-level plugin manifest (``plugin-manifest.toml``)."""

    model_config = ConfigDict(extra="forbid")

    manifest_version: int = 1
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    description: str = ""
    author: str = ""
    plugin_type: PluginType
    entry_point: EntryPoint
    communication: Communication = Field(default_factory=Communication)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    mcp: McpConfig | None = None
    meta: Meta | None = None
    protocols: list[str] = Field(default_factory=lambda: ["openai_function"])
    hooks: list[str] = Field(default_factory=list)
    skill_refs: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must be non-empty")
        return v

    # -- Migration --

    def migrate_to_current_in_memory(self) -> None:
        """Lift an older manifest version to the current in-memory shape (v3).

        The on-disk file is **not** rewritten — the gateway only fills in
        new fields with their documented defaults so downstream code sees a
        uniform v3 structure.

        Migration steps (cumulative, idempotent):
          - v1 -> v2: bump ``manifest_version``.
          - v2 -> v3: bump ``manifest_version`` for non-MCP plugins. MCP-
            flavoured manifests (``plugin_type = "mcp"`` or an explicit
            ``[mcp]`` block) keep their authored version so ``validate_all``
            surfaces the bump-required error.
        """
        if self.manifest_version < 2:
            self.manifest_version = 2
        if self.manifest_version < 3:
            is_mcp_flavoured = self.plugin_type == PluginType.MCP or self.mcp is not None
            if not is_mcp_flavoured:
                self.manifest_version = 3

    # -- Validation --

    def validate_all(self) -> None:
        """Run cross-field validation (protocol whitelist, version ceiling,
        MCP version gating). Raises ``ValueError`` with the human-readable
        message on the first failure; the caller wraps it in a
        ``ManifestValidationError``.
        """
        if self.manifest_version == 0 or self.manifest_version > MAX_SUPPORTED_MANIFEST_VERSION:
            raise ValueError(
                f"manifest_version {self.manifest_version} is not supported "
                f"(this gateway supports 1..={MAX_SUPPORTED_MANIFEST_VERSION}); "
                "upgrade the gateway to load newer manifests"
            )

        for proto in self.protocols:
            if proto not in KNOWN_PROTOCOLS:
                raise ValueError(
                    f"unknown protocol {proto!r}; allowed: {list(KNOWN_PROTOCOLS)!r}"
                )

        # Unknown hook names are forward-compat warnings (not errors). We
        # don't have a tracing layer here, so the discovery / registry layer
        # is expected to surface these via structlog when it wants to.

        # MCP cross-field checks.
        is_mcp = self.plugin_type == PluginType.MCP
        has_mcp_table = self.mcp is not None

        if has_mcp_table and self.manifest_version < MCP_MIN_MANIFEST_VERSION:
            raise ValueError(
                f"[mcp] table requires manifest_version >= {MCP_MIN_MANIFEST_VERSION} "
                f"(got {self.manifest_version})"
            )

        if is_mcp and self.manifest_version < MCP_MIN_MANIFEST_VERSION:
            raise ValueError(
                f'plugin_type = "mcp" requires manifest_version >= '
                f"{MCP_MIN_MANIFEST_VERSION} (got {self.manifest_version}); "
                "bump the manifest to v3"
            )

        if has_mcp_table and not is_mcp:
            raise ValueError(
                f'[mcp] table is only valid when plugin_type = "mcp" '
                f'(got plugin_type = "{self.plugin_type.value}")'
            )


# ---------- Loader ----------


def _load_raw(path: Path) -> Mapping[str, Any]:
    """Read a manifest off disk as TOML (default) or YAML (by suffix).

    Raises:
        ManifestIoError: read failure.
        ManifestTomlError: parse failure.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ManifestIoError(path, f"read failed: {err}") from err

    suffix = path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            loaded = yaml.safe_load(raw_text)
        else:
            loaded = tomllib.loads(raw_text)
    except (tomllib.TOMLDecodeError, yaml.YAMLError) as err:
        raise ManifestTomlError(path, f"parse failed: {err}") from err

    if not isinstance(loaded, Mapping):
        raise ManifestTomlError(path, "top-level value must be a table / mapping")
    return loaded


def parse_manifest_file(path: str | Path) -> PluginManifest:
    """Parse a manifest file, migrate to the current in-memory shape, and
    validate. Returns a fully-validated ``PluginManifest``.

    Mirrors ``rust/crates/corlinman-plugins/src/manifest.rs::parse_manifest_file``.
    The on-disk byte stream is **never** mutated — the migration runs purely
    in memory so the operator's source of truth survives untouched.
    """
    p = Path(path)
    raw = _load_raw(p)
    try:
        manifest = PluginManifest.model_validate(dict(raw))
    except Exception as err:
        raise ManifestValidationError(p, f"schema validation failed: {err}") from err

    manifest.migrate_to_current_in_memory()

    try:
        manifest.validate_all()
    except ValueError as err:
        raise ManifestValidationError(p, str(err)) from err

    return manifest


__all__ = [
    "KNOWN_HOOK_EVENTS",
    "KNOWN_PROTOCOLS",
    "MANIFEST_FILENAME",
    "MAX_SUPPORTED_MANIFEST_VERSION",
    "MCP_MIN_MANIFEST_VERSION",
    "AllowlistMode",
    "Capabilities",
    "Communication",
    "EntryPoint",
    "EnvPassthrough",
    "ManifestIoError",
    "ManifestParseError",
    "ManifestTomlError",
    "ManifestValidationError",
    "McpConfig",
    "Meta",
    "PluginManifest",
    "PluginType",
    "ResourcesAllowlist",
    "RestartPolicy",
    "SandboxConfig",
    "Tool",
    "ToolsAllowlist",
    "parse_manifest_file",
]
