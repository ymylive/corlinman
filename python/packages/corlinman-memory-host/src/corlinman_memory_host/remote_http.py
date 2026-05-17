"""HTTP-backed :class:`MemoryHost`.

Python port of ``rust/crates/corlinman-memory-host/src/remote_http.rs``.
Speaks the same minimal JSON protocol the Rust crate documents::

    POST   {base}/query   -> {"hits": [{"id":..., "content":..., "score":..., "metadata":...}]}
    POST   {base}/upsert  -> {"id": "..."}
    DELETE {base}/docs/{id}
    GET    {base}/health  -> 2xx OK

Bearer-token auth: when ``token`` is set, sent as
``Authorization: Bearer <token>`` on every request. Uses
:class:`httpx.AsyncClient`; defaults match the Rust ``reqwest`` builder
(``timeout = 5s``)."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from corlinman_memory_host.base import MemoryHost
from corlinman_memory_host.types import (
    HealthStatus,
    MemoryDoc,
    MemoryHit,
    MemoryHostError,
    MemoryQuery,
)

# Match the Rust constant ``REQUEST_TIMEOUT = Duration::from_secs(5)``.
_DEFAULT_TIMEOUT_SECS: float = 5.0


class RemoteHttpHost(MemoryHost):
    """HTTP-backed memory host.

    Cheap to construct; reuse one instance per endpoint to amortise the
    connection pool. Either close explicitly with :meth:`aclose` or use
    as an async context manager (matches the ``corlinman-newapi-client``
    convention).
    """

    def __init__(
        self,
        host_name: str,
        base_url: str,
        token: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECS,
    ) -> None:
        # The Rust constructor calls ``trim_end_matches('/')`` so we
        # don't end up with ``//query``. ``urljoin`` in Python is also
        # fine but the trim keeps the wire URLs identical to the Rust
        # client's, which simplifies mock assertions.
        self._name = host_name
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._owned_client = client is None
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=timeout)

    # ---- lifecycle --------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        """Close the owned httpx client. Idempotent."""
        if self._owned_client and self._client is not None:
            await self._client.aclose()
            self._owned_client = False

    async def __aenter__(self) -> RemoteHttpHost:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ---- internal helpers -------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # ---- MemoryHost surface -----------------------------------------------

    def name(self) -> str:
        return self._name

    async def query(self, req: MemoryQuery) -> list[MemoryHit]:
        try:
            resp = await self._client.post(
                self._url("/query"),
                headers=self._headers(),
                json=req.to_json(),
            )
        except httpx.HTTPError as exc:
            raise MemoryHostError(
                f"POST {self._base_url}/query: {exc}"
            ) from exc

        if not resp.is_success:
            body = self._safe_text(resp)
            raise MemoryHostError(
                f"RemoteHttpHost {self._name}: query HTTP {resp.status_code}: {body}"
            )

        parsed = self._safe_json(resp, context=f"parse query response from {self._name}")
        hits_raw = parsed.get("hits") if isinstance(parsed, dict) else None
        if not isinstance(hits_raw, list):
            raise MemoryHostError(
                f"parse query response from {self._name}: missing 'hits' array"
            )
        out: list[MemoryHit] = []
        for h in hits_raw:
            if not isinstance(h, dict):
                raise MemoryHostError(
                    f"parse query response from {self._name}: hit is not an object"
                )
            try:
                out.append(
                    MemoryHit(
                        id=str(h["id"]),
                        content=str(h["content"]),
                        score=float(h["score"]),
                        source=self._name,
                        metadata=h.get("metadata"),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MemoryHostError(
                    f"parse query response from {self._name}: {exc}"
                ) from exc
        return out

    async def upsert(self, doc: MemoryDoc) -> str:
        try:
            resp = await self._client.post(
                self._url("/upsert"),
                headers=self._headers(),
                json=doc.to_json(),
            )
        except httpx.HTTPError as exc:
            raise MemoryHostError(
                f"POST {self._base_url}/upsert: {exc}"
            ) from exc

        if not resp.is_success:
            body = self._safe_text(resp)
            raise MemoryHostError(
                f"RemoteHttpHost {self._name}: upsert HTTP {resp.status_code}: {body}"
            )

        parsed = self._safe_json(resp, context=f"parse upsert response from {self._name}")
        if not isinstance(parsed, dict) or "id" not in parsed:
            raise MemoryHostError(
                f"parse upsert response from {self._name}: missing 'id'"
            )
        return str(parsed["id"])

    async def delete(self, doc_id: str) -> None:
        path = f"/docs/{doc_id}"
        try:
            resp = await self._client.delete(
                self._url(path),
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise MemoryHostError(
                f"DELETE {self._base_url}{path}: {exc}"
            ) from exc
        if not resp.is_success:
            body = self._safe_text(resp)
            raise MemoryHostError(
                f"RemoteHttpHost {self._name}: delete HTTP {resp.status_code}: {body}"
            )

    async def health(self) -> HealthStatus:
        try:
            resp = await self._client.get(
                self._url("/health"),
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            return HealthStatus.down(str(exc))
        if resp.is_success:
            return HealthStatus.ok()
        return HealthStatus.degraded(f"HTTP {resp.status_code}")

    # ---- response decoding -------------------------------------------------

    @staticmethod
    def _safe_text(resp: httpx.Response) -> str:
        # Match the Rust client's ``.text().await.unwrap_or_default()`` —
        # never let a body-decode failure mask the real upstream status.
        try:
            return resp.text
        except Exception:
            return ""

    @staticmethod
    def _safe_json(resp: httpx.Response, *, context: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise MemoryHostError(f"{context}: {exc}") from exc


__all__ = ["RemoteHttpHost"]
