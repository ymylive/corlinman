# Providers reference

corlinman talks to LLMs through a free-form, operator-configured registry.
Every entry under `[providers.<name>]` in `config.toml` becomes one
`CorlinmanProvider` instance at boot. The map key is whatever name reads
well in your logs and aliases; the `kind` field is the wire-shape
discriminator that picks which adapter actually gets built.

This doc is the working reference: the schema, the table of supported
kinds, the recipe for adding a new market provider without a Rust patch,
and a handful of common operator scenarios. The fully-annotated TOML
sample lives in [`docs/config.example.toml`](config.example.toml).

## 1. The model

```toml
[providers.<operator-chosen-name>]
kind     = "<one of 14 valid values>"
api_key  = { env = "<ENV_VAR>" }     # or { value = "..." }
base_url = "https://..."             # required for openai_compatible
enabled  = true                       # false = declared-only, no adapter built
params   = { ... }                    # provider-level default request params
```

A few rules the validator enforces (run `corlinman config validate` to
surface them up-front):

- **`kind` is required for free-form names.** The six legacy slot names
  (`anthropic`, `openai`, `google`, `deepseek`, `qwen`, `glm`) infer their
  kind so pre-refactor configs round-trip unchanged. Any other table key
  must declare `kind = "..."` explicitly. Missing `kind` raises a
  `missing_kind` error pointing at the offending entry.
- **At least one entry must be `enabled = true` AND have an `api_key`.**
  Otherwise the validator emits a `no_provider_enabled` warning and the
  gateway boots but refuses chat requests.
- **Embedding bindings reference a name, not a kind.** `[embedding].provider`
  must match a `[providers.*]` key. The referenced provider must be
  embedding-capable (see the table below).
- **`[models.aliases.<name>].provider` references a name, too.** The full
  form pins an alias to one specific entry, useful when two entries could
  serve the same model id (e.g. `gpt-4o` via OpenAI and via OpenRouter).

The schema lives in
[`rust/crates/corlinman-core/src/config.rs`](../rust/crates/corlinman-core/src/config.rs)
under `ProvidersConfig` / `ProviderEntry` / `ProviderKind`. The Python
side mirrors it in
[`corlinman_providers.specs`](../python/packages/corlinman-providers/src/corlinman_providers/specs.py).

## 2. Supported kinds

| `kind`              | Default `base_url`                                  | Auth                   | Embedding | Notes                                                                                  |
| ------------------- | --------------------------------------------------- | ---------------------- | --------- | -------------------------------------------------------------------------------------- |
| `anthropic`         | `https://api.anthropic.com`                         | `x-api-key` header     | No        | Bespoke adapter (Claude). No embedding support.                                        |
| `openai`            | `https://api.openai.com/v1`                         | Bearer token           | Yes       | Bespoke adapter. Default for the seed config.                                          |
| `google`            | `https://generativelanguage.googleapis.com/v1beta`  | API key (query string) | Yes       | Bespoke adapter for Gemini.                                                            |
| `deepseek`          | `https://api.deepseek.com/v1`                       | Bearer token           | Yes       | OpenAI-wire-compatible. Bespoke adapter for completions; embedding via shared OAI path. |
| `qwen`              | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Bearer token           | Yes       | DashScope OpenAI-compat endpoint.                                                      |
| `glm`               | `https://open.bigmodel.cn/api/paas/v4`              | Bearer token           | Yes       | Zhipu's OpenAI-compat endpoint.                                                        |
| `openai_compatible` | **none — `base_url` REQUIRED**                      | Bearer token           | Yes       | Universal escape hatch. 95% of market providers fit here.                              |
| `mistral`           | `https://api.mistral.ai/v1`                         | Bearer token           | Yes       | Routes through the shared OpenAI-compat adapter; named for clarity.                    |
| `cohere`            | `https://api.cohere.ai/compatibility/v1`            | Bearer token           | Yes       | OpenAI-compat endpoint, not Cohere's native API.                                       |
| `together`          | `https://api.together.xyz/v1`                       | Bearer token           | Yes       | Pure OpenAI-compat.                                                                    |
| `groq`              | `https://api.groq.com/openai/v1`                    | Bearer token           | No\*      | Ultra-fast inference. \*Embedding endpoint not exposed today.                          |
| `replicate`         | `https://api.replicate.com/openai/v1`               | Bearer token           | Yes       | OpenAI-compat predictions endpoint.                                                    |
| `bedrock`           | n/a                                                 | SigV4 (TODO)           | n/a       | **Declared-only stub.** Runtime raises `NotImplementedError`. See workaround below.    |
| `azure`             | n/a                                                 | API key + deployment   | n/a       | **Declared-only stub.** Runtime raises `NotImplementedError`. See workaround below.    |
| `sub2api`           | **none — `base_url` REQUIRED**                      | Bearer token           | No\*      | OpenAI-wire sidecar for subscription pooling ([Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api), LGPL-3.0). \*Embedding endpoint not advertised; declare a separate paid provider for embeddings. See `docs/design/sub2api-integration.md`. |

The seven kinds added in the free-form-providers refactor (`mistral`,
`cohere`, `together`, `groq`, `replicate`, `bedrock`, `azure`) all dispatch
through the shared `OpenAICompatibleProvider` Python adapter at runtime.
They exist as named kinds so the admin UI shows `Mistral` / `Groq` /
`Cohere` instead of an undifferentiated `OpenAI-compatible`, and so per-
kind quirks (Bedrock SigV4, Azure deployment IDs, …) can land later as
adapter overrides without a schema change.

### Bedrock and Azure today

Both kinds parse and round-trip through the validator, but build-time
adapter construction raises `NotImplementedError` so the failure is loud.
Until proper SigV4 / deployment-routing support lands, declare them as
`openai_compatible` against a compatible proxy:

```toml
[providers.bedrock]
kind = "openai_compatible"
base_url = "https://your-sigv4-proxy.example.com/v1"
api_key = { env = "BEDROCK_PROXY_KEY" }
enabled = true
```

## 3. Adding a new market provider

The vast majority of LLM vendors ship an OpenAI-wire-compatible endpoint.
For those, an operator can wire up a brand-new provider with **zero Rust
or Python code changes** — just two TOML lines:

```toml
[providers.fireworks]
kind = "openai_compatible"
api_key = { env = "FIREWORKS_API_KEY" }
base_url = "https://api.fireworks.ai/inference/v1"
enabled = true
```

Then point an alias at it:

```toml
[models.aliases.llama-fast]
model = "accounts/fireworks/models/llama-v3p1-8b-instruct"
provider = "fireworks"
```

That's it — chat traffic to model `llama-fast` now flows through the
Fireworks endpoint via the shared OpenAI-compat adapter. Restart (or hot-
reload via `POST /admin/config/reload`) and the new provider appears in
`/admin/providers` and is available to every alias.

A new kind is only worth adding to the Rust enum when one of the
following is true:

1. The vendor's wire format diverges from OpenAI in a way the shared
   adapter can't accommodate (Anthropic-style messages, Google-style
   `safety_settings`, etc.).
2. Operators benefit from a named kind in the admin UI and per-kind
   defaults in the JSON Schema (e.g. Groq's lack of an embedding endpoint
   should disable that field in the `<DynamicParamsForm>` for `kind =
   "groq"`).

## 4. Common recipes

### "I want one key and every model" — OpenRouter for LLM, OpenAI for embedding

```toml
[providers.openrouter]
kind = "openai_compatible"
api_key = { env = "OPENROUTER_API_KEY" }
base_url = "https://openrouter.ai/api/v1"
enabled = true

[providers.openai]
kind = "openai"
api_key = { env = "OPENAI_API_KEY" }
enabled = true

[embedding]
provider = "openai"
model = "text-embedding-3-small"
dimension = 1536
enabled = true

[models]
default = "openai/gpt-4o"

[models.aliases.opus]
model = "anthropic/claude-opus-4-7"
provider = "openrouter"

[models.aliases.haiku]
model = "anthropic/claude-haiku-4-5-20251001"
provider = "openrouter"
```

OpenRouter handles every chat call against any upstream model id;
OpenAI handles RAG embeddings directly so you control the embedding
dimension and don't pay OpenRouter's overhead on it.

### "I want a fully local stack" — Ollama for both

```toml
[providers.ollama]
kind = "openai_compatible"
api_key = { value = "ollama" }   # Ollama ignores the value, but the field is required
base_url = "http://localhost:11434/v1"
enabled = true

[embedding]
provider = "ollama"
model = "nomic-embed-text"
dimension = 768
enabled = true

[models]
default = "llama3.2:3b"
```

`ollama serve` running on the host supplies both completions and
embeddings; no external network calls.

### "I want a CN-resident stack" — SiliconFlow

```toml
[providers.siliconflow]
kind = "openai_compatible"
api_key = { env = "SILICONFLOW_API_KEY" }
base_url = "https://api.siliconflow.cn/v1"
enabled = true

[embedding]
provider = "siliconflow"
model = "BAAI/bge-large-zh-v1.5"
dimension = 1024
enabled = true

[models]
default = "Qwen/Qwen2.5-72B-Instruct"
```

Both completions and embeddings stay on SiliconFlow's mainland-friendly
infrastructure. Pair with `[rag.rerank]` pointing at the same `api_base`
for a fully CN-resident retrieval stack.

### "I want a fast model for one alias" — Groq alongside OpenAI

```toml
[providers.openai]
kind = "openai"
api_key = { env = "OPENAI_API_KEY" }
enabled = true

[providers.groq]
kind = "groq"
api_key = { env = "GROQ_API_KEY" }
enabled = true

[models.aliases.fast]
model = "llama-3.3-70b-versatile"
provider = "groq"
```

Default traffic still routes through OpenAI; the `fast` alias drops
through to Groq's low-latency endpoint when an agent or operator picks
it.

## 5. Inspection and admin

- `corlinman config validate` runs every check (schema + cross-field) and
  exits non-zero on hard errors. The default fresh-install config emits a
  `no_provider_enabled` warning, which is informational — `validate`
  still exits zero on warnings.
- `corlinman config show` prints the current config with secrets
  redacted (`SecretRef::Literal` becomes `***REDACTED***`; env refs keep
  the env var name for debuggability).
- `GET /admin/providers` returns every declared entry with the same
  redaction; `POST /admin/providers` upserts; `DELETE /admin/providers/:name`
  refuses with HTTP 409 if any alias references the entry — unbind first.
- The admin UI's `/providers` page renders the kind dropdown straight
  from `ProviderKind::all()`, so any kind added to the Rust enum appears
  there automatically once the gateway is rebuilt.

## See also

- [`docs/config.example.toml`](config.example.toml) — fully annotated TOML.
- [`docs/architecture.md` §7](architecture.md#7-数据与配置组织) — data
  directory layout and config path resolution.
- [`rust/crates/corlinman-core/src/config.rs`](../rust/crates/corlinman-core/src/config.rs)
  — schema source of truth.
- [`python/packages/corlinman-providers/src/corlinman_providers/`](../python/packages/corlinman-providers/src/corlinman_providers/)
  — adapter implementations.
