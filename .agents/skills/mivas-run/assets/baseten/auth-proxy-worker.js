// Cloudflare Worker: Bluejay (basic auth) → Baseten WebSocket model (bearer auth).
//
// Bluejay's WEBSOCKET agent can only send `Authorization: Basic user:pass`; Baseten's
// WebSocket endpoint wants `Authorization: Bearer <BASETEN_API_KEY>`. This Worker checks the
// basic credentials, swaps the header and proxies the upgrade. Everything else, including
// X-Simulation-Result-Id, passes through untouched.
//
// Vars (wrangler secret put …): CHIRP_USER, CHIRP_PASS, BASETEN_API_KEY,
//   BASETEN_WS_URL = wss://model-<id>.api.baseten.co/environments/production/websocket
// Bluejay agent: websocket_url = wss://<worker>.workers.dev, username/password = CHIRP_USER/PASS.

export default {
  async fetch(request, env) {
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return new Response("mivas baseten proxy\n", { status: 200 });
    }
    const expected = "Basic " + btoa(`${env.CHIRP_USER}:${env.CHIRP_PASS}`);
    if (request.headers.get("Authorization") !== expected) {
      return new Response("unauthorized\n", { status: 401 });
    }
    const upstream = new URL(env.BASETEN_WS_URL);
    const headers = new Headers(request.headers);
    headers.set("Authorization", `Bearer ${env.BASETEN_API_KEY}`);
    headers.set("Host", upstream.host);
    // A 101 response carries the upstream socket; returning it hands the pipe to Bluejay.
    return fetch(upstream.toString(), { method: "GET", headers });
  },
};
