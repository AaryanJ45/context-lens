"""Rule adherence: did past sessions follow the instruction files they loaded?

The only part of `doctor` that calls a model, and only when asked (`--rules`).
What leaves the machine, per sampled session:

* the rule text, taken from your own startup instruction files;
* your prompts and one line per tool call (the command, the file path, the
  search pattern), each cut to a few hundred characters and passed through
  `redact`, the whole excerpt capped at `EXCERPT_CHARS`.

Never sent: tool output, file contents, diffs, the assistant's prose. That
also bounds what the judge can see: a rule about the *content* of code (how a
component is styled, which helper formats money) usually has no evidence in a
command line, so it comes back `not_applicable` rather than a guess.
"""

from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .activity import SessionActivity
from .llm import Backend
from .profile import Profile
from .redact import redact

DEFAULT_JUDGE_MODEL = "claude-haiku-5-5"
EXCERPT_CHARS = 12_000
LINE_CHARS = 300
RULE_CHARS = 700
MIN_CALLS = 5
CANDIDATES = 80
PREVIEW_CHARS = 300

SELECT_SYSTEM = (
    "You sort a coding agent's project rules by whether they can be checked from a session log that shows "
    "only the user's prompts and one line per tool call (shell commands, file paths edited or read, search "
    "patterns, MCP tool names with arguments). Checkable: rules about which commands to run or avoid, git and "
    "branch workflow, tests and builds, which tools or skills to use, asking before an action, where files go. "
    "Not checkable: rules about the content of code (styling, which component or helper to use, formatting), "
    "and reference material that states facts rather than asks for an action."
)

SELECT_SCHEMA = {
    "type": "object",
    "properties": {"checkable": {"type": "array", "items": {"type": "string"}, "description": "rule ids"}},
    "required": ["checkable"],
}

SYSTEM = (
    "You audit a coding agent's session against its project rules. You see the user's prompts and one line "
    "per tool call the agent made, nothing else. For each rule decide: `followed` (the session clearly did "
    "something the rule governs, and did it the way the rule says), `violated` (it clearly did it another way), "
    "or `not_applicable` (the session never did what the rule is about, or the lines don't show enough to "
    "tell). When unsure, answer not_applicable. Cite the line number that shows it."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string", "description": "the rule id, e.g. R3"},
                    "verdict": {"type": "string", "enum": ["followed", "violated", "not_applicable"]},
                    "line": {"type": "integer", "description": "excerpt line number of the evidence, 0 if none"},
                    "why": {"type": "string", "description": "one short sentence"},
                },
                "required": ["rule", "verdict", "line", "why"],
            },
        }
    },
    "required": ["verdicts"],
}


@dataclass
class Rule:
    id: str
    title: str
    source: str  # file:line
    tokens: int
    text: str


@dataclass
class Evidence:
    session_id: str
    verdict: str
    line: str
    why: str


@dataclass
class RuleResult:
    rule: Rule
    followed: int = 0
    violated: int = 0
    not_applicable: int = 0
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def applied(self) -> int:
        return self.followed + self.violated

    @property
    def adherence(self) -> float | None:
        return self.followed / self.applied if self.applied else None


@dataclass
class Payload:
    session_id: str
    lines: list[str]
    prompt: str


def candidate_rules(profile: Profile, limit: int = CANDIDATES) -> list[Rule]:
    """The most expensive startup sections, largest first: the ones that most need to earn their place."""
    from .discovery import STARTUP
    from .report import _rel

    candidates = [
        (f, s) for f, s in profile.all_sections()
        if f.file.loaded == STARTUP and f.file.kind not in ("auto-memory", "skill-listing") and len(s.section.text.strip()) > 80
    ]
    candidates.sort(key=lambda fs: -fs[1].tokens)
    rules = []
    for i, (f, s) in enumerate(candidates[:limit], 1):
        text = s.section.text.strip()
        if len(text) > RULE_CHARS:
            text = text[: RULE_CHARS - 1].rstrip() + "…"
        rules.append(Rule(f"R{i}", s.section.title, f"{_rel(f.file.path, profile.repo)}:{s.section.start_line}",
                          s.tokens, text))
    return rules


def select_payload(rules: list[Rule]) -> str:
    return "\n".join(f"{r.id} ({r.title}): {' '.join(r.text.split())[:PREVIEW_CHARS]}" for r in rules)


def select_checkable(backend: Backend, rules: list[Rule], limit: int) -> list[Rule]:
    """Keep the rules a log of commands can show being followed, largest first.

    Sends only rule text, which comes from the user's own instruction files.
    """
    answer = backend.ask_json(SELECT_SYSTEM, redact(select_payload(rules)), SELECT_SCHEMA)
    wanted = set(answer.get("checkable", []))
    return [r for r in rules if r.id in wanted][:limit]


def pick_sessions(acts: list[SessionActivity], limit: int, seed: int = 0) -> list[SessionActivity]:
    """Recent sessions that did real work, spread rather than all from one afternoon."""
    busy = [a for a in acts if len(a.calls) >= MIN_CALLS]
    if len(busy) <= limit:
        return busy
    recent = busy[-limit * 3:]
    return sorted(random.Random(seed).sample(recent, limit), key=lambda a: a.started or 0)


def build_payload(act: SessionActivity, rules: list[Rule]) -> Payload:
    lines: list[str] = []
    used = 0
    for raw in act.timeline:
        line = redact(raw)
        if len(line) > LINE_CHARS:
            line = line[: LINE_CHARS - 1] + "…"
        if used + len(line) > EXCERPT_CHARS:
            lines.append(f"[… {len(act.timeline) - len(lines)} more lines not sent]")
            break
        lines.append(line)
        used += len(line) + 1
    rule_block = "\n\n".join(f"{r.id} ({r.title}):\n{redact(r.text)}" for r in rules)
    excerpt = "\n".join(f"{i}. {ln}" for i, ln in enumerate(lines, 1))
    prompt = f"<rules>\n{rule_block}\n</rules>\n\n<session>\n{excerpt}\n</session>\n\nGive one verdict per rule."
    return Payload(act.session_id, lines, prompt)


def write_dry_run(rules: list[Rule], payloads: list[Payload], path: Path) -> None:
    """Every request --rules would make, one JSON object per line. The session payloads are built
    from all candidate rules here; the real run sends only the ones the first call marks checkable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"step": "select", "system": SELECT_SYSTEM, "prompt": redact(select_payload(rules))})]
    lines += [json.dumps({"step": "judge", "session_id": p.session_id, "system": SYSTEM, "prompt": p.prompt})
              for p in payloads]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def judge(backend: Backend, rules: list[Rule], payloads: list[Payload], jobs: int = 4) -> tuple[list[RuleResult], int]:
    """Verdicts per rule, and how many sessions the judge failed on."""
    results = {r.id: RuleResult(r) for r in rules}
    failed = 0

    def one(p: Payload) -> tuple[Payload, dict | None]:
        try:
            return p, backend.ask_json(SYSTEM, p.prompt, SCHEMA)
        except Exception:  # noqa: BLE001 - one failed session must not sink the rest
            return p, None

    with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        for p, answer in pool.map(one, payloads):
            if not answer:
                failed += 1
                continue
            for v in answer.get("verdicts", []):
                res = results.get(v.get("rule"))
                verdict = v.get("verdict")
                if res is None or verdict not in ("followed", "violated", "not_applicable"):
                    continue
                setattr(res, verdict, getattr(res, verdict) + 1)
                if verdict != "not_applicable":
                    n = v.get("line") or 0
                    line = p.lines[n - 1] if 0 < n <= len(p.lines) else ""
                    res.evidence.append(Evidence(p.session_id, verdict, line, v.get("why", "")))
    return list(results.values()), failed


def results_to_json(results: list[RuleResult]) -> list[dict]:
    return [{**asdict(r), "applied": r.applied, "adherence": r.adherence} for r in results]
