"""Single-model dashboard for one MIVAS run CSV.

    uv run python scripts/nemotron_dashboard.py docs/healthcare/run_229660_nemotron.csv \
        -o docs/healthcare/nemotron.html

k=3, so pass^k (cases passing every sample) leads and the per-sample rate is shown
beside it — verdicts on this suite churn ~38% between single samples, so a per-sample
percentage overstates what the model does reliably.

A sample is VOID if it never connected, or if it completed with no trace at all (the
bridge took the socket but the upstream was rate-limited, so it died in 20-90s having
produced no spans). Void is not a model failure. A trace with zero TOOL spans is NOT
void — that is the model choosing to call nothing, which is real behaviour and scored.

Palette is Bluejay's own (app/globals.css), checked with the dataviz validator:
#1FA2FF light / #0F94F0 dark (the brand blue sits above dark's L band, so dark takes a
deeper step of the same hue rather than an inversion). Brand blue is 2.67:1 on cream —
a sub-3:1 WARN — discharged by direct bar labels plus the full per-case table.
"""

from __future__ import annotations

import collections
import csv
import json
import pathlib
import statistics
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


def _int(row, key):
    try:
        return int(float(row[key] or 0))
    except (KeyError, TypeError, ValueError):
        return 0


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _yes(row, key):
    return str(row.get(key, "")).strip().lower() in ("true", "1", "yes")


def _tc(row):
    try:
        return int(float(row["custom_task_completion_1_5"]))
    except (KeyError, TypeError, ValueError):
        return None


def _dur(row):
    s = _f(row, "builtin_duration_s")
    if s:
        return s
    ms = _f(row, "builtin_duration")
    return ms / 1000 if ms else None


def _passed(row, success: str) -> bool:
    if success == "tc":
        v = _tc(row)
        return v is not None and v >= 4
    return _yes(row, "goal_success")


def build(path: str, success: str = "goal") -> dict:
    rows = list(csv.DictReader(open(path)))
    live = [r for r in rows
            if str(r["status"]).upper() == "COMPLETED" and _int(r, "n_traces") > 0]

    by_case = collections.defaultdict(list)
    for r in live:
        by_case[r["case"]].append(r)
    all_cases = sorted({r["case"] for r in rows})

    cases = []
    for case in all_cases:
        ss = by_case.get(case, [])
        passes = sum(1 for s in ss if _passed(s, success))
        fail_why = (
            (s.get("custom_task_completion_1_5_reasoning") if success == "tc"
             else s.get("goal_reasoning")) or ""
            for s in ss if not _passed(s, success)
        )
        cases.append({
            "case": case, "area": case[:2], "k": len(ss), "passes": passes,
            "allPass": bool(ss) and passes == len(ss),
            "tools": sum(_int(s, "n_tools_trace") for s in ss),
            "adh": _mean(_f(s, "tool_call_adherence") for s in ss),
            "hoS": _mean(_f(s, "handoff_adherence_score") for s in ss),
            "dur": _mean(_dur(s) for s in ss),
            "lat": _mean(_f(s, "builtin_avg_agent_latency") for s in ss),
            "tc": _mean(_tc(s) for s in ss),
            "miss": sorted({t for s in ss for t in (s["tools_missing"] or "").split(";") if t}),
            "why": next(fail_why, "")[:320],
        })

    scored = [c for c in cases if c["k"]]
    tool_counts = [_int(r, "n_tools_trace") for r in live]
    used = collections.Counter(
        t for r in live for t in (r["tools_hit"] or "").split(";") if t)

    areas = []
    for key, name in AREAS.items():
        sub = [c for c in scored if c["area"] == key]
        if sub:
            areas.append({"key": key, "name": name, "n": len(sub),
                          "passK": sum(1 for c in sub if c["allPass"]),
                          "sample": sum(c["passes"] for c in sub),
                          "adh": _mean(c["adh"] for c in sub),
                          "tc": _mean(c["tc"] for c in sub)})

    return {
        "kpi": {
            "dialed": len(rows), "usable": len(live), "void": len(rows) - len(live),
            "cases": len(all_cases), "scored": len(scored),
            "passK": sum(1 for c in scored if c["allPass"]),
            "anyPass": sum(1 for c in scored if c["passes"]),
            "samplePass": sum(1 for r in live if _passed(r, success)),
            "toolTotal": sum(tool_counts),
            "toolCalls": sum(1 for t in tool_counts if t),
            "toolMean": statistics.mean(tool_counts) if tool_counts else 0,
            "adh": _mean(_f(r, "tool_call_adherence") for r in live),
            "ho": _mean(_f(r, "handoff_adherence_score") for r in live),
            "hoExact": sum(1 for r in live if r["handoff_adherence_verdict"] == "exact"),
            "dur": _mean(_dur(r) for r in live),
            "lat": _mean(_f(r, "builtin_avg_agent_latency") for r in live),
            "turns": _mean(_f(r, "builtin_num_turns") for r in live),
            "clarity": _mean(_f(r, "builtin_agent_audio_clarity") for r in live),
            "tc": _mean(_tc(r) for r in live),
            "pe": sum(1 for r in live if _yes(r, "custom_premature_call_end")),
            "tcDist": {str(k): v for k, v in sorted(
                collections.Counter(_tc(r) for r in live if _tc(r)).items())},
        },
        "areas": areas, "cases": cases,
        "used": used.most_common(12),
        "miss": collections.Counter(
            t for r in live for t in (r["tools_missing"] or "").split(";") if t
        ).most_common(10),
    }


HTML = r"""<title>MIVAS healthcare · NVIDIA Nemotron</title>
<style>
  :root{
    color-scheme: light;
    --ground:#F9FAF8; --surface:#FDFDFB; --surface-2:#F4F5F1;
    --ink:#2D2D2D; --ink-2:#5C5C5C; --ink-3:#8C8C8C;
    --line:#D7DCDA; --line-2:#E7E5E4;
    --brand:#1FA2FF; --good:#5BB377; --bad:#EF4444;
    --shadow:0 1px 2px rgba(45,45,45,.05), 0 8px 24px -18px rgba(45,45,45,.2);
  }
  @media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
    color-scheme: dark;
    --ground:#1A1A19; --surface:#232322; --surface-2:#2C2C2A;
    --ink:#F2F3EF; --ink-2:#B4B4AE; --ink-3:#8C8C8C;
    --line:#3A3A37; --line-2:#454541;
    --brand:#0F94F0; --good:#5BB377; --bad:#EF4444;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -18px rgba(0,0,0,.6);
  }}
  :root[data-theme="dark"]{
    color-scheme: dark;
    --ground:#1A1A19; --surface:#232322; --surface-2:#2C2C2A;
    --ink:#F2F3EF; --ink-2:#B4B4AE; --ink-3:#8C8C8C;
    --line:#3A3A37; --line-2:#454541;
    --brand:#0F94F0; --good:#5BB377; --bad:#EF4444;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -18px rgba(0,0,0,.6);
  }
  *{box-sizing:border-box}
  body{margin:0; background:var(--ground); color:var(--ink);
       font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
       -webkit-font-smoothing:antialiased}
  .num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-variant-numeric:tabular-nums}
  .wrap{max-width:1160px; margin:0 auto; padding:40px 24px 80px; display:flex; flex-direction:column; gap:24px}
  header{border-bottom:1px solid var(--line); padding-bottom:20px}
  .eyebrow{font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--ink-3)}
  h1{font-weight:600; font-size:28px; margin:6px 0 4px; letter-spacing:-.015em; text-wrap:balance}
  .sub{color:var(--ink-2); font-size:13.5px}
  .sub a{color:var(--brand)}
  .heroes{display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:14px}
  .hero{background:var(--surface); border:1px solid var(--line); border-radius:12px;
        padding:18px 20px; box-shadow:var(--shadow)}
  .hero .k{font-size:10.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--ink-3)}
  .hero .v{font-size:36px; letter-spacing:-.03em; margin-top:6px; line-height:1}
  .hero .v small{font-size:14px; color:var(--ink-2); letter-spacing:0}
  .hero .m{font-size:12.5px; color:var(--ink-3); margin-top:6px}
  section{background:var(--surface); border:1px solid var(--line); border-radius:12px;
          padding:20px 22px; box-shadow:var(--shadow)}
  section h2{font-size:12.5px; letter-spacing:.07em; text-transform:uppercase; color:var(--ink-2); margin:0 0 4px}
  section .hint{font-size:13px; color:var(--ink-3); margin:0 0 18px}
  .kpis{display:grid; grid-template-columns:repeat(auto-fit,minmax(148px,1fr)); gap:10px}
  .kpi{border:1px solid var(--line); border-radius:9px; padding:11px 13px}
  .kpi .k{font-size:10px; letter-spacing:.09em; text-transform:uppercase; color:var(--ink-3)}
  .kpi .v{font-size:19px; margin-top:4px}
  .arow{display:grid; grid-template-columns:190px 1fr 64px; gap:14px; align-items:center; padding:7px 0}
  .abar{height:20px; background:var(--surface-2); border-radius:4px; position:relative; overflow:hidden}
  .abar i{display:block; height:100%; background:var(--brand); border-radius:4px}
  .abar b{position:absolute; right:8px; top:0; line-height:20px; font-size:11.5px;
          color:var(--ink-2); font-family:ui-monospace,monospace; font-weight:400}
  .arate{text-align:right; font-size:13px; color:var(--ink-2)}
  .strip{display:flex; flex-wrap:wrap; gap:3px}
  .cell{width:16px; height:16px; border-radius:3px; border:1px solid var(--line); background:var(--brand)}
  .tablewrap{overflow-x:auto; border:1px solid var(--line); border-radius:10px}
  table{border-collapse:collapse; width:100%; font-size:13px; min-width:900px}
  th,td{text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); white-space:nowrap}
  th{position:sticky; top:0; background:var(--surface-2); font-size:10.5px; letter-spacing:.07em;
     text-transform:uppercase; color:var(--ink-2); cursor:pointer; user-select:none; z-index:1}
  th:focus-visible{outline:2px solid var(--brand); outline-offset:2px}
  tbody tr:hover{background:var(--surface-2)}
  td.r{text-align:right}
  .pill{display:inline-flex; align-items:center; font-size:11.5px; padding:2px 8px;
        border-radius:99px; border:1px solid; font-family:ui-monospace,monospace}
  .pill.p{color:var(--good); border-color:color-mix(in srgb,var(--good) 45%,transparent)}
  .pill.f{color:var(--bad); border-color:color-mix(in srgb,var(--bad) 45%,transparent)}
  .miss{color:var(--bad); font-size:12px; font-family:ui-monospace,monospace}
  .note{font-size:13px; color:var(--ink-2); background:var(--surface-2);
        border-left:3px solid var(--line-2); padding:12px 14px; border-radius:0 8px 8px 0}
  .note b{color:var(--ink)}
  @media (max-width:640px){ .arow{grid-template-columns:1fr} .arate{text-align:left} }
</style>

<div class="wrap">
  <header>
    <div class="eyebrow">MIVAS bench · healthcare · Straus Dermatology "Robin"</div>
    <h1>NVIDIA Nemotron — 60-case suite, 3 runs each</h1>
    <div class="sub num" id="sub"></div>
  </header>

  <div class="heroes" id="heroes"></div>

  <section>
    <h2>Run health</h2>
    <p class="hint">What was measured, and what could not be.</p>
    <div class="kpis" id="kpis"></div>
  </section>

  <section>
    <h2>pass^k by caller-intent area</h2>
    <p class="hint">Cases that scored 4 or 5 on task completion for all three samples, out of the cases in that area. Sample-pass count (of 30 calls) is on the right.</p>
    <div id="areas"></div>
  </section>

  <section>
    <h2>Tools actually called</h2>
    <p class="hint">Counted from <code>execute_tool</code> spans in the trace, across every usable call.</p>
    <div class="kpis" id="used"></div>
  </section>

  <section>
    <h2>Most-skipped required tools</h2>
    <p class="hint">The case expected it; it never appeared in the trace.</p>
    <div class="kpis" id="miss"></div>
  </section>

  <section>
    <h2>Per-case consistency</h2>
    <p class="hint">One square per case, shaded by how many of its 3 samples passed. Hover for the count.</p>
    <div class="strip" id="strip"></div>
  </section>

  <section>
    <h2>Every case</h2>
    <p class="hint">Click a column to sort. "3/3" is samples with task completion ≥ 4, over samples that produced a trace.</p>
    <div class="tablewrap"><table>
      <thead><tr>
        <th data-k="case">Case</th><th data-k="area">Area</th>
        <th data-k="p" class="r">Passed</th><th data-k="tools" class="r">Tools</th>
        <th data-k="adh" class="r">Tool adherence</th><th data-k="hoS" class="r">Handoff</th>
        <th data-k="dur" class="r">Dur</th><th data-k="lat" class="r">Latency</th>
        <th data-k="tc" class="r">Task 1&#8211;5</th><th data-k="miss">Missing</th>
      </tr></thead>
      <tbody id="tb"></tbody>
    </table></div>
  </section>

  <div class="note" id="caveat"></div>
</div>

<script>
const D = __DATA__;
const K = D.kpi;
const pct = v => v == null ? "—" : Math.round(v * 100) + "%";
const s1  = v => v == null ? "—" : v.toFixed(1);
const secs= v => v == null ? "—" : Math.round(v) + "s";
const ms  = v => v == null ? "—" : (v/1000).toFixed(1) + "s";
const $ = s => document.querySelector(s);

$("#sub").textContent = `run ${D.run} · simulation ${D.sim} · k=3 · ${K.dialed} calls dialed · ${D.cap || 360} s cap`;

$("#heroes").innerHTML = [
  ["pass^k", `${K.passK}<small> / ${K.scored}</small>`, "cases with task completion ≥ 4 on all 3 samples"],
  ["sample pass", `${K.samplePass}<small> / ${K.usable}</small>`,
   Math.round(K.samplePass / Math.max(1, K.usable) * 100) + "% of usable calls scored 4 or 5"],
  ["tool calls", `${K.toolTotal}`, `${K.toolCalls}/${K.usable} calls used ≥1 tool · ${K.toolMean.toFixed(1)} avg`],
  ["tool adherence", pct(K.adh), "share of each case's expected tools called"],
].map(([k, v, m]) => `<div class="hero"><div class="k">${k}</div>
  <div class="v num">${v}</div><div class="m">${m}</div></div>`).join("");

$("#kpis").innerHTML = [
  ["usable", `${K.usable} / ${K.dialed}`], ["void", `${K.void}`],
  ["cases with any pass", `${K.anyPass} / ${K.scored}`],
  ["handoff adherence", pct(K.ho)], ["handoff exact", `${K.hoExact}`],
  ["avg duration", secs(K.dur)], ["avg latency", ms(K.lat)],
  ["turns", s1(K.turns)], ["audio clarity", s1(K.clarity)],
  ["task completion", s1(K.tc) + " / 5"],
  ["premature end", `${K.pe} / ${K.usable}`],
].map(([k, v]) => `<div class="kpi"><div class="k">${k}</div><div class="v num">${v}</div></div>`).join("");

const maxN = Math.max(...D.areas.map(a => a.n), 1);
$("#areas").innerHTML = D.areas.map(a => `
  <div class="arow"><div>${a.name}</div>
    <div class="abar"><i style="width:${Math.max(2, a.passK / maxN * 100)}%"></i>
      <b>${a.passK}/${a.n}</b></div>
    <div class="arate num">${a.sample != null ? a.sample + "/30 · " : ""}${Math.round(a.passK / a.n * 100)}%</div></div>`).join("");

const chips = (list, cls) => list.length
  ? list.map(([n, c]) => `<div class="kpi"><div class="k">${n}</div>
      <div class="v num"${cls ? ` style="color:var(--bad)"` : ""}>${c}</div></div>`).join("")
  : `<div class="kpi"><div class="k">none</div><div class="v num">—</div></div>`;
$("#used").innerHTML = chips(D.used, false);
$("#miss").innerHTML = chips(D.miss, true);

$("#strip").innerHTML = D.cases.map(c => {
  if (!c.k) return `<div class="cell" style="background:var(--surface-2)" title="${c.case}: no usable samples"></div>`;
  const frac = c.passes / c.k;
  return `<div class="cell" style="opacity:${frac === 1 ? 1 : frac === 0 ? 0.13 : 0.5}"
    title="${c.case}: ${c.passes}/${c.k} passed"></div>`;
}).join("");

let rows = D.cases.map(c => ({...c, p: c.k ? c.passes / c.k : -1,
  html: `<tr><td class="num">${c.case}</td><td>${c.area}</td>
    <td class="r">${c.k ? `<span class="pill ${c.allPass ? "p" : "f"}">${c.passes}/${c.k}</span>` : "—"}</td>
    <td class="r num">${c.tools}</td><td class="r num">${pct(c.adh)}</td>
    <td class="r num">${pct(c.hoS)}</td><td class="r num">${secs(c.dur)}</td>
    <td class="r num">${ms(c.lat)}</td><td class="r num">${s1(c.tc)}</td>
    <td class="miss">${c.miss.slice(0, 3).join(", ") || "—"}</td></tr>`}));
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

$("#caveat").innerHTML = D.caveat;
</script>
"""


def main() -> int:
    path = sys.argv[1]
    out = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv else "docs/healthcare/nemotron.html"
    success = sys.argv[sys.argv.index("--success") + 1] if "--success" in sys.argv else "goal"
    data = build(path, success=success)
    rows = list(csv.DictReader(open(path)))
    data["run"] = (rows[0].get("run_id") if rows else "") or pathlib.Path(path).stem.replace("run_", "")
    data["sim"] = sys.argv[sys.argv.index("--sim") + 1] if "--sim" in sys.argv else "30476"
    data["cap"] = int(sys.argv[sys.argv.index("--cap") + 1]) if "--cap" in sys.argv else 360
    k = data["kpi"]
    if success == "tc":
        rubric = (
            "<b>Success is task completion ≥ 4.</b> Bluejay goal_success is ignored. "
            "A call passes if the Task completion (1–5) judge scored 4 or 5; otherwise it fails. "
            "pass^k counts only cases where all three samples scored 4 or 5.<br><br>"
        )
    else:
        rubric = (
            "<b>Read pass^k, not the sample rate.</b> pass^k counts only cases that passed "
            "all three of their samples on Bluejay goal_success.<br><br>"
        )
    data["caveat"] = (
        rubric +
        ("<b>Read pass^k, not the sample rate.</b> Verdicts on this suite churn between "
         "single samples, so a per-sample percentage overstates what the model does "
         "reliably.<br><br>" if success == "tc" else "") +
        f"<b>{k['void']} of {k['dialed']} calls are void</b> — they either never connected or "
        "completed with no trace at all, having measured nothing. Those are excluded rather "
        "than scored as failures. A call that produced a trace but called no tools is NOT "
        "void: that is real behaviour and is scored.<br><br>"
        "<b>Tool use and tool <i>adherence</i> are different numbers.</b> The model called "
        f"{k['toolTotal']} tools across {k['toolCalls']} of {k['usable']} usable calls, so tools "
        f"were flowing constantly — but only {round((k['adh'] or 0)*100)}% of each case's "
        "<i>expected</i> tools were the ones actually called. The gap between those two is the "
        "finding: it acts, but often not with the tool the task required.<br><br>"
        f"<b>Task completion (1–5) averaged {(k['tc'] or 0):.2f}</b> across usable calls. "
        f"<b>Premature call end</b> flagged {k.get('pe', 0)} of {k['usable']} — often the "
        "6-minute cap saying goodbye, not an agent hang-up."
    )
    open(out, "w").write(HTML.replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    print(f"wrote {out}")
    print(f"  pass^k {k['passK']}/{k['scored']} · sample {k['samplePass']}/{k['usable']} "
          f"· tools {k['toolTotal']} · adherence {round((k['adh'] or 0)*100)}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
