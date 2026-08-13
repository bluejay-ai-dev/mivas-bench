"""Two-model comparison dashboard from two run CSVs.

    uv run python scripts/compare_dashboard.py 229xxx 229yyy -o docs/healthcare/compare.html

k=3 runs, so the headline is pass^k (cases passing every sample), not the sample pass
rate — verdicts on this suite churn ~38% between single samples, so a per-sample
percentage overstates what the model reliably does. Both are shown; pass^k leads.

Palette is Bluejay's own (app/globals.css), validated with the dataviz six checks:
light #1FA2FF/#EA580C, dark #0F94F0/#E8590C (brand blue sits above dark's L band, so
dark uses a deeper step of the same hue rather than a flip). The brand blue is
2.67:1 on cream, a sub-3:1 WARN, discharged by direct bar labels + the per-case table.
"""

from __future__ import annotations

import collections
import csv
import json
import re
import sys

AREAS = {
    "A1": "New-patient access", "A2": "Appointment management",
    "A3": "Coverage & benefits", "A4": "Cosmetic concierge",
    "A5": "Billing & payments", "A6": "Clinical & escalation",
}


def _f(row, key):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _yes(row, key):
    return str(row.get(key, "")).strip().lower() in ("true", "1")


def _int(row, key):
    try:
        return int(float(row[key] or 0))
    except (KeyError, TypeError, ValueError):
        return 0


def _tc(row):
    try:
        return int(float(row["custom_task_completion_1_5"]))
    except (KeyError, TypeError, ValueError):
        return None


def model(csv_path: str, label: str) -> dict:
    rows = list(csv.DictReader(open(csv_path)))
    # k=3: group the samples by case so pass^k and per-case consistency are computable
    by_case = collections.defaultdict(list)
    for r in rows:
        by_case[r["case"]].append(r)

    cases = []
    for case, samples in sorted(by_case.items()):
        # A sample measures nothing if it never connected OR if it completed with no
        # trace at all — the bridge took the socket but the upstream was rate-limited, so
        # it dies in 20-90s having produced no spans. Both are void, not model failures.
        # Note a trace with zero TOOL spans is NOT void: that is a model that chose to
        # call nothing, which is exactly what VoiceChat does and must be scored.
        live = [s for s in samples
                if str(s["status"]).upper() == "COMPLETED" and _int(s, "n_traces") > 0]
        passes = sum(1 for s in live if _yes(s, "goal_success"))
        cases.append({
            "case": case, "area": case[:2], "k": len(live), "void": len(samples) - len(live),
            "passes": passes,
            "allPass": bool(live) and passes == len(live),
            "miss": sorted({t for s in live for t in (s["tools_missing"] or "").split(";") if t}),
            "dur": _mean(_f(s, "builtin_duration") for s in live),
            "lat": _mean(_f(s, "builtin_avg_agent_latency") for s in live),
            "adh": _mean(_f(s, "tool_call_adherence") for s in live),
            "hoS": _mean(_f(s, "handoff_adherence_score") for s in live),
            "tc": _mean(_tc(s) for s in live),
            "why": next((s.get("goal_reasoning") or "" for s in live
                         if not _yes(s, "goal_success")), "")[:300],
        })

    live_rows = [r for r in rows
                 if str(r["status"]).upper() == "COMPLETED" and _int(r, "n_traces") > 0]
    scored = [c for c in cases if c["k"]]
    areas = []
    for key, name in AREAS.items():
        sub = [c for c in scored if c["area"] == key]
        if sub:
            areas.append({"key": key, "name": name, "n": len(sub),
                          "passK": sum(1 for c in sub if c["allPass"]),
                          "adh": _mean(c["adh"] for c in sub),
                          "tc": _mean(c["tc"] for c in sub)})

    return {
        "label": label,
        "kpi": {
            "cases": len(cases), "scored": len(scored),
            "passK": sum(1 for c in scored if c["allPass"]),
            "samples": len(live_rows), "dialed": len(rows),
            "samplePass": sum(1 for r in live_rows if _yes(r, "goal_success")),
            "void": len(rows) - len(live_rows),
            "dur": _mean(_f(r, "builtin_duration") for r in live_rows),
            "lat": _mean(_f(r, "builtin_avg_agent_latency") for r in live_rows),
            "adh": _mean(_f(r, "tool_call_adherence") for r in live_rows),
            "ho": _mean(_f(r, "handoff_adherence_score") for r in live_rows),
            "tc": _mean(_tc(r) for r in live_rows),
            "turns": _mean(_f(r, "builtin_num_turns") for r in live_rows),
            "clarity": _mean(_f(r, "builtin_agent_audio_clarity") for r in live_rows),
        },
        "areas": areas,
        "cases": cases,
        "miss": collections.Counter(
            t for r in live_rows for t in (r["tools_missing"] or "").split(";") if t
        ).most_common(10),
    }


HTML = r"""<title>MIVAS healthcare · Nemotron vs Nemotron VoiceChat</title>
<style>
  :root{
    color-scheme: light;
    --ground:#F9FAF8; --surface:#FDFDFB; --surface-2:#F4F5F1;
    --ink:#2D2D2D; --ink-2:#5C5C5C; --ink-3:#8C8C8C;
    --line:#D7DCDA; --line-2:#E7E5E4;
    --m1:#1FA2FF; --m2:#EA580C;
    --good:#5BB377; --bad:#EF4444;
    --shadow:0 1px 2px rgba(45,45,45,.05), 0 8px 24px -18px rgba(45,45,45,.2);
  }
  @media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
    color-scheme: dark;
    --ground:#1A1A19; --surface:#232322; --surface-2:#2C2C2A;
    --ink:#F2F3EF; --ink-2:#B4B4AE; --ink-3:#8C8C8C;
    --line:#3A3A37; --line-2:#454541;
    --m1:#0F94F0; --m2:#E8590C;
    --good:#5BB377; --bad:#EF4444;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -18px rgba(0,0,0,.6);
  }}
  :root[data-theme="dark"]{
    color-scheme: dark;
    --ground:#1A1A19; --surface:#232322; --surface-2:#2C2C2A;
    --ink:#F2F3EF; --ink-2:#B4B4AE; --ink-3:#8C8C8C;
    --line:#3A3A37; --line-2:#454541;
    --m1:#0F94F0; --m2:#E8590C;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -18px rgba(0,0,0,.6);
  }
  *{box-sizing:border-box}
  body{margin:0; background:var(--ground); color:var(--ink);
       font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
       -webkit-font-smoothing:antialiased}
  .num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-variant-numeric:tabular-nums}
  .wrap{max-width:1200px; margin:0 auto; padding:40px 24px 80px; display:flex; flex-direction:column; gap:26px}
  header{border-bottom:1px solid var(--line); padding-bottom:20px}
  .eyebrow{font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--ink-3)}
  h1{font-weight:600; font-size:27px; margin:6px 0 4px; letter-spacing:-.015em; text-wrap:balance}
  .sub{color:var(--ink-2); font-size:13.5px}
  .sub a{color:var(--m1)}
  .legend{display:flex; gap:18px; align-items:center; font-size:13px; color:var(--ink-2); margin-top:10px}
  .sw{width:10px; height:10px; border-radius:2px; display:inline-block; margin-right:7px; vertical-align:-1px}
  .tabs{display:flex; gap:6px; flex-wrap:wrap}
  .tabs button{font:inherit; font-size:13px; padding:7px 14px; border-radius:8px; cursor:pointer;
    background:var(--surface); color:var(--ink-2); border:1px solid var(--line)}
  .tabs button[aria-selected="true"]{background:var(--ink); color:var(--surface); border-color:var(--ink)}
  .tabs button:focus-visible, th:focus-visible{outline:2px solid var(--m1); outline-offset:2px}
  section{background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:20px 22px; box-shadow:var(--shadow)}
  section h2{font-size:12.5px; letter-spacing:.07em; text-transform:uppercase; color:var(--ink-2); margin:0 0 4px}
  section .hint{font-size:13px; color:var(--ink-3); margin:0 0 18px}
  .heroes{display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px}
  .hero{background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:18px 20px; box-shadow:var(--shadow)}
  .hero .who{font-size:12px; letter-spacing:.04em; color:var(--ink-2); display:flex; align-items:center}
  .hero .big{font-size:40px; letter-spacing:-.03em; margin-top:6px; line-height:1}
  .hero .big small{font-size:15px; color:var(--ink-2); letter-spacing:0}
  .hero .note{font-size:12.5px; color:var(--ink-3); margin-top:6px}
  .kpis{display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px}
  .kpi{border:1px solid var(--line); border-radius:9px; padding:11px 13px; background:var(--surface)}
  .kpi .k{font-size:10px; letter-spacing:.09em; text-transform:uppercase; color:var(--ink-3)}
  .kpi .row{display:flex; justify-content:space-between; align-items:baseline; margin-top:5px; font-size:14px}
  .arow{display:grid; grid-template-columns:184px 1fr; gap:14px; align-items:center; padding:8px 0}
  .aname{font-size:13.5px}
  .agrp{display:flex; flex-direction:column; gap:4px}
  .abar{height:18px; background:var(--surface-2); border-radius:4px; position:relative; overflow:hidden}
  .abar i{display:block; height:100%; border-radius:4px 4px 4px 4px}
  .abar b{position:absolute; right:8px; top:0; line-height:18px; font-size:11.5px; color:var(--ink-2);
          font-family:ui-monospace,monospace; font-weight:400}
  .strip{display:flex; flex-wrap:wrap; gap:3px}
  .cell{width:15px; height:15px; border-radius:3px; border:1px solid var(--line)}
  .tablewrap{overflow-x:auto; border:1px solid var(--line); border-radius:10px}
  table{border-collapse:collapse; width:100%; font-size:13px; min-width:900px}
  th,td{text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); white-space:nowrap}
  th{position:sticky; top:0; background:var(--surface-2); font-size:10.5px; letter-spacing:.07em;
     text-transform:uppercase; color:var(--ink-2); cursor:pointer; user-select:none; z-index:1}
  tbody tr:hover{background:var(--surface-2)}
  td.r{text-align:right}
  .pill{display:inline-flex; align-items:center; gap:5px; font-size:11.5px; padding:2px 8px;
        border-radius:99px; border:1px solid; font-family:ui-monospace,monospace}
  .pill.p{color:var(--good); border-color:color-mix(in srgb, var(--good) 45%, transparent)}
  .pill.f{color:var(--bad); border-color:color-mix(in srgb, var(--bad) 45%, transparent)}
  .miss{color:var(--bad); font-size:12px; font-family:ui-monospace,monospace}
  .note{font-size:13px; color:var(--ink-2); background:var(--surface-2); border-left:3px solid var(--line-2);
        padding:12px 14px; border-radius:0 8px 8px 0}
  .note b{color:var(--ink)}
  [hidden]{display:none !important}
  @media (max-width:640px){ .arow{grid-template-columns:1fr} }
</style>

<div class="wrap">
  <header>
    <div class="eyebrow">MIVAS bench · healthcare · Straus Dermatology "Robin"</div>
    <h1>NVIDIA Nemotron vs Nemotron VoiceChat</h1>
    <div class="sub num" id="sub"></div>
    <div class="legend">
      <span><i class="sw" style="background:var(--m1)"></i><span id="l1"></span></span>
      <span><i class="sw" style="background:var(--m2)"></i><span id="l2"></span></span>
    </div>
  </header>

  <div class="tabs" role="tablist" id="tabs"></div>

  <div id="pane-overview">
    <div class="heroes" id="heroes"></div>
    <section style="margin-top:26px">
      <h2>Where they differ</h2>
      <p class="hint">Same 60 digital humans, same industry pack, same judges. Lower is better for duration and latency; higher is better for the rest.</p>
      <div class="kpis" id="kpis"></div>
    </section>
    <section style="margin-top:26px">
      <h2>pass^k by caller-intent area</h2>
      <p class="hint">Cases that passed all three samples, out of the cases in that area. Counts are labelled on each bar.</p>
      <div id="areas"></div>
    </section>
    <section style="margin-top:26px">
      <h2>Most-skipped required tools</h2>
      <p class="hint">Expected tool absent from the trace, counted across every completed sample.</p>
      <div class="kpis" id="miss"></div>
    </section>
  </div>

  <div id="pane-consistency" hidden>
    <section>
      <h2>Per-case consistency across 3 samples</h2>
      <p class="hint">One square per case: filled means that model passed all three, half-tone means it passed some, hollow means none. Hover for the count.</p>
      <div id="strips"></div>
    </section>
  </div>

  <div id="pane-table" hidden>
    <section>
      <h2>Every case, both models</h2>
      <p class="hint">Click a column to sort. "3/3" is samples passed over samples that connected.</p>
      <div class="tablewrap"><table>
        <thead><tr>
          <th data-k="case">Case</th><th data-k="area">Area</th>
          <th data-k="p1" class="r">Nemotron</th><th data-k="p2" class="r">VoiceChat</th>
          <th data-k="a1" class="r">Adh 1</th><th data-k="a2" class="r">Adh 2</th>
          <th data-k="d1" class="r">Dur 1</th><th data-k="d2" class="r">Dur 2</th>
          <th data-k="t1" class="r">Task 1</th><th data-k="t2" class="r">Task 2</th>
        </tr></thead>
        <tbody id="tb"></tbody>
      </table></div>
    </section>
  </div>

  <div class="note" id="caveat"></div>
</div>

<script>
const D = __DATA__;
const [A, B] = D.models;
const pct = v => v == null ? "—" : Math.round(v * 100) + "%";
const s1 = v => v == null ? "—" : v.toFixed(1);
const secs = v => v == null ? "—" : Math.round(v) + "s";
const ms = v => v == null ? "—" : (v / 1000).toFixed(1) + "s";
const $ = s => document.querySelector(s);

$("#sub").textContent = `runs ${D.runs.join(" · ")} · k=3 · ${D.dialed} calls dialed per model · ${D.cap}s cap`;
$("#l1").textContent = A.label;
$("#l2").textContent = B.label;

$("#heroes").innerHTML = [A, B].map((m, i) => `
  <div class="hero">
    <div class="who"><i class="sw" style="background:var(--m${i + 1})"></i>${m.label}</div>
    <div class="big num">${m.kpi.passK}<small> / ${m.kpi.scored} cases</small></div>
    <div class="note">pass^k — passed all 3 samples. Sample rate ${m.kpi.samplePass}/${m.kpi.samples}
      (${Math.round(m.kpi.samplePass / Math.max(1, m.kpi.samples) * 100)}%)${
        m.kpi.void ? ` · ${m.kpi.void} void` : ""}</div>
  </div>`).join("");

const ROWS = [
  ["pass^k", m => `${m.kpi.passK}/${m.kpi.scored}`],
  ["sample pass", m => pct(m.kpi.samplePass / Math.max(1, m.kpi.samples))],
  ["tool adherence", m => pct(m.kpi.adh)],
  ["handoff adherence", m => pct(m.kpi.ho)],
  ["task completion", m => s1(m.kpi.tc) + " / 5"],
  ["avg duration", m => secs(m.kpi.dur)],
  ["avg latency", m => ms(m.kpi.lat)],
  ["turns", m => s1(m.kpi.turns)],
  ["audio clarity", m => s1(m.kpi.clarity)],
  ["connected", m => `${m.kpi.samples}/${m.kpi.dialed}`],
];
$("#kpis").innerHTML = ROWS.map(([k, f]) => `
  <div class="kpi"><div class="k">${k}</div>
    <div class="row"><span class="num" style="color:var(--m1)">${f(A)}</span>
                     <span class="num" style="color:var(--m2)">${f(B)}</span></div></div>`).join("");

const maxN = Math.max(...A.areas.map(a => a.n), 1);
$("#areas").innerHTML = A.areas.map((a, idx) => {
  const b = B.areas[idx] || {passK: 0, n: a.n};
  const bar = (v, n, i) => `<div class="abar">
      <i style="width:${Math.max(2, v / maxN * 100)}%; background:var(--m${i})"></i>
      <b>${v}/${n}</b></div>`;
  return `<div class="arow"><div class="aname">${a.name}</div>
    <div class="agrp">${bar(a.passK, a.n, 1)}${bar(b.passK, b.n, 2)}</div></div>`;
}).join("");

const missAll = {};
for (const [t, c] of A.miss) (missAll[t] = missAll[t] || [0, 0])[0] = c;
for (const [t, c] of B.miss) (missAll[t] = missAll[t] || [0, 0])[1] = c;
$("#miss").innerHTML = Object.entries(missAll)
  .sort((x, y) => (y[1][0] + y[1][1]) - (x[1][0] + x[1][1])).slice(0, 10)
  .map(([t, [c1, c2]]) => `<div class="kpi"><div class="k">${t}</div>
    <div class="row"><span class="num" style="color:var(--m1)">${c1}</span>
                     <span class="num" style="color:var(--m2)">${c2}</span></div></div>`).join("");

const byCase = m => Object.fromEntries(m.cases.map(c => [c.case, c]));
const CA = byCase(A), CB = byCase(B);
const allCases = [...new Set([...Object.keys(CA), ...Object.keys(CB)])].sort();

$("#strips").innerHTML = [A, B].map((m, i) => {
  const map = i ? CB : CA;
  return `<div style="margin-bottom:18px">
    <div class="who" style="font-size:12.5px; margin-bottom:8px">
      <i class="sw" style="background:var(--m${i + 1})"></i>${m.label}</div>
    <div class="strip">${allCases.map(k => {
      const c = map[k]; if (!c || !c.k) return `<div class="cell" title="${k}: no data"></div>`;
      const frac = c.passes / c.k;
      const op = frac === 1 ? 1 : frac === 0 ? 0.12 : 0.45;
      return `<div class="cell" title="${k}: ${c.passes}/${c.k} passed"
        style="background:var(--m${i + 1}); opacity:${op}"></div>`;
    }).join("")}</div></div>`;
}).join("");

const trow = k => {
  const a = CA[k] || {}, b = CB[k] || {};
  const cell = (c, i) => c.k ? `<span class="pill ${c.allPass ? "p" : "f"}">${c.passes}/${c.k}</span>` : "—";
  return {k, area: (a.area || b.area || ""),
    p1: a.k ? a.passes / a.k : -1, p2: b.k ? b.passes / b.k : -1,
    a1: a.adh, a2: b.adh, d1: a.dur, d2: b.dur, t1: a.tc, t2: b.tc,
    html: `<tr><td class="num">${k}</td><td>${(a.area || b.area || "")}</td>
      <td class="r">${cell(a, 1)}</td><td class="r">${cell(b, 2)}</td>
      <td class="r num">${pct(a.adh)}</td><td class="r num">${pct(b.adh)}</td>
      <td class="r num">${secs(a.dur)}</td><td class="r num">${secs(b.dur)}</td>
      <td class="r num">${s1(a.tc)}</td><td class="r num">${s1(b.tc)}</td></tr>`};
};
let rows = allCases.map(trow);
const draw = () => { $("#tb").innerHTML = rows.map(r => r.html).join(""); };
draw();
let sortKey = null, asc = true;
document.querySelectorAll("th[data-k]").forEach(th => th.addEventListener("click", () => {
  const k = th.dataset.k;
  asc = sortKey === k ? !asc : true; sortKey = k;
  rows.sort((x, y) => {
    const a = x[k] ?? -1, b = y[k] ?? -1;
    return (typeof a === "string" ? a.localeCompare(b) : a - b) * (asc ? 1 : -1);
  });
  document.querySelectorAll("th[data-k]").forEach(o => o.removeAttribute("aria-sort"));
  th.setAttribute("aria-sort", asc ? "ascending" : "descending");
  draw();
}));

const PANES = [["overview", "Overview"], ["consistency", "Consistency"], ["table", "Every case"]];
$("#tabs").innerHTML = PANES.map(([id, name], i) =>
  `<button role="tab" data-p="${id}" aria-selected="${i === 0}">${name}</button>`).join("");
$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-p]"); if (!b) return;
  PANES.forEach(([id]) => { $(`#pane-${id}`).hidden = id !== b.dataset.p; });
  document.querySelectorAll("#tabs button").forEach(o =>
    o.setAttribute("aria-selected", o === b));
});

$("#caveat").innerHTML = D.caveat;
</script>
"""


def main() -> int:
    a_run, b_run = sys.argv[1], sys.argv[2]
    out = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv else "docs/healthcare/compare.html"
    A = model(f"docs/healthcare/run_{a_run}.csv", "Nemotron (cascaded)")
    B = model(f"docs/healthcare/run_{b_run}.csv", "Nemotron VoiceChat (S2S)")
    data = {
        "models": [A, B], "runs": [a_run, b_run],
        "dialed": max(A["kpi"]["dialed"], B["kpi"]["dialed"]), "cap": 480,
        "caveat": (
            "<b>Read pass^k, not the sample rate.</b> Verdicts on this suite churn about 38% "
            "between single samples, so a per-sample percentage overstates what a model does "
            "reliably; pass^k counts only cases that passed all three. Calls that never "
            "connected are voided rather than scored as model failures — both stacks hit "
            "NVIDIA upstream <code>HTTP 429</code> rate limits, which is a quota fact about "
            "the endpoint, not a capability of the agent."
            "<br><br><b>VoiceChat's score is not a quality score.</b> Across every completed "
            "call it invoked <b>zero tools</b> and never left the reception agent — the "
            "industry tool server logged 0 dispatches for VoiceChat against 17 for the "
            "cascaded stack in the same window, and the harness's own <code>CALL END</code> "
            "line reports <code>tools=[] agent=reception</code>. Every case in this suite "
            "requires tool calls, so its near-zero result reflects a model that converses "
            "but does not call tools or hand off on a 7-agent pack, not a model that answers "
            "badly. The NVCF function-calling protocol is injected by the harness "
            "(<code>_FC_PROTOCOL</code>), and the same harness does call tools on the simpler "
            "control-industry pack, so the likely limit is pack scale: a ~4000-word prompt "
            "across seven agents with full tool catalogs."
        ),
    }
    html = HTML.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    open(out, "w").write(html)
    k = lambda m: f"{m['kpi']['passK']}/{m['kpi']['scored']} pass^k, {m['kpi']['samples']} samples"
    print(f"wrote {out}\n  {A['label']}: {k(A)}\n  {B['label']}: {k(B)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
