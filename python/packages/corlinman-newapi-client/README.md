# corlinman-newapi-client

Async HTTP client for the [QuantumNous / new-api](https://github.com/QuantumNous/new-api)
admin & runtime endpoints corlinman consumes.

Surface is intentionally small: probe + channel listing + 1-token
round-trip test. Mirrors the Rust crate
`rust/crates/corlinman-newapi-client`.

## Public API

- `NewapiClient(base_url, user_token, admin_token=None)` — async client.
  Methods:
  - `probe() -> ProbeResult`
  - `get_user_self() -> User`
  - `list_channels(channel_type: ChannelType) -> list[Channel]`
  - `test_round_trip(model: str) -> TestResult`
  - `aclose()` — close the owned httpx client.
- Error hierarchy: `NewapiError` (base), `HttpError`, `UrlError`,
  `UpstreamError(status, body)`, `JsonError`, `NotNewapiError`.
- Models (pydantic v2): `Channel`, `ChannelType`, `ProbeResult`,
  `TestResult`, `User`.

```python
import asyncio
from corlinman_newapi_client import NewapiClient, ChannelType

async def main() -> None:
    async with NewapiClient("https://newapi.example.com", "sk-user", "sys-admin") as c:
        probe = await c.probe()
        print(probe.user.username, probe.server_version)
        for ch in await c.list_channels(ChannelType.LLM):
            print(ch.id, ch.name, ch.models)

asyncio.run(main())
```
