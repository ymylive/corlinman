"""Canvas Host protocol types — Python port of `protocol.rs`.

Mirrors the Rust crate's public surface:

- :class:`ArtifactKind` — closed enum of supported artifact kinds.
- :class:`ThemeClass` — `tp-light` / `tp-dark` tag.
- :class:`ArtifactBody` — discriminated body, with one dataclass per kind.
- :class:`CanvasPresentPayload` — top-level wire shape with a custom JSON
  decoder that re-validates `body` against `artifact_kind`.
- :class:`RenderedArtifact` — what :class:`~corlinman_canvas.renderer.Renderer`
  returns.
- :class:`CanvasError` — typed error variants.

The JSON wire format is byte-equivalent to the Rust crate's output: the
discriminator (`artifact_kind`) lives at the top level, the `body` is
"untagged" and contains only that variant's native fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ArtifactKind(str, Enum):
    """Closed enum of artifact kinds the renderer understands.

    Adding a new kind requires touching this enum *and* the renderer
    dispatch in :mod:`corlinman_canvas.renderer` — intentional: closed
    vocabulary, no producer surprises.
    """

    CODE = "code"
    MERMAID = "mermaid"
    TABLE = "table"
    LATEX = "latex"
    SPARKLINE = "sparkline"

    def as_str(self) -> str:
        """Wire name as it appears in `payload.artifact_kind`."""
        return self.value


class ThemeClass(str, Enum):
    """Theme tag for non-CSS-var consumers (Swift / mobile, future static export)."""

    TP_LIGHT = "tp-light"
    TP_DARK = "tp-dark"

    @classmethod
    def default(cls) -> ThemeClass:
        return cls.TP_LIGHT


# --- ArtifactBody variants --------------------------------------------------

# Each variant is a small frozen dataclass. They all share the
# :class:`ArtifactBody` union; the actual dispatch lives on
# :class:`CanvasPresentPayload.artifact_kind` (see Rust commentary in
# `protocol.rs` for why we don't use an untagged-enum form on the wire).


@dataclass(frozen=True)
class CodeBody:
    """`code` body. `language` is a Pygments-recognised name; unknown
    languages fall back to a plain `<pre>` in the renderer (no error).
    """

    language: str
    source: str

    def to_json(self) -> dict[str, Any]:
        return {"language": self.language, "source": self.source}


@dataclass(frozen=True)
class MermaidBody:
    """`mermaid` body. The diagram source is rendered client-side; see
    :mod:`corlinman_canvas.mermaid`.
    """

    diagram: str

    def to_json(self) -> dict[str, Any]:
        return {"diagram": self.diagram}


@dataclass(frozen=True)
class LatexBody:
    """`latex` body. `display=True` -> block; `display=False` -> inline."""

    tex: str
    display: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"tex": self.tex, "display": self.display}


@dataclass(frozen=True)
class SparklineBody:
    """`sparkline` body. ``values`` is required; ``unit`` is optional."""

    values: tuple[float, ...]
    unit: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"values": list(self.values)}
        if self.unit is not None:
            out["unit"] = self.unit
        return out


@dataclass(frozen=True)
class TableBody:
    """`table` body. Exactly one of ``markdown`` / ``csv`` must be set."""

    markdown: str | None = None
    csv: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.markdown is not None:
            out["markdown"] = self.markdown
        if self.csv is not None:
            out["csv"] = self.csv
        return out


ArtifactBody = CodeBody | MermaidBody | LatexBody | SparklineBody | TableBody


# --- Errors -----------------------------------------------------------------


class CanvasError(Exception):
    """Renderer-level error base class. Subclasses mirror the Rust crate's
    `CanvasError` variants 1:1.
    """


class UnimplementedKind(CanvasError):
    """Kind is recognised but no adapter is wired in this iteration."""

    def __init__(self, kind: ArtifactKind):
        super().__init__(f"renderer for `{kind!r}` not implemented in this build")
        self.kind = kind


class UnknownKind(CanvasError):
    """Wire payload had an `artifact_kind` the renderer doesn't know."""

    def __init__(self, raw: str):
        super().__init__(f"unknown canvas artifact kind: `{raw}`")
        self.raw = raw


class BodyTooLarge(CanvasError):
    """Producer body exceeded `[canvas] max_artifact_bytes`."""

    def __init__(self, max_bytes: int, kind: ArtifactKind):
        super().__init__(
            f"artifact body exceeded {max_bytes} bytes (kind={kind!r})"
        )
        self.max_bytes = max_bytes
        self.kind = kind


class TimeoutError_(CanvasError):  # noqa: N818 — mirrors the Rust variant name
    """Render exceeded `[canvas] render_timeout_ms`."""

    def __init__(self, timeout_ms: int, kind: ArtifactKind):
        super().__init__(
            f"renderer timed out after {timeout_ms} ms (kind={kind!r})"
        )
        self.timeout_ms = timeout_ms
        self.kind = kind


class AdapterError(CanvasError):
    """Adapter-specific parse / runtime failure."""

    def __init__(self, kind: ArtifactKind, message: str):
        super().__init__(f"canvas adapter error ({kind!r}): {message}")
        self.kind = kind
        self.message = message


# --- Payload + rendered artifact -------------------------------------------


@dataclass(frozen=True)
class RenderedArtifact:
    """Renderer output. Self-contained: callers need only the fragment +
    theme class to surface the artifact. Hash and warnings are
    optional UX / diagnostics extras.
    """

    html_fragment: str
    theme_class: ThemeClass
    render_kind: ArtifactKind
    content_hash: str = ""
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "html_fragment": self.html_fragment,
            "theme_class": self.theme_class.value,
            "render_kind": self.render_kind.value,
            "content_hash": self.content_hash,
        }
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


@dataclass(frozen=True)
class CanvasPresentPayload:
    """Top-level shape inside the `present` frame's `payload`.

    JSON encoding via :meth:`to_json` matches the Rust crate's serialised
    layout. :meth:`from_json` performs the Rust-side two-pass decode:
    read `artifact_kind` first, then dispatch to the matching body
    variant, rejecting unknown fields and mismatched shapes.
    """

    artifact_kind: ArtifactKind
    body: ArtifactBody
    idempotency_key: str
    theme_hint: ThemeClass | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "artifact_kind": self.artifact_kind.value,
            "body": self.body.to_json(),
            "idempotency_key": self.idempotency_key,
        }
        if self.theme_hint is not None:
            out["theme_hint"] = self.theme_hint.value
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> CanvasPresentPayload:
        """Decode a JSON dict into a payload, validating the body against
        ``artifact_kind`` and rejecting unknown top-level fields.
        """

        allowed_top = {"artifact_kind", "body", "idempotency_key", "theme_hint"}
        extra = set(raw) - allowed_top
        if extra:
            raise AdapterError(
                ArtifactKind.CODE,  # placeholder; gateway uses serde at boundary
                f"unknown top-level field(s): {sorted(extra)!r}",
            )

        kind_raw = raw.get("artifact_kind")
        if not isinstance(kind_raw, str):
            raise AdapterError(
                ArtifactKind.CODE,
                "missing or non-string `artifact_kind`",
            )
        try:
            kind = ArtifactKind(kind_raw)
        except ValueError as exc:
            raise UnknownKind(kind_raw) from exc

        body_raw = raw.get("body")
        if not isinstance(body_raw, dict):
            raise AdapterError(kind, "missing or non-object `body`")

        idem = raw.get("idempotency_key")
        if not isinstance(idem, str):
            raise AdapterError(kind, "missing or non-string `idempotency_key`")

        theme_raw = raw.get("theme_hint")
        theme_hint: ThemeClass | None
        if theme_raw is None:
            theme_hint = None
        elif isinstance(theme_raw, str):
            try:
                theme_hint = ThemeClass(theme_raw)
            except ValueError as exc:
                raise AdapterError(kind, f"unknown theme_hint `{theme_raw}`") from exc
        else:
            raise AdapterError(kind, "non-string `theme_hint`")

        body = _decode_body(kind, body_raw)
        return cls(
            artifact_kind=kind,
            body=body,
            idempotency_key=idem,
            theme_hint=theme_hint,
        )


def _decode_body(kind: ArtifactKind, body: dict[str, Any]) -> ArtifactBody:
    """Per-variant body decode. Each branch enforces `deny_unknown_fields`."""

    if kind == ArtifactKind.CODE:
        _require_fields(kind, body, {"language", "source"}, set())
        language = body["language"]
        source = body["source"]
        if not isinstance(language, str) or not isinstance(source, str):
            raise AdapterError(kind, "code body fields must be strings")
        return CodeBody(language=language, source=source)
    if kind == ArtifactKind.MERMAID:
        _require_fields(kind, body, {"diagram"}, set())
        diagram = body["diagram"]
        if not isinstance(diagram, str):
            raise AdapterError(kind, "mermaid `diagram` must be a string")
        return MermaidBody(diagram=diagram)
    if kind == ArtifactKind.LATEX:
        _require_fields(kind, body, {"tex"}, {"display"})
        tex = body["tex"]
        display = body.get("display", False)
        if not isinstance(tex, str) or not isinstance(display, bool):
            raise AdapterError(kind, "latex `tex` must be string and `display` bool")
        return LatexBody(tex=tex, display=display)
    if kind == ArtifactKind.SPARKLINE:
        _require_fields(kind, body, {"values"}, {"unit"})
        values = body["values"]
        if not isinstance(values, list) or not all(
            isinstance(v, int | float) for v in values
        ):
            raise AdapterError(kind, "sparkline `values` must be a list of numbers")
        unit = body.get("unit")
        if unit is not None and not isinstance(unit, str):
            raise AdapterError(kind, "sparkline `unit` must be a string or null")
        return SparklineBody(
            values=tuple(float(v) for v in values),
            unit=unit,
        )
    if kind == ArtifactKind.TABLE:
        _require_fields(kind, body, set(), {"markdown", "csv"})
        markdown = body.get("markdown")
        csv = body.get("csv")
        if markdown is not None and not isinstance(markdown, str):
            raise AdapterError(kind, "table `markdown` must be a string")
        if csv is not None and not isinstance(csv, str):
            raise AdapterError(kind, "table `csv` must be a string")
        return TableBody(markdown=markdown, csv=csv)
    raise UnknownKind(kind.value)


def _require_fields(
    kind: ArtifactKind,
    body: dict[str, Any],
    required: set[str],
    optional: set[str],
) -> None:
    allowed = required | optional
    extra = set(body) - allowed
    if extra:
        raise AdapterError(
            kind, f"unknown field(s) in body: {sorted(extra)!r}"
        )
    missing = required - set(body)
    if missing:
        raise AdapterError(
            kind, f"missing required field(s) in body: {sorted(missing)!r}"
        )


__all__ = [
    "AdapterError",
    "ArtifactBody",
    "ArtifactKind",
    "BodyTooLarge",
    "CanvasError",
    "CanvasPresentPayload",
    "CodeBody",
    "LatexBody",
    "MermaidBody",
    "RenderedArtifact",
    "SparklineBody",
    "TableBody",
    "ThemeClass",
    "TimeoutError_",
    "UnimplementedKind",
    "UnknownKind",
]
