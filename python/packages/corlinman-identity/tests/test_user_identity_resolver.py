""":class:`UserIdentityResolver` facade + :class:`ChannelRegistry` tests.

No 1:1 Rust counterpart — these cover the Python-side
``UserIdentityResolver`` and the channel adapter plug-in surface that
the port adds on top of the Rust types.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_identity import (
    ChannelAdapter,
    ChannelRegistry,
    TenantId,
    UserIdentityResolver,
    VerificationPhrase,
    legacy_default,
)


class _RecordingAdapter:
    """Test-double :class:`ChannelAdapter` that records ``echo_phrase`` calls."""

    def __init__(self, slug: str) -> None:
        self._slug = slug
        self.echoes: list[tuple[str, VerificationPhrase]] = []

    def name(self) -> str:
        return self._slug

    async def echo_phrase(
        self, channel_user_id: str, phrase: VerificationPhrase
    ) -> None:
        self.echoes.append((channel_user_id, phrase))


async def test_open_creates_per_tenant_db(tmp_path: Path) -> None:
    resolver = await UserIdentityResolver.open(tmp_path, TenantId("acme"))
    try:
        assert resolver.store.path.exists()
        assert "/tenants/acme/" in str(resolver.store.path)
    finally:
        await resolver.close()


async def test_resolve_link_verify_round_trip(tmp_path: Path) -> None:
    resolver = await UserIdentityResolver.open(tmp_path, legacy_default())
    try:
        qq_uid = await resolver.resolve("qq", "1234", display_name_hint="Alice")
        tg_uid = await resolver.resolve("telegram", "9876")
        assert qq_uid != tg_uid

        # Verification-phrase round trip.
        phrase = await resolver.verify_issue(qq_uid, "qq", "1234")
        surviving = await resolver.verify_redeem(
            phrase.phrase, "telegram", "9876"
        )
        assert surviving == qq_uid

        # Lookup post-merge resolves to the survivor.
        assert await resolver.lookup("telegram", "9876") == qq_uid

        # link() (operator-driven merge): start a third user and fold it in.
        ios_uid = await resolver.resolve("ios", "device-abc")
        merged = await resolver.link(qq_uid, ios_uid, decided_by="op-jane")
        assert merged == qq_uid
        assert await resolver.lookup("ios", "device-abc") == qq_uid
    finally:
        await resolver.close()


async def test_verify_issue_echoes_via_registered_adapter(tmp_path: Path) -> None:
    registry = ChannelRegistry()
    adapter = _RecordingAdapter("qq")
    registry.register(adapter)
    # Sanity: registered adapters honour the Protocol.
    assert isinstance(adapter, ChannelAdapter)
    assert registry.get("qq") is adapter

    resolver = await UserIdentityResolver.open(
        tmp_path, legacy_default(), channels=registry
    )
    try:
        uid = await resolver.resolve("qq", "1234")
        phrase = await resolver.verify_issue(uid, "qq", "1234")
        # Adapter saw the echo.
        assert len(adapter.echoes) == 1
        echoed_channel_user_id, echoed_phrase = adapter.echoes[0]
        assert echoed_channel_user_id == "1234"
        assert echoed_phrase == phrase
    finally:
        await resolver.close()


async def test_verify_issue_skips_echo_when_disabled(tmp_path: Path) -> None:
    registry = ChannelRegistry()
    adapter = _RecordingAdapter("qq")
    registry.register(adapter)

    resolver = await UserIdentityResolver.open(
        tmp_path, legacy_default(), channels=registry
    )
    try:
        uid = await resolver.resolve("qq", "1234")
        await resolver.verify_issue(uid, "qq", "1234", echo=False)
        assert adapter.echoes == []
    finally:
        await resolver.close()


def test_channel_registry_rejects_blank_name() -> None:
    class _Bad:
        def name(self) -> str:
            return ""

        async def echo_phrase(
            self, channel_user_id: str, phrase: VerificationPhrase
        ) -> None:
            return None

    registry = ChannelRegistry()
    try:
        registry.register(_Bad())
    except ValueError:
        return
    raise AssertionError("blank-name adapter must be rejected")
