#!/usr/bin/env bash
# Start industry tool server, then either the CHIRP adapter (Bluejay) or agent.py.
set -euo pipefail

: "${HARNESS:=${VOICE_AGENT:-}}"
: "${HARNESS:?HARNESS is required (family/runtime, e.g. openai/realtime-2.1)}"
: "${INDUSTRY:?INDUSTRY is required}"

APP_ROOT="${APP_ROOT:-/app}"
INDUSTRY_DIR="${INDUSTRY_DIR:-${APP_ROOT}/industry}"
HARNESS_DIR="${HARNESS_DIR:-${APP_ROOT}/harness}"
TOOL_SERVER_PORT="${TOOL_SERVER_PORT:-8000}"
TOOL_SERVER_URL="${TOOL_SERVER_URL:-http://127.0.0.1:${TOOL_SERVER_PORT}}"
MIVAS_MODE="${MIVAS_MODE:-chirp}"
CHIRP_PORT="${CHIRP_PORT:-8765}"
export MIVAS_DB_PATH="${MIVAS_DB_PATH:-/data/industry.db}"
export TOOL_SERVER_URL
export INDUSTRY_DIR
export INDUSTRY
export HARNESS

# Derive Realtime model from harness runtime folder when unset.
HARNESS_RUNTIME="${HARNESS_RUNTIME:-$(basename "${HARNESS_DIR}")}"
case "${HARNESS_RUNTIME}" in
  realtime-2.1) : "${OPENAI_REALTIME_MODEL:=gpt-realtime-2.1}" ;;
  realtime-2.1-mini) : "${OPENAI_REALTIME_MODEL:=gpt-realtime-2.1-mini}" ;;
esac
export OPENAI_REALTIME_MODEL="${OPENAI_REALTIME_MODEL:-}"
export HARNESS_RUNTIME

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

if [[ "${AGENT_CHECK:-}" == "1" || "${MIVAS_MODE}" == "check" ]]; then
  echo "starting harness agent check (${HARNESS})"
  exec python "${HARNESS_DIR}/agent.py" "${INDUSTRY}" --check
fi

if [[ "${MIVAS_MODE}" == "chirp" && -f "${HARNESS_DIR}/adapters/chirp.py" ]]; then
  HARNESS_FAMILY="${HARNESS_FAMILY:-${HARNESS%%/*}}"
  CHIRP_ARGS=(--industry "${INDUSTRY}" --host 0.0.0.0 --port "${CHIRP_PORT}")
  case "${HARNESS_FAMILY}" in
    openai)
      if [[ -z "${OPENAI_REALTIME_MODEL}" ]]; then
        echo "OPENAI_REALTIME_MODEL unset (and no default for harness_runtime=${HARNESS_RUNTIME})" >&2
        exit 1
      fi
      CHIRP_ARGS+=(--model "${OPENAI_REALTIME_MODEL}")
      echo "starting CHIRP (${HARNESS} model=${OPENAI_REALTIME_MODEL}) on :${CHIRP_PORT}"
      ;;
    *)
      echo "starting CHIRP (${HARNESS}) on :${CHIRP_PORT}"
      ;;
  esac
  exec python "${HARNESS_DIR}/adapters/chirp.py" "${CHIRP_ARGS[@]}"
fi

HARNESS_FAMILY="${HARNESS_FAMILY:-${HARNESS%%/*}}"
HARNESS_FAMILY_DIR="${HARNESS_FAMILY_DIR:-${APP_ROOT}/harness}"

if [[ "${HARNESS_FAMILY}" == "livekit" ]]; then
  echo "starting LiveKit Cloud worker (${HARNESS})"
  exec python "${HARNESS_DIR}/agent.py" start
fi

if [[ "${HARNESS_FAMILY}" == "pipecat" ]]; then
  echo "starting Pipecat LiveKit Cloud worker (${HARNESS})"
  exec python "${HARNESS_FAMILY_DIR}/adapters/livekit_worker.py"
fi

echo "starting harness agent (${HARNESS}) mode=${MIVAS_MODE}"
exec python "${HARNESS_DIR}/agent.py" "${INDUSTRY}"
