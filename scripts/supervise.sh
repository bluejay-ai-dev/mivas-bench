#!/usr/bin/env bash
# Keep a chirp adapter (or any long-running harness process) alive for the length
# of a benchmark run.
#
# The adapters exit silently mid-run. When that happens the call's `finally` never
# posts trace_ids, the voice.call root span is never exported, and Bluejay ends up
# with a linked trace holding no spans — which reads exactly like "the agent called
# no tools" and quietly turns a strict criterion into a pass. A restart loop plus a
# preserved crash log is the cheap fix; the exit reason still needs a root cause.
#
#   scripts/supervise.sh <name> <cmd...>
#
# Logs to $MIVAS_LOG_DIR/<name>.log (default /tmp/mivas), one line per restart, and
# keeps the tail of each dead process's output in <name>.crash-<n>.log.
#
# ponytail: no systemd/pm2 for a benchmark rig. Ctrl-C or SIGTERM stops it.

set -uo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <name> <cmd...>" >&2
  exit 64
fi

name=$1
shift
log_dir=${MIVAS_LOG_DIR:-/tmp/mivas}
mkdir -p "$log_dir"
log="$log_dir/$name.log"
# 0 = restart forever
max_restarts=${MIVAS_MAX_RESTARTS:-0}
backoff=${MIVAS_RESTART_BACKOFF:-2}

child=""
stop() {
  [ -n "$child" ] && kill "$child" 2>/dev/null
  echo "$(date -u +%FT%TZ) supervise[$name] stopping" >> "$log"
  exit 0
}
trap stop INT TERM

n=0
while :; do
  n=$((n + 1))
  echo "$(date -u +%FT%TZ) supervise[$name] start #$n: $*" >> "$log"
  "$@" >> "$log" 2>&1 &
  child=$!
  wait "$child"
  code=$?
  child=""
  echo "$(date -u +%FT%TZ) supervise[$name] exited code=$code after start #$n" >> "$log"
  # keep the tail of what it said before dying — the exits leave no traceback
  tail -n 80 "$log" > "$log_dir/$name.crash-$n.log" 2>/dev/null

  if [ "$max_restarts" -ne 0 ] && [ "$n" -ge "$max_restarts" ]; then
    echo "$(date -u +%FT%TZ) supervise[$name] giving up after $n starts" >> "$log"
    exit "$code"
  fi
  sleep "$backoff"
done
