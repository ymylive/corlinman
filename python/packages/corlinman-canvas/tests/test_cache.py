"""Port of the Rust ``cache.rs`` inline tests and ``lib.rs`` cache tests.

Validates LRU semantics, capacity == 0 kill-switch, deterministic key
derivation, and hex-form `content_hash` shape.
"""

from __future__ import annotations

from corlinman_canvas import (
    ArtifactKind,
    CanvasPresentPayload,
    CodeBody,
    RenderCache,
    Renderer,
    TableBody,
    ThemeClass,
    cache_key_for,
    canonical_json_bytes,
    key_to_hex,
)


def _body_a() -> CodeBody:
    return CodeBody(language="rust", source="fn main() {}")


def _body_b() -> CodeBody:
    return CodeBody(language="rust", source="fn other() {}")


def _payload(source: str) -> CanvasPresentPayload:
    return CanvasPresentPayload(
        artifact_kind=ArtifactKind.CODE,
        body=CodeBody(language="rust", source=source),
        idempotency_key="art_t",
        theme_hint=ThemeClass.TP_LIGHT,
    )


# --- RenderCache primitive --------------------------------------------------


def test_disabled_cache_always_misses() -> None:
    cache = RenderCache(0)
    assert cache.is_disabled()
    k = cache_key_for(ArtifactKind.CODE, _body_a(), ThemeClass.TP_LIGHT)
    assert cache.get(k) is None
    # Stub artifact — never actually inserted because cache is disabled.
    cache.insert(k, _stub_artifact())
    assert cache.get(k) is None
    assert len(cache) == 0
    assert cache.is_empty()


def test_enabled_cache_returns_same_instance_on_hit() -> None:
    cache = RenderCache(8)
    assert not cache.is_disabled()
    k = cache_key_for(ArtifactKind.CODE, _body_a(), ThemeClass.TP_LIGHT)
    artifact = _stub_artifact()
    cache.insert(k, artifact)
    hit = cache.get(k)
    assert hit is artifact  # same object identity
    assert len(cache) == 1


def test_cache_evicts_at_capacity() -> None:
    cache = RenderCache(2)
    k1 = cache_key_for(ArtifactKind.CODE, _body_a(), ThemeClass.TP_LIGHT)
    k2 = cache_key_for(ArtifactKind.CODE, _body_b(), ThemeClass.TP_LIGHT)
    k3 = cache_key_for(
        ArtifactKind.CODE,
        CodeBody(language="rust", source="fn third() {}"),
        ThemeClass.TP_LIGHT,
    )

    cache.insert(k1, _stub_artifact("1"))
    cache.insert(k2, _stub_artifact("2"))
    # Touch k1 → k2 becomes the LRU victim.
    cache.get(k1)
    cache.insert(k3, _stub_artifact("3"))

    assert len(cache) == 2
    assert cache.get(k1) is not None
    assert cache.get(k3) is not None
    assert cache.get(k2) is None


# --- Key derivation --------------------------------------------------------


def test_key_is_deterministic() -> None:
    k1 = cache_key_for(ArtifactKind.CODE, _body_a(), ThemeClass.TP_LIGHT)
    k2 = cache_key_for(ArtifactKind.CODE, _body_a(), ThemeClass.TP_LIGHT)
    assert k1 == k2


def test_key_differs_by_kind() -> None:
    code = cache_key_for(ArtifactKind.CODE, _body_a(), ThemeClass.TP_LIGHT)
    table = cache_key_for(
        ArtifactKind.TABLE,
        TableBody(markdown="|a|\n|-|\n|1|"),
        ThemeClass.TP_LIGHT,
    )
    assert code != table


def test_key_differs_by_theme() -> None:
    light = cache_key_for(ArtifactKind.CODE, _body_a(), ThemeClass.TP_LIGHT)
    dark = cache_key_for(ArtifactKind.CODE, _body_a(), ThemeClass.TP_DARK)
    assert light != dark


def test_key_to_hex_is_64_lowercase_hex() -> None:
    k = cache_key_for(ArtifactKind.CODE, _body_a(), ThemeClass.TP_LIGHT)
    hx = key_to_hex(k)
    assert len(hx) == 64
    assert all(ch in "0123456789abcdef" for ch in hx)


def test_canonical_json_sorts_object_keys() -> None:
    # Build two bodies with different declaration order; the canonical
    # bytes should be byte-equal.
    a = canonical_json_bytes(
        CodeBody(language="rust", source="fn main() {}")
    )
    b = canonical_json_bytes(
        CodeBody(source="fn main() {}", language="rust")  # type: ignore[call-arg]
    )
    assert a == b


# --- Renderer integration --------------------------------------------------


def test_render_populates_content_hash() -> None:
    r = Renderer()
    out = r.render(_payload("fn main() {}"))
    assert len(out.content_hash) == 64
    assert all(ch in "0123456789abcdef" for ch in out.content_hash)


def test_equal_inputs_produce_equal_content_hash() -> None:
    r = Renderer()
    a = r.render(_payload("fn main() {}"))
    b = r.render(_payload("fn main() {}"))
    assert a.content_hash == b.content_hash


def test_different_sources_produce_different_content_hash() -> None:
    r = Renderer()
    a = r.render(_payload("fn main() {}"))
    b = r.render(_payload("fn other() {}"))
    assert a.content_hash != b.content_hash


def test_cache_hit_short_circuits_adapter() -> None:
    r = Renderer.with_cache(8)
    p = _payload("fn main() {}")
    r.render(p)
    assert len(r.cache) == 1
    r.render(p)
    assert len(r.cache) == 1


def test_disabled_cache_does_not_grow() -> None:
    r = Renderer()
    r.render(_payload("fn main() {}"))
    r.render(_payload("fn other() {}"))
    assert len(r.cache) == 0
    assert r.cache.is_disabled()


def test_cache_evicts_at_capacity_via_render() -> None:
    r = Renderer.with_cache(2)
    r.render(_payload("a"))
    r.render(_payload("b"))
    r.render(_payload("c"))
    assert len(r.cache) == 2


# --- Helpers ---------------------------------------------------------------


def _stub_artifact(html: str = "<pre/>"):
    from corlinman_canvas import RenderedArtifact

    return RenderedArtifact(
        html_fragment=html,
        theme_class=ThemeClass.TP_LIGHT,
        render_kind=ArtifactKind.CODE,
        content_hash="",
        warnings=(),
    )
