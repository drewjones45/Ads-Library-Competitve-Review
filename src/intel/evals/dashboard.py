"""Eval HTML dashboard — mirrors the pattern of synthesis/dashboard.py.

Reads eval_runs + eval_task_results + llm_calls from the real db, emits a
single self-contained HTML file with: headline card, trend chart, per-task
grid, drill-down panel, cost panel, run selector.
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

# Rough Anthropic pricing ($/MTok). Used for ballpark cost in the dashboard.
# Source: anthropic.com pricing page snapshot — keep loose, this is sanity-check.
PRICING_USD_PER_MTOK = {
    "claude-opus-4-7":             {"in": 15.0, "out": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-6":           {"in":  3.0, "out": 15.0, "cache_read": 0.30, "cache_write":  3.75},
    "claude-haiku-4-5-20251001":   {"in":  0.80, "out": 4.0, "cache_read": 0.08, "cache_write":  1.00},
}


def _est_cost(model: str, in_tok: int, out_tok: int, cache_r: int = 0, cache_w: int = 0) -> float:
    p = PRICING_USD_PER_MTOK.get(model)
    if not p:
        # Default fallback bucket (Sonnet-ish)
        p = {"in": 3.0, "out": 15.0, "cache_read": 0.30, "cache_write": 3.75}
    return (
        in_tok    * p["in"]          / 1_000_000 +
        out_tok   * p["out"]         / 1_000_000 +
        cache_r   * p["cache_read"]  / 1_000_000 +
        cache_w   * p["cache_write"] / 1_000_000
    )


def _collect(db_path: Path, run_id: int | None = None) -> dict[str, Any]:
    """Pull data needed by the dashboard. If run_id is None, use the latest run."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # All runs (oldest → newest) for the trend chart.
        runs = [dict(r) for r in conn.execute(
            "SELECT id, started_at, finished_at, git_sha, task_count, pass_count, "
            "pass_slow_count, fail_count, skip_count, headline_score, notes "
            "FROM eval_runs ORDER BY id ASC"
        ).fetchall()]
        if not runs:
            return {"runs": [], "current": None, "tasks": [], "calls": []}

        # Pick run (default = latest)
        if run_id is None:
            run_id = runs[-1]["id"]
        current = next((r for r in runs if r["id"] == run_id), runs[-1])

        # Tasks for the chosen run.
        tasks = [dict(r) for r in conn.execute(
            "SELECT id, task_id, title, category, status, elapsed_ms, "
            "graders_json, output_json, error, skipped_reason "
            "FROM eval_task_results WHERE eval_run_id=? ORDER BY task_id",
            (run_id,),
        ).fetchall()]
        # Parse JSON blobs once.
        for t in tasks:
            try:
                t["graders"] = json.loads(t.pop("graders_json") or "[]")
            except Exception:
                t["graders"] = []
            try:
                raw_out = t.pop("output_json")
                t["output"] = json.loads(raw_out) if raw_out else None
            except Exception:
                t["output"] = None

        # LLM calls for the chosen run (Phase 1+ populates this).
        calls = [dict(r) for r in conn.execute(
            "SELECT call_site, model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens, latency_ms, success, error, task_id "
            "FROM llm_calls WHERE eval_run_id=? ORDER BY ts ASC",
            (run_id,),
        ).fetchall()]

    return {"runs": runs, "current": current, "tasks": tasks, "calls": calls}


_CSS = """
:root {
  --bg: #f6f7fb; --panel: #ffffff; --border: #e3e5ec; --text: #1f2c4a;
  --muted: #6b7280; --green: #16a34a; --yellow: #d97706; --red: #dc2626;
  --skip: #94a3b8; --brand: #5a9fdf; --brand-dark: #1f2c4a;
}
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: var(--bg); color: var(--text); }
header { background: linear-gradient(135deg, #1f2c4a 0%, #2d4373 100%); color: white;
         padding: 22px 32px; }
header h1 { margin: 0 0 4px; font-size: 22px; }
header .meta { opacity: 0.7; font-size: 12px; }
main { padding: 24px 32px 80px; max-width: 1320px; }
section { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
          padding: 20px; margin-bottom: 20px; }
section h2 { margin: 0 0 14px; font-size: 16px; letter-spacing: 0.02em; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }

/* Headline card */
.headline { display: grid; grid-template-columns: auto 1fr; gap: 28px; align-items: center; }
.score-big { font-size: 56px; font-weight: 700; line-height: 1; }
.score-delta { font-size: 14px; margin-left: 10px; }
.score-delta.up { color: var(--green); }
.score-delta.down { color: var(--red); }
.score-delta.flat { color: var(--muted); }
.headline .counts { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.headline .counts .pill { padding: 8px 12px; border-radius: 6px; background: #f1f3f7; text-align: center; }
.headline .counts .pill .n { font-size: 22px; font-weight: 600; }
.headline .counts .pill .lbl { font-size: 11px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.1em; }
.headline .counts .pill.pass .n { color: var(--green); }
.headline .counts .pill.slow .n { color: var(--yellow); }
.headline .counts .pill.fail .n { color: var(--red); }
.headline .counts .pill.skip .n { color: var(--skip); }

/* Trend chart */
.trend-wrap { position: relative; }
.trend-svg { width: 100%; height: 180px; display: block; }
.trend-axis { stroke: var(--border); stroke-width: 1; }
.trend-line { fill: none; stroke: var(--brand); stroke-width: 2.5; }
.trend-pt { fill: var(--brand); }
.trend-pt:hover { fill: var(--brand-dark); cursor: pointer; }
.trend-pt.current { fill: var(--brand-dark); stroke: white; stroke-width: 2; }
.trend-label { font-size: 10px; fill: var(--muted); }

/* Per-task grid */
.tasks-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
.task-tile { padding: 12px; border-radius: 8px; border: 1px solid var(--border);
             cursor: pointer; transition: transform 0.08s; background: white; }
.task-tile:hover { transform: translateY(-2px); border-color: var(--brand); }
.task-tile.active { border-color: var(--brand-dark); box-shadow: 0 0 0 2px rgba(31,44,74,0.15); }
.task-tile .id { font-weight: 600; font-size: 13px; }
.task-tile .status { font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em;
                     padding: 2px 6px; border-radius: 3px; display: inline-block; margin-top: 2px; }
.task-tile.PASS { background: #ecfdf5; }
.task-tile.PASS .status { background: var(--green); color: white; }
.task-tile.PASS_SLOW { background: #fef3c7; }
.task-tile.PASS_SLOW .status { background: var(--yellow); color: white; }
.task-tile.FAIL, .task-tile.ERROR { background: #fef2f2; }
.task-tile.FAIL .status, .task-tile.ERROR .status { background: var(--red); color: white; }
.task-tile.SKIP { background: #f8fafc; opacity: 0.7; }
.task-tile.SKIP .status { background: var(--skip); color: white; }
.task-tile .title { font-size: 11px; color: var(--muted); margin-top: 6px;
                    line-height: 1.3; min-height: 28px; }
.task-tile .meta-row { font-size: 10px; color: var(--muted); margin-top: 4px; }

/* Filters */
.filters { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
.filters .chip { padding: 4px 10px; border-radius: 12px; background: #eef0f4;
                 cursor: pointer; font-size: 12px; user-select: none; }
.filters .chip.active { background: var(--brand-dark); color: white; }
.filters .chip:hover { background: var(--brand); color: white; }
.filters .label { font-size: 11px; color: var(--muted); text-transform: uppercase;
                  letter-spacing: 0.1em; align-self: center; margin-right: 4px; }

/* Drill-down panel */
.drill { background: #f8fafc; border-radius: 8px; padding: 18px; }
.drill h3 { margin: 0 0 4px; font-size: 16px; }
.drill .subtitle { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
.drill .graders-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.drill .graders-table th, .drill .graders-table td { padding: 6px 8px; text-align: left;
                                                       border-bottom: 1px solid var(--border); }
.drill .graders-table th { font-weight: 600; color: var(--muted); font-size: 11px;
                            text-transform: uppercase; letter-spacing: 0.08em; }
.drill .graders-table .pass { color: var(--green); }
.drill .graders-table .fail { color: var(--red); }
.drill .graders-table .speed { color: var(--yellow); font-size: 10px; margin-left: 4px; }
.drill .output { background: white; padding: 12px; border-radius: 6px; border: 1px solid var(--border);
                 font-family: ui-monospace, SFMono-Regular, monospace; font-size: 11px;
                 max-height: 400px; overflow: auto; white-space: pre-wrap; word-break: break-word; }
.drill .err { background: #fef2f2; color: #991b1b; padding: 10px; border-radius: 6px; font-size: 12px;
              font-family: ui-monospace, monospace; white-space: pre-wrap; }

/* Cost panel */
.cost-grid { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr 1fr; gap: 4px;
             font-size: 12px; }
.cost-grid .h { font-weight: 600; color: var(--muted); font-size: 10px;
                text-transform: uppercase; letter-spacing: 0.1em; padding: 6px; border-bottom: 1px solid var(--border); }
.cost-grid .cell { padding: 6px; border-bottom: 1px solid var(--border); }
.cost-grid .num { text-align: right; font-variant-numeric: tabular-nums; }
.cost-total { font-weight: 600; }

/* Run selector */
.runsel { float: right; font-size: 12px; }
.runsel select { padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px; font-size: 12px; }

/* Empty state */
.empty { text-align: center; padding: 40px; color: var(--muted); }
"""


_JS = """
const DATA = JSON.parse(document.getElementById('eval-data').textContent);
const runs = DATA.runs;
const current = DATA.current;
const tasks = DATA.tasks;
const calls = DATA.calls;
const pricing = DATA.pricing;

// ---- run selector ----
const sel = document.getElementById('run-select');
if (sel) {
  runs.slice().reverse().forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    const score = (r.headline_score * 100).toFixed(0);
    opt.textContent = `#${r.id} · ${r.started_at.slice(0,16).replace('T',' ')} · ${score}%`;
    if (current && r.id === current.id) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener('change', () => {
    const url = new URL(window.location.href);
    url.searchParams.set('run', sel.value);
    // Without a server we can't re-render; for now show a hint:
    alert('To switch runs, run `intel evals dashboard --run-id ' + sel.value + '`');
  });
}

// ---- trend chart ----
(function drawTrend() {
  const svg = document.getElementById('trend');
  if (!svg || runs.length === 0) return;
  const W = svg.clientWidth || 800, H = 180;
  const pad = {l: 36, r: 16, t: 14, b: 24};
  const innerW = W - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const xFor = i => pad.l + (runs.length === 1 ? innerW/2 : (i / (runs.length - 1)) * innerW);
  const yFor = s => pad.t + (1 - Math.max(0, Math.min(1, s))) * innerH;

  // gridlines
  let g = '';
  for (let v = 0; v <= 1; v += 0.25) {
    const y = yFor(v);
    g += `<line class="trend-axis" x1="${pad.l}" x2="${W-pad.r}" y1="${y}" y2="${y}"/>`;
    g += `<text class="trend-label" x="4" y="${y+3}">${(v*100).toFixed(0)}%</text>`;
  }
  // line + points
  const pts = runs.map((r, i) => `${xFor(i)},${yFor(r.headline_score)}`).join(' ');
  g += `<polyline class="trend-line" points="${pts}"/>`;
  runs.forEach((r, i) => {
    const cls = (current && r.id === current.id) ? 'trend-pt current' : 'trend-pt';
    g += `<circle class="${cls}" cx="${xFor(i)}" cy="${yFor(r.headline_score)}" r="5">
            <title>Run #${r.id} · ${(r.headline_score*100).toFixed(1)}% · ${r.started_at}</title>
          </circle>`;
  });
  if (runs.length > 1) {
    g += `<text class="trend-label" x="${xFor(0)}" y="${H-6}" text-anchor="start">#${runs[0].id}</text>`;
    g += `<text class="trend-label" x="${xFor(runs.length-1)}" y="${H-6}" text-anchor="end">#${runs[runs.length-1].id}</text>`;
  }
  svg.innerHTML = g;
})();

// ---- filters + tile rendering ----
const state = { status: new Set(), category: new Set() };
const STATUSES = ['PASS','PASS_SLOW','FAIL','ERROR','SKIP'];
const CATS = ['regression','failure_mode'];

function renderFilters() {
  const sBar = document.getElementById('filter-status');
  const cBar = document.getElementById('filter-category');
  if (!sBar || !cBar) return;
  sBar.innerHTML = '<span class="label">status</span>' +
    STATUSES.map(s => {
      const active = state.status.has(s) ? ' active' : '';
      return `<span class="chip${active}" data-s="${s}">${s.toLowerCase()}</span>`;
    }).join('');
  cBar.innerHTML = '<span class="label">type</span>' +
    CATS.map(c => {
      const active = state.category.has(c) ? ' active' : '';
      const label = c === 'failure_mode' ? 'failure mode' : c;
      return `<span class="chip${active}" data-c="${c}">${label}</span>`;
    }).join('');
  sBar.querySelectorAll('.chip').forEach(el => el.addEventListener('click', () => {
    const v = el.dataset.s;
    state.status.has(v) ? state.status.delete(v) : state.status.add(v);
    renderFilters(); renderTiles();
  }));
  cBar.querySelectorAll('.chip').forEach(el => el.addEventListener('click', () => {
    const v = el.dataset.c;
    state.category.has(v) ? state.category.delete(v) : state.category.add(v);
    renderFilters(); renderTiles();
  }));
}

let activeTaskId = tasks.length ? tasks[0].task_id : null;

function renderTiles() {
  const grid = document.getElementById('tasks-grid');
  if (!grid) return;
  const filtered = tasks.filter(t =>
    (state.status.size === 0 || state.status.has(t.status)) &&
    (state.category.size === 0 || state.category.has(t.category))
  );
  if (filtered.length === 0) {
    grid.innerHTML = '<div class="empty">No tasks match current filters.</div>';
    return;
  }
  grid.innerHTML = filtered.map(t => {
    const active = (t.task_id === activeTaskId) ? ' active' : '';
    return `
      <div class="task-tile ${t.status}${active}" data-id="${t.task_id}">
        <div><span class="id">${t.task_id}</span></div>
        <div><span class="status">${t.status}</span></div>
        <div class="title">${escapeHtml(t.title)}</div>
        <div class="meta-row">${t.elapsed_ms}ms · ${t.category.replace('_',' ')}</div>
      </div>`;
  }).join('');
  grid.querySelectorAll('.task-tile').forEach(el => {
    el.addEventListener('click', () => {
      activeTaskId = el.dataset.id;
      renderTiles();
      renderDrill();
    });
  });
}

function renderDrill() {
  const box = document.getElementById('drill');
  if (!box) return;
  const t = tasks.find(x => x.task_id === activeTaskId);
  if (!t) { box.innerHTML = '<div class="empty">Click a task tile to inspect.</div>'; return; }
  let h = `<h3>${escapeHtml(t.task_id)} · ${escapeHtml(t.title)}</h3>`;
  h += `<div class="subtitle">${t.category.replace('_',' ')} · ${t.status} · ${t.elapsed_ms}ms`;
  if (t.skipped_reason) h += ` · skipped: ${escapeHtml(t.skipped_reason)}`;
  h += `</div>`;

  if (t.graders && t.graders.length) {
    h += `<table class="graders-table"><thead><tr><th>grader</th><th>verdict</th><th>detail</th></tr></thead><tbody>`;
    t.graders.forEach(g => {
      const cls = g.passed ? 'pass' : 'fail';
      const speed = g.is_speed ? '<span class="speed">SPEED</span>' : '';
      h += `<tr><td>${escapeHtml(g.name)}${speed}</td>` +
           `<td class="${cls}">${g.passed ? 'PASS' : 'FAIL'}</td>` +
           `<td>${escapeHtml(g.detail || '')}</td></tr>`;
    });
    h += `</tbody></table>`;
  }

  if (t.error) {
    h += `<h4 style="margin-top:18px;">Error</h4><pre class="err">${escapeHtml(t.error)}</pre>`;
  }

  if (t.output !== null && t.output !== undefined) {
    h += `<h4 style="margin-top:18px;">Output</h4>`;
    const pretty = typeof t.output === 'string' ? t.output : JSON.stringify(t.output, null, 2);
    h += `<pre class="output">${escapeHtml(pretty)}</pre>`;
  }

  // LLM calls for this task
  const myCalls = calls.filter(c => c.task_id === t.task_id);
  if (myCalls.length) {
    h += `<h4 style="margin-top:18px;">LLM calls (${myCalls.length})</h4>`;
    h += `<table class="graders-table"><thead><tr><th>call site</th><th>model</th><th>in</th><th>out</th><th>latency</th><th>cost</th></tr></thead><tbody>`;
    myCalls.forEach(c => {
      const cost = estimateCost(c);
      h += `<tr><td>${escapeHtml(c.call_site)}</td><td>${escapeHtml(c.model)}</td>` +
           `<td>${c.input_tokens}</td><td>${c.output_tokens}</td>` +
           `<td>${c.latency_ms}ms</td><td>$${cost.toFixed(4)}</td></tr>`;
    });
    h += `</tbody></table>`;
  }

  box.innerHTML = h;
}

function estimateCost(c) {
  const p = pricing[c.model] || pricing._default;
  return c.input_tokens * p.in / 1e6 +
         c.output_tokens * p.out / 1e6 +
         (c.cache_read_tokens||0) * p.cache_read / 1e6 +
         (c.cache_creation_tokens||0) * p.cache_write / 1e6;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ---- cost summary ----
(function renderCostSummary() {
  const box = document.getElementById('cost-summary');
  if (!box) return;
  if (!calls.length) {
    box.innerHTML = '<div class="empty">No LLM call telemetry yet — Phase 1 wires the client wrapper that records this.</div>';
    return;
  }
  const bySite = {};
  calls.forEach(c => {
    const k = c.call_site;
    if (!bySite[k]) bySite[k] = {n:0, in:0, out:0, cost:0, models:new Set()};
    bySite[k].n++; bySite[k].in += c.input_tokens; bySite[k].out += c.output_tokens;
    bySite[k].cost += estimateCost(c); bySite[k].models.add(c.model);
  });
  const rows = Object.entries(bySite).sort((a,b) => b[1].cost - a[1].cost);
  let h = `<div class="cost-grid">
    <div class="h">call site</div><div class="h num">calls</div><div class="h num">in tok</div><div class="h num">out tok</div><div class="h num">$ est</div>`;
  let tot = {n:0, in:0, out:0, cost:0};
  rows.forEach(([site, v]) => {
    h += `<div class="cell">${escapeHtml(site)} <small style="color:#94a3b8">(${[...v.models].join(',')})</small></div>` +
         `<div class="cell num">${v.n}</div><div class="cell num">${v.in.toLocaleString()}</div>` +
         `<div class="cell num">${v.out.toLocaleString()}</div><div class="cell num">$${v.cost.toFixed(4)}</div>`;
    tot.n += v.n; tot.in += v.in; tot.out += v.out; tot.cost += v.cost;
  });
  h += `<div class="cell cost-total">TOTAL</div><div class="cell num cost-total">${tot.n}</div>
        <div class="cell num cost-total">${tot.in.toLocaleString()}</div>
        <div class="cell num cost-total">${tot.out.toLocaleString()}</div>
        <div class="cell num cost-total">$${tot.cost.toFixed(4)}</div></div>`;
  box.innerHTML = h;
})();

renderFilters();
renderTiles();
renderDrill();
"""


def _delta_html(current: dict, runs: list[dict]) -> str:
    idx = next((i for i, r in enumerate(runs) if r["id"] == current["id"]), len(runs) - 1)
    if idx <= 0:
        return '<span class="score-delta flat">first run</span>'
    prev = runs[idx - 1]
    cur_s = current["headline_score"] or 0
    prev_s = prev["headline_score"] or 0
    delta = (cur_s - prev_s) * 100
    if abs(delta) < 0.05:
        return f'<span class="score-delta flat">flat vs #{prev["id"]}</span>'
    cls = "up" if delta > 0 else "down"
    sign = "+" if delta > 0 else ""
    return f'<span class="score-delta {cls}">{sign}{delta:.1f} pts vs #{prev["id"]}</span>'


def build_eval_dashboard(
    out_dir: str | Path,
    *,
    db_path: Path | None = None,
    run_id: int | None = None,
) -> dict:
    """Render the HTML eval dashboard. Returns {path, size_bytes, run_id}."""
    if db_path is None:
        # Same project-root resolution as runner._real_db_path
        db_path = Path(__file__).resolve().parents[3] / "data" / "intel.db"

    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    data = _collect(db_path, run_id=run_id)

    if not data["runs"]:
        body = (
            '<div class="empty">'
            'No eval runs yet. Run <code>intel evals run</code> to create one.'
            '</div>'
        )
        sections = f'<main><section>{body}</section></main>'
    else:
        cur = data["current"]
        score_pct = (cur["headline_score"] or 0) * 100
        delta = _delta_html(cur, data["runs"])
        headline_section = f"""
        <section>
          <div class="runsel">run: <select id="run-select"></select></div>
          <h2>Headline</h2>
          <div class="headline">
            <div>
              <div class="score-big">{score_pct:.0f}% {delta}</div>
              <div style="color:var(--muted); font-size:12px; margin-top:4px;">
                run #{cur['id']} · {cur['started_at']}
                {(' · git ' + cur['git_sha']) if cur.get('git_sha') else ''}
              </div>
            </div>
            <div class="counts">
              <div class="pill pass"><div class="n">{cur['pass_count']}</div><div class="lbl">pass</div></div>
              <div class="pill slow"><div class="n">{cur['pass_slow_count']}</div><div class="lbl">slow</div></div>
              <div class="pill fail"><div class="n">{cur['fail_count']}</div><div class="lbl">fail</div></div>
              <div class="pill skip"><div class="n">{cur['skip_count']}</div><div class="lbl">skip</div></div>
            </div>
          </div>
        </section>"""

        trend_section = """
        <section>
          <h2>Score trend</h2>
          <div class="trend-wrap"><svg id="trend" class="trend-svg"></svg></div>
          <div style="color:var(--muted); font-size:11px; margin-top:6px;">
            One point per eval run. Hover for details. Bible §6: keep what climbs, revert what doesn't.
          </div>
        </section>"""

        tasks_section = """
        <section>
          <h2>Tasks <span style="color:var(--muted); font-weight:400; font-size:12px;">(click to drill in)</span></h2>
          <div class="filters" id="filter-status"></div>
          <div class="filters" id="filter-category"></div>
          <div class="tasks-grid" id="tasks-grid"></div>
        </section>

        <section>
          <h2>Drill-down</h2>
          <div class="drill" id="drill"></div>
        </section>

        <section>
          <h2>Cost / token usage <span style="color:var(--muted); font-weight:400; font-size:12px;">(estimated)</span></h2>
          <div id="cost-summary"></div>
        </section>"""

        sections = f'<main>{headline_section}{trend_section}{tasks_section}</main>'

    # Build pricing payload subset for JS
    pricing_payload = {**PRICING_USD_PER_MTOK,
                       "_default": {"in": 3.0, "out": 15.0, "cache_read": 0.3, "cache_write": 3.75}}

    payload = {
        "runs": data["runs"],
        "current": data["current"],
        "tasks": data["tasks"],
        "calls": data["calls"],
        "pricing": pricing_payload,
    }

    title = "Intel · Eval Dashboard"
    generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html_doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head><body>
<header>
  <h1>Intel · Eval Dashboard</h1>
  <div class="meta">{html.escape(generated)} · bible-aligned hill-climbing</div>
</header>
{sections}
<script id="eval-data" type="application/json">{json.dumps(payload, default=str)}</script>
<script>{_JS}</script>
</body></html>
"""

    target = out / "index.html"
    target.write_text(html_doc, encoding="utf-8")
    return {
        "path": str(target),
        "size_bytes": target.stat().st_size,
        "run_id": data["current"]["id"] if data["current"] else None,
    }
