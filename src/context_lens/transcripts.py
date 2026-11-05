"""Read Claude Code transcripts (~/.claude/projects/*/*.jsonl) for real token usage.

The first main-chain assistant message of a session carries the startup
context as it was actually billed: input + cache writes + cache reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .pricing import session_cost


@dataclass
class SessionUsage:
    session_id: str
    cwd: str
    model: str
    started: datetime
    startup_tokens: int
    turns: int
    input_tokens: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    output_tokens: int
    cost_usd: float | None


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _usage_of(record: dict) -> dict | None:
    msg = record.get("message")
    if record.get("type") != "assistant" or not isinstance(msg, dict) or record.get("isSidechain"):
        return None
    return msg.get("usage")


def _first_cwd(path: Path) -> str | None:
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh):
            if n > 200:
                return None
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("cwd"):
                return rec["cwd"]
    return None


def read_session(path: Path) -> SessionUsage | None:
    first = None
    turns = inp = w5 = w1 = rd = out = 0
    seen_ids: set[str] = set()
    model = ""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            usage = _usage_of(rec)
            if not usage:
                continue
            # One API response can be split over several transcript records.
            mid = rec["message"].get("id") or rec.get("requestId") or rec.get("uuid")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            cc = usage.get("cache_creation") or {}
            write_total = usage.get("cache_creation_input_tokens", 0) or 0
            w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
            w5m = cc.get("ephemeral_5m_input_tokens", write_total - w1h) or 0
            turns += 1
            inp += usage.get("input_tokens", 0) or 0
            w5 += w5m
            w1 += w1h
            rd += usage.get("cache_read_input_tokens", 0) or 0
            out += usage.get("output_tokens", 0) or 0
            model = rec["message"].get("model") or model
            if first is None:
                first = (rec, usage)
    if first is None:
        return None
    rec, usage = first
    startup = sum(usage.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return SessionUsage(
        session_id=rec.get("sessionId") or path.stem,
        cwd=rec.get("cwd", ""),
        model=model,
        started=_parse_ts(rec["timestamp"]),
        startup_tokens=startup,
        turns=turns,
        input_tokens=inp,
        cache_write_5m=w5,
        cache_write_1h=w1,
        cache_read=rd,
        output_tokens=out,
        cost_usd=session_cost(model, inp, w5, w1, rd, out),
    )


def sessions_for(repo: Path, days: int, projects_dir: Path | None = None) -> list[SessionUsage]:
    """Sessions whose working directory is the repo or anything inside it."""
    projects_dir = projects_dir or Path.home() / ".claude" / "projects"
    repo_s = str(repo.resolve())
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[SessionUsage] = []
    if not projects_dir.is_dir():
        return out
    for path in projects_dir.glob("*/*.jsonl"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < since:
                continue
        except OSError:
            continue
        cwd = _first_cwd(path)
        if not cwd or not (cwd == repo_s or cwd.startswith(repo_s + "/")):
            continue
        session = read_session(path)
        if session and session.started >= since:
            out.append(session)
    return sorted(out, key=lambda s: s.started)
