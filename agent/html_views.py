"""Self-contained HTML view generator for wizard steps.

All HTML is fully offline — no CDN dependencies. Open with any browser.
Each function returns an HTML string; call open_in_browser() to launch it.
"""

from __future__ import annotations

import json
import os
import tempfile
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


# ── Colour constants (venue clusters — Glass tokens from DESIGN.md) ───────────
_CLUSTER_COLORS = ["#64748b", "#6366f1", "#ec4899", "#14b8a6", "#f59e0b"]
_CLUSTER_LABELS = ["Unknown", "Venetian/Wynn/Encore", "Caesars/LINQ", "MGM Grand", "Mandalay Bay"]
_CLUSTER_KEYWORDS: list[list[str]] = [
    [],
    ["venetian", "wynn", "encore", "palazzo"],
    ["caesars", "forum", "linq", "harrahs", "paris"],
    ["mgm", "park mgm", "aria"],
    ["mandalay", "delano"],
]
_LEVEL_COLORS = {
    "Expert": "#f43f5e",
    "Advanced": "#f59e0b",
    "Intermediate": "#60a5fa",
    "Foundational": "#94a3b8",
}
_DAY_NAMES = {
    "2026-11-30": "Sun Nov 30",
    "2026-12-01": "Mon Dec 1",
    "2026-12-02": "Tue Dec 2",
    "2026-12-03": "Wed Dec 3",
    "2026-12-04": "Thu Dec 4",
    "2026-12-05": "Fri Dec 5",
}
_HOP_MINUTES: dict[tuple[int, int], int] = {
    (1, 2): 15, (2, 1): 15,
    (1, 3): 25, (3, 1): 25,
    (1, 4): 35, (4, 1): 35,
    (2, 3): 20, (3, 2): 20,
    (2, 4): 30, (4, 2): 30,
    (3, 4): 20, (4, 3): 20,
}


def _venue_cluster(location: str | None) -> int:
    if not location:
        return 0
    loc = location.lower()
    for i, kws in enumerate(_CLUSTER_KEYWORDS[1:], start=1):
        if any(kw in loc for kw in kws):
            return i
    return 0


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


# ── Shared CSS + JS base (Glass design — DESIGN.md tokens) ────────────────────

_BASE_STYLE = """
<style>
/* ── Tokens ─────────────────────────────────────────── */
:root {
  --geist: 'Geist', -apple-system, 'Segoe UI', sans-serif;
  --c1: #6366f1; --c2: #ec4899; --c3: #14b8a6; --c4: #f59e0b;
  --lvl-expert: #f43f5e; --lvl-advanced: #f59e0b;
  --lvl-intermediate: #60a5fa; --lvl-foundational: #94a3b8;
  --state-ok: #34d399; --state-warn: #f59e0b; --state-error: #f87171;
  --keynote: #a78bfa; --star: #fbbf24;
  --r-sm: 6px; --r-md: 10px; --r-lg: 14px; --r-full: 9999px;
}
[data-theme="dark"] {
  --bg:        linear-gradient(145deg, #111827 0%, #1a1033 100%);
  --surface:   rgba(255,255,255,0.05);
  --surface2:  rgba(255,255,255,0.09);
  --border:    rgba(255,255,255,0.10);
  --border-f:  rgba(99,102,241,0.5);
  --text:      #e2e8f0; --text-2: #94a3b8; --text-3: #64748b;
  --header-bg: rgba(17,24,39,0.88);
  --accent:    #818cf8;
  --green:     #34d399; --yellow: #f59e0b; --red: #f87171; --purple: #a78bfa;
  --badge-bg:  rgba(255,255,255,0.07);
  --chip-expert-bg: rgba(244,63,94,0.12); --chip-expert-c: #f43f5e; --chip-expert-b: rgba(244,63,94,0.35);
  --chip-advanced-bg: rgba(245,158,11,0.12); --chip-advanced-c: #f59e0b; --chip-advanced-b: rgba(245,158,11,0.35);
  --chip-intermediate-bg: rgba(96,165,250,0.12); --chip-intermediate-c: #60a5fa; --chip-intermediate-b: rgba(96,165,250,0.35);
  --chip-foundational-bg: rgba(148,163,184,0.10); --chip-foundational-c: #94a3b8; --chip-foundational-b: rgba(148,163,184,0.25);
  --chip-keynote-bg: rgba(167,139,250,0.12); --chip-keynote-c: #a78bfa; --chip-keynote-b: rgba(167,139,250,0.4);
  --pill-ok-bg: rgba(52,211,153,0.12); --pill-warn-bg: rgba(245,158,11,0.12); --pill-error-bg: rgba(248,113,113,0.12);
  --pill-purple-bg: rgba(167,139,250,0.12);
  --table-th-bg: rgba(255,255,255,0.04); --table-hover: rgba(255,255,255,0.04);
  --score-bar-bg: rgba(255,255,255,0.08);
}
[data-theme="light"] {
  --bg:        linear-gradient(145deg, #f8faff 0%, #eeeeff 100%);
  --surface:   rgba(255,255,255,0.70);
  --surface2:  rgba(255,255,255,0.90);
  --border:    rgba(99,102,241,0.14);
  --border-f:  rgba(99,102,241,0.4);
  --text:      #1e1b4b; --text-2: #4c4b7a; --text-3: #94a3b8;
  --header-bg: rgba(248,250,255,0.90);
  --accent:    #4f46e5;
  --green:     #059669; --yellow: #d97706; --red: #dc2626; --purple: #7c3aed;
  --badge-bg:  rgba(99,102,241,0.07);
  --chip-expert-bg: rgba(225,29,72,0.08); --chip-expert-c: #e11d48; --chip-expert-b: rgba(225,29,72,0.3);
  --chip-advanced-bg: rgba(217,119,6,0.08); --chip-advanced-c: #d97706; --chip-advanced-b: rgba(217,119,6,0.3);
  --chip-intermediate-bg: rgba(37,99,235,0.08); --chip-intermediate-c: #2563eb; --chip-intermediate-b: rgba(37,99,235,0.3);
  --chip-foundational-bg: rgba(100,116,139,0.08); --chip-foundational-c: #64748b; --chip-foundational-b: rgba(100,116,139,0.25);
  --chip-keynote-bg: rgba(124,58,237,0.08); --chip-keynote-c: #7c3aed; --chip-keynote-b: rgba(124,58,237,0.35);
  --pill-ok-bg: rgba(5,150,105,0.08); --pill-warn-bg: rgba(217,119,6,0.08); --pill-error-bg: rgba(220,38,38,0.08);
  --pill-purple-bg: rgba(124,58,237,0.08);
  --table-th-bg: rgba(99,102,241,0.06); --table-hover: rgba(99,102,241,0.04);
  --score-bar-bg: rgba(99,102,241,0.12);
}
/* ── Reset + Base ───────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { min-height: 100%; }
body {
  font-family: var(--geist);
  font-size: 14px; line-height: 1.5; color: var(--text);
  background: var(--bg); min-height: 100vh;
  transition: background .2s, color .15s;
}
h1, h2, h3 { color: var(--text); }
a { color: var(--accent); }
/* ── Badges / chips ─────────────────────────────────── */
.badge {
  display: inline-flex; align-items: center;
  padding: 1px 8px; border-radius: var(--r-full);
  font-size: 11px; font-weight: 600; letter-spacing: .02em; border: 1px solid;
}
.chip-Expert       { background: var(--chip-expert-bg);       color: var(--chip-expert-c);       border-color: var(--chip-expert-b); }
.chip-Advanced     { background: var(--chip-advanced-bg);     color: var(--chip-advanced-c);     border-color: var(--chip-advanced-b); }
.chip-Intermediate { background: var(--chip-intermediate-bg); color: var(--chip-intermediate-c); border-color: var(--chip-intermediate-b); }
.chip-Foundational { background: var(--chip-foundational-bg); color: var(--chip-foundational-c); border-color: var(--chip-foundational-b); }
.chip-Keynote      { background: var(--chip-keynote-bg);      color: var(--chip-keynote-c);      border-color: var(--chip-keynote-b); }
.chip-—            { background: var(--badge-bg); color: var(--text-3); border-color: var(--border); }
.star { color: var(--star); }
.tag {
  background: var(--badge-bg); border: 1px solid var(--border); border-radius: var(--r-sm);
  padding: 0 6px; font-size: 11px; color: var(--text-3); margin: 1px; display: inline-block;
}
/* ── Table ──────────────────────────────────────────── */
table { width: 100%; border-collapse: collapse; }
th {
  background: var(--table-th-bg); padding: 8px 12px; text-align: left;
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em;
  color: var(--text-3); border-bottom: 1px solid var(--border); position: sticky; top: 56px;
}
td { padding: 8px 12px; border-bottom: 1px solid var(--border); vertical-align: top; color: var(--text); }
tr:hover td { background: var(--table-hover); }
tr.excluded td { opacity: .4; text-decoration: line-through; }
/* ── Form elements ──────────────────────────────────── */
.score-bar-bg { background: var(--score-bar-bg); border-radius: 4px; height: 6px; width: 80px; display: inline-block; vertical-align: middle; }
.score-bar { background: var(--accent); height: 6px; border-radius: 4px; }
input[type=text], select {
  background: var(--surface2); border: 1px solid var(--border);
  color: var(--text); padding: 6px 10px; border-radius: var(--r-sm); font-family: var(--geist); font-size: 13px;
}
input[type=checkbox] { width: 16px; height: 16px; cursor: pointer; accent-color: var(--accent); }
button {
  background: rgba(99,102,241,0.18); color: var(--accent);
  border: 1px solid rgba(99,102,241,0.4); border-radius: var(--r-sm);
  padding: 6px 16px; cursor: pointer; font-weight: 600; font-size: 13px; font-family: var(--geist);
}
button.secondary { background: var(--surface); color: var(--text-2); border-color: var(--border); }
button:hover { opacity: .85; }
/* ── Layout ─────────────────────────────────────────── */
.panel {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r-md); padding: 16px; margin: 12px 0;
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
}
.flex { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.container { max-width: 1100px; margin: 0 auto; padding: 24px 24px 64px; }
/* ── Status colors ──────────────────────────────────── */
.warn { color: var(--yellow); } .error { color: var(--red); } .ok { color: var(--green); }
/* ── Pills ──────────────────────────────────────────── */
.pill { padding: 2px 10px; border-radius: var(--r-full); font-size: 12px; font-weight: 600; border: 1px solid; }
.pill-ok     { background: var(--pill-ok-bg);     color: var(--green);  border-color: rgba(52,211,153,.35); }
.pill-warn   { background: var(--pill-warn-bg);   color: var(--yellow); border-color: rgba(245,158,11,.35); }
.pill-error  { background: var(--pill-error-bg);  color: var(--red);    border-color: rgba(248,113,113,.35); }
.pill-purple { background: var(--pill-purple-bg); color: var(--purple); border-color: rgba(167,139,250,.4); }
/* ── Mode toggle ────────────────────────────────────── */
.mode-toggle {
  width: 32px; height: 32px; border-radius: var(--r-sm);
  border: 1px solid var(--border); background: var(--surface); color: var(--text-2);
  font-size: 15px; cursor: pointer; display: flex; align-items: center; justify-content: center;
  flex-shrink: 0; font-family: var(--geist);
}
.mode-toggle:hover { background: var(--surface2); color: var(--text); }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition: none !important; } }
</style>
"""


def _html_page(title: str, body: str, extra_head: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — reinvent-planner</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&display=swap" rel="stylesheet">
{_BASE_STYLE}
<style>
.site-header {{
  position: sticky; top: 0; z-index: 50;
  background: var(--header-bg);
  backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  padding: 0 24px;
}}
.site-header-inner {{
  max-width: 1100px; margin: 0 auto;
  display: flex; align-items: center; gap: 12px; height: 52px;
}}
.wordmark {{
  font-size: 14px; font-weight: 700; letter-spacing: -.02em; color: var(--text);
  display: flex; align-items: center; gap: 6px; flex-shrink: 0;
}}
.wordmark-dot {{
  width: 8px; height: 8px; border-radius: 50%;
  background: linear-gradient(135deg, #6366f1, #a78bfa);
}}
.breadcrumb {{
  font-size: 13px; color: var(--text-3); flex: 1;
}}
</style>
{extra_head}
<script>
(function(){{
  const saved = localStorage.getItem('rp-theme');
  const pref = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', saved || pref);
}})();
</script>
</head>
<body>
<header class="site-header">
  <div class="site-header-inner">
    <div class="wordmark"><span class="wordmark-dot"></span>reinvent-planner</div>
    <div class="breadcrumb">{title}</div>
    <button class="mode-toggle" onclick="(function(){{var t=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',t);localStorage.setItem('rp-theme',t);document.getElementById('micon').textContent=t==='dark'?'☀':'🌙';}})()" aria-label="Toggle theme"><span id="micon">☀</span></button>
  </div>
</header>
<div class="container">
{body}
</div>
<script>
(function(){{
  const t = document.documentElement.getAttribute('data-theme');
  const el = document.getElementById('micon');
  if (el) el.textContent = t === 'dark' ? '☀' : '🌙';
}})();
</script>
</body>
</html>"""


def open_in_browser(html: str, filename: str = "view.html") -> str:
    """Write HTML to a temp file and open it in the default browser. Returns the path."""
    tmp = Path(tempfile.gettempdir()) / f"reinvent_{filename}"
    tmp.write_text(html, encoding="utf-8")
    webbrowser.open(f"file://{tmp}")
    return str(tmp)


# ── Step 3: Recommendations ────────────────────────────────────────────────────

def html_recommendations(scored_sessions: list, excluded_ids: set[str] | None = None) -> str:
    excluded_ids = excluded_ids or set()

    rows = []
    for s in scored_sessions:
        ex = "excluded" if s.event_id in excluded_ids else ""
        star = "&#9733;" if s.score == 1.0 else ""
        lvl = s.learning_level or "—"
        lvl_chip = f'<span class="badge chip-{lvl}">{lvl}</span>' if lvl != "—" else "—"
        score_pct = int(s.score * 100)
        score_bar = f'<div class="score-bar-bg"><div class="score-bar" style="width:{score_pct}%"></div></div>'
        loc = (s.location or "—").split("|")[0].strip()
        cluster = _venue_cluster(s.location)
        cluster_dot = f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{_CLUSTER_COLORS[cluster]};margin-right:4px;vertical-align:middle"></span>'
        date_str = str(s.start_date) if s.start_date else "—"
        day_str = _DAY_NAMES.get(date_str, date_str)
        time_str = s.start_time or "—"
        title_link = f'<a href="{s.registration_url}" target="_blank">{s.title}</a>' if s.registration_url else s.title
        reg_btn = (
            f'<a href="{s.registration_url}" target="_blank" class="badge" '
            f'style="background:#1a3a1a;color:#3fb950;border:1px solid #3fb950;text-decoration:none">Register</a>'
            if s.registration_url else ""
        )
        rows.append(f"""
<tr class="{ex}" data-id="{s.event_id}" data-level="{lvl}" data-score="{s.score:.2f}">
  <td><input type="checkbox" onchange="toggleExclude('{s.event_id}',this)" {"checked" if ex else ""}> {star}</td>
  <td>{score_bar}<br><small style="color:var(--text-dim)">{score_pct}%</small></td>
  <td>{lvl_chip}</td>
  <td style="white-space:nowrap">{day_str}<br><small style="color:var(--text-dim)">{time_str}</small></td>
  <td><strong>{title_link}</strong><br><small style="color:var(--text-dim)">{(s.description or '')[:120]}...</small></td>
  <td>{cluster_dot}{loc[:28]}</td>
  <td>{reg_btn}</td>
</tr>""")

    body = f"""
<div class="panel flex" style="margin-bottom:16px">
  <div>
    <strong>{len(scored_sessions)}</strong> sessions scored &nbsp;|&nbsp;
    <span id="excl-count" class="error">{len(excluded_ids)}</span> excluded
  </div>
  <input type="text" id="filter-text" placeholder="Filter by title..." onkeyup="applyFilters()" style="width:250px">
  <select id="filter-level" onchange="applyFilters()">
    <option value="">All levels</option>
    <option value="Expert">Expert</option>
    <option value="Advanced">Advanced</option>
    <option value="Intermediate">Intermediate</option>
    <option value="Foundational">Foundational</option>
  </select>
  <select id="filter-min-score" onchange="applyFilters()">
    <option value="0">All scores</option>
    <option value="0.5">≥ 0.50</option>
    <option value="0.7">≥ 0.70</option>
    <option value="0.9">≥ 0.90</option>
  </select>
  <button onclick="saveExclusions()">Save Exclusions →</button>
  <button class="secondary" onclick="clearExclusions()">Clear All</button>
</div>
<div style="overflow-x:auto">
<table id="recs-table">
<thead><tr>
  <th style="width:50px">Exclude</th>
  <th style="width:100px">Score</th>
  <th style="width:120px">Level</th>
  <th style="width:120px">Day/Time</th>
  <th>Session</th>
  <th style="width:180px">Venue</th>
  <th style="width:90px"></th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</div>
<div id="save-banner" class="panel" style="display:none;border-color:var(--green)">
  <span class="ok">✓ Exclusions saved to <code>exclusions.json</code></span>
  — run <code>awsevents setup</code> again to apply them, or press Ctrl+C and re-run with the file present.
</div>
<script>
const excluded = new Set({json.dumps(list(excluded_ids))});
function toggleExclude(id, cb) {{
  const row = cb.closest('tr');
  if (cb.checked) {{ excluded.add(id); row.classList.add('excluded'); }}
  else {{ excluded.delete(id); row.classList.remove('excluded'); }}
  document.getElementById('excl-count').textContent = excluded.size;
}}
function applyFilters() {{
  const txt = document.getElementById('filter-text').value.toLowerCase();
  const lvl = document.getElementById('filter-level').value;
  const minScore = parseFloat(document.getElementById('filter-min-score').value || '0');
  document.querySelectorAll('#recs-table tbody tr').forEach(row => {{
    const title = row.querySelector('td:nth-child(5)').textContent.toLowerCase();
    const rowLvl = row.dataset.level;
    const rowScore = parseFloat(row.dataset.score);
    const show = (!txt || title.includes(txt)) && (!lvl || rowLvl === lvl) && rowScore >= minScore;
    row.style.display = show ? '' : 'none';
  }});
}}
function clearExclusions() {{
  excluded.clear();
  document.querySelectorAll('#recs-table input[type=checkbox]').forEach(cb => {{
    cb.checked = false;
    cb.closest('tr').classList.remove('excluded');
  }});
  document.getElementById('excl-count').textContent = 0;
}}
function saveExclusions() {{
  const data = JSON.stringify(Array.from(excluded));
  const a = document.createElement('a');
  a.href = 'data:application/json;charset=utf-8,' + encodeURIComponent(data);
  a.download = 'exclusions.json';
  a.click();
  document.getElementById('save-banner').style.display = 'block';
}}
</script>"""

    return _html_page("Step 3 — Recommendations", body)


# ── Step 4: Conflict Resolver ─────────────────────────────────────────────────

def html_conflicts(conflicts: list[tuple[dict, dict]]) -> str:
    if not conflicts:
        body = '<div class="panel ok">✓ No conflicts found — schedule is clean.</div>'
        return _html_page("Step 4 — Conflicts", body)

    cards = []
    for idx, (a, b) in enumerate(conflicts, 1):
        def _fmt(s: dict, letter: str) -> str:
            lvl = s.get("learning_level") or "—"
            chip = f'<span class="badge chip-{lvl}">{lvl}</span>'
            a_start = (s.get("scheduled_start") or "")[-8:-3]
            a_end = (s.get("scheduled_end") or "")[-8:-3]
            cluster = _venue_cluster(s.get("location"))
            cdot = f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{_CLUSTER_COLORS[cluster]};margin-right:4px"></span>'
            score = s.get("score", 0)
            return f"""
<div class="panel" style="flex:1;border-color:var(--border)">
  <div style="font-size:22px;font-weight:700;color:var(--accent)">Option {letter}</div>
  <div style="margin:8px 0"><strong>{s['title']}</strong></div>
  <div class="flex" style="margin:6px 0">
    {chip}
    <span class="tag">{s.get('event_type') or '—'}</span>
    <span class="tag">{a_start}–{a_end}</span>
    <span style="color:var(--text-dim);font-size:12px">Score: <strong>{score:.2f}</strong></span>
  </div>
  <div>{cdot}<small style="color:var(--text-dim)">{(s.get('location') or '—').split('|')[0].strip()[:40]}</small></div>
  <div style="margin-top:8px;font-size:12px;color:var(--text-dim)">{(s.get('description') or '')[:150]}…</div>
</div>"""

        day = a.get("start_date", "")
        cards.append(f"""
<div class="panel" style="border-color:var(--yellow)">
  <h3 style="color:var(--yellow)">⚡ Conflict {idx} — {_DAY_NAMES.get(day, day)}</h3>
  <div class="flex" style="margin-top:12px;align-items:stretch">
    {_fmt(a, "A")}
    <div style="display:flex;flex-direction:column;justify-content:center;color:var(--text-dim);padding:0 8px">VS</div>
    {_fmt(b, "B")}
  </div>
</div>""")

    body = f"""
<div class="panel flex">
  <span class="warn">Found <strong>{len(conflicts)}</strong> overlapping pair(s).</span>
  <span style="color:var(--text-dim)">Resolve each conflict in the console (awsevents setup) — this view is for reference.</span>
</div>
{''.join(cards)}"""

    return _html_page("Step 4 — Conflict Resolver", body)


# ── Step 5: Schedule View ─────────────────────────────────────────────────────

def html_schedule(schedule: list[dict]) -> str:
    by_day: dict[str, list[dict]] = {}
    for s in schedule:
        by_day.setdefault(s.get("start_date", "unknown"), []).append(s)

    day_tabs = []
    day_panels = []

    for day_idx, (day, sessions) in enumerate(sorted(by_day.items())):
        label = _DAY_NAMES.get(day, day)
        ordered = sorted(sessions, key=lambda x: x.get("scheduled_start") or x.get("start_time") or "")

        # Build timeline items
        items_html = []
        prev_end: datetime | None = None
        prev_cluster = 0

        for s in ordered:
            title = s.get("title", "")
            lvl = s.get("learning_level") or "—"
            stype = s.get("event_type") or "—"
            loc = (s.get("location") or "—").split("|")[0].strip()
            score = s.get("score", 0)
            is_wishlist = s.get("is_wishlisted", False) or score == 1.0
            is_keynote = stype == "Keynote"
            cluster = _venue_cluster(s.get("location"))
            cluster_color = _CLUSTER_COLORS[cluster]
            cluster_label = _CLUSTER_LABELS[cluster]
            lvl_color = _LEVEL_COLORS.get(lvl, "#888")

            ss = s.get("scheduled_start")
            se = s.get("scheduled_end")
            try:
                start_dt = datetime.fromisoformat(ss) if ss else None
                end_dt = datetime.fromisoformat(se) if se else None
            except Exception:
                start_dt = end_dt = None

            start_str = start_dt.strftime("%H:%M") if start_dt else (s.get("start_time") or "?")[:5]
            end_str = end_dt.strftime("%H:%M") if end_dt else "?"

            dur_min = 0
            if start_dt and end_dt:
                dur_min = int((end_dt - start_dt).total_seconds() / 60)

            # Travel warning
            if prev_end and start_dt and prev_cluster and cluster and prev_cluster != cluster:
                gap_min = int((start_dt - prev_end).total_seconds() / 60)
                travel_min = _HOP_MINUTES.get((prev_cluster, cluster), 25)
                from_lbl = _CLUSTER_LABELS[prev_cluster]
                to_lbl = _CLUSTER_LABELS[cluster]
                tight = gap_min < travel_min
                if tight:
                    tw_style = "color:var(--state-error);background:rgba(248,113,113,0.08);border:1px solid rgba(248,113,113,0.2)"
                    tw_text = f"⚠&nbsp; {from_lbl} → {to_lbl} &nbsp;·&nbsp; {travel_min} min travel, {gap_min} min gap — TIGHT"
                else:
                    tw_style = "color:var(--text-3)"
                    tw_text = f"→&nbsp; {from_lbl} → {to_lbl} &nbsp;·&nbsp; {travel_min} min travel, {gap_min} min gap ✓"
                items_html.append(f"""
<div style="display:flex;align-items:center;gap:0;margin:2px 0 4px">
  <div style="width:56px;flex-shrink:0"></div>
  <div style="width:3px;flex-shrink:0;align-self:stretch;min-height:20px;margin:0;background:{'rgba(248,113,113,0.25)' if tight else 'rgba(52,211,153,0.15)'}"></div>
  <div style="flex:1;margin-left:12px;font-size:11px;padding:4px 10px;border-radius:var(--r-sm);{tw_style}">{tw_text}</div>
</div>""")

            reg_url = s.get("registration_url")
            reg_link = (
                f'<a href="{reg_url}" target="_blank" style="display:inline-flex;align-items:center;gap:4px;'
                f'padding:4px 12px;border-radius:var(--r-sm);background:rgba(99,102,241,0.15);'
                f'border:1px solid rgba(99,102,241,0.4);color:var(--accent);font-size:12px;font-weight:600;'
                f'text-decoration:none;margin-left:8px">↗ Register</a>'
                if reg_url else ""
            )

            star = '<span style="color:var(--star)">★</span> ' if is_wishlist else ""
            if is_keynote:
                keynote_header = (
                    f'<div style="margin-bottom:6px">'
                    f'<span class="badge" style="background:rgba(167,139,250,0.12);color:var(--keynote);border-color:rgba(167,139,250,0.4)">'
                    f'★ KEYNOTE · Open attendance</span></div>'
                )
                card_border = "border-color:rgba(167,139,250,0.35)"
                bar_color = "var(--keynote)"
            else:
                keynote_header = ""
                card_border = ""
                bar_color = cluster_color

            desc = s.get("description") or ""
            desc_snippet = (desc[:160] + "…") if len(desc) > 160 else desc
            venue_dot = (
                f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;'
                f'background:{cluster_color};margin-right:4px;vertical-align:middle"></span>'
            )
            notes = s.get("notes", "")

            items_html.append(f"""
<div style="display:flex;gap:0;margin:4px 0;align-items:flex-start">
  <div style="width:56px;text-align:right;padding-right:12px;color:var(--text-3);font-size:12px;padding-top:13px;flex-shrink:0;font-variant-numeric:tabular-nums">
    {start_str}
  </div>
  <div style="width:3px;background:{bar_color};border-radius:2px;flex-shrink:0;min-height:52px;margin:4px 0"></div>
  <div style="flex:1;background:var(--surface);border:1px solid var(--border);border-radius:var(--r-md);
              margin-left:12px;padding:10px 14px;{card_border};
              backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)">
    {keynote_header}
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px">
      <div style="font-size:13px;font-weight:600;line-height:1.4;flex:1">{star}{title}{reg_link}</div>
      <div style="font-size:11px;color:var(--text-3);white-space:nowrap;padding-top:1px;flex-shrink:0">
        {start_str}–{end_str}
      </div>
    </div>
    <div class="flex" style="margin-top:6px;gap:6px">
      {'<span class="badge chip-' + lvl + '">' + lvl + '</span>' if not is_keynote else ""}
      {'<span class="tag">' + stype + '</span>' if not is_keynote else ""}
      <span style="font-size:11px;color:var(--text-3)">{venue_dot}{cluster_label}</span>
      <span style="font-size:11px;color:var(--text-3)">{loc[:35]}</span>
    </div>
    {f'<div style="margin-top:6px;font-size:12px;color:var(--text-3);line-height:1.6">{desc_snippet}</div>' if desc_snippet else ""}
    {f'<div style="margin-top:4px;font-size:12px;font-style:italic;color:var(--text-3)">{notes[:100]}</div>' if notes else ""}
  </div>
</div>""")

            prev_end = end_dt
            if cluster:
                prev_cluster = cluster

        session_count = len(ordered)
        active_id = "active" if day_idx == 0 else ""
        day_tabs.append(
            f'<button class="day-tab {active_id}" onclick="showDay(\'{day}\')" id="tab-{day}" '
            f'role="tab" aria-selected="{"true" if day_idx == 0 else "false"}">'
            f'<span style="font-size:12px;font-weight:600">{label}</span>'
            f'<span style="font-size:10px;opacity:.6;display:block">{session_count} sessions</span>'
            f'</button>'
        )
        day_panels.append(f"""
<div class="day-panel" id="day-{day}" style="{'display:block' if day_idx == 0 else 'display:none'}">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--border)">
    <div>
      <div style="font-size:20px;font-weight:700;letter-spacing:-.02em">{label}</div>
      <div style="font-size:13px;color:var(--text-3);margin-top:2px">{session_count} sessions</div>
    </div>
    <div style="display:flex;gap:6px;margin-left:auto;align-items:center">
      {' '.join(f'<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--text-3)"><span style="width:9px;height:9px;background:{_CLUSTER_COLORS[i]};border-radius:50%;display:inline-block"></span>{_CLUSTER_LABELS[i]}</span>' for i in range(1, 5))}
    </div>
  </div>
  {''.join(items_html)}
</div>""")

    legend = ''.join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--text-3)">'
        f'<span style="width:10px;height:10px;background:{_CLUSTER_COLORS[i]};border-radius:50%;display:inline-block"></span>'
        f'{_CLUSTER_LABELS[i]}</span>'
        for i in range(1, 5)
    )

    body = f"""
<div style="display:flex;gap:4px;flex-wrap:nowrap;overflow-x:auto;margin-bottom:24px;scrollbar-width:none">
  {''.join(day_tabs)}
</div>
{''.join(day_panels)}
<div style="margin-top:40px;padding-top:20px;border-top:1px solid var(--border);display:flex;gap:20px;flex-wrap:wrap">
  <span style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--text-3);align-self:center">Venue</span>
  {legend}
</div>
<style>
.day-tab {{
  flex-shrink:0; padding:6px 14px; border-radius:var(--r-sm);
  border:1px solid transparent; background:transparent;
  color:var(--text-3); font-family:var(--geist); cursor:pointer;
  text-align:center; transition:background .15s, color .15s;
}}
.day-tab:hover {{ background:var(--surface); color:var(--text-2); }}
.day-tab.active {{
  background:rgba(99,102,241,0.15); border-color:rgba(99,102,241,0.4); color:var(--text);
}}
</style>
<script>
function showDay(id) {{
  document.querySelectorAll('.day-panel').forEach(p => p.style.display='none');
  document.querySelectorAll('.day-tab').forEach(t => {{ t.classList.remove('active'); t.setAttribute('aria-selected','false'); }});
  document.getElementById('day-'+id).style.display='block';
  const tab = document.getElementById('tab-'+id);
  tab.classList.add('active'); tab.setAttribute('aria-selected','true');
}}
</script>"""

    return _html_page("Step 5 — Schedule", body)


# ── Step 7: Registration Status ────────────────────────────────────────────────

_STATUS_CONFIG = {
    "registered":       ("ok",     "✓", "Registered"),
    "already_registered": ("ok",   "✓", "Already Registered"),
    "full":             ("error",  "✗", "Full — consider walk-in"),
    "not_open":         ("warn",   "○", "Not Open Yet"),
    "waitlisted":       ("warn",   "⋯", "Waitlisted"),
    "error":            ("error",  "!", "Error"),
    "walkin":           ("warn",   "↯", "Walk-in Only"),
    "missed":           ("error",  "✗", "Missed"),
}


def html_registration_status(results: list[dict], schedule: list[dict]) -> str:
    registered = [r for r in results if r.get("status") in ("registered", "already_registered")]
    waitlisted = [r for r in results if r.get("status") in ("waitlisted", "walkin")]
    full_missed = [r for r in results if r.get("status") == "full"]
    not_open = [r for r in results if r.get("status") == "not_open"]
    errors = [r for r in results if r.get("status") == "error"]

    # Summary cards
    summary = f"""
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px">
  <div class="panel" style="border-color:var(--green);text-align:center">
    <div style="font-size:36px;color:var(--green)">{len(registered)}</div>
    <div class="ok">Registered ✓</div>
  </div>
  <div class="panel" style="border-color:var(--yellow);text-align:center">
    <div style="font-size:36px;color:var(--yellow)">{len(waitlisted)}</div>
    <div class="warn">Walk-in / Waitlist</div>
  </div>
  <div class="panel" style="border-color:var(--red);text-align:center">
    <div style="font-size:36px;color:var(--red)">{len(full_missed)}</div>
    <div class="error">Full / Missed</div>
  </div>
  <div class="panel" style="border-color:var(--border);text-align:center">
    <div style="font-size:36px;color:var(--text-dim)">{len(not_open)}</div>
    <div style="color:var(--text-dim)">Not Open Yet</div>
  </div>
  <div class="panel" style="border-color:var(--red);text-align:center">
    <div style="font-size:36px;color:var(--red)">{len(errors)}</div>
    <div class="error">Errors</div>
  </div>
</div>"""

    # Walk-in tip
    walkin_tip = ""
    if full_missed:
        walkin_tip = f"""
<div class="panel" style="border-color:var(--yellow)">
  <h3 class="warn">Walk-in Options ({len(full_missed)} full sessions)</h3>
  <p style="margin:8px 0;color:var(--text-dim)">
    Sessions marked as full still allow walk-in entry — arrive <strong>5–10 min before start</strong>,
    join the walk-in line, and enter once registered attendees are seated.
    The earlier you arrive, the better your odds.
  </p>
  <ul style="margin:8px 0 0 20px;color:var(--text-dim)">
    {"".join(f"<li>{r.get('title','?')[:80]}</li>" for r in full_missed)}
  </ul>
</div>"""

    # Detailed table
    rows = []
    sched_by_id = {s["event_id"]: s for s in schedule}
    for r in sorted(results, key=lambda x: (x.get("status",""), x.get("title",""))):
        status = r.get("status", "—")
        cfg = _STATUS_CONFIG.get(status, ("", "?", status))
        cls, icon, label = cfg
        sched = sched_by_id.get(r.get("event_id", ""), {})
        day = sched.get("start_date", "")
        time = sched.get("start_time") or (sched.get("scheduled_start") or "")[-8:-3]
        lvl = sched.get("learning_level") or "—"
        loc = (sched.get("location") or "—").split("|")[0].strip()[:28]
        reg_url = sched.get("registration_url")
        reg_link = (
            f'<a href="{reg_url}" target="_blank" style="color:var(--accent);font-size:12px">View →</a>'
            if reg_url else ""
        )
        rows.append(f"""
<tr>
  <td><span class="pill pill-{cls}">{icon} {label}</span></td>
  <td>{_DAY_NAMES.get(day, day)}</td>
  <td>{time}</td>
  <td><span class="badge chip-{lvl}">{lvl}</span></td>
  <td><strong>{r.get('title','?')[:70]}</strong>{f"<br><small class='error'>{r.get('error','')[:60]}</small>" if r.get('error') else ""}</td>
  <td style="color:var(--text-dim)">{loc}</td>
  <td>{reg_link}</td>
</tr>""")

    table = f"""
<table>
<thead><tr>
  <th>Status</th><th>Day</th><th>Time</th><th>Level</th><th>Session</th><th>Venue</th><th></th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>"""

    # Gaps analysis — find free hour blocks
    gaps_html = _gaps_analysis(schedule, results)

    body = summary + walkin_tip + "<h3 style='margin:20px 0 12px'>All Sessions</h3>" + table + gaps_html
    return _html_page("Step 7 — Registration Status", body)


def _gaps_analysis(schedule: list[dict], results: list[dict]) -> str:
    registered_ids = {
        r["event_id"] for r in results
        if r.get("status") in ("registered", "already_registered")
    }

    by_day: dict[str, list[dict]] = {}
    for s in schedule:
        if s.get("event_type") == "Keynote":
            registered_ids.add(s["event_id"])
        by_day.setdefault(s.get("start_date", ""), []).append(s)

    gap_rows = []
    for day in sorted(by_day):
        secured = [s for s in by_day[day] if s["event_id"] in registered_ids]
        if not secured:
            gap_rows.append(f"<tr><td>{_DAY_NAMES.get(day,day)}</td><td colspan=2 class='warn'>No secured sessions — entire day is free</td></tr>")
            continue

        ordered = sorted(secured, key=lambda x: x.get("scheduled_start") or "")
        prev_end = None
        for s in ordered:
            ss = s.get("scheduled_start")
            se = s.get("scheduled_end")
            try:
                s_dt = datetime.fromisoformat(ss) if ss else None
                e_dt = datetime.fromisoformat(se) if se else None
            except Exception:
                s_dt = e_dt = None
            if prev_end and s_dt and s_dt > prev_end:
                gap = int((s_dt - prev_end).total_seconds() / 60)
                if gap >= 60:
                    gap_rows.append(
                        f"<tr><td>{_DAY_NAMES.get(day,day)}</td>"
                        f"<td>{prev_end.strftime('%H:%M')} – {s_dt.strftime('%H:%M')}</td>"
                        f"<td class='warn'>{gap} min free slot</td></tr>"
                    )
            prev_end = e_dt

    if not gap_rows:
        return ""

    return f"""
<h3 style="margin:24px 0 12px">Free Slots in Registered Schedule</h3>
<div class="panel">
<table><thead><tr><th>Day</th><th>Time Window</th><th>Duration</th></tr></thead>
<tbody>{''.join(gap_rows)}</tbody>
</table>
<p style="margin-top:12px;color:var(--text-dim);font-size:12px">
  These gaps could be filled with walk-in attempts or unregistered sessions.
  Run <code>awsevents edit</code> to browse alternatives for any slot.
</p>
</div>"""


# ── Slot alternatives (for `edit` command) ────────────────────────────────────

def html_slot_alternatives(slot_sessions: list, day: str, time_window: str) -> str:
    rows = []
    for s in slot_sessions:
        lvl = s.get("learning_level") or "—"
        chip = f'<span class="badge chip-{lvl}">{lvl}</span>'
        score = s.get("score", 0)
        score_pct = int(score * 100)
        score_bar = f'<div class="score-bar-bg"><div class="score-bar" style="width:{score_pct}%"></div></div>'
        loc = (s.get("location") or "—").split("|")[0].strip()
        cluster = _venue_cluster(s.get("location"))
        cdot = f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{_CLUSTER_COLORS[cluster]};margin-right:4px;vertical-align:middle"></span>'
        time_str = s.get("start_time") or s.get("time") or "—"
        aoi = ", ".join((s.get("areas_of_interest") or [])[:3])
        reg_url = s.get("registration_url") or s.get("learn_more_url")
        reg_link = (
            f'<a href="{reg_url}" target="_blank" class="badge" '
            f'style="background:#1a3a1a;color:#3fb950;border:1px solid #3fb950;text-decoration:none">Register</a>'
            if reg_url else ""
        )
        rows.append(f"""
<tr>
  <td>{score_bar}<br><small style="color:var(--text-dim)">{score_pct}%</small></td>
  <td>{chip}</td>
  <td style="white-space:nowrap">{time_str}</td>
  <td>
    <strong>{s.get('title','?')}</strong><br>
    <small style="color:var(--text-dim)">{aoi}</small><br>
    <small style="color:var(--text-dim)">{(s.get('description') or '')[:120]}…</small>
  </td>
  <td>{cdot}{loc[:28]}</td>
  <td><span class="tag">{(s.get('session_type') or s.get('event_type') or '—')[:18]}</span></td>
  <td>{reg_link}</td>
</tr>""")

    body = f"""
<div class="panel flex">
  <h3>{_DAY_NAMES.get(day, day)} &nbsp;·&nbsp; {time_window}</h3>
  <span style="color:var(--text-dim)">{len(slot_sessions)} alternatives matching your interests</span>
</div>
<div style="overflow-x:auto">
<table>
<thead><tr>
  <th style="width:100px">Score</th>
  <th>Level</th>
  <th>Time</th>
  <th>Session</th>
  <th>Venue</th>
  <th>Type</th>
  <th></th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</div>"""

    return _html_page(f"Edit Slot — {_DAY_NAMES.get(day, day)} {time_window}", body)
