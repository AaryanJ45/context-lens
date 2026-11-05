"""Shared report helpers and the JSON form of a Profile."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path

from .pricing import PRICES_AS_OF
from .profile import Profile


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
