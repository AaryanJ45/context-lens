"""Terminal and JSON renderings of a Profile."""

from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict
from pathlib import Path

from .discovery import STARTUP
from .pricing import PRICES_AS_OF
from .profile import Profile

TOP_SECTIONS = 15


def _n(v: int | None) -> str:
    return "n/a" if v is None else f"{v:,}"


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _rel(path: Path, repo: Path) -> str:
    s = str(path)
    if s.startswith(str(repo) + os.sep):
        return s[len(str(repo)) + 1 :]
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


def savings_buckets(p: Profile) -> dict[str, int]:
    """Tokens in startup files that a compaction could move or drop, by reason."""
    buckets: dict[str, int] = defaultdict(int)
    for f in p.startup_files:
        if f.file.kind == "auto-memory":
            continue
        for s in f.sections:
            if s.hint != "core":
                buckets[s.hint.split(":")[0]] += s.tokens
    return dict(buckets)


def render_text(p: Profile) -> str:
    est = "" if p.token_mode == "exact" else "≈"
    lines: list[str] = []
    w = lines.append
    w(f"context-lens · {p.repo}")
    w(f"token counts: {'exact (count_tokens API)' if p.token_mode == 'exact' else 'estimated (pip install context-lens[exact] for exact)'}")
    w("")

    w("STARTUP CONTEXT")
    if p.sessions:
        vals = [s.startup_tokens for s in p.sessions]
        measured = p.measured_startup or 0
        w(f"  measured over {len(p.sessions)} sessions in the last {p.days} days")
        w(f"  median tokens before your first message   {measured:>10,}   (range {min(vals):,}–{max(vals):,})")
        w(f"  from instruction files below              {est}{p.startup_file_tokens:>9,}   ({p.startup_file_tokens / measured:.0%})")
        w(f"  Claude Code prompt, tools, MCP (inferred) {est}{max(measured - p.startup_file_tokens, 0):>9,}")
        w(f"  median turns per session                  {p.median_turns:>10,}")
        w(f"  sessions per day                          {p.sessions_per_day:>10.1f}")
        costs = [s.cost_usd for s in p.sessions if s.cost_usd is not None]
        if costs:
            w(f"  API-equivalent cost per session (median)  {_usd(statistics.median(costs)):>10}")
        fc = p.file_cost_per_session(p.startup_file_tokens)
        if fc is not None:
            w(f"    of which instruction files              {_usd(fc):>10}   (written once, re-read every turn)")
    else:
        w("  no Claude Code transcripts found for this repo; showing file sizes only")
        w(f"  instruction files at startup              {est}{p.startup_file_tokens:>9,}")
    w("")

    w("FILES")
    w(f"  {'tokens':>9}  {'loaded':<10} {'kind':<14} path")
    order = sorted(p.files, key=lambda f: (f.file.loaded != STARTUP, -f.tokens))
    for f in order:
        w(f"  {est}{f.tokens:>8,}  {f.file.loaded:<10} {f.file.kind:<14} {_rel(f.file.path, p.repo)}")
    w("")

    w("WHERE THE TOKENS ARE (by top-level heading)")
    for f in [f for f in order if f.file.loaded == STARTUP and f.sections][:3]:
        roll: dict[str, int] = defaultdict(int)
        for s in f.sections:
            parts = s.section.title.split(" › ")
            roll[parts[1] if len(parts) > 1 and s.section.level >= 2 else parts[0]] += s.tokens
        w(f"  {_rel(f.file.path, p.repo)}")
        for title, tok in sorted(roll.items(), key=lambda kv: -kv[1])[:6]:
            w(f"    {est}{tok:>8,}  {tok / max(f.tokens, 1):>4.0%}  {title}")
    w("")

    secs = sorted(
        ((f, s) for f, s in p.all_sections() if f.file.loaded == STARTUP), key=lambda fs: -fs[1].tokens
    )
    w(f"BIGGEST SECTIONS LOADED AT STARTUP (top {TOP_SECTIONS} of {len(secs)})")
    w(f"  {'tokens':>8}  {'hint':<20} {'edited':<10} section")
    for f, s in secs[:TOP_SECTIONS]:
        edited = s.last_edited.date().isoformat() if s.last_edited else "-"
        where = f"{_rel(f.file.path, p.repo)}:{s.section.start_line}"
        w(f"  {est}{s.tokens:>7,}  {s.hint:<20} {edited:<10} {s.section.title}  ({where})")
    w("")

    w("SIGNALS")
    startup_secs = [s for _, s in secs]
    struck = sum(s.struck_chars for s in startup_secs)
    w(f"  struck-through text            {struck:>9,} chars in {sum(1 for s in startup_secs if s.struck_chars)} sections")
    w(f"  'corrected <date>' notes       {sum(s.corrections for s in startup_secs):>9,}")
    w(f"  ticket ids mentioned           {sum(s.tickets for s in startup_secs):>9,}")
    dups = sorted((s for s in startup_secs if s.hint == "reference"), key=lambda s: -s.tokens)
    if dups:
        w(f"  sections repeating a repo doc  {len(dups):>9,}   e.g. {dups[0].section.title!r} ≈ {dups[0].dup_doc} ({dups[0].dup_share:.0%})")
    scoped: dict[str, int] = defaultdict(int)
    for s in startup_secs:
        if s.hint.startswith("scoped:"):
            scoped[s.hint[7:]] += s.tokens
    if scoped:
        top = ", ".join(f"{d} {t:,}" for d, t in sorted(scoped.items(), key=lambda kv: -kv[1])[:4])
        w(f"  only about one directory       {est}{sum(scoped.values()):>8,} tokens   ({top})")
    missing = sorted({r for s in startup_secs for r in s.missing_refs})
    if missing:
        w(f"  referenced paths not found     {len(missing):>9,}   e.g. {', '.join(missing[:4])}")
    w("")

    if len(p.history) >= 2:
        a, b = p.history[0], p.history[-1]
        w(f"GROWTH  CLAUDE.md {a.bytes / 1024:,.1f} KB ({a.when.date()}) → {b.bytes / 1024:,.1f} KB ({b.when.date()}) over {len(p.history)} commits")
        w("")

    buckets = savings_buckets(p)
    total = sum(buckets.values())
    if total and p.startup_file_tokens:
        w("COMPACTION CANDIDATES (heuristic; `compact` reviews every section)")
        for name, why in (("history", "struck-through / corrected history, kept in git"),
                          ("reference", "repeats a doc; replace with a pointer"),
                          ("scoped", "about one directory; load on demand")):
            if buckets.get(name):
                w(f"  {name:<10} {est}{buckets[name]:>8,}   {why}")
        w(f"  total      {est}{total:>8,}   ({total / p.startup_file_tokens:.0%} of startup instruction files)")
        w("")
    w(f"prices: API list, as of {PRICES_AS_OF}")
    return "\n".join(lines)


def to_json(p: Profile) -> str:
    def sec(s):
        return {
            "title": s.section.title, "start_line": s.section.start_line, "end_line": s.section.end_line,
            "tokens": s.tokens, "hint": s.hint, "struck_chars": s.struck_chars, "corrections": s.corrections,
            "tickets": s.tickets, "path_refs": s.path_refs, "missing_refs": s.missing_refs,
            "scope_dir": s.scope_dir, "dup_doc": s.dup_doc, "dup_share": round(s.dup_share, 3),
            "last_edited": s.last_edited.isoformat() if s.last_edited else None,
        }

    data = {
        "repo": str(p.repo), "token_mode": p.token_mode, "days": p.days, "prices_as_of": PRICES_AS_OF,
        "startup_file_tokens": p.startup_file_tokens, "measured_startup_median": p.measured_startup,
        "median_turns": p.median_turns, "sessions_per_day": p.sessions_per_day,
        "files": [
            {"path": str(f.file.path), "kind": f.file.kind, "loaded": f.file.loaded, "tokens": f.tokens,
             "scope": f.file.scope, "sections": [sec(s) for s in f.sections]}
            for f in p.files
        ],
        "sessions": [
            {"session_id": s.session_id, "started": s.started.isoformat(), "cwd": s.cwd, "model": s.model,
             "startup_tokens": s.startup_tokens, "turns": s.turns, "cost_usd": s.cost_usd}
            for s in p.sessions
        ],
        "history": [{"commit": h.commit, "when": h.when.isoformat(), "bytes": h.bytes} for h in p.history],
        "savings_buckets": savings_buckets(p),
    }
    return json.dumps(data, indent=2)
