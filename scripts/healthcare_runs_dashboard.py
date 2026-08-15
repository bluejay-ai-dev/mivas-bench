"""Live status board for the k8s healthcare 60-case runs that were queued.

    uv run python scripts/healthcare_runs_dashboard.py

Opens http://127.0.0.1:8765 — Reload hits Bluejay again (no cache).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("BLUEJAY_API_URL", "https://api.getbluejay.ai/v1").rstrip("/")
PORT = int(os.environ.get("MIVAS_DASH_PORT", "8765"))

# the 13 suites queued on 2026-08-14 (Pipecat / LiveKit / Nemotron left idle)
RUNS = [
    {"harness": "openai/realtime-2.1", "sim": 30513, "run": 232737},
    {"harness": "openai/realtime-2.1-mini", "sim": 30514, "run": 232750},
    {"harness": "gemini/flash-live-3.1", "sim": 30515, "run": 232739},
    {"harness": "gemini/2.5-flash-native-audio", "sim": 30516, "run": 232738},
    {"harness": "assemblyai/voice-agent", "sim": 30517, "run": 232751},
    {"harness": "deepgram/voice-agent", "sim": 30518, "run": 232752},
    {"harness": "elevenlabs/convai", "sim": 30519, "run": 232740},
    {"harness": "vapi/flux-gpt4.1-flash2.5", "sim": 30520, "run": 232754},
    {"harness": "retell/flux-gpt4.1-flash2.5", "sim": 30521, "run": 232753},
    {"harness": "cartesia/line", "sim": 30522, "run": 232756},
    {"harness": "bland/base", "sim": 30523, "run": 232757},
    {"harness": "grok/voice", "sim": 30526, "run": 232758},
    {"harness": "twilio/conversationrelay-gpt4.1", "sim": 30527, "run": 232759},
]

PENDING = {"INITIALIZING", "QUEUED", "PENDING", "IN_PROGRESS"}
RUNNING = {"RUNNING"}
EVALUATING = {"EVALUATING", "CONVERSATION_ENDED"}
COMPLETED = {"COMPLETED"}
NO_CONNECT = {"NO_ANSWER", "NO_CONNECTION", "SYSTEM_ERROR", "ERROR", "CANCELLED", "FAILED"}
TERMINAL = COMPLETED | NO_CONNECT


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def _get(path: str) -> dict:
    key = os.environ.get("BLUEJAY_API_KEY")
    if not key:
        raise RuntimeError("need BLUEJAY_API_KEY")
    req = urllib.request.Request(
        f"{API}/{path}",
        headers={"X-API-Key": key, "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def _metric_value(entry: dict):
    val = entry.get("response_value")
    if val is None:
        for k in (
            "yes_no_response", "quantitative_response", "pass_fail_response",
            "boolean_value", "int_value", "float_value", "value",
        ):
            if entry.get(k) is not None:
                val = entry[k]
                break
    return val


def _custom(result: dict) -> dict:
    evs = result.get("evaluations") or []
    ev = evs[0] if isinstance(evs, list) and evs else (evs if isinstance(evs, dict) else {})
    cm = (
        (ev.get("custom_metrics") if isinstance(ev, dict) else None)
        or (ev.get("custom_evals") if isinstance(ev, dict) else None)
        or result.get("custom_metrics")
        or []
    )
    if isinstance(cm, dict):
        cm = [
            dict(v, name=k) if isinstance(v, dict) else {"name": k, "value": v}
            for k, v in cm.items()
        ]
    out = {}
    for entry in cm if isinstance(cm, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("metric_name") or entry.get("name") or "").strip().lower()
        out[name] = _metric_value(entry)
    return out


def _yes(val) -> bool | None:
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0"):
        return False
    return None


def _tc(val) -> int | None:
    try:
        n = int(float(val))
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


def _metrics(result: dict) -> dict:
    out = {}
    for m in result.get("metrics") or []:
        if isinstance(m, dict) and m.get("name") is not None:
            out[str(m["name"])] = m.get("value")
    return out


def _duration_s(result: dict, mets: dict) -> float | None:
    # result.duration is seconds; metrics duration is milliseconds
    d = result.get("duration")
    if isinstance(d, (int, float)) and d > 0:
        return float(d)
    md = mets.get("duration")
    if isinstance(md, (int, float)) and md > 0:
        return float(md) / 1000.0 if md >= 1000 else float(md)
    return None


def _latency_ms(result: dict, mets: dict) -> float | None:
    val = mets.get("avg_agent_latency")
    if val is None:
        evs = result.get("evaluations") or []
        ev = evs[0] if isinstance(evs, list) and evs else (evs if isinstance(evs, dict) else {})
        if isinstance(ev, dict):
            val = ev.get("avg_agent_latency")
    try:
        n = float(val)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _mean(vals: list[float]) -> float | None:
    return (sum(vals) / len(vals)) if vals else None


def summarize_run(cfg: dict) -> dict:
    run_id = cfg["run"]
    try:
        body = _get(f"retrieve-simulation-results/{run_id}")
    except Exception as e:
        return {
            **cfg,
            "error": str(e)[:240],
            "total": 0,
            "url": f"https://app.getbluejay.ai/simulations/{cfg['sim']}/runs/{run_id}",
        }
    results = body.get("simulation_results") or body.get("results") or []
    counts = Counter(str(r.get("status") or "UNKNOWN") for r in results)
    n = len(results) or 1
    completed = sum(counts[s] for s in COMPLETED)
    pending = sum(counts[s] for s in PENDING)
    running = sum(counts[s] for s in RUNNING)
    evaluating = sum(counts[s] for s in EVALUATING)
    no_answer = counts.get("NO_ANSWER", 0)
    no_connection = counts.get("NO_CONNECTION", 0)
    errors = counts.get("ERROR", 0) + counts.get("SYSTEM_ERROR", 0) + counts.get("FAILED", 0)
    cancelled = counts.get("CANCELLED", 0)
    fail = no_answer + no_connection + errors + cancelled
    terminal = completed + fail

    with_trace = 0
    tc_vals: list[int] = []
    durs: list[float] = []
    lats: list[float] = []
    pe_yes = pe_n = 0
    for r in results:
        status = str(r.get("status") or "")
        tids = r.get("trace_ids") or []
        if status in COMPLETED and tids:
            with_trace += 1
        if status not in COMPLETED:
            continue
        mets = _metrics(r)
        dur = _duration_s(r, mets)
        if dur is not None:
            durs.append(dur)
        lat = _latency_ms(r, mets)
        if lat is not None:
            lats.append(lat)
        cm = _custom(r)
        tc = None
        pe = None
        for k, v in cm.items():
            if "task completion" in k:
                tc = _tc(v)
            elif "premature" in k:
                pe = _yes(v)
        if tc is not None:
            tc_vals.append(tc)
        if pe is not None:
            pe_n += 1
            if pe:
                pe_yes += 1

    conn_n = terminal
    connected = completed  # completed implies a conversation started
    conn_rate = (connected / conn_n) if conn_n else None
    trace_rate = (with_trace / completed) if completed else None
    alerts = []
    if no_connection >= 3:
        alerts.append(f"{no_connection} NO_CONNECTION")
    if no_answer >= 3:
        alerts.append(f"{no_answer} NO_ANSWER")
    if errors >= 3:
        alerts.append(f"{errors} ERROR/FAILED")
    if fail >= 3 and conn_n and fail / conn_n >= 0.10:
        alerts.append(f"connection failures {fail}/{conn_n} ({fail / conn_n:.0%})")
    if completed >= 10 and with_trace < completed * 0.8:
        alerts.append(f"missing traces on {completed - with_trace}/{completed} completed")

    return {
        **cfg,
        "error": None,
        "total": len(results),
        "counts": dict(counts),
        "pending": pending,
        "running": running,
        "evaluating": evaluating,
        "completed": completed,
        "fail": fail,
        "no_answer": no_answer,
        "no_connection": no_connection,
        "errors": errors,
        "cancelled": cancelled,
        "with_trace": with_trace,
        "trace_rate": trace_rate,
        "conn_rate": conn_rate,
        "tc_n": len(tc_vals),
        "tc_mean": (sum(tc_vals) / len(tc_vals)) if tc_vals else None,
        "tc_ge4": (sum(1 for v in tc_vals if v >= 4) / len(tc_vals)) if tc_vals else None,
        "pe_n": pe_n,
        "pe_rate": (pe_yes / pe_n) if pe_n else None,
        "dur_n": len(durs),
        "dur_mean": _mean(durs),
        "lat_n": len(lats),
        "lat_ms": _mean(lats),
        "alerts": alerts,
        "pct_complete": (completed / len(results)) if results else 0,
        "pct_failed": (fail / len(results)) if results else 0,
        "pct_settled": (terminal / len(results)) if results else 0,
        "pct_done": (completed / len(results)) if results else 0,
        "url": f"https://app.getbluejay.ai/simulations/{cfg['sim']}/runs/{run_id}",
    }


def snapshot() -> dict:
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(summarize_run, cfg): cfg for cfg in RUNS}
        for fut in as_completed(futs):
            rows.append(fut.result())
    rows.sort(key=lambda r: r["harness"])
    total = sum(r.get("total") or 0 for r in rows)
    completed = sum(r.get("completed") or 0 for r in rows)
    pending = sum(r.get("pending") or 0 for r in rows)
    running = sum(r.get("running") or 0 for r in rows)
    evaluating = sum(r.get("evaluating") or 0 for r in rows)
    fail = sum(r.get("fail") or 0 for r in rows)
    no_answer = sum(r.get("no_answer") or 0 for r in rows)
    no_connection = sum(r.get("no_connection") or 0 for r in rows)
    with_trace = sum(r.get("with_trace") or 0 for r in rows)
    tc_parts = [(r["tc_mean"], r["tc_n"]) for r in rows if r.get("tc_n")]
    tc_n = sum(n for _, n in tc_parts)
    tc_mean = (sum(m * n for m, n in tc_parts) / tc_n) if tc_n else None
    pe_n = sum(r.get("pe_n") or 0 for r in rows)
    pe_yes = sum((r["pe_rate"] or 0) * (r["pe_n"] or 0) for r in rows if r.get("pe_n"))
    dur_parts = [(r["dur_mean"], r["dur_n"]) for r in rows if r.get("dur_n")]
    dur_n = sum(n for _, n in dur_parts)
    dur_mean = (sum(m * n for m, n in dur_parts) / dur_n) if dur_n else None
    lat_parts = [(r["lat_ms"], r["lat_n"]) for r in rows if r.get("lat_n")]
    lat_n = sum(n for _, n in lat_parts)
    lat_ms = (sum(m * n for m, n in lat_parts) / lat_n) if lat_n else None
    alert_rows = [r for r in rows if r.get("alerts")]
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "runs": len(rows),
            "calls": total,
            "completed": completed,
            "pending": pending,
            "running": running,
            "evaluating": evaluating,
            "fail": fail,
            "no_answer": no_answer,
            "no_connection": no_connection,
            "with_trace": with_trace,
            "trace_rate": (with_trace / completed) if completed else None,
            "conn_rate": (completed / (completed + fail)) if (completed + fail) else None,
            "tc_mean": tc_mean,
            "tc_n": tc_n,
            "pe_rate": (pe_yes / pe_n) if pe_n else None,
            "pe_n": pe_n,
            "dur_mean": dur_mean,
            "dur_n": dur_n,
            "lat_ms": lat_ms,
            "lat_n": lat_n,
            "pct_complete": (completed / total) if total else 0,
            "pct_failed": (fail / total) if total else 0,
            "pct_settled": ((completed + fail) / total) if total else 0,
            "pct_done": (completed / total) if total else 0,
        },
        "alerts": [
            {"harness": r["harness"], "alerts": r["alerts"], "url": r["url"]}
            for r in alert_rows
        ],
        "rows": rows,
    }


HTML = r"""<!doctype html>
<meta charset="utf-8">
<title>MIVAS healthcare runs</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f6f6f4; --card: #fff; --ink: #222; --muted: #666; --line: #ddd;
    --good: #2f7d4a; --bad: #b42318; --warn: #9a6700; --brand: #0b6bcb;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #161616; --card: #1f1f1f; --ink: #eee; --muted: #aaa; --line: #333; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.45 ui-sans-serif, system-ui, sans-serif;
         background: var(--bg); color: var(--ink); }
  .wrap { max-width: 1360px; margin: 0 auto; padding: 28px 20px 64px; }
  header { display: flex; justify-content: space-between; gap: 16px;
           align-items: flex-end; flex-wrap: wrap; margin-bottom: 20px; }
  h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 13px; }
  button { background: var(--brand); color: #fff; border: 0; border-radius: 6px;
           padding: 8px 14px; font: inherit; cursor: pointer; }
  button:disabled { opacity: .5; cursor: wait; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
          gap: 10px; margin-bottom: 16px; }
  .kpi { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
         padding: 12px 14px; }
  .kpi .l { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  .kpi .v { font-size: 22px; font-variant-numeric: tabular-nums; margin-top: 2px; }
  .banner { border: 1px solid var(--bad); color: var(--bad); border-radius: 8px;
            padding: 12px 14px; margin-bottom: 16px; background: var(--card); }
  .banner.warn { border-color: var(--warn); color: var(--warn); }
  table { width: 100%; border-collapse: collapse; background: var(--card);
          border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
           font-variant-numeric: tabular-nums; vertical-align: top; }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
       font-weight: 600; }
  tr:last-child td { border-bottom: 0; }
  .bad { color: var(--bad); }
  .good { color: var(--good); }
  .muted { color: var(--muted); }
  a { color: var(--brand); }
  .bar { display: flex; height: 6px; background: var(--line); border-radius: 99px;
         overflow: hidden; margin-top: 4px; }
  .bar > i { display: block; height: 100%; background: var(--brand); }
  .bar > i.fail { background: var(--bad); }
</style>
<div class="wrap">
  <header>
    <div>
      <h1>MIVAS healthcare · queued k8s runs</h1>
      <div class="sub" id="meta">Loading live Bluejay data…</div>
    </div>
    <button id="reload" type="button">Reload</button>
  </header>
  <div id="alerts"></div>
  <div class="kpis" id="kpis"></div>
  <table>
    <thead>
      <tr>
        <th>Harness</th>
        <th>Progress</th>
        <th>Live</th>
        <th>Connected</th>
        <th>Traces</th>
        <th>Avg duration</th>
        <th>Avg latency</th>
        <th>Task completion</th>
        <th>Premature end</th>
        <th>Failures</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
</div>
<script>
const $ = (id) => document.getElementById(id);
const pct = (x) => x == null ? "—" : (x * 100).toFixed(0) + "%";
const num = (x, d=1) => x == null ? "—" : Number(x).toFixed(d);
const secs = (x) => x == null ? "—" : Math.round(x) + "s";
const lat = (x) => x == null ? "—" : (x / 1000).toFixed(1) + "s";
function load() {
  const btn = $("reload");
  btn.disabled = true;
  $("meta").textContent = "Fetching Bluejay…";
  fetch("/api.json?t=" + Date.now(), { cache: "no-store" })
    .then(r => r.json())
    .then(draw)
    .catch(err => { $("meta").textContent = "Fetch failed: " + err; })
    .finally(() => { btn.disabled = false; });
}
function draw(d) {
  const t = d.totals;
  const when = new Date(d.fetched_at).toLocaleString();
  $("meta").textContent = "Live from Bluejay · fetched " + when + " · " +
    t.completed + "/" + t.calls + " calls completed · Reload always re-queries the API.";
  $("kpis").innerHTML = [
    kpi("Calls completed", t.completed + " / " + t.calls, pct(t.pct_complete) + " completed · " + (t.fail || 0) + " failed"),
    kpi("Running now", t.running, t.evaluating + " evaluating"),
    kpi("Still queued", t.pending, ""),
    kpi("Connection rate", pct(t.conn_rate), (t.fail || 0) + " failed to connect"),
    kpi("Avg duration", secs(t.dur_mean), (t.dur_n || 0) + " completed calls"),
    kpi("Avg latency", lat(t.lat_ms), (t.lat_n || 0) + " completed calls"),
    kpi("Traces on completed", pct(t.trace_rate), t.with_trace + " with a trace"),
    kpi("Task completion", t.tc_mean == null ? "—" : num(t.tc_mean), t.tc_n + " scored"),
    kpi("Premature end", pct(t.pe_rate), t.pe_n + " scored"),
  ].join("");
  const alerts = d.alerts || [];
  const box = $("alerts");
  if (!alerts.length) {
    const fail = (t.no_connection || 0) + (t.no_answer || 0) + (t.fail || 0);
    box.innerHTML = fail
      ? '<div class="banner warn">Some connection failures exist but below the “bunch” threshold. See the Failures column.</div>'
      : "";
  } else {
    box.innerHTML = '<div class="banner"><b>Attention</b> — ' +
      alerts.map(a => '<a href="'+a.url+'">'+a.harness+'</a>: '+a.alerts.join("; ")).join("<br>") +
      "</div>";
  }
  $("rows").innerHTML = d.rows.map(row).join("");
}
function kpi(l, v, s) {
  return '<div class="kpi"><div class="l">'+l+'</div><div class="v">'+v+'</div>' +
         (s ? '<div class="sub">'+s+'</div>' : '') + '</div>';
}
function row(r) {
  if (r.error) {
    return '<tr><td>'+r.harness+'</td><td colspan="9" class="bad">'+r.error+'</td></tr>';
  }
  const failBits = [];
  if (r.no_connection) failBits.push(r.no_connection + " no conn");
  if (r.no_answer) failBits.push(r.no_answer + " no answer");
  if (r.errors) failBits.push(r.errors + " error");
  if (r.cancelled) failBits.push(r.cancelled + " cancelled");
  const fail = failBits.length ? '<span class="bad">'+failBits.join(", ")+'</span>' : '<span class="muted">0</span>';
  const live = (r.running || 0) + " run · " + (r.evaluating || 0) + " eval · " + (r.pending || 0) + " queued";
  const completePct = (r.pct_complete || 0) * 100;
  const failPct = (r.pct_failed || 0) * 100;
  return '<tr>' +
    '<td><a href="'+r.url+'">'+r.harness+'</a></td>' +
    '<td>'+r.completed+' / '+r.total+' completed ('+pct(r.pct_complete)+')' +
      '<div class="bar"><i style="width:'+completePct+'%"></i>' +
      (failPct ? '<i class="fail" style="width:'+failPct+'%"></i>' : '') +
      '</div></td>' +
    '<td class="muted">'+live+'</td>' +
    '<td>'+pct(r.conn_rate)+'</td>' +
    '<td>'+(r.completed ? (r.with_trace+' / '+r.completed) : "—")+'</td>' +
    '<td>'+secs(r.dur_mean)+'</td>' +
    '<td>'+lat(r.lat_ms)+'</td>' +
    '<td>'+(r.tc_mean == null ? "—" : num(r.tc_mean)+' <span class="muted">('+pct(r.tc_ge4)+' ≥4)</span>')+'</td>' +
    '<td>'+pct(r.pe_rate)+'</td>' +
    '<td>'+fail+'</td>' +
    '</tr>';
}
$("reload").onclick = load;
load();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, HTML.encode(), "text/html; charset=utf-8")
            return
        if path == "/api.json":
            try:
                payload = snapshot()
                body = json.dumps(payload).encode()
                self._send(200, body, "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        self._send(404, b"not found", "text/plain")


def main() -> int:
    _load_dotenv()
    if not os.environ.get("BLUEJAY_API_KEY"):
        print("need BLUEJAY_API_KEY in the environment or repo .env", file=sys.stderr)
        return 1
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"healthcare runs dashboard → http://127.0.0.1:{PORT}")
    print("Reload in the page re-queries Bluejay. Ctrl-C to stop.")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
