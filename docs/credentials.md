# Credentials

The **Credentials** page at `/(admin)/credentials` is the supported
way to add or rotate provider API keys after first boot. It's a
typed UI over the `[providers.<name>]` blocks in `config.toml` — same
data plane as the [Providers reference](providers.md), different
ergonomics.

The mental model is the same one [hermes-agent's EnvPage][hermes-env]
established: one row per (provider, field), grouped by provider,
masked previews, paste-only inputs, an eye icon to reveal what you
just pasted. No `.env` file — corlinman writes directly to TOML.

---

## What the page manages

For each well-known provider, the page exposes a fixed set of editable
fields. The whitelist lives in
[`routes_admin_b/credentials.py`][routes] (`_ALLOWED_FIELDS`):

| Provider     | Fields                                  |
| ------------ | --------------------------------------- |
| `openai`     | `api_key`, `base_url`, `org_id`         |
| `anthropic`  | `api_key`, `base_url`                   |
| `openrouter` | `api_key`, `base_url`                   |
| `ollama`     | `base_url`                              |
| `custom`     | `api_key`, `base_url`, `kind`           |
| `mock`       | — (no fields; toggle `enabled` only)    |

The whitelist is intentionally small. Anything outside it gets a clean
`400 unknown_field` so the UI can surface a precise error without us
needing to round-trip through pydantic. Extending the whitelist is one
line in `_ALLOWED_FIELDS`.

For everything else — niche aggregators, OpenAI-compatible local
servers, vLLM, SiliconFlow, Groq — use the **`custom`** row or hand-edit
`config.toml` (the page coexists with manual edits).

---

## What gets written

The page writes back to `config.toml` via the same atomic-write helper
the onboard wizard uses. A fresh OpenAI configuration produces:

```toml
[providers.openai]
kind = "openai"
api_key = "sk-…"           # or { env = "OPENAI_API_KEY" } if you set it via env
base_url = "https://api.openai.com/v1"
enabled = true
```

Three semantics worth knowing:

- **First write flips `enabled = true`.** When you paste an API key
  into an empty provider row, the page also sets `enabled = true` so
  the provider is wired on the next config reload. The "primary field"
  is `api_key` for keyed providers and `base_url` for keyless ones
  (Ollama).
- **Deleting the last required field flips `enabled = false`.** The
  block itself stays as a stub for UX continuity (so the row keeps
  rendering with empty fields) — only the `enabled` flag changes.
- **The `enable` endpoint toggles without touching field data.** Useful
  for parking a provider without losing its credentials.

---

## The eye-icon reveal contract

The backend **never returns plaintext values**. The `GET` response
carries a `preview` string for each field:

| Stored value        | `preview` returned         |
| ------------------- | -------------------------- |
| 5+ character string | `"…xyz9"` (last 4 chars)   |
| 1–4 character string | `"***"`                   |
| Empty / missing     | `null`                     |
| `{ env = "FOO" }`   | `null` + `env_ref="FOO"`   |

The eye icon in the UI reveals **the preview**, not the secret. The
operator paste-only contract means the original plaintext only ever
existed on your clipboard and inside the backend's atomic write — it
never round-trips through a GET response, never appears in browser
history, never lands in the SSE log stream.

If you forget what you pasted, the only way to read it back is to
inspect `config.toml` on disk. By design.

---

## `env_ref`: when the value lives in the environment

If your `config.toml` references an env var:

```toml
[providers.openai]
api_key = { env = "OPENAI_API_KEY" }
```

…the credentials page renders the row as `set=true` with
`env_ref="OPENAI_API_KEY"` — without ever calling `os.environ.get`.

That's deliberate: the page tells you *which env var the operator
chose to read from*, but it never resolves the value. If you want to
rotate, set the env var in your shell / systemd unit / docker compose
and restart the gateway; the page will keep showing the same
`env_ref`.

To stop reading from the env and paste a literal instead, type the
new value into the field and Save. The TOML changes from
`{ env = "OPENAI_API_KEY" }` to a plain string; subsequent GETs
return a `preview` and clear `env_ref`.

---

## API reference

All routes mount behind admin auth. Base URL: `http://localhost:6005`.

| Method | Path                                          | Body / params                       | Response                  |
| ------ | --------------------------------------------- | ----------------------------------- | ------------------------- |
| GET    | `/admin/credentials`                          | —                                   | `200` `CredentialsListResponse` |
| PUT    | `/admin/credentials/{provider}/{key}`         | `{value: "..."}`                    | `204`                     |
| DELETE | `/admin/credentials/{provider}/{key}`         | —                                   | `204`                     |
| POST   | `/admin/credentials/{provider}/enable`        | `{enabled: true \| false}`          | `204`                     |

### `CredentialsListResponse` shape

```json
{
  "providers": [
    {
      "name": "openai",
      "kind": "openai",
      "enabled": true,
      "fields": [
        { "key": "api_key", "set": true, "preview": "…xyz9", "env_ref": null },
        { "key": "base_url", "set": true, "preview": null,    "env_ref": null },
        { "key": "org_id",   "set": false, "preview": null,   "env_ref": "OPENAI_ORG_ID" }
      ]
    }
  ]
}
```

### Error codes

| Code               | When                                             |
| ------------------ | ------------------------------------------------ |
| `unknown_provider` | Provider name not in the whitelist (400)         |
| `unknown_field`    | Field name not in `_ALLOWED_FIELDS` (400)        |
| `empty_value`      | PUT with empty `value` (422; use DELETE instead) |

---

## Operating recipes

### Rotate an OpenAI key

1. Generate the new key on the OpenAI dashboard.
2. Open `/(admin)/credentials`.
3. Paste the new key into the `openai.api_key` field. Save.
4. The next chat request uses the new key (config is hot-reloaded —
   no restart needed).
5. Revoke the old key on OpenAI.

### Park a provider without losing its credentials

1. Open `/(admin)/credentials`.
2. Click the **Enabled** toggle on the provider card → off.
3. The block stays in `config.toml`; `enabled = false`. Chat requests
   to that model fail-fast with `provider_disabled` instead of being
   silently routed elsewhere.

### Add a niche OpenAI-compatible aggregator (no UI yet)

The page handles `custom` for one-off endpoints. For a more
permanent setup, hand-edit `config.toml`:

```toml
[providers.siliconflow]
kind = "openai_compatible"
api_key = { env = "SILICONFLOW_API_KEY" }
base_url = "https://api.siliconflow.cn/v1"
enabled = true
```

…then `POST /admin/config` (or click **Reload** on the Config page).
The credentials page won't show a Siliconflow card (it's not in
`_ALLOWED_FIELDS`), but the provider will be live. To make it
first-class, add a row to `_ALLOWED_FIELDS`, `_DEFAULT_KIND`, and
`_DEFAULT_ENV_REF`.

---

## See also

- [Providers reference](providers.md) — full kind list + recipes
- [Quickstart](quickstart.md) — onboarding wizard vs credentials page
- [`routes_admin_b/credentials.py`][routes] — the four-endpoint surface
- [`config.example.toml`](config.example.toml) — annotated full config

[routes]: ../python/packages/corlinman-server/src/corlinman_server/gateway/routes_admin_b/credentials.py
[hermes-env]: https://github.com/yamamoto-toru/hermes-agent
