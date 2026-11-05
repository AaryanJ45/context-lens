"""Shared HTML pieces: no external scripts, fonts or images."""

from __future__ import annotations

import html as h
from collections import defaultdict

from .discovery import STARTUP
from .profile import Profile
from .report import _rel, savings_buckets

HINT_COLORS = {"core": "var(--muted)", "history": "var(--c3)", "reference": "var(--c2)", "scoped": "var(--c1)"}


def _e(v: object) -> str:
    return h.escape(str(v))


def _line_chart(points: list[tuple[str, float]], unit: str, width: int = 640, height: int = 180) -> str:
    if len(points) < 2:
        return "<p class=muted>Not enough history to chart.</p>"
    pad_l, pad_b, pad_t = 56, 24, 12
    ys = [y for _, y in points]
    top = max(ys) * 1.1 or 1
    step = (width - pad_l - 8) / (len(points) - 1)
    coords = [(pad_l + i * step, pad_t + (1 - y / top) * (height - pad_t - pad_b)) for i, (_, y) in enumerate(points)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    ticks = "".join(
        f'<text x="{pad_l - 6}" y="{pad_t + (1 - f) * (height - pad_t - pad_b) + 4:.1f}" text-anchor="end">'
        f"{top * f:,.0f}{unit}</text>"
        f'<line x1="{pad_l}" x2="{width - 8}" y1="{pad_t + (1 - f) * (height - pad_t - pad_b):.1f}" '
        f'y2="{pad_t + (1 - f) * (height - pad_t - pad_b):.1f}" class="grid"/>'
        for f in (0, 0.5, 1)
    )
    labels = (
        f'<text x="{pad_l}" y="{height - 6}">{_e(points[0][0])}</text>'
        f'<text x="{width - 8}" y="{height - 6}" text-anchor="end">{_e(points[-1][0])}</text>'
    )
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5"><title>{_e(points[i][0])}: {points[i][1]:,.0f}{unit}</title></circle>'
        for i, (x, y) in enumerate(coords)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">{ticks}{labels}'
        f'<path d="{path}" class="line"/>{dots}</svg>'
    )


def _stack(parts: list[tuple[str, int, str]]) -> str:
    total = sum(v for _, v, _ in parts) or 1
    bars = "".join(
        f'<div style="flex:{v};background:{c}" title="{_e(label)}: {v:,}"></div>' for label, v, c in parts if v
    )
    legend = "".join(
        f'<li><span class="sw" style="background:{c}"></span>{_e(label)} <b>{v:,}</b> ({v / total:.0%})</li>'
        for label, v, c in parts if v
    )
    return f'<div class="stack">{bars}</div><ul class="legend">{legend}</ul>'


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#8a877f;--line:#e4e1d9;--card:#fff;
--c1:#3b6fd8;--c2:#c2603a;--c3:#8a4fb8;--c4:#2f9a76;--c5:#c79a22;--c6:#5a8ca8}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#16171a;--fg:#ecebe6;--muted:#8d8b85;--line:#2c2e33;--card:#1e2024;
--c1:#6f97ee;--c2:#e08a63;--c3:#b585e0;--c4:#5cc39e;--c5:#e2bb52;--c6:#84b3cc}}
:root[data-theme="dark"]{--bg:#16171a;--fg:#ecebe6;--muted:#8d8b85;--line:#2c2e33;--card:#1e2024}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}
main{max-width:1040px;margin:0 auto;padding:32px 16px 64px}h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:36px 0 12px}
.muted{color:var(--muted);font-size:13px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}.label{font-size:13px;color:var(--muted)}
.value{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}.sub{font-size:12px;color:var(--muted)}
.stack{display:flex;height:22px;border-radius:6px;overflow:hidden;border:1px solid var(--line)}
.legend{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.chart{width:100%;height:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
.chart text{fill:var(--muted);font-size:11px}.chart .grid{stroke:var(--line)}.chart .line{fill:none;stroke:var(--c1);stroke-width:2}.chart circle{fill:var(--c1)}
.wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px;background:var(--card);border:1px solid var(--line);border-radius:10px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:600}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.tag{border:1.5px solid;border-radius:99px;padding:1px 8px;font-size:12px;white-space:nowrap}
.status{font-weight:650}.fix{color:var(--c2)}.look{color:var(--c5)}.ok{color:var(--c4)}
.areas{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.area{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.area h3{margin:0 0 8px;font-size:15px;display:flex;justify-content:space-between;gap:8px}
.area dl{display:grid;grid-template-columns:1fr auto;gap:2px 12px;margin:0;font-size:13px}.area dt{color:var(--muted)}
.area dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
ol.fixes{padding-left:22px}ol.fixes li{margin:0 0 18px}.fixes .t{font-weight:600}.fixes p{margin:4px 0}
.fixes ul{margin:6px 0 0;padding-left:18px;font-size:13px;color:var(--muted);overflow-wrap:anywhere}
.bars{display:grid;grid-template-columns:minmax(0,1fr) 2fr auto;gap:6px 10px;align-items:center;font-size:13px}
.bars .lbl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bar{height:12px;border-radius:3px;background:var(--c1)}
.bar.bad{background:var(--c2)}
.cta{margin:24px 0 8px;padding:18px 20px;background:var(--card);border:1px solid var(--line);border-left:4px solid var(--c1);border-radius:10px}
.cta h2{margin:4px 0 8px}.cta p{margin:6px 0}.cta .quotes{margin:8px 0;padding-left:18px;font-size:14px;color:var(--muted);overflow-wrap:anywhere}
.cmd{display:flex;gap:8px;align-items:center;margin:12px 0 8px;flex-wrap:wrap}
.cmd code{font:14px ui-monospace,SFMono-Regular,Menlo,monospace;padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.cmd button{font:inherit;font-size:13px;padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}.privacy{border-left:3px solid var(--c4);padding:8px 12px;background:var(--card);font-size:14px}
"""


def startup_html(p: Profile) -> str:
    """The startup-context sections of the doctor report."""
    est = "" if p.token_mode == "exact" else "≈"
    measured = p.measured_startup
    kinds: dict[str, int] = defaultdict(int)
    for f in p.startup_files:
        kinds[f.file.kind] += f.tokens
    palette = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)"]
    parts = [(k, v, palette[i % len(palette)]) for i, (k, v) in enumerate(sorted(kinds.items(), key=lambda kv: -kv[1]))]
    if measured:
        parts.append(("Claude Code prompt, tools, MCP (inferred)", max(measured - p.startup_file_tokens, 0), "var(--muted)"))
    rows = sorted(((f, s) for f, s in p.all_sections() if f.file.loaded == STARTUP), key=lambda fs: -fs[1].tokens)
    table = "".join(
        f"<tr><td class=num>{est}{s.tokens:,}</td>"
        f'<td><span class=tag style="border-color:{HINT_COLORS.get(s.hint.split(":")[0], "var(--muted)")}">{_e(s.hint)}</span></td>'
        f"<td style=\"white-space:nowrap\">{_e(s.last_edited.date() if s.last_edited else '-')}</td>"
        f"<td>{_e(s.section.title)}<div class=muted>{_e(_rel(f.file.path, p.repo))}:{s.section.start_line}"
        f"{' · repeats ' + _e(s.dup_doc) + f' ({s.dup_share:.0%})' if s.hint == 'reference' else ''}"
        f"{' · ' + str(len(s.missing_refs)) + ' paths not found' if s.missing_refs else ''}</div></td></tr>"
        for f, s in rows[:40]
    )
    buckets = savings_buckets(p)
    bucket_html = "".join(
        f"<li><b>{est}{v:,}</b> {_e(k)}</li>" for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])
    ) or "<li>none detected</li>"
    growth = _line_chart([(pt.when.date().isoformat(), pt.bytes / 1024) for pt in p.history], " KB")
    sessions_chart = _line_chart([(s.started.date().isoformat(), s.startup_tokens) for s in p.sessions], "")
    return f"""<h2>What fills the context at startup</h2>{_stack(parts)}
<h2>Startup tokens per session</h2>{sessions_chart}
<h2>CLAUDE.md size over its git history</h2>{growth}
<h2>Compaction candidates</h2><ul>{bucket_html}</ul>
<p class=muted>Heuristic signals. `context-lens doctor --compact` reviews every section before anything moves.</p>
<h2>Biggest sections loaded at startup</h2>
<div class=wrap><table><thead><tr><th class=num>Tokens</th><th>Hint</th><th>Last edited</th><th>Section</th></tr></thead><tbody>{table}</tbody></table></div>"""
