"""Tests for ``corlinman_channels.channel`` — the shared Protocol /
registry / spawn_all surface.

Mirrors the Rust integration tests in ``rust/.../tests/trait_impl.rs``
(the per-adapter behaviour stays covered by the existing OneBot /
Telegram suites).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from corlinman_channels.channel import (
    ApnsChannel,
    Channel,
    ChannelContext,
    ChannelError,
    ChannelRegistry,
    QqChannel,
    TelegramChannel,
    spawn_all,
)
from corlinman_channels.common import UnsupportedError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_cfg() -> SimpleNamespace:
    """Build a Config-shaped object whose ``channels`` slots are all
    ``None`` — the equivalent of ``Config::default()`` in Rust."""
    return SimpleNamespace(channels=SimpleNamespace(qq=None, telegram=None, apns=None))


def _base_ctx(cfg: SimpleNamespace) -> ChannelContext:
    """Build a baseline :class:`ChannelContext` for tests that never
    actually dispatch a message."""
    return ChannelContext(
        config=cfg,
        chat_service=None,  # NoopChatService equivalent — never invoked.
        model="test-model",
        rate_limit_hook=None,
        hook_bus=None,
    )


# ---------------------------------------------------------------------------
# 1. builtin_registry_contains_qq_and_telegram
# ---------------------------------------------------------------------------


class TestBuiltinRegistry:
    def test_builtin_registry_contains_qq_and_telegram(self) -> None:
        registry = ChannelRegistry.builtin()
        ids = [c.id() for c in registry.iter()]
        assert "qq" in ids, "builtin registry must include qq"
        assert "telegram" in ids, "builtin registry must include telegram"
        assert len(registry) == 2, "no unexpected built-in channels"

    def test_registry_push_and_iter_preserves_order(self) -> None:
        class StubA:
            def id(self) -> str:
                return "a"

            def display_name(self) -> str:
                return "a"

            def enabled(self, cfg: object) -> bool:
                return False

            async def run(self, ctx: ChannelContext, cancel: asyncio.Event) -> None:
                return None

        class StubB:
            def id(self) -> str:
                return "b"

            def display_name(self) -> str:
                return "b"

            def enabled(self, cfg: object) -> bool:
                return False

            async def run(self, ctx: ChannelContext, cancel: asyncio.Event) -> None:
                return None

        r = ChannelRegistry()
        r.push(StubA())
        r.push(StubB())
        ids = [c.id() for c in r.iter()]
        assert ids == ["a", "b"]

    def test_builtin_ordering_matches_rust(self) -> None:
        r = ChannelRegistry.builtin()
        assert [c.id() for c in r.iter()] == ["qq", "telegram"]

    def test_display_name_overrides(self) -> None:
        """``QqChannel``/``TelegramChannel`` expose a richer display
        name than the id (matches Rust)."""
        assert QqChannel().display_name() == "QQ (OneBot v11)"
        assert TelegramChannel().display_name() == "Telegram"
        assert ApnsChannel().display_name() == "APNs (stub)"


# ---------------------------------------------------------------------------
# 2. disabled_channel_is_skipped_by_spawn_all
# ---------------------------------------------------------------------------


class TestSpawnAll:
    @pytest.mark.asyncio
    async def test_disabled_channels_skipped(self) -> None:
        """Both built-ins return ``enabled() == False`` for the default
        config; ``spawn_all`` must emit zero handles."""
        ctx = _base_ctx(_empty_cfg())
        cancel = asyncio.Event()
        tasks = spawn_all(ChannelRegistry.builtin(), ctx, cancel)
        assert tasks == [], f"expected zero tasks, got {len(tasks)}"

    @pytest.mark.asyncio
    async def test_mock_channel_run_respects_cancel(self) -> None:
        """A mock channel that loops until cancelled — ``spawn_all``
        should drive it, and ``cancel.set()`` must make ``run`` return
        within a bounded window."""
        entered = asyncio.Event()
        exited = asyncio.Event()

        class Mock:
            def id(self) -> str:
                return "mock"

            def display_name(self) -> str:
                return "mock"

            def enabled(self, cfg: object) -> bool:
                return True

            async def run(
                self,
                ctx: ChannelContext,
                cancel: asyncio.Event,
            ) -> None:
                entered.set()
                await cancel.wait()
                exited.set()

        registry = ChannelRegistry()
        registry.push(Mock())

        ctx = _base_ctx(_empty_cfg())
        cancel = asyncio.Event()
        tasks = spawn_all(registry, ctx, cancel)
        assert len(tasks) == 1

        # Wait for the task to observe its first yield.
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        assert not exited.is_set(), "mock.run should still be awaiting cancel"

        cancel.set()

        # ``run`` must complete promptly after cancel; 1s is generous.
        for t in tasks:
            await asyncio.wait_for(t, timeout=1.0)
            assert t.done()
            assert t.exception() is None
        assert exited.is_set()

    @pytest.mark.asyncio
    async def test_each_spawn_gets_its_own_context_copy(self) -> None:
        """Confirms ``spawn_all`` clones the context so concurrent
        adapters can't accidentally observe each other's mutations.
        Not in the Rust suite (Rust enforces this at the type level)
        but a sensible Python defensive test."""
        seen: list[int] = []

        class Probe:
            def __init__(self, marker: int) -> None:
                self._m = marker

            def id(self) -> str:
                return f"probe{self._m}"

            def display_name(self) -> str:
                return self.id()

            def enabled(self, cfg: object) -> bool:
                return True

            async def run(self, ctx: ChannelContext, cancel: asyncio.Event) -> None:
                seen.append(id(ctx))
                await cancel.wait()

        registry = ChannelRegistry()
        registry.push(Probe(1))
        registry.push(Probe(2))
        ctx = _base_ctx(_empty_cfg())
        cancel = asyncio.Event()
        tasks = spawn_all(registry, ctx, cancel)
        await asyncio.sleep(0.05)
        cancel.set()
        for t in tasks:
            await asyncio.wait_for(t, timeout=1.0)
        assert len(seen) == 2
        assert seen[0] != seen[1], "spawn_all should clone the ChannelContext per task"


# ---------------------------------------------------------------------------
# 3. channel_send_unsupported_default_errors
# ---------------------------------------------------------------------------


class TestChannelError:
    def test_unsupported_factory_message_stable(self) -> None:
        err = ChannelError.unsupported("send")
        assert isinstance(err, UnsupportedError)
        msg = str(err)
        assert "send" in msg, f"error message should mention op: {msg}"
        assert "not supported" in msg, f"error message should mention verdict: {msg}"

    def test_unsupported_is_raiseable(self) -> None:
        with pytest.raises(UnsupportedError) as exc_info:
            raise ChannelError.unsupported("edit")
        assert "edit" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. channel_typing_default_is_noop_ok — Bare Protocol impl compiles + spawns
# ---------------------------------------------------------------------------


class TestBareProtocolImpl:
    def test_minimal_channel_impl_satisfies_protocol(self) -> None:
        class Bare:
            def id(self) -> str:
                return "bare"

            def display_name(self) -> str:
                return self.id()

            def enabled(self, cfg: object) -> bool:
                return False

            async def run(
                self,
                ctx: ChannelContext,
                cancel: asyncio.Event,
            ) -> None:
                return None

        bare = Bare()
        # ``isinstance`` against a runtime_checkable Protocol confirms
        # the structural fit.
        assert isinstance(bare, Channel)

        r = ChannelRegistry()
        r.push(bare)
        assert len(r) == 1
        assert next(r.iter()).id() == "bare"


# ---------------------------------------------------------------------------
# 5. ApnsChannel stub
# ---------------------------------------------------------------------------


class TestApnsStub:
    def test_apns_is_always_disabled(self) -> None:
        a = ApnsChannel()
        assert a.id() == "apns"
        assert a.enabled(_empty_cfg()) is False
        # Even a config that lights up qq/telegram doesn't enable APNs.
        cfg = SimpleNamespace(
            channels=SimpleNamespace(
                qq=SimpleNamespace(enabled=True),
                telegram=SimpleNamespace(enabled=True),
                apns=SimpleNamespace(enabled=True),
            )
        )
        assert a.enabled(cfg) is False

    @pytest.mark.asyncio
    async def test_apns_run_is_noop(self) -> None:
        a = ApnsChannel()
        ctx = _base_ctx(_empty_cfg())
        cancel = asyncio.Event()
        # Should return ~immediately without raising.
        await asyncio.wait_for(a.run(ctx, cancel), timeout=1.0)
