#!/usr/bin/env bash
# E2E: brings up newapi via docker compose, sets up a corlinman
# config that points at it, and exercises chat + embeddings + TTS.
#
# Pre-requisites:
#   - docker compose available
#   - corlinman-gateway already built (cargo build --release)
#   - jq (for response parsing)
#   - curl
#
# Note: this script assumes the operator has done the new-api root
# user setup at least once and stored a sk-… token in the env var
# NEWAPI_USER_TOKEN before running. The script does NOT automate
# new-api's first-run user creation — that's a one-time manual step
# in the new-api console.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

: "${NEWAPI_USER_TOKEN:?Set NEWAPI_USER_TOKEN before running}"
: "${NEWAPI_LLM_MODEL:=gpt-4o-mini}"
: "${NEWAPI_EMBED_MODEL:=text-embedding-3-small}"
: "${NEWAPI_TTS_MODEL:=tts-1}"
GATEWAY_PORT="${GATEWAY_PORT:-6005}"

echo "==> bringing up new-api compose"
docker compose -f docker/compose/newapi.yml up -d
cleanup() {
    echo "==> stopping new-api compose"
    docker compose -f docker/compose/newapi.yml down || true
    if [[ -n "${GW_PID:-}" ]] && kill -0 "$GW_PID" 2>/dev/null; then
        kill "$GW_PID" || true
    fi
    [[ -n "${TMP_CFG:-}" ]] && rm -f "$TMP_CFG" "$TMP_CFG.new" || true
}
trap cleanup EXIT

echo "==> waiting for new-api /api/status"
for _ in $(seq 1 60); do
    if curl -fsS http://localhost:3000/api/status >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

echo "==> generating temp corlinman config"
TMP_CFG="$(mktemp -t corlinman-e2e-XXXXXX).toml"
cat > "$TMP_CFG" <<EOF
[admin]
username = "e2e"
password_hash = "\$argon2id\$v=19\$m=19456,t=2,p=1\$placeholder\$placeholder"

[providers.newapi]
kind = "newapi"
base_url = "http://localhost:3000"
api_key = { value = "$NEWAPI_USER_TOKEN" }
enabled = true

[providers.newapi.params]
newapi_admin_url = "http://localhost:3000/api"

[models]
default = "$NEWAPI_LLM_MODEL"

[models.aliases."$NEWAPI_LLM_MODEL"]
model = "$NEWAPI_LLM_MODEL"
provider = "newapi"

[embedding]
enabled = true
provider = "newapi"
model = "$NEWAPI_EMBED_MODEL"
dimension = 1536
EOF

echo "==> starting gateway against $TMP_CFG"
CORLINMAN_CONFIG="$TMP_CFG" \
    ./target/release/corlinman-gateway start &
GW_PID=$!
sleep 5

echo "==> chat round-trip"
curl -fsS "http://localhost:${GATEWAY_PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$NEWAPI_LLM_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}],\"max_tokens\":4}" \
    | jq -e '.choices[0].message.content' >/dev/null
echo "    chat OK"

echo "==> embedding round-trip"
curl -fsS "http://localhost:${GATEWAY_PORT}/v1/embeddings" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$NEWAPI_EMBED_MODEL\",\"input\":\"hello\"}" \
    | jq -e '.data[0].embedding | length > 0' >/dev/null
echo "    embedding OK"

# TTS via newapi REST is exercised by hitting newapi directly here.
# corlinman's [voice] block still uses Realtime WebSocket so it can't
# proxy /v1/audio/speech yet; this hop verifies the REST endpoint is
# reachable through the same newapi instance for future migration.
echo "==> tts reachable via newapi (REST)"
curl -fsS "http://localhost:3000/v1/audio/speech" \
    -H "Authorization: Bearer $NEWAPI_USER_TOKEN" \
    -H "Content-Type: application/json" \
    -o /tmp/tts.bin \
    -d "{\"model\":\"$NEWAPI_TTS_MODEL\",\"input\":\"hello\",\"voice\":\"alloy\"}"
test -s /tmp/tts.bin
echo "    tts OK ($(wc -c </tmp/tts.bin) bytes)"

echo "==> all green"
