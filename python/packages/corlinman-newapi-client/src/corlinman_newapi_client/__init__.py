"""corlinman-newapi-client — async HTTP client for QuantumNous/new-api.

Read-only operations: channel discovery, user/self introspection,
connection probe, 1-token round-trip test. corlinman uses this to power
the ``/admin/newapi`` page and the onboard wizard.

Python port of the Rust crate
``rust/crates/corlinman-newapi-client``; same endpoint coverage, same
error taxonomy.
"""

from __future__ import annotations

from corlinman_newapi_client.client import (
    HttpError,
    JsonError,
    NewapiClient,
    NewapiError,
    NotNewapiError,
    UpstreamError,
    UrlError,
)
from corlinman_newapi_client.types import (
    Channel,
    ChannelType,
    ProbeResult,
    TestResult,
    User,
)

__all__: list[str] = [
    "Channel",
    "ChannelType",
    "HttpError",
    "JsonError",
    "NewapiClient",
    "NewapiError",
    "NotNewapiError",
    "ProbeResult",
    "TestResult",
    "UpstreamError",
    "UrlError",
    "User",
]
