"""What sessions actually did: tool calls, failures, repeated work, context growth.

Read from the same transcripts as the startup numbers, entirely locally. Only
the main chain counts (subagent records are skipped), and one API response
split over several transcript records is counted once.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .tokens import CHARS_PER_TOKEN_ESTIMATE

FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# A tool result past this size is one Claude carries for the rest of the session.
LARGE_RESULT_TOKENS = 5_000
KEY_CHARS = 160
PROMPT_CHARS = 400
_COMMAND_RE = re.compile(r"<command-name>/?([^<\s]+)</command-name>")


@dataclass
class Call:
    name: str
    key: str
    error: bool = False
    result_tokens: int = 0


@dataclass
class SessionActivity:
    session_id: str
    path: Path
    started: datetime | None
    calls: list[Call] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    context: list[int] = field(default_factory=list)  # tokens in context at each turn
    compactions: int = 0
    rereads: int = 0
    reread_tokens: int = 0
    retry_loops: int = 0
    large_results: int = 0
    large_result_tokens: int = 0
    commands: Counter = field(default_factory=Counter)  # slash commands the user typed
    # One line per prompt and per tool call, in order: what the rule judge sees.
    timeline: list[str] = field(default_factory=list, repr=False)

    @property
    def peak_context(self) -> int:
        return max(self.context, default=0)

    @property
    def errors(self) -> int:
        return sum(c.error for c in self.calls)

    def tool_counts(self) -> Counter:
        return Counter(c.name for c in self.calls)

    def error_counts(self) -> Counter:
        return Counter(c.name for c in self.calls if c.error)



def mcp_server(tool: str) -> str | None:
    if not tool.startswith("mcp__"):
        return None
    parts = tool.split("__")
    return parts[1] if len(parts) >= 3 else None


def _key(name: str, inp: dict) -> str:
    if name in FILE_TOOLS:
        key = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if name == "Read" and (inp.get("offset") or inp.get("limit")):
            key += f"#{inp.get('offset') or 0}+{inp.get('limit') or ''}"
        return key
    if name == "Bash":
        return " ".join(str(inp.get("command", "")).split())[:KEY_CHARS]
    if name in ("Grep", "Glob"):
        return f"{inp.get('pattern', '')} {inp.get('path', '') or ''}".strip()[:KEY_CHARS]
    if name == "Skill":
        return str(inp.get("skill", ""))
    try:
        return json.dumps(inp, sort_keys=True, ensure_ascii=False)[:KEY_CHARS]
    except (TypeError, ValueError):
        return ""


def _result_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    return 0


def _prompt_text(content) -> str | None:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    text = text.strip()
    # Harness-injected messages (reminders, command caveats) are not the user's words.
    if not text or text.startswith("<"):
        return None
    return " ".join(text.split())[:PROMPT_CHARS]


def read_activity(path: Path) -> SessionActivity:
    act = SessionActivity(session_id=path.stem, path=path, started=None)
    pending: dict[str, Call] = {}
    seen_tool_ids: set[str] = set()
    seen_msg_ids: set[str] = set()
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain"):
                continue
            if rec.get("type") == "system" and rec.get("subtype") == "compact_boundary":
                act.compactions += 1
                act.timeline.append("[context compacted]")
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            if act.started is None and rec.get("timestamp"):
                act.started = datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00"))
                act.session_id = rec.get("sessionId") or act.session_id
            content = msg.get("content")
            if rec.get("type") == "assistant":
                usage = msg.get("usage") or {}
                mid = msg.get("id") or rec.get("requestId") or rec.get("uuid")
                if usage and mid not in seen_msg_ids:
                    seen_msg_ids.add(mid)
                    act.context.append(sum(usage.get(k, 0) or 0 for k in (
                        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tid = block.get("id")
                    if tid in seen_tool_ids:
                        continue
                    seen_tool_ids.add(tid)
                    name = block.get("name", "?")
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    call = Call(name, _key(name, inp))
                    act.calls.append(call)
                    pending[tid] = call
                    act.timeline.append(f"{name}: {call.key}" if call.key else name)
            elif rec.get("type") == "user" and not rec.get("isMeta"):
                if isinstance(content, str):
                    for name in _COMMAND_RE.findall(content):
                        act.commands[name] += 1
                        act.timeline.append(f"USER ran /{name}")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            call = pending.pop(block.get("tool_use_id"), None)
                            if call:
                                call.error = bool(block.get("is_error"))
                                call.result_tokens = int(_result_chars(block.get("content")) / CHARS_PER_TOKEN_ESTIMATE)
                                if call.error:
                                    act.timeline.append(f"  -> {call.name} failed")
                prompt = _prompt_text(content)
                if prompt:
                    act.prompts.append(prompt)
                    act.timeline.append(f"USER: {prompt}")
    _waste(act)
    return act


def _waste(act: SessionActivity) -> None:
    """Re-reads of unchanged files, repeated failing calls, and oversized outputs."""
    last_read: dict[str, bool] = {}  # key -> file changed since that read
    prev: Call | None = None
    streak = 0
    for call in act.calls:
        if call.name == "Read" and call.key:
            if call.key in last_read and not last_read[call.key] and not call.error:
                act.rereads += 1
                act.reread_tokens += call.result_tokens
            last_read[call.key] = False
        elif call.name in WRITE_TOOLS:
            path = call.key
            for k in last_read:
                if k == path or k.startswith(path + "#"):
                    last_read[k] = True
        if call.error and prev is not None and prev.error and prev.name == call.name and prev.key == call.key:
            streak += 1
            if streak == 1:
                act.retry_loops += 1
        else:
            streak = 0
        if call.result_tokens >= LARGE_RESULT_TOKENS:
            act.large_results += 1
            act.large_result_tokens += call.result_tokens
        prev = call
