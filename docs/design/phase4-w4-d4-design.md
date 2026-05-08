# Phase 4 W4 D4 — Voice surface (alpha)

**Status**: Design (pre-implementation) · **Owner**: TBD · **Created**: 2026-05-08 · **Estimate**: 5-7d

> First realtime-audio surface: a `/voice` WebSocket on the gateway
> that accepts PCM-16 from a client and streams TTS back, brokered by
> a single upstream provider (OpenAI Realtime first; Gemini Live
> second). Gated under `[voice]` because every minute spent on the
> wire bills cents, not micro-cents.

Design seed for the iterations that follow. Pins the duplex protocol,
the provider-WebSocket bridging shape, tool-approval pause semantics,
voice-session persistence (and its merge into the existing chat
session table), opt-in audio retention, and the per-tenant
minutes-per-day budget. Mirrors `phase4-w2-b1-design.md` in shape.

## Why this exists

Every existing surface — `/v1/chat/completions`
(`rust/crates/corlinman-gateway/src/routes/chat.rs:824-844`), the QQ /
Telegram channel adapters (`config.rs:670-758`), the canvas + admin
routes — is **request/response or one-way streaming SSE**. None are
**full-duplex**. A realtime-audio session is unavoidably duplex: the
client streams microphone PCM while the server simultaneously streams
TTS, and either side may interrupt the other mid-utterance. D4 lands
the first WebSocket-shaped chat surface and the bridging primitive
that future channels (Mac SwiftUI client, browser MCP-WS, mobile
push-to-talk) will reuse without rewriting transport.

The roadmap (`phase4-roadmap.md:303,377-378,412`) explicitly calls D4
**alpha** for one reason: **cost**. A 5-minute OpenAI Realtime session
is roughly 50× the per-token cost of an equivalent text turn. The
flag default is **off**; opt-in is per-tenant; budget is enforced
both at session start (refuse) and mid-session (terminate). Without
that gate, a leaked deployment burns the operator's budget overnight.

## Scope

What the alpha does:

- **`/voice` WebSocket**: client connects with `Sec-WebSocket-Protocol:
  corlinman.voice.v1`, streams 16 kHz mono PCM-16 frames as binary
  messages, and receives 24 kHz PCM-16 (or Opus) TTS frames back
  alongside JSON control events.
- **One provider wired**: OpenAI Realtime API first (mature, public
  WebSocket SDK surface, gpt-4o-realtime-preview). Gemini Live is the
  second target but lands in a follow-on iter; its transport differs
  enough (bidirectional gRPC inside a websocket) that we'd lose a day
  designing the abstraction the wrong way.
- **Provider routing is single-tenant config-driven**: `[voice]
  provider_alias = "openai-realtime"` → the alias resolves through the
  same registry pattern as text providers
  (`python/packages/corlinman-providers/src/corlinman_providers/registry.py:1-12`).
  No model picker on the request; tenants who want to A/B switch the
  alias.
- **Session = chat session**: a voice session reuses
  `sessions.sqlite` (`rust/crates/corlinman-core/src/session_sqlite.rs:44-65`)
  under the same `(tenant_id, session_key)` namespace, with `user` /
  `assistant` rows whose `content` is the **transcript text**. Audio
  is side-stored (see §Persistence). The reasoning loop reads text
  rows; audio is a presentational layer.
- **Tool calls reuse the agent loop**: provider tool-call events
  become `corlinman_proto::v1::ToolCall` (`routes/chat.rs:54-66`).
  No parallel tool runtime.

Not in scope (alpha):

- Client-side voice activity detection (VAD). The provider does
  silence detection upstream; the client streams continuously when
  unmuted. Mac/iOS clients ship VAD in their own iter.
- On-prem Whisper. The flag name calls it "whisper-compatible" because
  the audio shape (PCM-16 16 kHz) matches Whisper's expected input,
  but D4 ships **no local model**. The roadmap parks it
  (`phase4-roadmap.md:330` — `corlinman-voice` package abstracts it
  for later).
- Multi-speaker / diarization. Single user per session.

## Protocol surface — `/voice` WebSocket framing

Mounted in `routes/mod.rs:7-14` as `pub mod voice;` next to `chat`.
The route lives at `GET /voice` (WebSocket upgrade) — under `/v1/`
namespace would imply OpenAI compatibility we don't claim.

**Subprotocol**: `corlinman.voice.v1`. The handshake refuses any other
value with WebSocket close code `1002` (protocol error).

**Auth**: same `Authorization: Bearer <token>` as admin routes; the
`require_admin` chain doesn't apply, but a tenant-scoped session
token is required. Token validation runs **before** upgrade; an
upgrade response means auth is settled.

**Client → server frames**:

| Type | Wire | Payload |
|---|---|---|
| Control | text JSON | `{"type":"start","session_key":"...","agent_id":"...","sample_rate_hz":16000,"format":"pcm16"}` — first message after upgrade |
| Audio | binary | raw little-endian PCM-16, ≤200 ms per frame; concatenation is the audio stream |
| Control | text JSON | `{"type":"interrupt"}` — flushes server-side TTS buffer; provider barge-in |
| Control | text JSON | `{"type":"approve_tool","approval_id":"...","approve":true}` — operator decision relay (see §Tool approval) |
| Control | text JSON | `{"type":"end"}` — graceful close; server flushes transcript and closes with code `1000` |

**Server → client frames**:

| Type | Wire | Payload |
|---|---|---|
| Control | text JSON | `{"type":"started","session_id":"voice-...","provider":"openai-realtime"}` — ack of `start` |
| Audio | binary | TTS PCM-16 (24 kHz) — playback chunk |
| Control | text JSON | `{"type":"transcript_partial","role":"user","text":"..."}` — interim ASR |
| Control | text JSON | `{"type":"transcript_final","role":"user","text":"..."}` — committed user turn |
| Control | text JSON | `{"type":"agent_text","text":"..."}` — assistant turn text (mirrors TTS) |
| Control | text JSON | `{"type":"tool_approval_required","approval_id":"...","tool":"...","args":{...}}` — pause point |
| Control | text JSON | `{"type":"budget_warning","minutes_remaining":N}` — soft notice |
| Control | text JSON | `{"type":"error","code":"...","message":"..."}` — terminal; close follows |

Binary vs text framing follows axum's `Message::Binary` / `Message::Text`
straightforwardly; the existing `ws/logstream.rs` stub
(`rust/crates/corlinman-gateway/src/ws/logstream.rs:1-6`) is the file
neighbour but the voice handler will live under `routes/voice.rs`
because it owns route + state, not just a sink.

## Provider integration — bridging two WebSockets

Each `/voice` session spawns one **upstream provider WebSocket** and
bridges audio bidirectionally. Per session:

```text
  Client WS  <──audio──>  Gateway  <──audio──>  Provider WS
            <──control──>           <──events──>
```

The gateway owns three independent tokio tasks per session:

1. **Client→provider audio pump**: read binary frames; rate-limit
   (drop if > 100 frames/sec — defends against malformed clients);
   forward to provider in the provider's required envelope (OpenAI
   Realtime expects `{"type":"input_audio_buffer.append","audio":
   "<base64>"}` JSON, **not** raw binary, so we re-encode).
2. **Provider→client pump**: read provider events; demultiplex into
   binary TTS frames + JSON control. OpenAI Realtime emits
   `response.audio.delta`, `conversation.item.input_audio_transcription.
   completed`, `response.function_call_arguments.delta`, etc.
3. **Control plane**: inbound `interrupt` / `approve_tool` / `end`
   from client become provider commands; outbound budget /
   approval events come from the gateway itself, not the provider.

The Python `corlinman-voice` package (new — listed in
`phase4-roadmap.md:330`) owns the provider adapter. Its surface is **not**
the `CorlinmanProvider` Protocol from
`python/packages/corlinman-providers/src/corlinman_providers/base.py:79-...`;
realtime audio doesn't fit `ProviderChunk`. Instead a parallel
`CorlinmanVoiceProvider` Protocol with `async def session(audio_in,
control_in) -> AsyncIterator[VoiceEvent]` shape. Reuses the same
registry-alias config pattern.

Tool-call events from the provider are **translated** into the
`ServerFrame.ToolCall` shape the agent loop already understands
(`routes/chat.rs:54-66`). The agent decides whether it needs
operator approval (existing `ApprovalGate` —
`routes/admin/approvals.rs:1-22`); if so, the voice handler emits
`tool_approval_required` and pauses (next §).

## Tool approval mid-conversation

A voice session in the middle of "search the web for X and email
me the summary" hits the existing tool-approval gate. Three options:

| Approach | Pros | Cons |
|---|---|---|
| Pause TTS, ping admin UI, wait verbatim | Same UX as text chat | Voice session sits silent for tens of seconds; user thinks call dropped |
| Verbal confirm ("say yes to allow") | Conversational, natural | Trivially spoofable; user said "yes" to a wholly different question moments earlier |
| Both: agent **announces** the request via TTS, **admin UI** decides | User knows what's happening; auth stays out-of-band | Two surfaces to coordinate |

**Decision**: third option. When the agent loop yields a tool that
hits the approval gate:

1. Agent emits `tool_approval_required` JSON event to client.
2. Client renders a "waiting for operator approval — tool: web_search"
   banner; TTS continues with a one-shot phrase (`"Hold on, I need
   approval to use web_search before I continue."`) generated locally
   client-side from a fixed template — **not** an extra TTS round-trip.
3. Operator decides via `/admin/approvals` UI — same flow as text.
4. The decision broadcasts back into the voice session via the
   existing approval-gate subscribe channel
   (`routes/admin/approvals.rs:14-22`) and the agent loop resumes.
5. Voice session emits a follow-up `agent_text` ("Approved, continuing
   ...") and TTS resumes streaming.

The `approve_tool` client→server control frame is **opt-in**: a Mac
client may bind a hardware shortcut to fast-approve trusted tools
(operator's own session). Default is "approval gate is the gate", no
client-side override.

## Persistence

New SQLite table in the per-tenant `sessions.sqlite`
(same DB as text sessions —
`rust/crates/corlinman-core/src/session_sqlite.rs:44-65`):

```sql
CREATE TABLE IF NOT EXISTS voice_sessions (
  id              TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL DEFAULT 'default',
  session_key     TEXT NOT NULL,         -- chat-session FK; same value used for text turns
  agent_id        TEXT,
  provider_alias  TEXT NOT NULL,
  started_at      INTEGER NOT NULL,
  ended_at        INTEGER,
  duration_secs   INTEGER,
  audio_path      TEXT,                  -- NULL = audio dropped per retention policy
  transcript_text TEXT,                  -- full transcript snapshot at end
  end_reason      TEXT NOT NULL          -- 'graceful' | 'budget' | 'provider_error' | 'client_disconnect'
);
CREATE INDEX IF NOT EXISTS idx_voice_sessions_tenant_session
  ON voice_sessions(tenant_id, session_key, started_at);
```

The **transcript** is also written to the existing `sessions` table
as ordinary `user` / `assistant` rows so the agent's chat history
includes voice turns indistinguishably from typed turns. Downstream
features (memory distill, episode jobs in
`phase4-roadmap.md:298-303`) need no voice-awareness.

## Storage of audio

Default policy is **drop after session end**. Three reasons: (1) PCM
is large (16 kHz × 16-bit × 60 s ≈ 1.9 MB raw, dual-channel doubles
it), (2) audio is sensitive (voiceprint, ambient room contents), and
(3) cost gating is the alpha's whole point.

When `[voice] retain_audio = true`, audio writes to the same per-tenant
data tree the rest of the stack uses:
`<data_dir>/tenants/<t>/voice/<session_id>.pcm` (raw mono PCM-16) and
`<data_dir>/tenants/<t>/voice/<session_id>.tts.pcm` (assistant audio).
Encrypted-at-rest is **not** alpha scope — operators with that
requirement are pointed at OS-level FDE; an encrypted sink is a
follow-on iter.

Retention TTL: `[voice] audio_retention_days = 30` (default 7 when
`retain_audio = true`). A scheduled sweep job deletes files past
TTL; the `voice_sessions.audio_path` is nulled but the row is kept
forever (it's small and the transcript is already in the chat
session anyway).

## Cost guardrails

Three layers:

1. **Feature flag**: `[voice] enabled = false` (default). Route
   returns `503 voice_disabled` when off, with `Retry-After: 86400`
   so monitors don't hammer.
2. **Per-tenant daily minutes budget**: `[voice]
   budget_minutes_per_tenant_per_day = 30` (default; operator tunes
   per deployment). Enforced at session-start (refuse with `429
   budget_exhausted`) and mid-session (a 1-Hz ticker checks
   accumulated seconds; on overage, send `budget_warning` 60s before
   the cap, terminate at the cap with end_reason `budget`).
3. **Hard kill at session length cap**: `[voice]
   max_session_seconds = 600` regardless of budget. Defends against
   a stuck session that no client has the courtesy to end.

Spend accounting: a `voice_spend` table per-tenant tracks
`(date, seconds_used, sessions_count)` and increments on session
close. Budget check reads `WHERE date = today` once per session-start
+ once per minute thereafter. A future iter wires this to the
evolution observer (`evolution_observer.rs`) so cost spikes generate
signals.

## Test matrix

| Test | Layer | Asserts |
|---|---|---|
| `voice_disabled_returns_503` | route | `[voice] enabled = false` makes upgrade fail with 503 + `Retry-After` header |
| `voice_handshake_subprotocol_negotiated` | route | client sending `Sec-WebSocket-Protocol: corlinman.voice.v1` gets the upgrade; missing/wrong → 400 |
| `voice_round_trip_with_mock_provider` | integration | mock `CorlinmanVoiceProvider` echoes audio; client `start` + 5s of PCM yields `transcript_final` + TTS audio frames back |
| `interrupt_flushes_tts_buffer` | route | client `interrupt` while TTS streaming → server stops emitting binary frames within 100 ms |
| `tool_approval_pauses_session` | route | mock agent yields a gated tool; server emits `tool_approval_required`; injecting an admin-approval signal → session resumes |
| `tool_approval_denial_terminates_turn` | route | denial → `agent_text` apology + session stays open for next turn |
| `budget_exhausted_at_start` | route | tenant at-cap → upgrade returns 429 + JSON body `{"error":"budget_exhausted","reset_at":...}` |
| `budget_exhausted_mid_session` | route | spend ticker exceeds cap → `budget_warning` then close `4002 budget` |
| `max_session_seconds_terminates` | route | session hitting `max_session_seconds` closes regardless of budget |
| `provider_unreachable_falls_back_gracefully` | adapter | upstream WS connect fails → close `4003 provider_unavailable`; `voice_sessions.end_reason='provider_error'` row written |
| `transcript_persisted_to_chat_session` | persistence | after close, `sessions` table has `user` + `assistant` rows for the voice turns under same `session_key` |
| `audio_dropped_when_retain_false` | persistence | default config: no files under `voice/`; `voice_sessions.audio_path IS NULL` |
| `audio_retained_when_retain_true_and_swept_after_ttl` | persistence | files written; sweeper at TTL+1d deletes file + nulls path |
| `meta_approver_unaffected_by_voice` | route | voice routes don't touch the `meta_approver_users` capability gate |

## Config knobs

```toml
[voice]
enabled = false
provider_alias = "openai-realtime"          # must resolve via providers registry
budget_minutes_per_tenant_per_day = 30
max_session_seconds = 600
retain_audio = false
audio_retention_days = 7                    # only meaningful when retain_audio = true
sample_rate_hz_in = 16000                   # client PCM rate; provider may resample
sample_rate_hz_out = 24000                  # TTS rate the gateway emits to clients
```

The `[voice]` section is added to `Config`
(`rust/crates/corlinman-core/src/config.rs:54-122`) as an optional
field defaulting to `VoiceConfig { enabled: false, .. }` — same
pattern as `embedding: Option<EmbeddingConfig>` at line 64. An absent
section is a disabled section; existing configs round-trip
unchanged. Validator emits a warning if `enabled = true` but the
referenced `provider_alias` doesn't exist in `[providers]`.

## Open questions

1. **Mac SwiftUI client integration: required for E2E or `wscat`
   enough?** Lean: **wscat is enough for iter 10's E2E happy-path
   test** (a Python harness sending real PCM from a wav file).
   Hardware-mic UX validation belongs in the Mac client task; D4
   doesn't block on it.
2. **Voice-as-evolution-signal?** A confused agent says "uh" five
   times — does that emit an `evolution.voice.confusion_detected`
   signal the engine clusters? Lean: **defer**. Alpha logs raw
   transcripts; signal extraction is W5+. Avoids a feedback loop
   where voice quirks propose engine-prompt mutations from skewed
   data.
3. **TTS voice configurable per `agent_card`?** A "professional"
   agent gets a different voice than a "playful" agent. Lean:
   **yes, but as a string passthrough only**:
   `agent_card.voice_id = "alloy"` is forwarded verbatim to the
   provider; corlinman doesn't curate voice catalogues.
4. **What happens when the upstream provider WebSocket dies
   mid-session?** Auto-reconnect (preserve session) vs hard-close
   (force client to retry)? Lean: **hard-close with explicit error**.
   Reconnect mid-utterance is jarring and client-side reconnection
   is the same code path the client already needs for unrelated
   failures.
5. **Per-user voice rate limits separate from per-tenant minutes?**
   The QQ adapter has both (`config.rs:716-732`). Lean: **defer to
   alpha+1**. Per-tenant minutes is the bigger lever.

## Implementation order — 10 iterations

1. **Config schema + 503 stub route**.
   `VoiceConfig` struct + default; `routes/voice.rs` with a stub
   handler returning `503 voice_disabled` when `enabled = false`,
   `501 not_implemented_yet` when on. Mounted in `routes/mod.rs:7-14`.
   Tests: `voice_disabled_returns_503`, config round-trip.
2. **WebSocket upgrade + subprotocol negotiation**.
   Add `axum` `ws` feature usage to gateway (already in workspace
   deps — `Cargo.toml:16`); upgrade handler that validates
   subprotocol header, accepts the upgrade, and immediately closes
   with a `started` event then `1000`. Tests: handshake matrix.
3. **Client→server `start` / `end` control frames + session row**.
   `voice_sessions` table migration; insert on `start`, finalise
   on `end` or socket close. Tests: row presence with correct
   `end_reason`.
4. **Mock provider adapter + Python `corlinman-voice` package
   skeleton**. `CorlinmanVoiceProvider` Protocol; `MockEchoProvider`
   in tests. Bridge tasks (3 tokio tasks per session). Tests:
   `voice_round_trip_with_mock_provider`.
5. **Real OpenAI Realtime adapter**. WebSocket client to
   `wss://api.openai.com/v1/realtime`; envelope translation; tool-call
   event mapping back to `ServerFrame::ToolCall`. Behind a
   network-isolation test guard (skipped without `OPENAI_API_KEY`
   env). Tests: live smoke test in CI nightly only.
6. **Interrupt + barge-in**. Client `interrupt` flushes the upstream
   provider's TTS buffer; server-side outbound queue is drained.
   Tests: `interrupt_flushes_tts_buffer`.
7. **Tool approval pause**. Wire `tool_approval_required` event;
   subscribe to existing approval gate (`routes/admin/approvals.rs:14`)
   so admin decisions resume the session. Tests:
   `tool_approval_pauses_session`, `tool_approval_denial_terminates_turn`.
8. **Budget enforcement**. `voice_spend` table; start-time check;
   1-Hz ticker; `budget_warning` event; close codes 4002. Tests:
   `budget_exhausted_at_start`, `budget_exhausted_mid_session`,
   `max_session_seconds_terminates`.
9. **Audio retention**. `retain_audio` writes PCM files; sweeper
   job at scheduler-tick removes past-TTL files. Tests:
   `audio_dropped_when_retain_false`,
   `audio_retained_when_retain_true_and_swept_after_ttl`.
10. **E2E happy path with one real provider**. Python harness
    streams a 10s wav file; expects transcript + TTS audio bytes
    + `voice_sessions` row + chat-session transcript rows. Gated
    on `OPENAI_API_KEY`; the runbook adds a manual-test section.
    Tests: full integration; updates `phase4-next-tasks.md` to mark
    D4 alpha-shipped.

## Out of scope (D4)

- **Multi-speaker / diarization** — single user per session; no
  speaker-identification labels in the transcript.
- **Voice cloning** — provider's stock voices only; no custom
  voice training.
- **On-prem Whisper** — listed as `corlinman-voice` package goal in
  `phase4-roadmap.md:330` but no model weights ship with D4.
- **Voice-to-voice translation** — single language per session;
  language detection / switching is a follow-on.
- **Non-realtime batch transcription** — `/voice` is duplex only;
  uploaded audio file → transcript belongs in a separate
  `/v1/audio/transcriptions` endpoint not designed here.
- **Federation of voice across peers** — `share_with` (B3) plumbing
  applies to evolution proposals, not session audio. A peer wanting
  to "hear" another tenant's session is a privacy hazard worth its
  own design doc.
