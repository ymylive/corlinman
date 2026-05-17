"""HTTP client for the new-api admin & runtime endpoints corlinman
actually consumes. Surface is intentionally small: probe + channel
listing + 1-token round-trip test.

Python port of ``rust/crates/corlinman-newapi-client/src/client.rs``.
Uses ``httpx.AsyncClient`` with an 8s timeout to match the Rust default.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import ValidationError

from corlinman_newapi_client.types import (
    Channel,
    ChannelType,
    ProbeResult,
    TestResult,
    User,
)

# Match the Rust client's `Client::builder().timeout(Duration::from_secs(8))`.
_DEFAULT_TIMEOUT_SECS: float = 8.0


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class NewapiError(Exception):
    """Base class for every error raised by :class:`NewapiClient`."""


class HttpError(NewapiError):
    """A transport-level failure (DNS, connect, read timeout, ...).

    Wraps the underlying :class:`httpx.HTTPError`.
    """

    def __init__(self, source: httpx.HTTPError) -> None:
        super().__init__(f"http request failed: {source}")
        self.source = source


class UrlError(NewapiError):
    """The supplied ``base_url`` is not a parseable absolute URL."""

    def __init__(self, base_url: str, reason: str) -> None:
        super().__init__(f"invalid base url: {base_url!r}: {reason}")
        self.base_url = base_url
        self.reason = reason


class UpstreamError(NewapiError):
    """Non-success HTTP response from new-api. Carries status + raw body."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"upstream returned status {status}: {body}")
        self.status = status
        self.body = body


class JsonError(NewapiError):
    """Upstream returned malformed or unexpected JSON."""

    def __init__(self, source: Exception) -> None:
        super().__init__(f"upstream returned malformed json: {source}")
        self.source = source


class NotNewapiError(NewapiError):
    """``/api/status`` is missing or has the wrong shape — not a new-api host."""

    def __init__(self) -> None:
        super().__init__(
            "upstream is not new-api (missing /api/status or wrong shape)"
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _parse_base_url(base_url: str) -> str:
    """Validate that ``base_url`` is an absolute URL with a scheme + host.

    Returns the URL unchanged on success; raises :class:`UrlError` otherwise.
    Mirrors the upfront ``url::Url::parse`` check the Rust client does in
    its constructor.
    """
    if not isinstance(base_url, str) or not base_url:
        raise UrlError(str(base_url), "empty")
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise UrlError(base_url, "must be an absolute http(s) URL")
    return base_url


def _join(base: str, path: str) -> str:
    """``urljoin``-style join that mimics ``url::Url::join``.

    ``url::Url::join("http://h/x/", "/api/y")`` -> ``http://h/api/y``;
    Python's :func:`urllib.parse.urljoin` already matches that behaviour
    for absolute paths, which is the only shape we use here.
    """
    return urljoin(base, path)


class NewapiClient:
    """Async HTTP client for one logical new-api endpoint.

    Owns ``base_url`` + tokens + an ``httpx.AsyncClient``. Cheap to
    construct; reuse one instance per endpoint to amortise connection
    pooling. Either close explicitly with :meth:`aclose` or use as an
    async context manager.
    """

    def __init__(
        self,
        base_url: str,
        user_token: str,
        admin_token: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECS,
    ) -> None:
        self._base_url = _parse_base_url(base_url)
        self._user_token = user_token
        self._admin_token = admin_token
        self._timeout = timeout
        self._owned_client = client is None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=timeout)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        """Close the owned httpx client. Idempotent."""
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            # Guard against double-close.
            self._owned_client = False

    async def __aenter__(self) -> NewapiClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _admin_or_user_token(self) -> str:
        return self._admin_token if self._admin_token is not None else self._user_token

    async def _get(
        self,
        path: str,
        *,
        token: str | None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return await self._client.get(
                _join(self._base_url, path), headers=headers, params=params
            )
        except httpx.HTTPError as exc:
            raise HttpError(exc) from exc

    async def _post_json(
        self,
        path: str,
        *,
        token: str | None,
        json: dict[str, Any],
    ) -> httpx.Response:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return await self._client.post(
                _join(self._base_url, path), headers=headers, json=json
            )
        except httpx.HTTPError as exc:
            raise HttpError(exc) from exc

    @staticmethod
    def _envelope_data(resp: httpx.Response) -> Any:
        """Decode a ``{"success": bool, "data": ...}`` new-api envelope."""
        try:
            body = resp.json()
        except ValueError as exc:  # JSONDecodeError subclass of ValueError
            raise JsonError(exc) from exc
        if not isinstance(body, dict) or "data" not in body:
            raise JsonError(ValueError("missing `data` in envelope"))
        return body["data"]

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        # Match the Rust client's `.text().await.unwrap_or_default()`:
        # never let body-decode failure mask the real upstream status.
        try:
            body = resp.text
        except Exception:
            body = ""
        raise UpstreamError(status=resp.status_code, body=body)

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def probe(self) -> ProbeResult:
        """Validate that ``base_url`` + token point at a real new-api host.

        Runs two calls: ``/api/user/self`` (validates the token + resolves
        the user) and ``/api/status`` (signature endpoint — its presence
        is what distinguishes new-api from a bare OpenAI gateway). Used
        by both onboard step 2 and the ``/admin/newapi`` PATCH revalidation
        hook.
        """
        user = await self.get_user_self()

        resp = await self._get("/api/status", token=None)
        if not resp.is_success:
            raise NotNewapiError()
        data = self._envelope_data(resp)
        if not isinstance(data, dict):
            raise NotNewapiError()
        version = data.get("version")
        return ProbeResult(
            base_url=self._base_url,
            user=user,
            server_version=version if isinstance(version, str) else None,
        )

    async def get_user_self(self) -> User:
        """Return the user record bound to the configured admin/user token.

        Prefers the admin token when present (new-api's "system access
        token", which also authorises ``/api/channel/``); falls back to
        the user token otherwise.
        """
        resp = await self._get("/api/user/self", token=self._admin_or_user_token())
        self._raise_for_status(resp)
        data = self._envelope_data(resp)
        try:
            return User.model_validate(data)
        except ValidationError as exc:
            raise JsonError(exc) from exc

    async def list_channels(self, channel_type: ChannelType) -> list[Channel]:
        """List channels of the given type.

        Filters on the server side via the integer type code; we project
        the typed enum to the wire-int here.
        """
        resp = await self._get(
            "/api/channel/",
            token=self._admin_or_user_token(),
            params={"type": str(channel_type.as_int())},
        )
        self._raise_for_status(resp)
        data = self._envelope_data(resp)
        if not isinstance(data, list):
            raise JsonError(ValueError("expected list in `data`"))
        try:
            return [Channel.model_validate(row) for row in data]
        except ValidationError as exc:
            raise JsonError(exc) from exc

    async def test_round_trip(self, model: str) -> TestResult:
        """1-token chat round-trip used by ``/admin/newapi/test``.

        Measures wall-clock latency from request start to response. Always
        uses the user token (distinct from probe, which validates the
        admin path).
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
        }
        started = time.perf_counter()
        resp = await self._post_json(
            "/v1/chat/completions", token=self._user_token, json=payload
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._raise_for_status(resp)
        # Some upstreams return non-JSON bodies on 200 (e.g. raw text).
        # Match the Rust client's `unwrap_or` fallback rather than failing.
        parsed_model: str | None
        try:
            body = resp.json()
            raw_model = body.get("model") if isinstance(body, dict) else None
            parsed_model = raw_model if isinstance(raw_model, str) else model
        except ValueError:
            parsed_model = model
        return TestResult(
            status=resp.status_code,
            latency_ms=latency_ms,
            model=parsed_model,
        )


__all__ = [
    "HttpError",
    "JsonError",
    "NewapiClient",
    "NewapiError",
    "NotNewapiError",
    "UpstreamError",
    "UrlError",
]
