"""A self-contained before/after chart page for an eval run.

Two shades of one hue: `before` light, `after` dark (validated as an ordinal
pair against both chart surfaces). Every mark has a hover tooltip, every chart
has a table view, and the page follows the viewer's light/dark setting.
"""

from __future__ import annotations

import html
import json
import math
import statistics
from collections import defaultdict
from dataclasses import replace

from .eval import Probe, RunResult

ACTION_ORDER = ["move", "pointer", "condense", "keep"]
ACTION_LABEL = {
    "move": "Moved to an on-demand rule file",
    "pointer": "Replaced by a pointer to a doc",
    "condense": "Condensed in place",
    "keep": "Kept as is (control)",
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d, (c + m) / d


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _fmt_tokens(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def _change(before: float, after: float) -> str:
    return f"{(after - before) / before:+.0%}" if before else "n/a"


def _summary(results: list[RunResult], variant: str) -> dict:
    rs = [r for r in results if r.variant == variant]
    graded = [r for r in rs if r.passed is not None]
    k = sum(1 for r in graded if r.passed)
    lo, hi = wilson(k, len(graded))
    return {
        "n": len(graded), "k": k, "rate": k / max(len(graded), 1), "lo": lo, "hi": hi,
        "startup": statistics.median([r.startup_tokens for r in rs]) if rs else 0,
        "total": statistics.mean([r.input_tokens for r in rs]) if rs else 0,
        "cost": statistics.mean([r.cost_usd for r in rs]) if rs else 0,
        # Repeat runs share a warm prompt cache with the runs before them, which
        # discounts a big startup prefix; the first run of each probe shows it cold.
        "cost_first": statistics.mean([r.cost_usd for r in rs if r.rep == 0] or [0]),
        "cost_repeat": statistics.mean([r.cost_usd for r in rs if r.rep > 0] or [0]),
        "turns": statistics.mean([r.turns for r in rs]) if rs else 0,
    }


def _bars_panel(title: str, before: float, after: float, fmt) -> str:
    """Two horizontal bars on a zero-based axis, value labels at the ends."""
    w, h, left, right, bar, gap, top = 320, 66, 58, 70, 18, 10, 8
    span = w - left - right
    top_val = max(before, after) or 1
    rows = []
    for i, (name, val, cls) in enumerate([("Before", before, "m-before"), ("After", after, "m-after")]):
        y = top + i * (bar + gap)
        bw = max(span * val / top_val, 2)
        tip = _esc(f"{name}: {fmt(val)}")
        rows.append(
            f'<g class="hit" data-tip="{tip}">'
            f'<rect x="0" y="{y - gap / 2}" width="{w}" height="{bar + gap}" fill="transparent"/>'
            f'<text x="{left - 8}" y="{y + bar / 2}" class="lbl" text-anchor="end" dominant-baseline="central">{name}</text>'
            f'<path class="{cls}" d="M{left},{y} h{bw - 4} a4,4 0 0 1 4,4 v{bar - 8} a4,4 0 0 1 -4,4 h-{bw - 4} z"/>'
            f'<text x="{left + bw + 6}" y="{y + bar / 2}" class="val" dominant-baseline="central">{_esc(fmt(val))}</text>'
            "</g>"
        )
    delta = (after - before) / before if before else 0
    return (
        f'<figure class="panel"><figcaption><span class="ptitle">{_esc(title)}</span>'
        f'<span class="delta">{delta:+.0%}</span></figcaption>'
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{_esc(title)}: before {_esc(fmt(before))}, after {_esc(fmt(after))}">'
        f'<line x1="{left}" x2="{left}" y1="{top - 6}" y2="{top + 2 * bar + gap + 6}" class="axis"/>'
        + "".join(rows) + "</svg></figure>"
    )


def _dumbbell(probes: list[Probe], results: list[RunResult]) -> str:
    rate: dict[tuple[str, str], tuple[int, int]] = {}
    for p in probes:
        for v in ("before", "after"):
            rs = [r for r in results if r.probe_id == p.id and r.variant == v and r.passed is not None]
            rate[(p.id, v)] = (sum(1 for r in rs if r.passed), len(rs))
    order = sorted(probes, key=lambda p: (ACTION_ORDER.index(p.action) if p.action in ACTION_ORDER else 9, p.id))
    # Room for "p07 <rule text>" labels; bare "Rule 07" labels (anonymized) need far less.
    w, left, right, row, head = 760, (330 if any(p.rule for p in probes) else 100), 24, 22, 26
    span = w - left - right
    x = lambda f: left + span * f  # noqa: E731
    y, parts, last_action = head, [], None
    for p in order:
        if p.action != last_action:
            y += 10 if last_action else 0
            parts.append(f'<text x="0" y="{y + 8}" class="group">{_esc(ACTION_LABEL.get(p.action, p.action))}</text>')
            y += 20
            last_action = p.action
        kb, nb = rate[(p.id, "before")]
        ka, na = rate[(p.id, "after")]
        fb, fa = kb / max(nb, 1), ka / max(na, 1)
        rule = p.rule if len(p.rule) <= 42 else p.rule[:41].rstrip() + "…"
        name = f"{p.id} · {p.rule}" if p.rule else p.id
        tip = _esc(f"{name}\nBefore: {kb}/{nb} runs passed\nAfter: {ka}/{na} runs passed")
        cy = y + row / 2
        line = (f'<line x1="{x(min(fb, fa))}" x2="{x(max(fb, fa))}" y1="{cy}" y2="{cy}" class="link"/>'
                if fb != fa else "")
        parts.append(
            f'<g class="hit" data-tip="{tip}">'
            f'<rect x="0" y="{y}" width="{w}" height="{row}" class="rowbg"/>'
            f'<text x="0" y="{cy}" class="lbl" dominant-baseline="central"><tspan class="pid">{p.id}</tspan> {_esc(rule)}</text>'
            f"{line}"
            f'<circle cx="{x(fb)}" cy="{cy}" r="{6 if fb == fa else 5}" class="m-before ring"/>'
            f'<circle cx="{x(fa)}" cy="{cy}" r="5" class="m-after ring"/>'
            "</g>"
        )
        y += row
    ticks = "".join(
        f'<line x1="{x(t)}" x2="{x(t)}" y1="{head - 4}" y2="{y}" class="grid"/>'
        f'<text x="{x(t)}" y="{head - 10}" class="tick" text-anchor="middle">{t:.0%}</text>'
        for t in (0, 0.25, 0.5, 0.75, 1)
    )
    return (f'<svg viewBox="0 0 {w} {y + 6}" role="img" aria-label="Pass rate per probe, before and after">'
            f"{ticks}{''.join(parts)}</svg>")


def _table(probes: list[Probe], results: list[RunResult]) -> str:
    rows = []
    for p in probes:
        cells = []
        for v in ("before", "after"):
            rs = [r for r in results if r.probe_id == p.id and r.variant == v and r.passed is not None]
            cells.append(f"{sum(1 for r in rs if r.passed)}/{len(rs)}")
        rows.append(f"<tr><td>{p.id}</td><td>{_esc(p.action)}</td><td>{_esc(p.rule)}</td>"
                    f'<td class="num">{cells[0]}</td><td class="num">{cells[1]}</td></tr>')
    return ("<table><thead><tr><th>Probe</th><th>Change</th><th>Rule under test</th>"
            '<th class="num">Before</th><th class="num">After</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table>")


def anonymize(probes: list[Probe], results: list[RunResult]) -> tuple[list[Probe], list[RunResult]]:
    """Strip every word of the repo's rules: probes become "Rule 01", "Rule 02", … for publishing."""
    order = sorted(probes, key=lambda p: (ACTION_ORDER.index(p.action) if p.action in ACTION_ORDER else 9, p.id))
    names = {p.id: f"Rule {k + 1:02d}" for k, p in enumerate(order)}
    anon = [replace(p, id=names[p.id], section_id="", section_title="", rule="", prompt="", files=[],
                    rubric="", original="", related="", review_note="") for p in probes]
    runs = [replace(r, probe_id=names.get(r.probe_id, r.probe_id), answer="", judge_reason="", files_read=[])
            for r in results]
    return anon, runs


def render_eval_html(probes: list[Probe], results: list[RunResult], model: str, title: str = "CLAUDE.md compaction",
                     summary_only: bool = False) -> str:
    """summary_only leaves out the per-probe chart and table: headline numbers and paired counts only."""
    b, a = _summary(results, "before"), _summary(results, "after")
    reps = max((r.rep for r in results), default=0) + 1
    pairs = defaultdict(dict)
    for r in results:
        pairs[(r.probe_id, r.rep)][r.variant] = r.passed
    both = sum(1 for v in pairs.values() if v.get("before") and v.get("after"))
    only_b = sum(1 for v in pairs.values() if v.get("before") and v.get("after") is False)
    only_a = sum(1 for v in pairs.values() if v.get("after") and v.get("before") is False)
    neither = sum(1 for v in pairs.values() if v.get("before") is False and v.get("after") is False)

    def tile(label: str, before: str, after: str, note: str) -> str:
        return (f'<div class="tile"><div class="tlabel">{_esc(label)}</div>'
                f'<div class="tval"><span class="tb">{_esc(before)}</span><span class="arrow">→</span>'
                f'<span class="ta">{_esc(after)}</span></div><div class="tnote">{_esc(note)}</div></div>')

    tiles = "".join([
        tile("Startup context", _fmt_tokens(b["startup"]), _fmt_tokens(a["startup"]),
             f"{_change(b['startup'], a['startup'])} tokens before the first message (median)"),
        tile("Tokens per session", _fmt_tokens(b["total"]), _fmt_tokens(a["total"]),
             f"{_change(b['total'], a['total'])} total input, all turns (mean)"),
        tile("Cost per session, first run", f"${b['cost_first']:.2f}", f"${a['cost_first']:.2f}",
             f"{_change(b['cost_first'], a['cost_first'])} at list price, prompt cache mostly cold"
             + (f"; repeat runs with a warm cache ${b['cost_repeat']:.2f} → ${a['cost_repeat']:.2f}" if reps > 1 else "")),
        tile("Rules followed", f"{b['rate']:.0%}", f"{a['rate']:.0%}",
             f"95% CI {b['lo']:.0%}–{b['hi']:.0%} vs {a['lo']:.0%}–{a['hi']:.0%}"),
    ])
    panels = "".join([
        _bars_panel("Startup context (tokens)", b["startup"], a["startup"], _fmt_tokens),
        _bars_panel("Input tokens per session", b["total"], a["total"], _fmt_tokens),
        _bars_panel("Cost, first run (USD)", b["cost_first"], a["cost_first"], lambda v: f"${v:.2f}"),
        _bars_panel("Turns per session", b["turns"], a["turns"], lambda v: f"{v:.1f}"),
    ])
    legend = ('<div class="legend"><span><i class="sw m-before"></i>Before (original CLAUDE.md)</span>'
              '<span><i class="sw m-after"></i>After (compacted)</span></div>')
    if summary_only:
        per_probe = '<p class="hint">Every probe ran on both versions and was graded blind. Each run is paired with the same probe and run on the other version.</p>'
        table = ""
    else:
        per_probe = ('<p class="hint">Share of runs that passed, per probe. A single dot means both versions scored the same; '
                     "a line shows the gap.\nProbes are grouped by what the compaction did to the rule they test.</p>\n"
                     f'{legend}\n<div class="card scroll dumbbell">{_dumbbell(probes, results)}</div>')
        table = f'<details><summary>Table view</summary><div class="scroll">{_table(probes, results)}</div></details>'
    sub = (f"{len(probes)} rule probes × {reps} run{'s' if reps > 1 else ''} × 2 variants on {model}. "
           f"Each run is a fresh headless Claude Code session in a throwaway worktree; a blind judge grades it.")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Compaction eval</title>
<style>
:root {{ color-scheme: light; --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10); --before:#86b6ef; --after:#1c5cab; --hover:rgba(11,11,11,.04); }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ color-scheme: dark; --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --before:#256abf; --after:#86b6ef; --hover:rgba(255,255,255,.05); }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --plane:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10); --before:#256abf; --after:#86b6ef; --hover:rgba(255,255,255,.05); }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--plane); color:var(--ink); font:14px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 980px; margin: 0 auto; padding: 32px 16px 64px; }}
h1 {{ font-size: 24px; margin: 0 0 6px; letter-spacing: -.01em; }}
h2 {{ font-size: 16px; margin: 36px 0 4px; }}
.sub, .hint {{ color: var(--ink2); margin: 0 0 18px; max-width: 70ch; }}
.tiles {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(min(210px, 100%), 1fr)); gap: 12px; }}
.tile, .card {{ background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 14px 16px; }}
.tlabel {{ color: var(--ink2); font-size: 13px; }}
.tval {{ font-size: 23px; font-weight: 600; font-variant-numeric: tabular-nums; margin: 4px 0 2px; }}
.tb {{ color: var(--ink2); font-weight: 500; }} .arrow {{ color: var(--muted); margin: 0 8px; font-weight: 400; }}
.tnote {{ color: var(--muted); font-size: 12px; }}
.legend {{ display:flex; gap: 18px; flex-wrap: wrap; color: var(--ink2); font-size: 13px; margin: 8px 0 12px; }}
.sw {{ display:inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }}
.panels {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(min(200px, 100%), 1fr)); gap: 12px; }}
.panel {{ margin:0; background: var(--surface); border:1px solid var(--ring); border-radius:12px; padding: 12px 14px 6px; }}
.panel figcaption {{ display:flex; justify-content: space-between; align-items: baseline; }}
.ptitle {{ font-weight: 600; font-size: 13px; }} .delta {{ color: var(--ink2); font-variant-numeric: tabular-nums; font-size: 13px; }}
svg {{ width: 100%; height: auto; display: block; overflow: visible; }}
.m-before {{ fill: var(--before); background: var(--before); }} .m-after {{ fill: var(--after); background: var(--after); }}
.ring {{ stroke: var(--surface); stroke-width: 2; }}
.lbl {{ fill: var(--ink2); font-size: 12px; }} .val {{ fill: var(--ink); font-size: 12px; font-variant-numeric: tabular-nums; }}
.pid {{ fill: var(--muted); font-variant-numeric: tabular-nums; }}
.tick {{ fill: var(--muted); font-size: 11px; }} .grid {{ stroke: var(--grid); stroke-width: 1; }}
.axis {{ stroke: var(--axis); stroke-width: 1; }} .link {{ stroke: var(--muted); stroke-width: 2; }}
.group {{ fill: var(--ink); font-size: 12px; font-weight: 600; }}
.rowbg {{ fill: transparent; }} .hit:hover .rowbg {{ fill: var(--hover); }}
.pairs {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)); gap: 12px; margin-top: 8px; }}
.pairs b {{ display:block; font-size: 22px; font-variant-numeric: tabular-nums; }} .pairs span {{ color: var(--ink2); font-size: 13px; }}
details {{ margin-top: 12px; }} summary {{ cursor: pointer; color: var(--ink2); }}
table {{ width:100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }}
th, td {{ text-align:left; padding: 6px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }}
th {{ color: var(--ink2); font-weight: 600; }} .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.scroll {{ overflow-x: auto; }}
.dumbbell svg {{ min-width: 640px; }}
h1 {{ overflow-wrap: anywhere; }}
#tip {{ position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--ring);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; white-space: pre-line; max-width: 340px;
  box-shadow: 0 4px 16px rgba(0,0,0,.12); opacity: 0; transition: opacity .08s; z-index: 10; }}
</style></head>
<body><main>
<h1>Before vs after: {_esc(title)}</h1>
<p class="sub">{_esc(sub)}</p>
<div class="tiles">{tiles}</div>

<h2>What a session costs</h2>
<p class="hint">Each panel has its own scale; bars start at zero.</p>
{legend}
<div class="panels">{panels}</div>

<h2>Are the rules still followed?</h2>
{per_probe}
<div class="pairs">
  <div class="card"><b>{both}</b><span>run pairs pass in both</span></div>
  <div class="card"><b>{only_b}</b><span>pass only before</span></div>
  <div class="card"><b>{only_a}</b><span>pass only after</span></div>
  <div class="card"><b>{neither}</b><span>fail in both</span></div>
</div>
{table}
</main>
<div id="tip" role="tooltip"></div>
<script>
const tip = document.getElementById('tip');
document.querySelectorAll('.hit').forEach(el => {{
  el.addEventListener('mousemove', e => {{
    tip.textContent = el.dataset.tip; tip.style.opacity = 1;
    const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = x + 'px'; tip.style.top = (e.clientY + 14) + 'px';
  }});
  el.addEventListener('mouseleave', () => {{ tip.style.opacity = 0; }});
}});
</script>
</body></html>
"""


def load_results(path) -> list[RunResult]:
    text = path.read_text(encoding="utf-8")
    rows = json.loads(text) if text.lstrip().startswith("[") else [json.loads(l) for l in text.splitlines() if l.strip()]
    return [RunResult(**r) for r in rows]
