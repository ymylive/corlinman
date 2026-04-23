# Tidepool · Migration plan

Cascading F (Tidepool, warm-orange glass) from `_design/*.html` prototypes into
the real Next.js codebase at `ui/`.

**Principle**: ship in thin, reversible slices. Each phase leaves `main` in a
shippable state. No big-bang rewrite. Phase 0 and 1 are non-destructive; phases
2+ can be opt-in behind a feature flag if needed.

**Reference prototypes**:
- `_design/direction-f-tidepool.html` — Dashboard + ⌘K palette
- `_design/direction-f-logs.html` — Logs (info-density stress test)

**Reviewers verify each phase on**: `http://localhost:3000/` (dashboard),
`/logs`, `/approvals`, `/plugins` — light + dark toggled via the new control.

---

## Phase 0 — Tokens, fonts, motion primitives (non-visual)

Lays the foundation. Nothing renders differently yet.

### 0.1 Replace colour tokens — `ui/app/globals.css`

Current palette is indigo-on-neutral Linear clone. Replace with the Tidepool
warm-orange glass palette. Keep variable **names** the same where possible so
downstream consumers don't break; rename only where semantics shifted.

| Token | Current (indigo) | Target (warm amber) — Day | Target — Night |
|---|---|---|---|
| `--background` | `0 0% 100%` / `240 10% 4%` | `#fbf4e8` / `#1a120d` gradient base | same |
| `--primary` | `244 75% 57%` | `oklch(0.56 0.19 50)` | `oklch(0.80 0.17 58)` |
| `--accent` | `244 75% 96%` | amber-soft (10-14%) | amber-soft (14%) |
| `--accent-2` | teal `174 60% 50%` | **remove** — single accent discipline | — |
| `--accent-3` | amber `38 92% 55%` | **remove** — folded into primary | — |
| `--panel` | `240 10% 6%` | `rgba(255,255,255,0.58)` + backdrop-blur | `oklch(0.22 0.02 55 / 0.5)` + blur |

Add these new ones:

| Token | Day | Night |
|---|---|---|
| `--aurora-1 / --aurora-2 / --aurora-3` | soft warm washes | amber/orange/rose glows |
| `--glass` / `--glass-2` / `--glass-3` | white-translucent (0.58 / 0.78 / 0.9) | dark warm-translucent (0.5 / 0.65 / 0.72) |
| `--glass-edge` / `--glass-hl` | white highlights | white highlights (same) |
| `--glass-inner` etc. | row-fill states | row-fill states |
| `--amber` / `--amber-soft` / `--amber-glow` | primary accent family | same, brighter |
| `--ember` | secondary warm red-orange | same |
| `--peach` | tertiary | same |
| `--grad-text` | ember→amber gradient | amber→ember gradient |
| `--shadow-panel` / `--shadow-hero` / `--shadow-primary` | soft warm drop shadows | deep warm drop shadows |

**Action**: hand-port `:root` / `[data-theme="dark"]` blocks from
`direction-f-tidepool.html` into `ui/app/globals.css`. Delete the `--accent-2 /
--accent-3 / --state-*` blocks — not used in Tidepool.

### 0.2 Tailwind config — `ui/tailwind.config.ts`

Drop the teal/amber compound accents. Add `amber / ember / peach / glass / aurora` as semantic colors. Remove `boxShadow: glow-primary` (unused); add `shadow-primary` using the new `--shadow-primary` var. Add `backdropBlur: {'glass': '24px'}`.

### 0.3 Fonts — `ui/app/layout.tsx`

Current: Geist Sans + Geist Mono (Vercel defaults). Target adds one face:

- Keep: Geist Sans (body) — rename CSS var to `--font-sans`
- **Add**: Instrument Serif (display — hero big-number / streak value / occasional italic emphasis). Load via `next/font/google` with `subsets: ["latin"]`, `variable: "--font-serif"`, display: "swap"
- Keep: Geist Mono — rename CSS var to `--font-mono`
- **Optional add**: JetBrains Mono as alternative mono for data-dense places (Logs, Tag Memo). Drop Geist Mono once settled; halves the font cost.

Update `tailwind.config.ts` `fontFamily`:
- `sans: [var(--font-sans), ...]`
- `serif: [var(--font-serif), 'Instrument Serif', Georgia, serif]`
- `mono: [var(--font-mono), ...]`

### 0.4 Motion primitives — `ui/lib/motion.ts`

Already exists. Add these variants to the existing library (don't replace):

- `breathe` — 2.2s ease-in-out infinite box-shadow pulse for live dots
- `drawIn` — 1.8s cubic-bezier(0.16,1,0.3,1) for underline draws
- `tickUp` — 800ms cubic-bezier(0.16,1,0.3,1) for stat values on mount
- `justNowFade` — 2.8s fade-out for new log rows
- `paletteIn` — 260ms spring for ⌘K modal entrance

Keep the existing `fadeUp / stagger / springPop / listItem` — they still work.

### 0.5 Remove legacy utilities

- `.dashboard-hero-glow` in `globals.css` — deprecated, will be replaced by the hero's own `::before/::after` aurora layers
- `.shimmer` — keep but re-tune background stops to match new palette

### Phase 0 acceptance

- `pnpm typecheck && pnpm lint && pnpm build` all clean
- Run the existing app — should still look Linear-indigo because we haven't
  touched any JSX yet. All we changed is tokens + fonts + added motion vars.
- No runtime errors. No component renders differently.

Estimated effort: **4–6h**.

---

## Phase 1 — New primitive components

Build the reusable glass / palette / streak / filter components. Land them in
`ui/components/ui/` as composable building blocks. Don't use them yet.

### 1.1 Components to create

| Component | File | Responsibility |
|---|---|---|
| `<GlassPanel>` | `ui/components/ui/glass-panel.tsx` | Standardised glass surface — prop for `variant: 'soft' \| 'strong' \| 'primary'`. Uses `--glass / --glass-2 / --glass-3` + inset highlight + shadow. |
| `<AuroraBackground>` | `ui/components/ui/aurora-bg.tsx` | The three fixed radial gradient layers. Currently pinned to body in CSS; extract for reuse on auth pages etc. |
| `<ThemeToggle>` | `ui/components/ui/theme-toggle.tsx` | Sun/moon pill. Reads/writes `document.documentElement.dataset.theme` + localStorage. |
| `<StatChip>` | `ui/components/ui/stat-chip.tsx` | Label + value + sparkline + delta + primary variant with amber outline. Receives sparkline data + optional `live` flag. |
| `<CommandPalette>` | `ui/components/ui/command-palette.tsx` | Modal with cmdk under the hood — cmdk already in `package.json`. Wire to `⌘K` keybinding globally. |
| `<FilterChipGroup>` | `ui/components/ui/filter-chips.tsx` | The activity filter row (`All 12 / ok 3 / warn 1 / err 1`) and the log-page equivalents. |
| `<StreamPill>` | `ui/components/ui/stream-pill.tsx` | Live / paused / throttled status pill with breathing dot and rate readout. |
| `<LogRow>` | `ui/components/logs/log-row.tsx` | The grid row used in both Dashboard activity and Logs page. |
| `<DetailDrawer>` | `ui/components/ui/detail-drawer.tsx` | Right-side sticky drawer shell used by Logs / Approvals / Nodes. |
| `<JsonView>` | `ui/components/ui/json-view.tsx` | Syntax-highlighted JSON block (k/s/n/b/c spans per prototype). |
| `<UptimeStreak>` | `ui/components/admin/uptime-streak.tsx` | 90-day availability card + 30-bar histogram. |
| `<MiniSparkline>` | `ui/components/ui/mini-sparkline.tsx` | 6-bar service availability viz used in System Health rows. |

### 1.2 Components to keep (but retune styles)

- `<AnimatedNumber>` — works fine; just ensure it respects `var(--font-sans)` and `tabular-nums`
- `<LiveDot>` — update breathe animation to match new token
- `<TiltCard>` — the tilt effect is nice; may want to limit to certain surfaces once we see it with glass (could be too much)
- `<HealthRing>` — retained but reskinned; will coexist with the new `<UptimeStreak>` initially
- `<EmptyState>` — keep, restyle
- `<CountdownRing>` — keep, restyle
- `<MotionSafe>` / `useMotion` — no change

### 1.3 Components to deprecate

- `.dashboard-hero-glow` CSS utility (replaced by `<GlassPanel variant="hero">` with its own aurora layers)
- `<TiltCard>` on high-density pages (keep on Dashboard stat chips only, remove from plugins grid)

### Phase 1 acceptance

- Each new component has a Storybook-equivalent fixture in `ui/tests/` or a
  simple render-test. Axe audit passes for each component.
- `pnpm test` green.
- Still nothing user-visible has changed — components exist but aren't mounted.

Estimated effort: **2–3 days**.

---

## Phase 2 — Shell: layout, topbar, sidebar, theme toggle

First user-visible cutover. From here, light/dark toggle works, the aurora
background appears globally, sidebar adopts glass treatment.

### 2.1 Update `ui/app/(admin)/layout.tsx`

- Replace bare `<div className="flex min-h-dvh">` with a variant that establishes the aurora background either via CSS on `body` (simpler) or `<AuroraBackground>`
- Keep the auth-guard logic untouched
- Pass `theme` from localStorage on first paint (avoid flash) — use an inline
  `<Script strategy="beforeInteractive">` that reads localStorage and sets
  `documentElement.dataset.theme` before React hydrates

### 2.2 Rebuild `ui/components/layout/sidebar.tsx`

Target matches `direction-f-tidepool.html`'s `<aside>`:
- Glass panel with margin 16px (floats over the aurora)
- Brand mark: gradient amber→ember pill (30px, 9px radius)
- Search pill with `⌘K` kbd (opens `<CommandPalette>`)
- Nav groups with small uppercase labels
- Active nav: glass-inner-hover bg + 3px amber→ember left accent bar with glow
- Badge variant for Approvals (`<Badge variant="attention">2</Badge>` w/ pulse)
- Status-dot variant for Hooks (live)

### 2.3 Rebuild `ui/components/layout/nav.tsx` (top bar)

- Glass panel, 14-22px padding, matches sidebar treatment
- Breadcrumb on left (supports `A / B` form like `Dashboard / Logs`)
- Meta pill slot (optional — "All systems nominal")
- `<ThemeToggle>` in the centre-right
- Avatar last (gradient amber→ember, opens user menu on click — menu itself is a Phase 4 follow-up)

### 2.4 Global keyboard

- `⌘K` opens `<CommandPalette>` — goes through an app-level provider so any
  page can open it
- `Esc` closes the palette
- `G <letter>` navigate shortcuts (already scoped in prototype — implement with
  a small router helper)

### Phase 2 acceptance

- Visit every admin page — shell matches Tidepool. Content inside is still
  old Linear-indigo and will look jarring (expected — phase 3 fixes).
- Theme toggle works without flash on reload.
- ⌘K opens a (temporarily empty) palette.
- Lighthouse a11y ≥ 95 (maintained from v0.3.0 baseline).
- Existing Cypress / vitest tests all pass.

Estimated effort: **1.5 days**.

---

## Phase 3 — Dashboard cutover

The hero page. Replace content-area of `ui/app/(admin)/page.tsx` to match
`direction-f-tidepool.html`.

### 3.1 Hero section

- `<GlassPanel variant="hero">` wrapping:
  - pulse-lead pill ("v0.3.0 · batch 1–5 shipped 2 days ago")
  - `<h1>` greeting with inline `.running` gradient span
  - summary `<p>` with inline `.inline-metric` chips for live numbers (requests, tokens) and one `.inline-metric.warn` chip for pending approvals — values wired to existing queries (plugins, agents, rag, approvals count)
  - `<Button variant="primary">` for ⌘K + `<Button variant="highlight">` for approvals (highlight only when count > 0)
- Right column: `<UptimeStreak>` card fed by a new `/admin/uptime` endpoint (or a computed value from existing `/admin/health`; Phase 5 can upgrade to real 90-day data)

### 3.2 Stats row

- Four `<StatChip>`. First one (`Requests · 24h`) gets `variant="primary"` and `live` flag.
- Sparkline data: initially fake (same deterministic hash from current code); real `/admin/metrics/series?name=requests&window=24h` endpoint is Phase 5 scope.
- `<AnimatedNumber>` keeps the tick-up on value changes.

### 3.3 Activity + Health

- Activity pane: `<GlassPanel>` + `<FilterChipGroup>` + `<LogRow>` list driven by the existing SSE `/admin/logs/stream`. First row after mount gets the `justNow` 2.8s highlight.
- Health pane: `<UptimeStreak>` at top + per-service rows with `<MiniSparkline>`. Drops the current `<HealthRing>` in favour of richer representation (keep `<HealthRing>` the component for other future uses).

### 3.4 Remove

- `dashboard-hero-glow` utility class usage
- Inline parallax mousemove on the hero — the glass + aurora do the atmospheric work now; extra parallax is over-motion
- The old `<StatCard>` / `<Sparkline>` local components in `page.tsx`

### Phase 3 acceptance

- Dashboard looks identical to `direction-f-tidepool.html` at 1440w (allowing for real data values).
- Theme toggle flips everything cleanly (no hardcoded colours left in the dashboard).
- All existing tests still green; add a visual-regression snapshot.
- `prefers-reduced-motion` disables: aurora animations (keep static), tick-up, just-now highlight, palette spring. Leaves functional content intact.

Estimated effort: **1.5–2 days**.

---

## Phase 4 — Logs page + detail drawer

Proves the system holds on the info-density stress test.

### 4.1 Control bar

New component `<LogsControlBar>` housing:
- `<StreamPill>` with pause/resume
- `<TimeRangeToggle>` pill group (15m / 1h / 24h / 7d / custom)
- `<SelectPill>` × 2 (severity, subsystem) — open Radix popover multi-select
- `<SearchInput>` with `⌘F` kbd and text / regex modes
- Icon buttons: export (.csv/.json), settings

### 4.2 Stats strip

Small row of counts — `1,842 events · 1,412 ok · 378 info · 38 warn · 14 err ·
across 9 subsystems · 3 unique trace_ids`. Generated from the current result
set; updates on filter change.

### 4.3 Log list

`<LogList>` with virtualisation (use `@tanstack/react-virtual` — already
transitively available via react-query or add). Each row renders through the
shared `<LogRow>` component. Day-divider rows (`Today · 14:02`) inserted by a
client-side grouping helper.

### 4.4 Detail drawer

`<DetailDrawer>` on the right (380px), sticky under the control bar:
- Header: severity pill, big timestamp, relative "2m ago"
- Subsystem name (amber)
- `<h2>` with the message + inline `<code>`
- trace_id row + copy button
- Payload section with `<JsonView>` (syntax-highlighted)
- Related-by-trace list (re-uses `<LogRow>` compact variant)
- "Likely cause" section if the error has a known pattern (Phase 5: plumb LLM-authored explanations)

### Phase 4 acceptance

- Open `/logs` — matches `direction-f-logs.html` at 1440w.
- 10k rows render without jank (virtual scroll works).
- Click any row → drawer populates. Copy trace_id works. ESC closes drawer.
- Filter changes update both list and stats strip in < 100ms.

Estimated effort: **2–3 days**.

---

## Phase 5 — Cascade to remaining pages

Same visual language, applied page by page. Each page is an independent PR;
order by visibility / value.

1. **Approvals** (`/approvals`) — highest-attention page; the Dashboard already
   pulses the sidebar badge. Drawer pattern from Logs reused for each pending
   approval.
2. **Plugins** (`/plugins`, `/plugins/detail`) — convert cards to `<GlassPanel>`
   with hover-lift only (drop tilt).
3. **Hooks** (`/hooks`) — real-time event list → reuse `<LogRow>` in an active-
   feed variant; add a "category chips" row at top.
4. **Scheduler** (`/scheduler`) — table with row actions, `<GlassPanel>` wrapper, add `<CountdownRing>` inline per cron.
5. **Skills** (`/skills`) — gallery of `<GlassPanel variant="soft">` cards; skill icon + metadata.
6. **Characters** (`/characters`) — card stack with the flip-card already built; just retoken borders/shadows to glass.
7. **Channels** (`/channels/qq`, `/channels/telegram`) — chat panel, retoken.
8. **Playground** (`/playground/protocol`) — split pane; keep current structure, retoken.
9. **Nodes** (`/nodes`) — radial topology; retoken stroke colors to amber/ember family.
10. **Tag Memo** (`/tagmemo`) — viz accents shift from `--primary` (indigo) to `--amber` (warm orange).
11. **Diary** (`/diary`) — timeline; retoken.
12. **Canvas** (`/canvas`) — viewer stub; retoken.
13. **Config** (`/config`) — form pages; retoken.
14. **Login** (`/login`) — retoken, add `<AuroraBackground>`.

Per-page effort: 0.5–1.5 days depending on existing complexity. **Total: ~9–14 days.**

---

## Phase 6 — Polish & system-wide validation

- Accessibility pass: every focus state, every contrast check (WCAG AA).
  Tidepool's glass panels have lower contrast than solid backgrounds; audit
  each text-on-glass combination with the axe-core test matrix already in place.
- Reduced-motion pass: confirm every animation respects the media query.
- Visual regression: add Playwright snapshots per page, both themes, at 1440 / 1024 / 768 viewports.
- Performance: measure LCP / TBT before and after; backdrop-filter is not free.
  If Phase 5 degrades LCP > 15%, disable backdrop-filter on non-hero surfaces
  and fall back to solid with alpha.
- Documentation: `ui/README.md` gets a "Tidepool design system" section
  describing tokens, primitives, and usage.

Estimated effort: **2 days**.

---

## Order of operations (shipping order)

```
Phase 0  Tokens / fonts / motion primitives        (non-visual)     4-6h
Phase 1  Primitive components                      (dormant)        2-3d
Phase 2  Shell (layout / sidebar / topbar / ⌘K)    (visible)        1.5d
Phase 3  Dashboard cutover                         (hero)           2d
Phase 4  Logs + detail drawer                      (stress test)    2-3d
Phase 5  Cascade to 14 remaining pages             (iterative)      9-14d
Phase 6  Polish + a11y + perf + docs               (closeout)       2d

total                                                              ~18-26 days
```

Phases 0–4 can be one single branch merged feature-flagged
(`NEXT_PUBLIC_THEME=tidepool` gates the cutover). Phase 5 can parallelise
across 2–3 engineers since each page is independent. Phase 6 closes the door.

---

## Risk table

| Risk | Likelihood | Mitigation |
|---|---|---|
| `backdrop-filter` perf degrades on older macs / Windows | M | Measure in Phase 6; fall back to solid panels with higher alpha on low-end GPUs (CSS `@supports`) |
| Contrast regressions on glass surfaces | H | Audit every text-on-glass combo in Phase 6 before removing the old Linear theme |
| Font flash on first paint (Instrument Serif) | M | `display: swap` + preload — acceptable tradeoff for 1 face |
| Existing Cypress snapshots break | H (expected) | Rebaseline per phase; document in PR |
| Light/dark flash on page load | M | Phase 2 inline script reads localStorage *before* React — pattern already documented in Next.js docs |
| ⌘K collides with browser dev-tools shortcut | L | `⌘K` is safe on Chrome/Firefox/Safari for page-level handlers; only collides inside DevTools overlay (acceptable) |
| Parallel PRs in Phase 5 cause merge conflicts on shared components | M | Freeze `ui/components/ui/` changes between phase 1 and phase 5 start; any primitive changes require sync |

---

## Rollback strategy

Feature flag the theme at the root:

```tsx
// ui/app/(admin)/layout.tsx
const theme = process.env.NEXT_PUBLIC_THEME ?? 'linear';
// 'linear' = legacy / 'tidepool' = new
```

Phase 0 sets up both token sets under `:root.theme-linear` and `:root.theme-tidepool` scopes. Until Phase 2 flips the wrapper class, everything renders the legacy. Flipping off = one env var + redeploy.

Hard rollback after Phase 5 = revert the merge commit for each page PR — each
is self-contained. No DB migration, no config shape change, no API surface
change. The migration is entirely cosmetic; rollback is always safe.

---

## Validation criteria (ship gate per phase)

Every phase must pass before merging:

- `cargo fmt --check && cargo clippy --workspace -- -D warnings && cargo test --workspace`
- `uv run pytest python/packages/`
- `cd ui && pnpm lint && pnpm typecheck && pnpm test --run`
- `cd ui && pnpm build` — bundle size delta ≤ +30 KB gzipped per phase
- Axe-core: 0 serious / 0 critical
- Visual snapshot diff review (humans + Chromatic / equivalent)
- `prefers-reduced-motion` toggle verified on Dashboard + current phase's
  pages
- Manual test on actual QQ / Telegram channel and one real log stream (Dashboard must still function with live data)

---

## What this plan does NOT do

- Does not change the Rust/Python backend at all
- Does not change API shape (every data call stays as-is)
- Does not introduce a CSS-in-JS library (sticks with Tailwind + CSS vars)
- Does not introduce new state management (React Query is already here)
- Does not ship a mobile-specific variant (Phase 7 could tackle it)
- Does not touch the `(auth)` flow beyond retokening the login page

---

## Open decisions before starting

1. **Phase 1.3**: drop `<TiltCard>` entirely, or keep on Dashboard only? Recommend: keep on Dashboard stat chips only.
2. **Phase 0.3**: Instrument Serif across `/admin/*` or only Dashboard hero + uptime card? Recommend: only where explicitly called for (hero, uptime streak, streak summaries). Keep sans elsewhere.
3. **Phase 4.3**: vendor `@tanstack/react-virtual` now or later? Recommend: now — blocks Phase 5 Hooks page too.
4. **Phase 6**: accept the perf cost of `backdrop-filter` on every panel, or cap to 2–3 "hero" surfaces? Recommend: cap — `<GlassPanel variant="subtle">` uses solid + low-alpha background without filter. Current Dashboard uses blur on sidebar + topbar + hero + 4 stats + 2 panes = 9 blur layers. Profiling may show 5 is the sweet spot.

First PR (Phase 0) is ready to scope once these four are answered.
