#!/usr/bin/env bash
# Keep a cloudflared quick tunnel alive AND keep the Bluejay agent pointed at it.
#
# Quick tunnels die on their own ("control stream encountered a failure while serving")
# and come back with a *different* hostname, so every death costs a whole run to
# NO_CONNECTION unless the agent is repointed. This watches both halves: it restarts
# cloudflared when the URL stops answering, and PUTs the new wss:// URL onto the agent
# the moment it changes.
#
#   scripts/tunnel_keeper.sh <local_port> <agent_id>
#
# Needs BLUEJAY_API_KEY. Logs to $MIVAS_LOG_DIR/tunnel-keeper-<port>.log.
# ponytail: a named tunnel with a stable hostname would delete this script — worth doing
# if finance stays on a laptop, but that needs `cloudflared login` (interactive).

set -uo pipefail
port=${1:?usage: tunnel_keeper.sh <local_port> <agent_id>}
agent=${2:?usage: tunnel_keeper.sh <local_port> <agent_id>}
api=${BLUEJAY_API_URL:-https://api.getbluejay.ai/v1}
log_dir=${MIVAS_LOG_DIR:-/tmp/mivas}
mkdir -p "$log_dir"
log="$log_dir/tunnel-keeper-$port.log"
out="$log_dir/tunnel-$port.out"
current=""

say() { echo "$(date -u +%FT%TZ) $*" >> "$log"; }

start_tunnel() {
  pkill -f "cloudflared tunnel --url http://127.0.0.1:$port" 2>/dev/null
  sleep 2
  : > "$out"
  nohup cloudflared tunnel --url "http://127.0.0.1:$port" --no-autoupdate >> "$out" 2>&1 &
  for _ in $(seq 1 30); do
    sleep 2
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$out" | tail -1)
    [ -n "$url" ] && { echo "$url"; return 0; }
  done
  return 1
}

repoint() {
  local url=$1
  local wss="wss://${url#https://}"
  code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$api/update-agent" \
    -H "X-API-Key: $BLUEJAY_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"agent_id\":\"$agent\",\"websocket_url\":\"$wss\"}")
  say "repointed agent $agent -> $wss (http $code)"
}

while true; do
  # healthy when the tunnel answers the websocket upgrade check (426) or any 2xx/4xx
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 "${current:-http://127.0.0.1:$port}/" 2>/dev/null)
  if [ -z "$current" ] || [ "$code" = "000" ]; then
    say "tunnel unhealthy (code=$code) — restarting"
    if new=$(start_tunnel); then
      say "tunnel up: $new"
      if [ "$new" != "$current" ]; then current=$new; repoint "$current"; fi
    else
      say "tunnel failed to come up; retrying"
      sleep 10
    fi
  fi
  sleep 30
done
