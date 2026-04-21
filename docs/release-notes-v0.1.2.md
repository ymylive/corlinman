# corlinman 0.1.2 — admin UI redesign

Released 2026-04-21. Pure frontend release — no Rust, Python, or
Dockerfile changes. The admin bundle is served as a Next.js static
export, so upgrading this release only requires replacing the
contents of your `ui-static` mount (no container rebuild needed when
nginx serves the static files directly).

## Highlights

- **Linear / Vercel aesthetic**: dark-first with a single indigo
  accent, Geist Sans / Mono typography, borders-over-shadows, tight
  6-8 px radii. Kept `next-themes` light/dark toggle.
- **New dashboard landing page** (`/`): four stat cards with inline
  sparklines, SSE-driven recent-activity feed, and a 7-check system
  health panel wired to `/health`.
- **Redesigned layout**: 240 ↔ 56 px collapsible sidebar with an
  animated active-indicator (framer-motion `layoutId`), topnav with
  auto breadcrumb, live health dot, theme toggle, and a "search ⌘K"
  pill.
- **Global command palette**: `⌘K` / `Ctrl+K` opens a fuzzy
  navigator over all 10 destinations, a small test-chat drawer that
  POSTs to `/v1/chat/completions`, plus theme-toggle and logout
  actions. Recent commands persist in `localStorage`.
- **Motion language**: 200 ms fade + 4 px translate page transitions,
  skeleton shimmers, `sonner` toasts, slide-up issues drawer on the
  config page. No bouncy spring animations or scale pops.
- **Refined pages**: Plugins / Agents / RAG / Channels / Scheduler /
  Approvals / Models / Config / Logs — each with the same visual
  tokens, status dots, and motion vocabulary. Logs page gained
  virtualisation + a pause-stream toggle; Scheduler gained a live
  next-trigger countdown; Models gained inline alias editing.
- **New login page**: two-column layout with a constellation backdrop
  SVG, inline error with shake micro-animation, and theme-toggle
  before authentication.

## Stability

- `ui/tests/e2e/admin-full.spec.ts` selectors audited and preserved.
- `ui/tests/**/*.test.tsx` (vitest) all green — including Chinese form
  labels on the login page.
- All API contracts unchanged — `ui/lib/api.ts` added only
  `fetchHealth()` + `HealthStatus`; nothing removed or renamed.

## New dependencies

```
framer-motion  cmdk  geist  sonner
```

## Upgrade

```bash
# if nginx serves ui-static directly (recommended):
pnpm -C ui build
rsync -az --delete ui/out/ root@<host>:/path/to/ui-static/

# if bundled into the docker runtime image:
docker buildx build --platform linux/amd64 \
  -f docker/Dockerfile -t corlinman:v0.1.2 --target runtime --load .
```

## Known gaps

- Dashboard "chat req (24h)" tile shows `—` until a dedicated metrics
  endpoint lands (brief flagged this — no heavy dep added).
- Sparklines on the stat cards are deterministic demo series seeded
  by each stat value until a real time-series endpoint is available.
