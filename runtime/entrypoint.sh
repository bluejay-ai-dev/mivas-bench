#!/usr/bin/env bash
# Combined pod: industry FastAPI on :8000, then CHIRP/agent in the same process tree.
# MIVAS_ROLE=tools / harness remain for one-off local debugging only.
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
MIVAS_ROLE="${MIVAS_ROLE:-combined}"
CHIRP_PORT="${CHIRP_PORT:-8765}"
export MIVAS_DB_PATH="${MIVAS_DB_PATH:-/data/industry.db}"
export PYTHONPATH="${APP_ROOT}/runtime${PYTHONPATH:+:${PYTHONPATH}}"
export TOOL_SERVER_URL
export INDUSTRY_DIR
export INDUSTRY
export HARNESS

HARNESS_RUNTIME="${HARNESS_RUNTIME:-$(basename "${HARNESS_DIR}")}"
case "${HARNESS_RUNTIME}" in
  realtime-2.1) : "${OPENAI_REALTIME_MODEL:=gpt-realtime-2.1}" ;;
  realtime-2.1-mini) : "${OPENAI_REALTIME_MODEL:=gpt-realtime-2.1-mini}" ;;
esac
export OPENAI_REALTIME_MODEL="${OPENAI_REALTIME_MODEL:-}"
export HARNESS_RUNTIME

mkdir -p "$(dirname "$MIVAS_DB_PATH")"

wait_for_tools() {
  local url="$1"
  echo "waiting for tool server health at ${url}"
  for _ in $(seq 1 120); do
    if curl -sf "${url}/health" >/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  echo "tool server failed health check at ${url}" >&2
  return 1
}

start_ingress() {
  local script="$1"
  local label="$2"
  HARNESS_FAMILY="${HARNESS_FAMILY:-${HARNESS%%/*}}"
  CHIRP_ARGS=(--industry "${INDUSTRY}" --host 0.0.0.0 --port "${CHIRP_PORT}")
  case "${HARNESS_FAMILY}" in
    openai)
      if [[ -z "${OPENAI_REALTIME_MODEL}" ]]; then
        echo "OPENAI_REALTIME_MODEL unset (and no default for harness_runtime=${HARNESS_RUNTIME})" >&2
        exit 1
      fi
      CHIRP_ARGS+=(--model "${OPENAI_REALTIME_MODEL}")
      echo "starting ${label} (${HARNESS} model=${OPENAI_REALTIME_MODEL}) on :${CHIRP_PORT}"
      ;;
    *)
      echo "starting ${label} (${HARNESS}) on :${CHIRP_PORT}"
      ;;
  esac
  exec python "${script}" "${CHIRP_ARGS[@]}"
}

start_harness() {
  if [[ "${AGENT_CHECK:-}" == "1" || "${MIVAS_MODE}" == "check" ]]; then
    echo "starting harness agent check (${HARNESS})"
    exec python "${HARNESS_DIR}/agent.py" "${INDUSTRY}" --check
  fi

  CR_ADAPTER="${HARNESS_DIR}/adapters/conversationrelay.py"
  CHIRP_ADAPTER="${HARNESS_DIR}/adapters/chirp.py"
  if [[ -f "${CR_ADAPTER}" && ( "${MIVAS_MODE}" == "conversationrelay" || "${MIVAS_MODE}" == "chirp" ) ]]; then
    start_ingress "${CR_ADAPTER}" "ConversationRelay"
  fi
  if [[ "${MIVAS_MODE}" == "chirp" && -f "${CHIRP_ADAPTER}" ]]; then
    start_ingress "${CHIRP_ADAPTER}" "CHIRP"
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
}

if [[ "${MIVAS_ROLE}" == "tools" ]]; then
  echo "starting tool server (${INDUSTRY}) on :${TOOL_SERVER_PORT} (role=tools)"
  exec python "${INDUSTRY_DIR}/tool_server.py"
fi

if [[ "${MIVAS_ROLE}" == "harness" ]]; then
  echo "starting harness health stub on :${TOOL_SERVER_PORT}"
  python "${APP_ROOT}/runtime/harness_health.py" &
  wait_for_tools "${TOOL_SERVER_URL}"
  start_harness
fi

echo "starting tool server (${INDUSTRY}) on :${TOOL_SERVER_PORT}"
python "${INDUSTRY_DIR}/tool_server.py" &
TOOL_PID=$!
cleanup() {
  kill "${TOOL_PID}" 2>/dev/null || true
}
trap cleanup EXIT
if ! wait_for_tools "${TOOL_SERVER_URL}"; then
  if ! kill -0 "${TOOL_PID}" 2>/dev/null; then
    echo "tool server exited before becoming healthy" >&2
  fi
  exit 1
fi
start_harness
