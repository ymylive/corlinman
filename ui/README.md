# corlinman UI

Next.js admin console for the corlinman gateway.

## Dev

```bash
pnpm install
pnpm dev
```

## API source: mock vs real gateway

The admin pages talk to an API through `lib/api.ts`. Three switches control
where calls land:

| env var | effect |
| --- | --- |
| `NEXT_PUBLIC_GATEWAY_URL` | Real gateway base URL. Default: empty string (use current origin so nginx proxies `/admin/*` through). Set to `http://localhost:6005` for local dev without a proxy. |
| `NEXT_PUBLIC_MOCK_API_URL` | If set, *all* calls go here instead of the gateway (the standalone mock server in `ui/mock/server.ts`). |
| `NEXT_PUBLIC_MOCK_MODE` | `"1"` enables per-call inline mock payloads (offline dev with no mock server and no gateway). Anything else disables them. |

### Run against the real gateway (M6 default)

```bash
# 1. start the gateway (it reads ~/.corlinman/config.toml)
cargo run -p corlinman-gateway

# 2. run the UI against it
NEXT_PUBLIC_MOCK_API_URL= NEXT_PUBLIC_MOCK_MODE= pnpm dev
```

Admin routes (`/admin/*`) require HTTP Basic against
`config.admin.username` + `config.admin.password_hash` (argon2id).
For browser testing, visit `http://localhost:6005/admin/plugins` directly — the
browser prompts for credentials and then the UI at `http://localhost:3000`
picks up the stored creds via `credentials: "include"`.

### Run fully offline (no gateway, inline mocks)

```bash
NEXT_PUBLIC_MOCK_MODE=1 pnpm dev
```

### Run against the standalone mock server

```bash
pnpm mock &    # starts ui/mock/server.ts on :7777
NEXT_PUBLIC_MOCK_API_URL=http://127.0.0.1:7777 pnpm dev
```

## Tests

```bash
pnpm typecheck
pnpm lint
pnpm test        # vitest
pnpm build
```
