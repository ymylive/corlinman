"""Tests for the Telegram outbound modules:

- ``telegram_media`` (``get_file`` + ``download_to_media_dir``)
- ``telegram_send`` (``send_message`` / ``send_photo`` / ``send_voice``
  + ``build_multipart``)
- ``telegram_webhook`` (``verify_secret`` + ``process_update``)

Mirrors the Rust ``media::tests`` / ``send::tests`` / ``webhook::tests``
modules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from corlinman_channels.telegram import File, Update
from corlinman_channels.telegram_media import (
    MediaError,
    MediaNoFilePathError,
    download_to_media_dir,
)
from corlinman_channels.telegram_send import (
    PhotoSource,
    SendApiError,
    SendError,
    SendHttpError,
    TelegramSender,
    build_multipart,
)
from corlinman_channels.telegram_webhook import (
    MessageRoute,
    WebhookCtx,
    process_update,
    verify_secret,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeHttp:
    """In-memory :class:`TelegramHttp`. Mirrors Rust ``media::tests::FakeHttp``.

    ``scripted_file_path`` is what :meth:`get_file` will report;
    ``scripted_bytes`` is streamed back by :meth:`download_stream`.
    """

    def __init__(
        self,
        scripted_file_path: str | None,
        scripted_bytes: bytes,
    ) -> None:
        self.scripted_file_path = scripted_file_path
        self.scripted_bytes = scripted_bytes
        self.get_file_calls: list[str] = []
        self.download_calls: list[str] = []

    async def get_file(self, file_id: str) -> File:
        self.get_file_calls.append(file_id)
        return File(
            file_id=file_id,
            file_unique_id=f"uniq_{file_id}",
            file_size=len(self.scripted_bytes),
            file_path=self.scripted_file_path,
        )

    def download_stream(self, file_path: str) -> AsyncIterator[bytes]:
        self.download_calls.append(file_path)
        bytes_ = self.scripted_bytes

        async def _iter() -> AsyncIterator[bytes]:
            yield bytes_

        return _iter()


@pytest.fixture
def tmpdir_path(tmp_path: Path) -> Path:
    """Yield an absolute temp dir; thin wrapper for clarity."""
    return tmp_path


# ---------------------------------------------------------------------------
# Media downloads
# ---------------------------------------------------------------------------


class TestMediaDownload:
    @pytest.mark.asyncio
    async def test_media_download_streams_to_disk(
        self,
        tmpdir_path: Path,
    ) -> None:
        bytes_ = b"fake-ogg-bytes"
        http = FakeHttp("voice/file_7.oga", bytes_)
        got = await download_to_media_dir(http, "FILE123", tmpdir_path, "bin")
        assert got.bytes_written == len(bytes_)
        assert got.path.is_relative_to(tmpdir_path)
        assert got.path.suffix == ".oga"
        assert got.path.read_bytes() == bytes_

    @pytest.mark.asyncio
    async def test_missing_file_path_errors_out(
        self,
        tmpdir_path: Path,
    ) -> None:
        http = FakeHttp(None, b"")
        with pytest.raises(MediaNoFilePathError):
            await download_to_media_dir(http, "FILE", tmpdir_path, "bin")

    @pytest.mark.asyncio
    async def test_fallback_extension_used_when_path_lacks_one(
        self,
        tmpdir_path: Path,
    ) -> None:
        """If the resolved file_path has no extension, we use the
        caller-supplied fallback. Not in the Rust suite but the
        behaviour is documented in the port."""
        http = FakeHttp("no_extension", b"data")
        got = await download_to_media_dir(http, "FID", tmpdir_path, "ogg")
        assert got.path.suffix == ".ogg"

    @pytest.mark.asyncio
    async def test_unique_id_drives_idempotent_filename(
        self,
        tmpdir_path: Path,
    ) -> None:
        """Two downloads of the same asset share the same on-disk
        path (idempotent)."""
        http = FakeHttp("voice/x.oga", b"a")
        got1 = await download_to_media_dir(http, "X", tmpdir_path, "bin")
        got2 = await download_to_media_dir(http, "X", tmpdir_path, "bin")
        assert got1.path == got2.path


# ---------------------------------------------------------------------------
# Multipart builder
# ---------------------------------------------------------------------------


class TestMultipart:
    def test_multipart_includes_chat_id_filename_and_bytes(self) -> None:
        mp = build_multipart(
            42,
            "photo",
            "cat.jpg",
            b"\x89PNG\r\n",
            "hello",
            "image/jpeg",
        )
        s = mp.body.decode("latin-1")
        assert 'name="chat_id"' in s
        assert "42" in s
        assert 'name="photo"' in s
        assert 'filename="cat.jpg"' in s
        assert 'name="caption"' in s
        assert "hello" in s
        # Closing delimiter must be present.
        closer = f"--{mp.boundary}--"
        assert closer in s
        # Raw bytes preserved.
        assert b"\x89PNG\r\n" in mp.body

    def test_multipart_boundary_is_unique_per_call(self) -> None:
        a = build_multipart(1, "photo", "a", b"x", None, "image/jpeg")
        b = build_multipart(1, "photo", "a", b"x", None, "image/jpeg")
        assert a.boundary != b.boundary


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------


def _ok_envelope(message_id: int = 1) -> dict[str, Any]:
    return {"ok": True, "result": {"message_id": message_id}}


def _api_err_envelope() -> dict[str, Any]:
    return {"ok": False, "description": "Bad Request: chat not found"}


class TestSender:
    @pytest.mark.asyncio
    async def test_send_message_round_trips(self) -> None:
        recorded: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            recorded.append(req)
            assert req.url.path.endswith("/sendMessage")
            return httpx.Response(200, json=_ok_envelope(42))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sender = TelegramSender(client, "TEST")
        message_id = await sender.send_message(123, "hi", reply_to_message_id=99)
        await client.aclose()

        assert message_id == 42
        assert len(recorded) == 1
        body = recorded[0].read()
        assert b'"chat_id":123' in body
        assert b'"text":"hi"' in body
        assert b'"reply_to_message_id":99' in body

    @pytest.mark.asyncio
    async def test_send_message_surface_api_errors(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_api_err_envelope())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sender = TelegramSender(client, "TEST")
        with pytest.raises(SendApiError) as exc_info:
            await sender.send_message(123, "hi")
        await client.aclose()
        assert "Bad Request" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_send_message_http_error_surface(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="upstream is down")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sender = TelegramSender(client, "TEST")
        with pytest.raises(SendHttpError):
            await sender.send_message(1, "x")
        await client.aclose()

    @pytest.mark.asyncio
    async def test_send_photo_url_uses_json_form(self) -> None:
        recorded: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            recorded.append(req)
            return httpx.Response(200, json=_ok_envelope(7))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sender = TelegramSender(client, "TEST")
        message_id = await sender.send_photo(
            42, PhotoSource.Url("https://cdn/cat.jpg"), caption="meow"
        )
        await client.aclose()
        assert message_id == 7
        body = recorded[0].read()
        assert b'"photo":"https://cdn/cat.jpg"' in body
        assert b'"caption":"meow"' in body

    @pytest.mark.asyncio
    async def test_send_photo_path_uses_multipart(
        self,
        tmpdir_path: Path,
    ) -> None:
        photo = tmpdir_path / "cat.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0FAKE")

        recorded: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            recorded.append(req)
            return httpx.Response(200, json=_ok_envelope(11))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sender = TelegramSender(client, "TEST")
        message_id = await sender.send_photo(
            42, PhotoSource.Path(photo), caption="hi"
        )
        await client.aclose()
        assert message_id == 11

        req = recorded[0]
        ct = req.headers.get("content-type", "")
        assert ct.startswith("multipart/form-data; boundary=")
        body = req.read()
        assert b'name="chat_id"' in body
        assert b'name="photo"' in body
        assert b'filename="cat.jpg"' in body
        # Raw image bytes survived the multipart encoder.
        assert b"\xff\xd8\xff\xe0FAKE" in body

    @pytest.mark.asyncio
    async def test_send_voice_round_trips(
        self,
        tmpdir_path: Path,
    ) -> None:
        voice = tmpdir_path / "v.ogg"
        voice.write_bytes(b"OggS")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_envelope(13))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sender = TelegramSender(client, "TEST")
        message_id = await sender.send_voice(42, voice)
        await client.aclose()
        assert message_id == 13


# ---------------------------------------------------------------------------
# Webhook signature
# ---------------------------------------------------------------------------


class TestVerifySecret:
    def test_signature_valid_accepts_update(self) -> None:
        assert verify_secret("sekret", "sekret")

    def test_signature_invalid_returns_false(self) -> None:
        assert not verify_secret("sekret", "sekret2")
        assert not verify_secret("sekret", "")
        assert not verify_secret("sekret", None)

    def test_empty_config_disables_check(self) -> None:
        assert verify_secret("", None)
        assert verify_secret("", "anything")


# ---------------------------------------------------------------------------
# Webhook process_update
# ---------------------------------------------------------------------------


def _update_private_text(text: str = "hi") -> Update:
    return Update.model_validate({
        "update_id": 10,
        "message": {
            "message_id": 1,
            "from": {"id": 42, "is_bot": False, "username": "alice"},
            "chat": {"id": 42, "type": "private"},
            "date": 0,
            "text": text,
        },
    })


def _update_group_plain(text: str = "random chatter") -> Update:
    return Update.model_validate({
        "update_id": 11,
        "message": {
            "message_id": 2,
            "from": {"id": 77, "is_bot": False},
            "chat": {"id": -100, "type": "supergroup", "title": "room"},
            "date": 0,
            "text": text,
        },
    })


def _update_group_mention(text: str = "@corlinman_bot hello") -> Update:
    return Update.model_validate({
        "update_id": 12,
        "message": {
            "message_id": 3,
            "from": {"id": 77, "is_bot": False},
            "chat": {"id": -100, "type": "supergroup"},
            "date": 0,
            "text": text,
            "entities": [{"type": "mention", "offset": 0, "length": 14}],
        },
    })


def _update_group_reply_to_bot() -> Update:
    return Update.model_validate({
        "update_id": 13,
        "message": {
            "message_id": 4,
            "from": {"id": 77, "is_bot": False},
            "chat": {"id": -100, "type": "supergroup"},
            "date": 0,
            "text": "yes please",
            "reply_to_message": {
                "message_id": 99,
                "from": {"id": 999, "is_bot": True, "username": "corlinman_bot"},
                "chat": {"id": -100, "type": "supergroup"},
                "date": 0,
                "text": "Need anything?",
            },
        },
    })


def _update_private_voice() -> Update:
    return Update.model_validate({
        "update_id": 14,
        "message": {
            "message_id": 5,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": 42, "type": "private"},
            "date": 0,
            "voice": {"file_id": "V123", "duration": 3},
        },
    })


def _update_private_photo() -> Update:
    return Update.model_validate({
        "update_id": 15,
        "message": {
            "message_id": 6,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": 42, "type": "private"},
            "date": 0,
            "photo": [
                {"file_id": "P_SMALL", "width": 90, "height": 90, "file_size": 500},
                {"file_id": "P_MED", "width": 320, "height": 320, "file_size": 5000},
                {"file_id": "P_BIG", "width": 1280, "height": 1280, "file_size": 50000},
            ],
        },
    })


class TestProcessUpdate:
    @pytest.mark.asyncio
    async def test_private_chat_triggers_response(
        self,
        tmpdir_path: Path,
    ) -> None:
        ctx = WebhookCtx(
            bot_id=999,
            bot_username="corlinman_bot",
            data_dir=tmpdir_path,
            http=FakeHttp("x/y.txt", b""),
            hooks=None,
        )
        out = await process_update(ctx, _update_private_text("hi"))
        assert out is not None
        assert out.route == MessageRoute.PRIVATE
        assert out.route.should_respond()

    @pytest.mark.asyncio
    async def test_group_without_mention_emits_received_but_not_respond(
        self,
        tmpdir_path: Path,
    ) -> None:
        from corlinman_hooks import HookBus, HookPriority

        bus = HookBus(16)
        sub = bus.subscribe(HookPriority.NORMAL)
        ctx = WebhookCtx(
            bot_id=999,
            bot_username="corlinman_bot",
            data_dir=tmpdir_path,
            http=FakeHttp("x/y.txt", b""),
            hooks=bus,
        )
        out = await process_update(ctx, _update_group_plain("random chatter"))
        assert out is not None
        assert out.route == MessageRoute.GROUP_IGNORED
        assert not out.route.should_respond()

        ev = await sub.recv()
        assert ev.kind() == "message_received"

    @pytest.mark.asyncio
    async def test_group_with_at_mention_triggers_response(
        self,
        tmpdir_path: Path,
    ) -> None:
        ctx = WebhookCtx(
            bot_id=999,
            bot_username="corlinman_bot",
            data_dir=tmpdir_path,
            http=FakeHttp("x/y.txt", b""),
            hooks=None,
        )
        out = await process_update(ctx, _update_group_mention("@corlinman_bot hello"))
        assert out is not None
        assert out.route == MessageRoute.GROUP_ADDRESSED

    @pytest.mark.asyncio
    async def test_group_with_reply_to_bot_triggers_response(
        self,
        tmpdir_path: Path,
    ) -> None:
        ctx = WebhookCtx(
            bot_id=999,
            bot_username="corlinman_bot",
            data_dir=tmpdir_path,
            http=FakeHttp("x/y.txt", b""),
            hooks=None,
        )
        out = await process_update(ctx, _update_group_reply_to_bot())
        assert out is not None
        assert out.route == MessageRoute.GROUP_ADDRESSED

    @pytest.mark.asyncio
    async def test_voice_message_emits_transcribed_hook_with_empty_transcript(
        self,
        tmpdir_path: Path,
    ) -> None:
        from corlinman_hooks import HookBus, HookPriority

        bus = HookBus(16)
        sub = bus.subscribe(HookPriority.NORMAL)
        http = FakeHttp("voice/a.oga", b"ogg-bytes")
        ctx = WebhookCtx(
            bot_id=999,
            bot_username="corlinman_bot",
            data_dir=tmpdir_path,
            http=http,
            hooks=bus,
        )
        out = await process_update(ctx, _update_private_voice())
        assert out is not None
        assert out.media_kind == "voice"
        assert out.media is not None

        first = await sub.recv()
        assert first.kind() == "message_received"
        second = await sub.recv()
        assert second.kind() == "message_transcribed"
        # Inspect transcript / media fields.
        assert second.transcript == ""
        assert second.media_type == "voice"
        assert second.media_path  # non-empty path string

    @pytest.mark.asyncio
    async def test_photo_largest_file_id_selected_for_download(
        self,
        tmpdir_path: Path,
    ) -> None:
        http = FakeHttp("photos/p.jpg", b"fake-jpg")
        ctx = WebhookCtx(
            bot_id=999,
            bot_username="corlinman_bot",
            data_dir=tmpdir_path,
            http=http,
            hooks=None,
        )
        _ = await process_update(ctx, _update_private_photo())
        assert http.get_file_calls == ["P_BIG"], (
            "largest photo file_id must be chosen"
        )

    @pytest.mark.asyncio
    async def test_media_download_streams_to_disk_via_webhook(
        self,
        tmpdir_path: Path,
    ) -> None:
        http = FakeHttp("photos/cat.jpg", b"JPGDATA")
        ctx = WebhookCtx(
            bot_id=999,
            bot_username=None,
            data_dir=tmpdir_path,
            http=http,
            hooks=None,
        )
        out = await process_update(ctx, _update_private_photo())
        assert out is not None
        assert out.media is not None
        assert out.media.path.exists()
        assert out.media.path.read_bytes() == b"JPGDATA"
        assert out.media.path.is_relative_to(tmpdir_path)

    @pytest.mark.asyncio
    async def test_non_message_update_returns_none(
        self,
        tmpdir_path: Path,
    ) -> None:
        u = Update.model_validate({"update_id": 99})
        assert u.message is None
        ctx = WebhookCtx(
            bot_id=999,
            bot_username=None,
            data_dir=tmpdir_path,
            http=FakeHttp("x/y.txt", b""),
            hooks=None,
        )
        out = await process_update(ctx, u)
        assert out is None


# ---------------------------------------------------------------------------
# SendError enum-style attrs
# ---------------------------------------------------------------------------


class TestErrorEnumAttrs:
    def test_media_error_aliases(self) -> None:
        assert MediaError.NoFilePath is type(MediaError.NoFilePath())
        # All enum aliases should be subclasses of the base.
        for cls in (
            MediaError.Api,
            MediaError.NoFilePath,
            MediaError.TooLarge,
            MediaError.Io,
            MediaError.Http,
        ):
            assert issubclass(cls, MediaError)

    def test_send_error_aliases(self) -> None:
        for cls in (SendError.Api, SendError.Http, SendError.Io):
            assert issubclass(cls, SendError)
