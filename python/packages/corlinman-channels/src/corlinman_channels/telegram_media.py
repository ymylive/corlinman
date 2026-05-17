"""Telegram file-download orchestration.

Python port of ``rust/.../telegram/media.rs``. Telegram's attachment
API is a two-step dance:

1. ``GET /bot<token>/getFile?file_id=...`` → ``{file_path: "voice/x.ogg"}``
2. ``GET /file/bot<token>/<file_path>`` → the raw bytes.

Step 2's endpoint refuses files > 20 MB. We surface that as a typed
:class:`MediaError.TooLarge` so the caller can fall back to a polite
"this attachment is too large for the bot API" reply instead of crashing.

The HTTP surface is captured behind the :class:`TelegramHttp` Protocol
so tests can inject a fake that returns canned :class:`File` envelopes
and byte streams without touching the network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from corlinman_channels.telegram import (
    MAX_DOWNLOAD_BYTES,
    File,
)

__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "DownloadedMedia",
    "HttpxTelegramHttp",
    "MediaError",
    "TelegramHttp",
    "download_to_media_dir",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MediaError(Exception):
    """Base error for media downloads. Mirrors Rust's ``MediaError`` enum.

    Concrete subclasses cover each variant:

    - :class:`MediaApiError` (``Api(String)``)
    - :class:`MediaNoFilePathError` (``NoFilePath``)
    - :class:`MediaTooLargeError` (``TooLarge``)
    - :class:`MediaIoError` (``Io(...)``)
    - :class:`MediaHttpError` (``Http(...)``)

    Class-level attributes expose the subclasses as ``MediaError.Api``,
    ``MediaError.TooLarge``, ... mirroring the Rust enum constructor
    syntax.
    """


class MediaApiError(MediaError):
    """Telegram API rejected the request (``ok: false``)."""


class MediaNoFilePathError(MediaError):
    """``getFile`` returned no ``file_path`` (file too large)."""

    def __init__(self) -> None:
        super().__init__(
            "getFile returned no file_path (file likely too large for bot API)"
        )


class MediaTooLargeError(MediaError):
    """Download exceeded the 20 MB cap."""

    def __init__(self) -> None:
        super().__init__(f"download exceeded {MAX_DOWNLOAD_BYTES} byte cap")


class MediaIoError(MediaError):
    """Disk write failed."""


class MediaHttpError(MediaError):
    """Network / HTTP layer failed."""


# Expose enum-style attributes on the umbrella so callers can match
# ``isinstance(err, MediaError.TooLarge)`` mirroring Rust pattern syntax.
MediaError.Api = MediaApiError  # type: ignore[attr-defined]
MediaError.NoFilePath = MediaNoFilePathError  # type: ignore[attr-defined]
MediaError.TooLarge = MediaTooLargeError  # type: ignore[attr-defined]
MediaError.Io = MediaIoError  # type: ignore[attr-defined]
MediaError.Http = MediaHttpError  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TelegramHttp(Protocol):
    """Narrow HTTP surface the media helper depends on.

    Production wiring uses :class:`HttpxTelegramHttp`; tests can
    define their own structural impl returning canned :class:`File`
    envelopes and a list of byte chunks. Mirrors the Rust
    ``TelegramHttp`` trait.
    """

    async def get_file(self, file_id: str) -> File:
        """Resolve a ``file_id`` to the :class:`File` envelope."""
        ...

    def download_stream(self, file_path: str) -> AsyncIterator[bytes]:
        """Stream the file bytes.

        Returns an async iterator over ``bytes`` chunks rather than a
        single buffer so large voice notes never force a full
        allocation during the download. The Rust trait returns a
        boxed ``Stream``; we map that to ``AsyncIterator`` here.
        """
        ...


# ---------------------------------------------------------------------------
# Production impl
# ---------------------------------------------------------------------------


class HttpxTelegramHttp:
    """httpx-backed :class:`TelegramHttp` implementation.

    Mirrors Rust's ``ReqwestHttp``. Shares the :class:`httpx.AsyncClient`
    with the webhook handler; ``base`` is overridable purely to keep
    the shape ready for a sandbox instance (not used in tests — those
    pass a fake).
    """

    __slots__ = ("base", "client", "token")

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        base: str = "https://api.telegram.org",
    ) -> None:
        self.client = client
        self.token = token
        self.base = base

    async def get_file(self, file_id: str) -> File:
        url = f"{self.base}/bot{self.token}/getFile"
        try:
            resp = await self.client.get(url, params={"file_id": file_id})
        except httpx.HTTPError as exc:
            raise MediaHttpError(str(exc)) from exc
        if resp.status_code >= 400:
            raise MediaHttpError(f"getFile HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise MediaHttpError(str(exc)) from exc
        if not isinstance(body, dict):
            raise MediaApiError("getFile response was not an object")
        if not body.get("ok"):
            raise MediaApiError(body.get("description") or "")
        result = body.get("result")
        if result is None:
            raise MediaApiError("no result")
        return File.model_validate(result)

    async def download_stream(self, file_path: str) -> AsyncIterator[bytes]:
        """Stream the file. ``aiter_bytes`` from httpx is wrapped in
        an async generator that translates network errors into
        :class:`MediaHttpError`. Returning the generator (instead of
        ``async def``-yielding) lets callers ``async for`` over it
        without an extra await."""
        url = f"{self.base}/file/bot{self.token}/{file_path}"
        return self._download_iter(url)

    async def _download_iter(self, url: str) -> AsyncIterator[bytes]:
        try:
            async with self.client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise MediaHttpError(f"download HTTP {resp.status_code}")
                async for chunk in resp.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as exc:
            raise MediaHttpError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DownloadedMedia:
    """Downloaded-media metadata returned to the caller. Mirrors Rust
    ``DownloadedMedia``."""

    path: Path
    """Absolute filesystem path to the persisted file."""

    bytes_written: int
    """Total bytes written (named ``bytes_written`` to avoid clashing
    with the Python builtin ``bytes``; the Rust field is ``bytes``)."""

    file_id: str
    """The ``file_id`` that was downloaded (useful for logging)."""


# ---------------------------------------------------------------------------
# download_to_media_dir
# ---------------------------------------------------------------------------


def _sanitize(unique: str) -> str:
    """Sanitize a filename — replace path separators & control chars
    with ``_``. Mirrors Rust ``download_to_media_dir`` inline sanitizer.
    """
    return "".join(
        c if (c.isalnum() or c in ("-", "_")) else "_" for c in unique
    )


async def download_to_media_dir(
    http: TelegramHttp,
    file_id: str,
    data_dir: Path,
    fallback_ext: str,
) -> DownloadedMedia:
    """Resolve + stream a Telegram attachment to
    ``<data_dir>/media/telegram/<unique>.<ext>``.

    - ``file_id``: the Telegram handle (photo/voice/document).
    - ``data_dir``: the gateway's configured ``server.data_dir``.
    - ``fallback_ext``: extension to use when ``file_path`` has none
      (``.ogg`` for voice, ``.bin`` otherwise).

    The file name uses ``file_unique_id`` when Telegram returned one
    (idempotent across downloads) and falls back to ``file_id``. This
    means a retry of the same webhook won't duplicate the blob — the
    second write overwrites the first with identical bytes.

    Mirrors ``download_to_media_dir`` in Rust step-for-step.
    """
    file = await http.get_file(file_id)
    file_path = file.file_path
    if not file_path:
        raise MediaError.NoFilePath()

    # Derive extension from the resolved ``file_path``.
    ext = Path(file_path).suffix.lstrip(".")
    if not ext:
        ext = fallback_ext.lstrip(".")

    unique = file.file_unique_id or file_id
    safe = _sanitize(unique)

    target_dir = data_dir / "media" / "telegram"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{safe}.{ext}"

    written = 0
    try:
        stream = http.download_stream(file_path)
        # ``download_stream`` may be either a coroutine returning an
        # iterator (HttpxTelegramHttp) or a direct generator (test
        # fakes). Normalize.
        if hasattr(stream, "__await__"):
            stream = await stream  # type: ignore[assignment]
        # Open for writing inside a try/except so a partial download
        # gets cleaned up on cap-overflow.
        with target.open("wb") as f_out:
            async for chunk in stream:
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    # Best-effort cleanup — ignore the error since
                    # TooLarge is the real story.
                    try:
                        f_out.close()
                        target.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise MediaError.TooLarge()
                f_out.write(chunk)
    except OSError as exc:
        raise MediaIoError(str(exc)) from exc

    return DownloadedMedia(
        path=target,
        bytes_written=written,
        file_id=file_id,
    )
