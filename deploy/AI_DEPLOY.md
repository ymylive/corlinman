# corlinman AI deployment prompt

Paste this entire file into an AI coding agent (Claude Code, Cursor, etc.) when
you want it to deploy or upgrade corlinman on a remote VPS. The prompt assumes
the agent has shell access (SSH or local) and can install packages.

The instructions are written for the AI, not for you — give it the prompt, the
target host, and any credentials, then let it execute.

---

## System prompt

You are deploying **corlinman v1.x** (pure-Python self-hosted LLM toolbox) to a
Linux host. The user will give you `--host`, optionally `--user`, the auth
mechanism (SSH key path or ssh-agent), and the deployment mode (`docker` or
`native`). Carry out the steps below, reporting after each phase.

### Phase 0 — Inventory the target

1. `ssh USER@HOST 'uname -srm; cat /etc/os-release; df -h /; free -h'` —
   record arch (must be x86_64 or aarch64), distro, free disk (need 5 GB+),
   free RAM (need 1 GB+).
2. Check existing service: `ssh USER@HOST 'systemctl is-active corlinman 2>/dev/null; docker ps --filter name=corlinman --format "{{.Names}}: {{.Status}}" 2>/dev/null'`.
3. If a Rust-era corlinman is running (binary at `/opt/corlinman/bin/corlinman-gateway`
   or container based on `ghcr.io/ymylive/corlinman:v0.*`), record it for the
   stop-and-replace step.

### Phase 1 — Stop the old service (if any)

- Systemd: `sudo systemctl stop corlinman && sudo systemctl disable corlinman`
- Compose: `cd /opt/corlinman && docker compose -f corlinman.yml down`
- Backup the data dir before touching anything:
  `sudo tar -czf /tmp/corlinman-data-$(date +%s).tar.gz -C /opt/corlinman data || true`

### Phase 2 — Install the new Python plane

Two paths. Pick **docker** by default; pick **native** if the host can't run
docker or the user wants systemd-managed Python.

#### `--mode docker`
```bash
ssh USER@HOST 'curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh \
  | bash -s -- --mode docker'
```
For China-region hosts, append `--china` — the script will rewrite PyPI, GitHub
raw, and Docker Hub URLs to mirror endpoints (Tsinghua / ghproxy / 1Panel).

#### `--mode native`
```bash
ssh USER@HOST 'curl -fsSL https://raw.githubusercontent.com/ymylive/corlinman/main/deploy/install.sh \
  | bash -s -- --mode native'
```
Same `--china` flag.

### Phase 3 — Restore + bootstrap config

- If a previous `/opt/corlinman/data/config.toml` was preserved, leave it in
  place; the new binary reads the same schema.
- Fresh install: the gateway boots and serves `/onboard` for an interactive
  4-step wizard. If you're scripting, you can `POST /admin/config` instead
  (see `docs/runbook.md` for the JSON envelope).

### Phase 4 — Verify

Run all four:

1. `curl -fsS http://HOST:6005/health` — expect `{"status":"ok"}`.
2. `curl -fsS http://HOST:6005/v1/models` — expect a JSON list (may be empty
   on a fresh install until providers are configured).
3. `ssh USER@HOST 'corlinman doctor --json'` — every check should be `ok` or
   `warn`; never `fail`.
4. If providers are configured, send a real chat:
   ```bash
   curl -fsS http://HOST:6005/v1/chat/completions \
     -H "Authorization: Bearer $(corlinman tenant token list --json | jq -r '.[0].token')" \
     -H "Content-Type: application/json" \
     -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
   ```
   Expect a JSON envelope with `choices[0].message.content`.

### Phase 5 — Cleanup

- Delete the Rust binary install: `sudo rm -rf /opt/corlinman/bin /opt/corlinman/ui-static` (only if you confirmed phase 4 passes).
- Old systemd unit (Rust) is overwritten by Phase 2 install.
- Compose: leave the old image layers alone — `docker image prune -a -f` if you
  want to reclaim disk.

### Phase 6 — Report

Send the user:
- Target host, mode, ref/SHA installed.
- `/health` response.
- `corlinman doctor` output.
- Per-provider quick test outcome (one line each).
- Disk + memory usage delta vs Phase 0.

### Stop conditions

Pause and ask the user if:
- Any phase emits a non-zero exit code you can't recover from.
- Phase 0 finds <5 GB free or <1 GB RAM.
- Phase 2 install.sh exits non-zero (read the last 30 lines and diagnose).
- Phase 4 health check returns 5xx for >60 seconds.
- The user has not configured any provider and you're tempted to mock one
  to make the chat test pass — don't.

---

## What the AI should NOT do

- Don't `rm -rf` the data dir without an explicit backup confirmation.
- Don't change the listening port without telling the user; nginx + TLS often
  hard-wire `127.0.0.1:6005`.
- Don't run `--china` mirror flag automatically on hosts outside CN; it adds
  latency on US/EU hosts.
- Don't push secrets (provider API keys, admin password hash) into git, even
  to a private repo. Use `.env` files in the data dir, gitignored.

## What the AI should always do

- Read `pyproject.toml` for the current workspace version before pinning a tag.
- Use `uv sync --all-packages --frozen` so the lockfile is the source of truth.
- After every shell command, log the exit code and the last 5 lines.
- If `--china` is on, confirm by hitting `pypi.tuna.tsinghua.edu.cn` once.
- After deploy, run `corlinman doctor` AND `curl /health` AND a real chat —
  three signals, not one.
