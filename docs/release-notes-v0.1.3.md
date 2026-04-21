# corlinman 0.1.3 — i18n + static-bundle API fix

Released 2026-04-21. Pure frontend release — no Rust, Python, or
Dockerfile changes. Upgrade by `pnpm -C ui build` + rsync of `ui/out/`
to the nginx document root.

## Added

- **Full zh-CN / en internationalisation** across all 10 admin pages,
  the login page, layout shell (sidebar, topnav, breadcrumbs, user
  chip), dashboard, and `⌘K` command palette. Backed by `react-i18next`
  with two TypeScript locale bundles whose shapes are statically
  enforced (378 keys each, compile-time parity via `satisfies
  LocaleBundle<DeepString<typeof zhCN>>`).
- **Language toggle** in the topnav (indicator pill showing `中` / `EN`)
  plus a "Switch language" entry in the command palette. Choice
  persists in `localStorage.corlinman_lang`.
- **Boot-time language detection**: inline `<head>` script reads the
  persisted choice (or `navigator.language` on first visit — `zh` →
  Chinese, everything else → English) and sets `<html lang>` before
  hydration to avoid FOUC.
- **SSG-safe i18n init**: the bundle pre-renders under `output:
  "export"` with `zh-CN` as the default locale; language detection
  branches on `typeof window`, so server-side generation skips the
  detector and stays synchronous.

## Fixed

- **`GATEWAY_BASE_URL` default** in `ui/lib/api.ts`: changed from
  `"http://localhost:6005"` to `""`. The static export bakes the
  default at build time, so the old value made the visitor's browser
  try to reach `localhost:6005/admin/me` — which always refused the
  connection once the bundle was hosted on a real origin. Empty string
  makes every admin / health / chat / metrics call resolve relative to
  the current origin, which nginx already reverse-proxies to the
  gateway. `NEXT_PUBLIC_GATEWAY_URL` remains the opt-in override for
  local dev. Mock-server mode untouched.

## Stability

- Playwright E2E (`admin-full.spec.ts`) selectors audited — both
  `保存成功` (zh-CN `agents.saveSuccess`) and `new version:` (zh-CN
  `config.newVersion`) render unchanged when zh-CN is active, which is
  the default.
- Vitest login tests still green — `vitest.setup.ts` pre-seeds the
  zh-CN locale so `用户名` / `密码` / `登录` assertions pass.
- No API contracts changed.

## Notes

- The pre-existing scaffolding under `ui/lib/i18n/` (JSON bundles + a
  hand-rolled context) has been replaced wholesale by `ui/lib/i18n.ts`
  + `ui/lib/locales/{zh-CN,en}.ts`. Only `providers.tsx` consumed the
  old module.
- i18next v26 renamed a couple of init flags — the runtime now
  disables `initAsync` (v26 name for the inverted `initImmediate`)
  and drops `nonExplicitSupportedLngs` + `load: "currentOnly"`, which
  together broke key resolution.

## Dependencies

```
+ i18next
+ react-i18next
+ i18next-browser-languagedetector
```

## Upgrade

```bash
pnpm -C ui install       # pick up the three new deps
pnpm -C ui build         # re-emit the static bundle
rsync -az --delete ui/out/ root@<host>:/path/to/ui-static/
```

No container restart required.
