"""The doctor report as one self-contained HTML page."""

from __future__ import annotations

import statistics
from collections import Counter

from .doctor import AREAS, Doctor
from .html import CSS, _e, _line_chart, startup_html
from .pricing import PRICES_AS_OF

STATUS_LABEL = {"fix": "Fix this", "look": "Worth a look", "ok": "Good"}


def _bars(rows: list[tuple[str, float, bool]], fmt: str = "{:,.0f}") -> str:
    if not rows:
        return "<p class=muted>No data.</p>"
    top = max(v for _, v, _ in rows) or 1
    return "<div class=bars>" + "".join(
        f'<div class=lbl title="{_e(label)}">{_e(label)}</div>'
        f'<div><div class="bar{" bad" if bad else ""}" style="width:{max(v / top * 100, 1):.1f}%"></div></div>'
        f"<div class=num>{fmt.format(v)}</div>"
        for label, v, bad in rows
    ) + "</div>"


def _command(cmd: str) -> str:
    return (f'<div class=cmd><code>{_e(cmd)}</code><button type=button onclick="navigator.clipboard'
            f'.writeText(this.previousElementSibling.textContent).then(()=>{{this.textContent=\'Copied\'}})">Copy</button></div>')


def _learn_cta(doc: Doctor) -> str:
    """After a plain checkup, point at --learn when there are corrections to learn from."""
    from .learn import gather

    if doc.lessons:
        return ""
    found = gather(doc.activity)
    if not found:
        return ""
    sessions = len({c.session_id for c in found})
    quotes = "".join([f"<li>\u201c{_e(c.said[:140])}\u201d</li>" for c in found if c.said][:3])
    return (
        "<section class=cta><div class=label>Next step</div>"
        f"<h2>You corrected Claude {len(found)} times in {sessions} sessions</h2>"
        "<p>Each time you rejected an action, interrupted, or said \u201cno, don\u2019t\u2026\u201d, you stated a rule "
        "your CLAUDE.md doesn\u2019t have. <b>--learn</b> groups the ones you repeat, writes the missing rules, and "
        "flags any of your instructions that say the opposite.</p>"
        f"<ul class=quotes>{quotes}</ul>"
        f"{_command('context-lens doctor --learn')}"
        f"<p class=muted>Run it in {_e(doc.profile.repo)}. One model call, about $0.25. It sends your redacted "
        "correction words and the action before each, never tool output or file contents. To see exactly what "
        "would be sent without sending it: <code>context-lens doctor --learn-dry-run</code></p></section>"
    )


def render_doctor_html(doc: Doctor) -> str:
    p, acts = doc.profile, doc.activity
    costs = [s.cost_usd for s in p.sessions if s.cost_usd is not None]
    calls = [c for a in acts for c in a.calls]
    errors = sum(c.error for c in calls)
    kpis = [
        ("Tokens before your first message", f"{p.measured_startup:,}" if p.measured_startup else "n/a",
         f"median of {len(p.sessions)} sessions"),
        ("Cost per session", f"${statistics.median(costs):,.2f}" if costs else "n/a", "API-equivalent, median"),
        ("Could stay out of context", f"≈{doc.recoverable:,}", "tokens per session, from the fixes below"),
        ("Failed tool calls", f"{errors / len(calls):.1%}" if calls else "n/a", f"{errors:,} of {len(calls):,}"),
    ]
    kpi_html = "".join(f"<div class=kpi><div class=label>{_e(a)}</div><div class=value>{_e(b)}</div>"
                       f"<div class=sub>{_e(c)}</div></div>" for a, b, c in kpis)

    areas = []
    for area in AREAS:
        status = doc.area_status(area) if doc.metrics.get(area) else None
        m = doc.metrics.get(area)
        body = ("<dl>" + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in m.items()) + "</dl>") if m else (
            "<p class=muted>Not checked. Run with <code>--rules</code> to have a model judge redacted excerpts "
            "of past sessions against your rules.</p>" if area == "Rule adherence" else "<p class=muted>No data.</p>")
        label = f'<span class="status {status}">{STATUS_LABEL[status]}</span>' if status else f'<span class=muted>{"Off" if area == "Rule adherence" else "No data"}</span>'
        if area == "Corrections" and m and not doc.lessons:
            body += "<p class=muted>Turn these into rules with <code>--learn</code>.</p>"
        areas.append(f"<div class=area><h3>{_e(area)}{label}</h3>{body}</div>")

    fixes = "".join(
        f'<li><div class=t><span class="status {f.severity}">{STATUS_LABEL[f.severity]}</span> · {_e(f.title)}'
        f"{f' <span class=muted>≈{f.tokens_per_session:,} tokens/session</span>' if f.tokens_per_session else ''}</div>"
        f"<p>{_e(f.detail)}</p><p><b>What to do:</b> {_e(f.fix)}</p>"
        + ("<ul>" + "".join(f"<li><code>{_e(e)}</code></li>" for e in f.evidence) + "</ul>" if f.evidence else "")
        + "</li>"
        for f in doc.ranked()
    ) or "<li>Nothing to fix.</li>"

    tools = Counter(c.name for c in calls)
    failed = Counter(c.name for c in calls if c.error)
    tool_rows = [(f"{name} ({failed[name]} failed)" if failed[name] else name, n, False) for name, n in tools.most_common(12)]
    peak_chart = _line_chart([(a.started.date().isoformat(), a.peak_context) for a in acts if a.started and a.context], "")
    mcp_rows = [(name, n, n == 0) for name, n in sorted(doc.mcp_servers.items(), key=lambda kv: -kv[1])]

    lessons_html = ""
    if doc.lessons:
        items = "".join(
            f"<li><div class=t>{_e(l.rule)}</div><p class=muted>{l.count}× · "
            f"{_e('already in ' + l.covered_by if l.covered_by else ('personal' if l.scope == 'personal' else 'repo'))}"
            f"{'' if l.covered_by else f' · add with <code>--add-rules {n}</code>'}</p><ul>"
            + "".join(f"<li>{_e(e['kind'])}: \u201c{_e(e['said'][:200])}\u201d"
                      f"{'<br><span class=muted>after: ' + _e(e['before'][:160]) + '</span>' if e['before'] else ''}</li>"
                      for e in l.examples[:4] if e["said"])
            + "</ul></li>"
            for n, l in enumerate(doc.lessons, 1)
        )
        lessons_html = f"<h2>Learned from your corrections</h2><ol class=fixes>{items}</ol>"

    rules_html = ""
    if doc.rules:
        rows = "".join(
            f"<tr><td>{_e(r.rule.id)}</td><td>{_e(r.rule.title)}<div class=muted>{_e(r.rule.source)}</div></td>"
            f"<td class=num>{r.followed}</td><td class=num>{r.violated}</td><td class=num>{r.not_applicable}</td>"
            f"<td class=num>{'' if r.adherence is None else f'{r.adherence:.0%}'}</td></tr>"
            for r in sorted(doc.rules, key=lambda r: (r.adherence is None, r.adherence or 0))
        )
        rules_html = ("<h2>Rule adherence</h2><div class=wrap><table><thead><tr><th>Id</th><th>Rule</th>"
                      "<th class=num>Followed</th><th class=num>Violated</th><th class=num>Didn't come up</th>"
                      f"<th class=num>Adherence</th></tr></thead><tbody>{rows}</tbody></table></div>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Context Lens Doctor</title><style>{CSS}</style></head><body><main>
<h1>Context Lens Doctor</h1>
<div class=muted>{_e(p.repo)} · {len(acts)} sessions in the last {p.days} days · token counts {'exact' if p.token_mode == 'exact' else 'estimated (≈)'} · API list prices as of {PRICES_AS_OF}</div>
<div class=kpis>{kpi_html}</div>
{_learn_cta(doc)}
<h2>Checkup</h2><div class=areas>{''.join(areas)}</div>
<h2>What to do, most important first</h2><ol class=fixes>{fixes}</ol>
{lessons_html}
<p class=privacy>{_e(doc.privacy)}</p>
<h2>Tools Claude used most</h2>{_bars(tool_rows)}
<h2>MCP servers: calls in this window</h2>{_bars(mcp_rows) if mcp_rows else '<p class=muted>No MCP servers configured.</p>'}
<h2>Peak context per session</h2>{peak_chart}
{rules_html}
{startup_html(p)}
</main></body></html>"""
