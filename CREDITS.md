# Credits & Acknowledgments

`corlinman` stands on the shoulders of several open-source projects. This file
attributes the prior art whose ideas, protocols, or implementation details
shaped specific subsystems. All code in this repository is our own work; the
entries below mark where a concept, algorithm, or protocol shape was drawn
from external reference implementations.

## Upstream reference projects

### VCPToolBox
- **Structured block tool protocol** — the `<<<[TOOL_REQUEST]>>>` envelope with
  「始」「末」 argument delimiters and the fuzz-tested permissive parser in
  `rust/crates/corlinman-plugins/src/protocol/block.rs` derive from the
  tool-call syntax popularized by VCPToolBox.
- **Four-tier variable cascade** — the Tar / Var / Sar / fixed resolution order,
  `\{\{SarPromptN\}\}` model-conditional substitution, and TVStxt file layout in
  `python/packages/corlinman-agent/src/corlinman_agent/variables/` follow the
  same semantics.
- **Character card expansion (`{{角色}}`)** — the privileged-message prefix gate,
  single-agent / first-writer-wins mechanic, and circular-reference detection in
  `python/packages/corlinman-agent/src/corlinman_agent/agents/` follow the same
  "Tavern-style" character-card model.
- **EPA projection + Residual Pyramid** — the Energy-Perception-Action weighted
  PCA projection and the Gram-Schmidt residual decomposition in
  `python/packages/corlinman-tagmemo/` reproduce the math described by the
  TagMemo subsystem in this project.

Repository: `https://github.com/` — specific URL to be added before the first
public release.

### openclaw
- **Hook event bus shape** — the lifecycle event names
  (`message.received/sent/transcribed/preprocessed`, `session.patch`,
  `agent.bootstrap`, `gateway.startup`, `config.changed`) and the priority tier
  model in `rust/crates/corlinman-hooks/` echo openclaw's public plugin SDK.
- **Skills manifest format** — the YAML-frontmatter + Markdown body layout,
  `metadata.openclaw.{emoji,requires,install}` field names, and the
  `allowed-tools` list in `skills/*.md` files mirror openclaw's skills spec.
- **Channel abstraction vocabulary** — the send/edit/delete/typing/media verbs
  on the `Channel` trait in `rust/crates/corlinman-channels/src/lib.rs` follow
  openclaw's channel adapter model.

Repository: `https://github.com/` — specific URL to be added before the first
public release.

## Third-party libraries

corlinman depends on a broad set of well-maintained Rust, Python, and
JavaScript libraries. See `Cargo.toml`, `pyproject.toml`, and `ui/package.json`
for the authoritative dependency list with pinned versions and licenses. Notable
load-bearing components:

- **Rust** — `tokio`, `axum`, `tonic`, `tower`, `tracing`, `sqlx`, `serde`,
  `notify`, `arc-swap`, `prometheus`.
- **Python** — `numpy`, `scikit-learn`, `grpcio`, `pydantic`, `pytest`, `ruff`.
- **Frontend** — Next.js 15, React 19, Tailwind CSS, shadcn/ui, framer-motion,
  `@visx/*`, `@tanstack/react-query`, `cmdk`, `sonner`, `lucide-react`.

## License

Code in this repository is licensed under the terms in `LICENSE`. Third-party
code is attributed inline where vendored and listed under the authoritative
lockfiles (`Cargo.lock`, `uv.lock`, `pnpm-lock.yaml`) with their own licenses.

## Upstream reference projects

### Wei-Shaw/sub2api

We integrate [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) as a
sidecar process. corlinman registers a `ProviderKind::Sub2api` that dials
sub2api over HTTP — sub2api itself is not vendored, linked, or otherwise
combined with this binary. **License: LGPL-3.0-or-later.** Sidecar
deployment honours the LGPL boundary; see
`docs/design/sub2api-integration.md` for the architecture and licence
reasoning.
