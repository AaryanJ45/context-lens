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
# What Claude Code writes back when the user says no to a tool call; the reason they typed follows.
_REJECTED = "doesn't want to proceed with this tool use"
_REJECT_REASON_RE = re.compile(r"the user said:\s*(.+)", re.I | re.S)
_INTERRUPT = "[Request interrupted by user"
# Pushback at the start of a prompt: "no, …", "don't …", "I told you …", "why did you …".
_PUSHBACK_RE = re.compile(
    r"^(?:no\b|nope\b|don'?t\b|do not\b|stop\b|wait\b|wrong\b|that'?s (?:not|wrong)\b|not that\b|"
    r"i (?:told|said|asked) you\b|why (?:did|are) you\b|you (?:should|shouldn'?t|didn'?t|forgot)\b|"
    r"never\b|please don'?t\b|again,? )",
    re.I,
)
BEFORE_LINES = 3


@dataclass
class Call:
    name: str
    key: str
    error: bool = False
    rejected: bool = False  # the user said no; not counted as a failure
    result_tokens: int = 0


@dataclass
class Correction:
    """A moment the user told Claude it was doing the wrong thing."""

    kind: str  # rejected | interrupted | pushback
    said: str  # the user's words (may be empty for a bare rejection)
    before: list[str]  # what Claude did just before, newest last
    session_id: str = ""
    when: datetime | None = None


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
    corrections: list[Correction] = field(default_factory=list)
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


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _clean(text: str) -> str:
    return " ".join(text.split())[:PROMPT_CHARS]


def _ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (AttributeError, ValueError):
        return None


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
    return _clean(text)


def read_activity(path: Path) -> SessionActivity:
    act = SessionActivity(session_id=path.stem, path=path, started=None)
    pending: dict[str, Call] = {}
    interrupted_before: list[str] | None = None  # set by an interruption; the next prompt says why
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
            when = _ts(rec.get("timestamp"))
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
                                text = _result_text(block.get("content"))
                                call.rejected = bool(block.get("is_error")) and _REJECTED in text
                                call.error = bool(block.get("is_error")) and not call.rejected
                                call.result_tokens = int(_result_chars(block.get("content")) / CHARS_PER_TOKEN_ESTIMATE)
                                # Declining to answer a clarifying question is not a correction.
                                if call.rejected and call.name != "AskUserQuestion":
                                    m = _REJECT_REASON_RE.search(text)
                                    said = _clean(m.group(1)) if m else ""
                                    act.corrections.append(Correction(
                                        "rejected", said, act.timeline[-BEFORE_LINES:], act.session_id, when))
                                    act.timeline.append(f"  -> user rejected {call.name}" + (f": {said}" if said else ""))
                                elif call.error:
                                    act.timeline.append(f"  -> {call.name} failed")
                prompt = _prompt_text(content)
                if prompt and prompt.startswith(_INTERRUPT):
                    interrupted_before = act.timeline[-BEFORE_LINES:]
                    act.timeline.append("[user interrupted]")
                elif prompt:
                    if interrupted_before is not None:
                        act.corrections.append(Correction("interrupted", prompt, interrupted_before, act.session_id, when))
                    elif _PUSHBACK_RE.match(prompt) and act.calls:
                        act.corrections.append(Correction(
                            "pushback", prompt, act.timeline[-BEFORE_LINES:], act.session_id, when))
                    interrupted_before = None
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
