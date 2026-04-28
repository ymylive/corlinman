# corlinman-user-model

Phase 3 W3-B of the auto-evolution / 类人 cognition stream.

This package distils per-user traits (interests, tone preference,
recurring topics, preferences) out of recent session turns and exposes
them as `{{user.*}}` placeholders for the agent's prompt assembly.

Pipeline:

1. Read raw turns for a `session_id` out of the gateway-owned
   `sessions.sqlite`.
2. Run a lightweight regex-based redaction pass to strip the obvious PII
   (phone, email, ID number, URL) before anything leaves the box.
3. Hand the redacted transcript to an LLM with a strict "extract only
   stable user traits, no PII" prompt; expect a JSON list back.
4. Upsert each `(user_id, trait_kind, trait_value)` triple into
   `user_model.sqlite` — same triple already present means weighted
   confidence average + appended `session_id`, never plain overwrite.

The placeholder resolver exposes `user.interests`, `user.tone`,
`user.topics`, `user.preferences`, each returning a comma-joined string
of the top-k traits by confidence. It is *not* wired into
`context_assembler` here — that's a one-line follow-up in
`corlinman-agent`.

See `docs/design/phase3-roadmap.md` §5 (`user_model.sqlite`) and §6
(`[user_model]`).
