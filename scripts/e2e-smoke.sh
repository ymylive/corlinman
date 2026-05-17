#!/usr/bin/env bash
# E2E smoke test: /v1/chat/completions non-stream + SSE against a mocked
# Anthropic provider. Does NOT hit any external network.
#
# Flow:
#   1. start corlinman-python-server with CORLINMAN_TEST_MOCK_PROVIDER set so
#      the agent servicer returns a canned response without touching SDKs;
#   2. start corlinman-gateway pointed at that server via CORLINMAN_PY_ADDR;
#   3. curl both non-streaming and streaming requests; verify fragments;
#   4. kill both processes; exit 0 on success, non-zero on failure.
#
# Intended to run in CI and locally. Timings allow for a first-run uv sync
# (workspace install may take ~30s on a cold cache).

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

PY_PORT="${E2E_PY_PORT:-50591}"
GW_PORT="${E2E_GW_PORT:-6091}"
MOCK_TEXT="hello world"
LOG_DIR="$(mktemp -d -t corlinman-e2e-XXXXXX)"
PY_LOG="${LOG_DIR}/python.log"
GW_LOG="${LOG_DIR}/gateway.log"
PY_PID=""
GW_PID=""
RESULT=0

cleanup() {
  if [[ -n "${PY_PID}" ]] && kill -0 "${PY_PID}" 2>/dev/null; then
    kill -TERM "${PY_PID}" 2>/dev/null || true
    wait "${PY_PID}" 2>/dev/null || true
  fi
  if [[ -n "${GW_PID}" ]] && kill -0 "${GW_PID}" 2>/dev/null; then
    kill -TERM "${GW_PID}" 2>/dev/null || true
    wait "${GW_PID}" 2>/dev/null || true
  fi
  if [[ ${RESULT} -ne 0 ]]; then
    echo "=== python server log (${PY_LOG}) ==="
    sed -n '1,200p' "${PY_LOG}" 2>/dev/null || true
    echo "=== gateway log (${GW_LOG}) ==="
    sed -n '1,200p' "${GW_LOG}" 2>/dev/null || true
  else
    rm -rf "${LOG_DIR}"
  fi
}
trap cleanup EXIT

wait_for_tcp() {
  local host="$1" port="$2" timeout="$3"
  local elapsed=0
  while ! (echo > "/dev/tcp/${host}/${port}") 2>/dev/null; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [[ ${elapsed} -ge ${timeout} ]]; then
      return 1
    fi
  done
  return 0
}

wait_for_http() {
  local url="$1" timeout="$2"
  local elapsed=0
  while ! curl -sf -o /dev/null "${url}"; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [[ ${elapsed} -ge ${timeout} ]]; then
      return 1
    fi
  done
  return 0
}

echo "== starting python agent server (port=${PY_PORT}) =="
(
  CORLINMAN_PY_ADDR="127.0.0.1:${PY_PORT}" \
  CORLINMAN_TEST_MOCK_PROVIDER="${MOCK_TEXT}" \
  ANTHROPIC_API_KEY="mock" \
    uv run --no-project --with-editable python/packages/corlinman-grpc \
                       --with-editable python/packages/corlinman-agent \
                       --with-editable python/packages/corlinman-providers \
                       --with-editable python/packages/corlinman-server \
      corlinman-python-server
) >"${PY_LOG}" 2>&1 &
PY_PID=$!

if ! wait_for_tcp "127.0.0.1" "${PY_PORT}" 30; then
  echo "FAIL: python server didn't open ${PY_PORT} within 30s"
  RESULT=1
  exit 1
fi
echo "python server up (pid=${PY_PID})"

echo "== starting gateway (port=${GW_PORT}) =="
(
  PORT="${GW_PORT}" \
  CORLINMAN_PY_ADDR="127.0.0.1:${PY_PORT}" \
    uv run corlinman-gateway --port "${GW_PORT}"
) >"${GW_LOG}" 2>&1 &
GW_PID=$!

if ! wait_for_http "http://127.0.0.1:${GW_PORT}/health" 60; then
  echo "FAIL: gateway /health didn't respond within 180s"
  RESULT=1
  exit 1
fi
echo "gateway up (pid=${GW_PID})"

echo "== step 1: non-streaming request =="
NON_STREAM_RESP=$(curl -sS -X POST \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-4-5","stream":false,"messages":[{"role":"user","content":"hi"}]}' \
  "http://127.0.0.1:${GW_PORT}/v1/chat/completions") || {
    echo "FAIL: curl non-stream exited non-zero"
    RESULT=1
    exit 1
  }
echo "non-stream response: ${NON_STREAM_RESP}"
if ! echo "${NON_STREAM_RESP}" | grep -q "hello world"; then
  echo "FAIL: non-stream body missing '${MOCK_TEXT}'"
  RESULT=1
  exit 1
fi
echo "non-stream OK"

echo "== step 2: streaming (SSE) request =="
STREAM_RESP=$(curl -sSN -X POST \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"model":"claude-sonnet-4-5","stream":true,"messages":[{"role":"user","content":"hi"}]}' \
  "http://127.0.0.1:${GW_PORT}/v1/chat/completions" \
  --max-time 15) || {
    # curl may exit non-zero if the stream is forcibly closed; we still check
    # the captured output below before declaring failure.
    :
  }
echo "stream response: ${STREAM_RESP}"
if ! echo "${STREAM_RESP}" | grep -q "^data:"; then
  echo "FAIL: stream body has no 'data:' events"
  RESULT=1
  exit 1
fi
if ! echo "${STREAM_RESP}" | grep -q "hello world"; then
  echo "FAIL: stream body missing mocked text '${MOCK_TEXT}'"
  RESULT=1
  exit 1
fi
if ! echo "${STREAM_RESP}" | grep -q "data: \[DONE\]"; then
  echo "FAIL: stream body missing 'data: [DONE]' sentinel"
  RESULT=1
  exit 1
fi
echo "stream OK"

echo "== e2e smoke PASSED =="
RESULT=0
exit 0
