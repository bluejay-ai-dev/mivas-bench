#!/usr/bin/env bash
# Start industry tool server, wait for health, then run the harness agent.
set -euo pipefail

: "${VOICE_AGENT:?VOICE_AGENT is required}"
: "${INDUSTRY:?INDUSTRY is required}"

APP_ROOT="${APP_ROOT:-/app}"
INDUSTRY_DIR="${INDUSTRY_DIR:-${APP_ROOT}/industry}"
HARNESS_DIR="${HARNESS_DIR:-${APP_ROOT}/harness}"
TOOL_SERVER_PORT="${TOOL_SERVER_PORT:-8000}"
TOOL_SERVER_URL="${TOOL_SERVER_URL:-http://127.0.0.1:${TOOL_SERVER_PORT}}"
export MIVAS_DB_PATH="${MIVAS_DB_PATH:-/data/industry.db}"
export TOOL_SERVER_URL
export INDUSTRY_DIR

mkdir -p "$(dirname "$MIVAS_DB_PATH")"

echo "starting tool server (${INDUSTRY}) on :${TOOL_SERVER_PORT}"
python "${INDUSTRY_DIR}/tool_server.py" &
TOOL_PID=$!

cleanup() {
  kill "${TOOL_PID}" 2>/dev/null || true
}
trap cleanup EXIT

echo "waiting for tool server health"
for _ in $(seq 1 60); do
  if curl -sf "${TOOL_SERVER_URL}/health" >/dev/null; then
    break
  fi
  if ! kill -0 "${TOOL_PID}" 2>/dev/null; then
    echo "tool server exited before becoming healthy" >&2
    exit 1
  fi
  sleep 0.5
done

if ! curl -sf "${TOOL_SERVER_URL}/health" >/dev/null; then
  echo "tool server failed health check" >&2
  exit 1
fi

AGENT_ARGS=()
if [[ "${AGENT_CHECK:-}" == "1" ]]; then
  AGENT_ARGS+=(--check)
fi

echo "starting harness agent (${VOICE_AGENT})"
exec python "${HARNESS_DIR}/agent.py" "${INDUSTRY}" "${AGENT_ARGS[@]+"${AGENT_ARGS[@]}"}"
