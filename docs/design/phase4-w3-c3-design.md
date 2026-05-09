# Phase 4 W3 C3 — Canvas Host renderer

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-05-08 · **Estimate**: 5-7d

> Phase 1 wired the Canvas transport — `/canvas/session`,
> `/canvas/frame`, SSE — but punted on rendering. C3 lands the
> renderer: a new `corlinman-canvas` crate turning producer-submitted
> artifacts (code, mermaid, tables, LaTeX, sparklines) into
> Tidepool-styled HTML the admin UI drops straight into transcripts.

Design seed for the iterations that follow. Pins crate boundary,
protocol surface over the Phase-1 frame stub, five artifact kinds and
their renderers, the Mermaid sandbox decision, content-addressed
cache, and UI integration. Mirrors `phase4-w2-b1-design.md` in shape.

## Why this exists

The Phase-1 stub at
`rust/crates/corlinman-gateway/src/routes/canvas.rs:9-17` is explicit:
*"There is no renderer here — only the transport / session
bookkeeping."* The handler whitelist (`canvas.rs:48-56`) accepts
seven frame kinds (`present`, `hide`, `navigate`, `eval`, `snapshot`,
`a2ui_push`, `a2ui_reset`) and forwards `payload` JSON verbatim
through SSE — no rendering server-side, no canonical artifact shape.

The operator-visible cost is in
`ui/components/sessions/transcript-view.tsx:131`: every assistant
turn renders through `whitespace-pre-wrap`, so a fenced
` ```rust ... ``` ` from a tool result lands in the admin UI as
monospace soup. Operators reading session replays today copy-paste
artifacts into a separate viewer. The roadmap
(`phase4-roadmap.md:284`) calls for the renderer; the acceptance
criterion (`phase4-roadmap.md:290-291`) is exact: *"Canvas Host
renders a code block from agent output as syntax-highlighted HTML
inside the admin UI."*

The Phase-1 default-off guard (`config.rs:1324`,
`host_endpoint_enabled: false`) stays the gate; C3 adds renderer
machinery behind it. `phase4-roadmap.md:373-375` flips the default
on once C3 ships.

## Canvas Host scope — what it renders

Five artifact kinds, one renderer per kind, all returning a single
`RenderedArtifact { html_fragment, theme_class, content_hash, ... }`.
Pinning the kinds early caps the dependency surface and gives the
producer side (`a2ui_push` callers) a closed vocabulary.

| Kind | Producer payload | Renderer | Library | Output |
|---|---|---|---|---|
| `code` | `{ language, source }` | syntax tree → coloured spans | `syntect` (oniguruma off, ZIP-bundled themes) | inline HTML `<pre class="cn-canvas-code">` |
| `mermaid` | `{ diagram }` | render to SVG via headless JS | `deno_core` + bundled `mermaid.min.js` | inline `<svg>` (sanitised) |
| `table` | `{ markdown }` *or* `{ csv }` | parse → `<table>` | `pulldown-cmark` (md) / `csv` crate | `<table class="cn-canvas-table">` |
| `latex` | `{ tex, display }` | TeX → MathML/HTML | `katex-rs` (no JS) | `<span class="katex">…</span>` |
| `sparkline` | `{ values, unit }` | inline SVG path | hand-rolled (60 LOC, no dep) | `<svg class="cn-canvas-spark">` |

**Why these five.** Code/mermaid/table cover ~95% of agent
tool-output patterns in `corlinman-skills` today (search → tables;
planner → flowchart; shell → fenced code). LaTeX is a low-cost
pure-Rust addition for math-heavy skills. Sparkline is the smallest
useful chat-surface visualisation without dragging in a charting
runtime; full charts deferred. Out: PDFs, images (already
`Attachment` in `agent.proto:71-77`), interactive widgets.

## Protocol surface

C3 reuses the Phase-1 `present` frame kind (`canvas.rs:48`) as the
producer-side opcode. The `payload` JSON gains a closed shape, which
the renderer expands. Wire diagram:

```
producer (skill / tool / engine)
  │  POST /canvas/frame
  │  { session_id, kind: "present",
  │    payload: { artifact_kind: "code", body: { … },
  │               idempotency_key: "art_a1b2…" } }
  ▼
gateway/routes/canvas.rs (Phase-1 stub) — auth, session lookup
  ▼
corlinman-canvas::render(payload)        ← NEW
  │  → RenderedArtifact { html_fragment, theme_class,
  │                       content_hash, render_kind, warnings }
  ▼
SSE fan-out to subscribers (Phase-1 unchanged)
  │  event: canvas
  │  data: { event_id, kind: "present",
  │          payload: { artifact_kind, rendered: { … } }, at_ms }
  ▼
ui/components/sessions/canvas-artifact.tsx  ← NEW (C3-FE)
```

**Shape decisions**, each from at least two options:

- **HTML fragment, not PNG**. PNG bakes in one theme; HTML fragments
  carry `var(--tp-amber)` references that resolve at the browser, so
  light/dark switches re-paint for free. Cost: tighter sanitisation.
- **Server pre-renders**. Alternative was ship raw bodies and
  re-bundle `mermaid` + `katex` per UI build; that blocks reuse with
  the Swift client (`phase4-roadmap.md:285`). Server-side wins on
  tree-shaking and uniformity.
- **Embedded in existing `present` frame, not a new kind**. The
  closed `payload` schema sits *inside* `present` so Phase-1 routes
  / tests / janitor stay byte-identical; `ALLOWED_FRAME_KINDS`
  (`canvas.rs:48`) doesn't grow.

The renderer never touches the network; pure function of
`(artifact_kind, body, theme_hint)`. Usable from tests, CLI, future
static export.

## Crate layout — `corlinman-canvas`

```
rust/crates/corlinman-canvas/
├── Cargo.toml            # syntect, pulldown-cmark, katex, csv,
│                          # deno_core (optional feature), ammonia
├── src/
│   ├── lib.rs            # pub use Renderer, RenderedArtifact
│   ├── protocol.rs       # CanvasPresentPayload, ArtifactBody,
│   │                     # RenderedArtifact, CanvasError
│   ├── renderer.rs       # Renderer struct (syntect Syntax/Theme,
│   │                     # mermaid sandbox, katex); dispatches on kind
│   ├── adapters/{code,mermaid,table,latex,sparkline}.rs
│   ├── sanitize.rs       # ammonia tag/class whitelist
│   ├── theme.rs          # token map → CSS class names
│   └── cache.rs          # content-addressed LRU
└── tests/{golden/,render_smoke.rs}
```

`Renderer::new(&CanvasConfig)` builds syntect SyntaxSet/ThemeSet
once, lazy-inits the mermaid sandbox (~150ms cold), warms KaTeX.
`Renderer::render(payload) -> Result<RenderedArtifact, CanvasError>`
is the single entry point. The gateway adds **one** call site; the
crate is composable from tests / CLI.

## Tidepool aesthetic enforcement

Tidepool is the repo's house style — warm-amber glass, day/night
dual theme — formalised in `ui/README.md:72-90` and in the token
blocks at `ui/app/globals.css:64-119` (light) /
`ui/app/globals.css:170-200` (dark). Renderer HTML never inlines
colour. Each adapter emits class names from a fixed palette:

| Class | Maps to | Used by |
|---|---|---|
| `cn-canvas-code` | `var(--tp-glass-inner)` bg, `var(--tp-ink)` fg | code |
| `cn-canvas-code-comment` | `var(--tp-ink-3)` | code |
| `cn-canvas-code-keyword` | `var(--tp-amber)` | code |
| `cn-canvas-code-string` | `var(--tp-ok)` | code |
| `cn-canvas-table` | `border-tp-glass-edge bg-tp-glass-inner` | table |
| `cn-canvas-mermaid` | wraps SVG, stroke=`var(--tp-amber)` | mermaid |
| `cn-canvas-spark` | path stroke `var(--tp-amber)`, fill `var(--tp-amber-soft)` | sparkline |
| `cn-canvas-katex` | colour `var(--tp-ink)` | latex |

Class definitions ship in **one place**: `ui/app/globals.css` gains
a `@layer components { /* canvas */ … }` block referencing existing
`--tp-*` tokens. Server-side syntect (in `theme.rs`) maps oniguruma
scope names → `cn-canvas-code-*` classes, *not* inline
`style="color: #ff6600"`. The rule: **server emits class names, CSS
resolves to tokens, theme switches just work**. Mermaid SVG is
post-processed (5 lines of `quick-xml`) to replace its default
palette with `currentColor` / `var(--tp-amber)` so dark mode
re-tints diagrams without re-rendering.

For Swift / non-web consumers (`phase4-roadmap.md:285`),
`RenderedArtifact` also emits `theme_class: "tp-light" | "tp-dark"`
— clients that can't resolve CSS vars ship their own stylesheet
keyed off that class.

## Sandboxing — Mermaid is the hard one

Of the five adapters, four are pure Rust (syntect, pulldown-cmark /
csv, katex-rs, sparkline). Mermaid is the outlier: there is no
production-quality pure-Rust Mermaid renderer; the JS library is the
de facto spec. Three options:

| Option | Pros | Cons |
|---|---|---|
| `node` subprocess per render | Simplest | 200-400ms cold; new external runtime; no isolation by default |
| Reuse `corlinman-sandbox` (W2-A docker, `phase4-roadmap.md:326`) | Strongest isolation | 500ms+ per render; overkill for SVG-string-out |
| `deno_core` embedded JS in-process | 50ms warm; no extra runtime | New dep (~6 MB); CVE surface from V8 |

**Decision**: `deno_core` in-process, with these guards:

1. **No `Deno.*` namespace** — engine initialised without the Deno
   extension; only ECMAScript primitives. Mermaid touches
   `document.createElement` for SVG node construction; we ship a
   200-line minimal DOM shim (same approach `mermaid-cli` uses).
2. **CPU + memory bounds via `v8::Isolate` constraints** — 64 MB
   heap, `terminate_execution()` after `canvas.render_timeout_ms`
   (default 5000). Hostile diagrams fork-bombing the SVG tree get
   cut off and surface as `CanvasError::Timeout`.
3. **Output size cap** — produced SVG > `max_artifact_bytes`
   (default 256 KB) → reject. Defends against `<rect>` spam.
4. **Sanitise the SVG** — `ammonia` whitelist scoped to the SVG tag
   set (`svg, g, path, rect, circle, line, text, defs, marker,
   polyline, polygon`). Strips `<script>`, event-handler attrs,
   `xlink:href` to non-fragment URLs.

**Why not `corlinman-sandbox`**: that crate is docker-backed for
*agent code execution* — multi-second runs, full FS, networking
gates. Per-render docker exec for a 100-byte mermaid string is two
orders of magnitude too expensive. Canvas mermaid is "interpret a
literal", not "execute untrusted code"; in-process `deno_core` with
hard limits is the right cost class.

CVE posture: V8 ships a CVE most quarters. Mitigation: `deno_core`
version pinning + quarterly bump task. Blast radius equals
`pulldown-cmark` parser bug — gateway process — just lower
probability, higher impact.

## Caching

Content-addressed, in-memory LRU keyed by:

```
cache_key = blake3( artifact_kind || canonicalize_json(body) || theme_hint )
```

`cache.rs` wraps `lru::LruCache<CacheKey, Arc<RenderedArtifact>>`.
Default `cache_max_entries = 512`. A re-rendered identical artifact
returns the same Arc — important because operators replaying the
same session many times (a B4 use-case) hammer the same five-ten
diagrams.

**Theme is part of the key** but only mermaid output bytes differ
across themes; syntect / table / latex / sparkline are byte-identical.
The dual entry is a small price for deterministic mermaid
post-processing. **Renderer version u32** in the key — bumped when
bundled syntect themes / mermaid bundle / katex options change. No
on-disk cache; restart = cold cache. `[canvas] cache_max_entries =
0` turns it off entirely.

## UI integration

Four files in `ui/components/sessions/`:

- **`transcript-view.tsx`** (existing, `:131`): the
  `whitespace-pre-wrap` div is replaced by a small parser splitting
  messages into text + canvas-artifact spans. The SSE event carries
  `canvas_artifacts: { idempotency_key → rendered_html }` and the
  text contains `[[canvas:art_a1b2…]]` placeholders.
- **`canvas-artifact.tsx`** (NEW): consumes rendered HTML via
  `dangerouslySetInnerHTML` after a *client-side* DOMPurify pass.
  Belt-and-braces: server already sanitised, client sanitises again.
- **`canvas-artifact-error.tsx`** (NEW): fallback for `Timeout`,
  `LanguageUnsupported`, `BodyTooLarge`. Raw text inside a dashed
  Tidepool glass panel with `lucide:triangle-alert` and error code.
- **`canvas-artifact-loading.tsx`** (NEW): skeleton for the ~50ms
  between SSE arrival and rendered HTML. Reuses `bg-state-skeleton`
  (`globals.css:166`).

Surfaces: **session replay** (`ui/app/(admin)/sessions/`) first;
**playground** (`ui/components/playground/token-stream.tsx`) second.
Both feed `TranscriptView` so chat live model dialog gets it free.
`cmdk-palette.tsx` and `log-detail-drawer.tsx` render `<pre>` raw
today — out of C3 scope, obvious next consumers.

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `code_round_trip_rust` | adapter | snapshot HTML matches golden; expected token classes present |
| `code_unsupported_language_fallback` | adapter | `language: "klingon"` → `<pre>` with no token classes; no error |
| `code_html_escape` | adapter | source containing `<script>` is text-escaped, not raw |
| `mermaid_simple_flowchart_renders` | adapter | `graph LR; A-->B` produces `<svg>` with two `<g>` and one `<path>` |
| `mermaid_timeout_terminates_v8` | adapter | infinite-loop diagram → `Timeout` within 5s; subsequent render still works |
| `mermaid_oversized_output_rejected` | adapter | diagram producing > 256 KB → `BodyTooLarge` |
| `mermaid_script_tag_stripped` | adapter | hostile `<g><script>` in raw output → ammonia-stripped |
| `table_markdown_round_trip` | adapter | 3x3 GFM table → `<table>`+`<thead>`+`<tbody>` correct |
| `table_csv_round_trip` | adapter | 3x3 CSV → same shape; embedded comma quoting respected |
| `latex_inline_vs_display` | adapter | `display: true` → `katex-display` class; `false` → inline span |
| `latex_macro_blacklist` | adapter | `\href` / `\input` rejected per katex-rs strict mode |
| `sparkline_4_points` | adapter | `[1,4,2,9]` produces `<path>` with 4 segments; min/max baseline correct |
| `cache_returns_arc_on_hit` | cache | second render returns same Arc; lru count = 1 |
| `cache_evicts_at_capacity` | cache | `cache_max_entries=2`, three distinct renders → first evicted |
| `theme_class_emitted` | renderer | both themes produce identical HTML for code/table/latex; mermaid differs only in stroke-hint |
| `protocol_present_payload_round_trips` | protocol | Serde round-trip; unknown `artifact_kind` → `UnknownKind` |
| `dos_input_bomb_capped` | renderer | 5 MB code body → `BodyTooLarge` before adapter dispatch |
| `disabled_route_short_circuits` | gateway | `host_endpoint_enabled = false` → renderer never called |
| `producer_idempotency_key_dedupes` | gateway | two `present` frames with same key → renderer invoked once |
| `transcript_renders_artifact_in_situ` | UI | RTL: SSE `present` → `canvas-artifact` element with role="figure" |

## Config knobs

```toml
[canvas]
host_endpoint_enabled = true              # Phase-1 gate (existing)
session_ttl_secs      = 1800              # Phase-1 gate (existing)

# C3 additions
renderer_kind         = "in_process"      # 'in_process' | future 'subprocess'
allowed_kinds         = ["code", "mermaid", "table", "latex", "sparkline"]
max_artifact_bytes    = 262144            # 256 KB; per-artifact body cap
render_timeout_ms     = 5000              # mermaid/v8 hard ceiling
cache_max_entries     = 512               # 0 disables cache
default_code_theme    = "tidepool-amber"  # syntect theme name; bundled
mermaid_enabled       = true              # cheap kill-switch for the JS path
```

`[canvas]` already exists at
`rust/crates/corlinman-core/src/config.rs:1323`; C3 extends it
additively, matching Phase-1's `#[serde(default,
deny_unknown_fields)]` style. Validation: `allowed_kinds` is checked
at parse time against the renderer's known kinds; an unknown kind
fails config load.

## Open questions

1. **SVG vs PNG.** Ship SVG inline. Swift / mobile
   (`phase4-roadmap.md:285`) may want PNG. Lean: SVG only in C3, add
   `alt_png: Option<Vec<u8>>` lazily in C4 when the Swift client
   needs it; don't pay rasterisation yet.
2. **Sandbox boundary for mermaid.** This doc commits to `deno_core`
   in-process. If iter 6 fuzz surfaces unrecoverable V8 crashes,
   fall back to a long-lived `node` subprocess pool. Deferred to
   fuzz results.
3. **How producers signal "render this".** Committed to `present`
   frame with closed `payload.artifact_kind` enum. Alternative:
   `corlinman-skills` detects fenced code / mermaid in plain-text
   tool output and auto-emits transparently — friendlier but easy to
   mis-trigger. Lean: explicit opt-in for C3; auto-detection is its
   own iteration.
4. **Sparkline data limits.** Cap at 1024 points server-side or
   refuse over-cap? Lean: refuse with `BodyTooLarge`; producer
   pre-aggregates.

## Implementation order — 10 iterations

Each item is one bounded iteration (~30 min - 2 hours):

1. **Crate skeleton + protocol types** — new `corlinman-canvas`;
   `protocol.rs` with `CanvasPresentPayload`, `ArtifactBody` (untagged
   enum), `RenderedArtifact`, `CanvasError`. `Renderer` stub returns
   `Unimplemented`. Tests: `protocol_present_payload_round_trips` +
   `unknown_kind_round_trips_as_error`. Workspace wires the crate;
   no caller depends yet.
2. **Code adapter (syntect)** — bundled `tidepool-amber` theme;
   class-based scope emission; HTML escape. Tests:
   `code_round_trip_rust`, `code_unsupported_language_fallback`,
   `code_html_escape`.
3. **Table adapter (markdown + csv)** — `pulldown-cmark` GFM;
   `csv` crate. Tests: `table_markdown_round_trip`,
   `table_csv_round_trip`, `table_markdown_with_inline_code`.
4. **LaTeX adapter (`katex-rs`)** — display vs inline; macro
   blacklist via strict mode. Tests: `latex_inline_vs_display`,
   `latex_macro_blacklist`, `latex_unicode_passthrough`.
5. **Sparkline adapter** — pure SVG; baseline=min, ceiling=max,
   linear scale. Tests: `sparkline_4_points`, `sparkline_constant`,
   `sparkline_empty_rejected`.
6. **Mermaid adapter (deno_core)** — embedded engine; bundled
   `mermaid.min.js` + DOM shim; v8 isolate with heap cap / timeout /
   output cap; `ammonia` post-process. Tests: render, timeout,
   oversized, script-strip. **Fuzz pass** (10 min `cargo-fuzz`); if
   gateway can't recover, switch to subprocess per Open Question 2.
7. **Cache layer** — blake3-keyed `LruCache<_, Arc<RenderedArtifact>>`.
   Tests: hit returns same Arc, eviction at capacity, disabled at 0.
8. **Gateway wiring** — `routes/canvas.rs::post_frame`
   (`canvas.rs:293`) gains a branch: `body.kind == "present"` +
   payload deserialises → `Renderer::render` → merge HTML into SSE
   event payload before `subscribers.send`. `CanvasState` gains
   `renderer: Arc<Renderer>`. No other Phase-1 changes. Tests:
   extend `canvas_host_disabled`, idempotency-key dedupe,
   unknown-artifact-kind 400.
9. **UI artifact components** — `canvas-artifact.tsx`,
   `canvas-artifact-loading.tsx`, `canvas-artifact-error.tsx` +
   transcript-view parser. `globals.css` `cn-canvas-*` block.
   Vitest: `transcript_renders_artifact_in_situ`,
   `canvas_artifact_error_renders_raw_fallback`,
   `theme_switch_re_resolves_classes`.
10. **E2E with a real producer skill** — pick `web_search` (search
    results fit a table); emit `present` on result. Drive
    skill → gateway → canvas → SSE → UI. Playwright against the
    playground asserts table, code, mermaid (planner skill), katex
    (math skill), and tokens-per-turn sparkline render. Five
    artifact kinds, one E2E.

## Out of scope (C3)

- **Interactive widgets / animations** — hover, zoom, pan belong to
  a future "Canvas v2". C3 ships static HTML; clicking does nothing.
- **Producer-side detection / auto-conversion** — agents that emit
  fenced-code text without opting into `present` continue to render
  through legacy `whitespace-pre-wrap`. Auto-detection is its own
  design (Open Question 3).
- **Streaming partial renders** — Mermaid/LaTeX/syntect are
  whole-string ops; partial token streams don't incremental-render.
  Frame fires once when the producer says ready.
- **Persistent on-disk cache** — in-memory LRU only; restart = cold
  cache.
- **Non-admin surfaces** — `cmdk-palette.tsx`, `log-detail-drawer`,
  `validation-drawer`, plugins detail header render `<pre>` raw
  today. Migrating them is a follow-up.
- **Full charting (bar / line / multi-series)** — sparkline only.
  Real `chart` artifact (Chart.js or `vega-lite` via deno_core) is
  out of scope; same sandbox tree applies, cost-benefit not done.
- **Federation / cross-tenant artifact sharing** — every render is
  per-tenant in-process; no shared artifact registry. Federation
  (`phase4-w2-b3-design.md`) is proposal-level, not artifact-level.

## Iter 10 — close-out notes (post-implementation)

Iter 10 shipped the E2E acceptance and reconciled the outstanding
design tensions from iter 8/9.

### `present` frame ↔ `/canvas/render` reconciliation — **resolved**

Iter 8 originally added `/canvas/render` as a side-channel and left
`/canvas/frame` Phase-1-byte-identical. That created two paths to
the renderer with no canonical answer for "which one should the
producer call". Iter 10 picks **enrichment-on-frame** as the
canonical happy path:

- `POST /canvas/frame { kind: "present", payload: {…C3 schema…} }`
  speculatively deserialises the payload as a `CanvasPresentPayload`
  and, on success, invokes the renderer in-line. The rendered
  artifact lands on the SSE event under `payload.rendered`; failures
  land under `payload.render_error`. Idempotency-key dedupe (per
  session) prevents double-renders on retry.
- Pre-C3 callers that POST legacy a2ui-style payloads under
  `present` keep working byte-identically — the speculative parse
  misses, the frame fans out unchanged, and `payload.rendered` is
  absent.
- `POST /canvas/render` survives as the **stateless preview**
  endpoint for non-frame consumers (Swift client, CLI, future
  static export). It does not touch the session store; it is a
  pure-function HTTP wrapper around the same renderer with the
  same body cap.

The UI side reads frames via `parsePresentFrame` (new in iter 10),
which returns one of `{ artifact, error, passthrough }` so the
transcript-view consumer doesn't need to know the JSON layout.

### Config knobs — **resolved**

The iter-8 `const MAX_ARTIFACT_BYTES` / `RENDERER_CACHE_CAPACITY`
stopgaps in `routes/canvas.rs` are gone. `CanvasConfig` in
`rust/crates/corlinman-core/src/config.rs` now carries the four C3
knobs (`max_artifact_bytes`, `cache_max_entries`, `render_timeout_ms`,
`mermaid_enabled`) with `validate(range)` ranges that prevent
operator misconfig. `max_artifact_bytes` is read live from the
`ArcSwap<Config>` snapshot on every request; `cache_max_entries`
is read once at gateway boot to size the LRU. The `render_timeout_ms`
and `mermaid_enabled` knobs are wired through to `CanvasConfig`
but the runtime read sites land with the mermaid feature build —
they're informational on the default workspace build.

### Deferred to Phase 5

- **Mermaid feature-gated E2E.** Iter 10 skips the V8 mermaid path
  intentionally — the default workspace build does not link
  `deno_core`, and the CI matrix would need a `--features mermaid`
  job to exercise it. The iter-10 E2E test
  `e2e_present_frame_attaches_render_error_on_failure` proves the
  *gateway-side* behaviour (typed adapter error, structured SSE
  surface) works with the feature off — the actual JS rendering
  acceptance is a Phase-5 follow-up.
- **`/canvas/render` retirement.** Now that enrichment-on-frame is
  canonical, `/canvas/render` is technically redundant for web
  consumers. It survives in iter 10 because Swift / CLI consumers
  may want it; a Phase-5 review can deprecate it once those
  consumers actually land.
- **Producer auto-detection.** "Agent emits fenced code without
  opting in to `present`" is still untouched (Open Question 3).
  Phase 5 owns this if the manual opt-in proves ergonomically
  costly.
