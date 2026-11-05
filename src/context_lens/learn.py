"""Learn from your corrections: turn the things you keep telling Claude into rules.

Finding the corrections is local (`activity.Correction`): rejected tool calls
with the reason you typed, interruptions with what you said next, and prompts
that push back ("no, …", "don't …", "I told you …"). Turning them into rules
is one model call, opt-in with `--learn`, under the same privacy terms as
`--rules`: what is sent is your words, the few actions Claude took just
before (one line each), and the titles of your existing rules, all redacted.
Never tool output or file contents.

The model groups corrections that mean the same thing, writes one concrete
rule per group, and says when you already have that rule (then the problem is
Claude not following it, which a rule can't fix and a hook can). One-off
redirections ("also add a chart") are set aside, not turned into rules.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .activity import Correction, SessionActivity
from .llm import Backend
from .profile import Profile
from .redact import redact

MAX_CORRECTIONS = 150
LINE_CHARS = 200
SAID_CHARS = 400
RULE_INDEX_CHARS = 160
# Bare "keep going" after an interruption is a nudge, not a correction.
_NUDGES = re.compile(r"^(?:continue|go( on| ahead)?|keep going|proceed|ok(ay)?|yes|y|here|done)\W*$", re.I)
LEARNED_HEADING = "## Learned from corrections"
MARKER = "<!-- added by context-lens doctor --add-rules -->"

SYSTEM = (
    "You read moments where a developer corrected their coding agent and turn the repeated ones into rules for "
    "the agent's instruction file. Each correction shows what the agent did just before and what the developer "
    "said. Group corrections that mean the same thing. Write a rule only for a group of two or more, or for a "
    "single correction where the developer stated a general rule themselves (always, never, every time, don't "
    "ever). Set aside one-off redirections about the task at hand (adding a feature, answering a question, "
    "changing a single output), and plain go-aheads. A rule is one or two sentences, imperative, concrete: name "
    "the command, file, tool or habit, and say what to do instead. If an existing rule already says it, give its "
    "id in covered_by and still return the group: it means the rule exists but isn't being followed. If an "
    "existing rule says the opposite of what the developer keeps asking for, give its id in conflicts_with: the "
    "agent is following stale instructions, and that rule is the one to change. scope is "
    "`personal` when the correction is about the developer's own machine or working style and would apply in "
    "any repo, else `repo`."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "a few words"},
                    "rule": {"type": "string", "description": "the rule to add, imperative, 1-2 sentences"},
                    "corrections": {"type": "array", "items": {"type": "string"}, "description": "ids, e.g. C4"},
                    "covered_by": {"type": "string", "description": "existing rule id, or empty"},
                    "conflicts_with": {"type": "string", "description": "existing rule id that says the opposite, or empty"},
                    "scope": {"type": "string", "enum": ["repo", "personal"]},
                },
                "required": ["title", "rule", "corrections", "covered_by", "conflicts_with", "scope"],
            },
        },
    },
    "required": ["lessons"],
}


@dataclass
class Lesson:
    title: str
    rule: str
    scope: str
    covered_by: str = ""  # "R12 (title)" when an existing rule already says this
    conflicts_with: str = ""  # "R7 (title)" when an existing rule says the opposite
    examples: list[dict] = field(default_factory=list)  # {kind, said, before, when}

    @property
    def count(self) -> int:
        return len(self.examples)


def gather(acts: list[SessionActivity]) -> list[Correction]:
    """Corrections worth reading, newest first."""
    out = [c for a in acts for c in a.corrections if c.kind != "interrupted" or not _NUDGES.match(c.said)]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(out, key=lambda c: c.when or epoch, reverse=True)


def _cut(text: str, n: int) -> str:
    text = redact(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def rule_index(profile: Profile) -> dict[str, str]:
    """Existing startup rules as `R<n>` → "title: first words", for covered_by."""
    from .discovery import STARTUP

    index: dict[str, str] = {}
    for f, s in profile.all_sections():
        if f.file.loaded != STARTUP or f.file.kind in ("auto-memory", "skill-listing"):
            continue
        body = " ".join(s.section.text.split())
        index[f"R{len(index) + 1}"] = f"{s.section.title}: {body[:RULE_INDEX_CHARS]}"
    return index


def build_prompt(corrections: list[Correction], rules: dict[str, str]) -> tuple[str, dict[str, Correction]]:
    ids: dict[str, Correction] = {}
    blocks = []
    for i, c in enumerate(corrections[:MAX_CORRECTIONS], 1):
        cid = f"C{i}"
        ids[cid] = c
        before = "\n".join(
            f"  developer earlier: {_cut(x[len('USER: '):], LINE_CHARS)}" if x.startswith("USER: ")
            else f"  agent: {_cut(x.strip(), LINE_CHARS)}" for x in c.before)
        said = _cut(c.said, SAID_CHARS) if c.said else "(rejected without a reason)"
        blocks.append(f"{cid} [{c.kind}]\n{before}\n  developer: {said}")
    existing = "\n".join(f"{rid}: {_cut(text, RULE_INDEX_CHARS + 80)}" for rid, text in rules.items())
    prompt = (f"<existing_rules>\n{existing}\n</existing_rules>\n\n<corrections>\n" + "\n\n".join(blocks)
              + "\n</corrections>\n\nReturn the lessons, largest group first.")
    return prompt, ids


def learn(backend: Backend, corrections: list[Correction], rules: dict[str, str]) -> list[Lesson]:
    prompt, ids = build_prompt(corrections, rules)
    answer = backend.ask_json(SYSTEM, prompt, SCHEMA)
    lessons = []
    for item in answer.get("lessons", []):
        examples = [ids[c] for c in item.get("corrections", []) if c in ids]
        rule = " ".join(str(item.get("rule", "")).split())
        if not examples or not rule:
            continue
        covered, conflict = (_label(item.get(k, ""), rules) for k in ("covered_by", "conflicts_with"))
        lessons.append(Lesson(
            title=str(item.get("title", "")).strip() or rule[:40], rule=rule,
            scope=item.get("scope") if item.get("scope") in ("repo", "personal") else "repo", covered_by=covered,
            conflicts_with=conflict,
            examples=[{"kind": c.kind, "said": c.said, "before": c.before[-1] if c.before else "",
                       "when": c.when.isoformat() if c.when else None} for c in examples],
        ))
    return sorted(lessons, key=lambda l: (bool(l.covered_by), -l.count))


def _label(rid: str, rules: dict[str, str]) -> str:
    return f"{rid} ({rules[rid].split(':')[0]})" if rid in rules else ""


def write_dry_run(prompt: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"system": SYSTEM, "prompt": prompt}) + "\n", encoding="utf-8")


def save(lessons: list[Lesson], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(l) for l in lessons], indent=2), encoding="utf-8")


def load(path: Path) -> list[Lesson]:
    return [Lesson(**d) for d in json.loads(path.read_text(encoding="utf-8"))]


def target_file(scope: str, repo: Path, to: str | None, home: Path | None = None) -> Path:
    """Where a lesson goes: the repo's CLAUDE.md, your CLAUDE.local.md, or your user CLAUDE.md."""
    where = to or ("personal" if scope == "personal" else "repo")
    if where == "personal":
        return (home or Path.home()) / ".claude" / "CLAUDE.md"
    if where == "local":
        return repo / "CLAUDE.local.md"
    return repo / "CLAUDE.md"


def add_rules(lessons: list[Lesson], picks: list[int], repo: Path, to: str | None = None,
              home: Path | None = None) -> dict[Path, list[str]]:
    """Append the picked lessons (1-based) under one heading per file. Skips rules already there."""
    written: dict[Path, list[str]] = {}
    for n in picks:
        if not 1 <= n <= len(lessons):
            raise ValueError(f"no lesson {n}; there are {len(lessons)}")
        lesson = lessons[n - 1]
        path = target_file(lesson.scope, repo, to, home)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        line = f"- {lesson.rule}"
        if line in text or line in written.get(path, []):
            continue
        written.setdefault(path, []).append(line)
    for path, lines in written.items():
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if LEARNED_HEADING in text:
            # Add to the end of our section, not the end of the file, which may be another heading's.
            start = text.index(LEARNED_HEADING) + len(LEARNED_HEADING)
            nxt = re.search(r"^#{1,2} ", text[start:], re.M)
            end = start + nxt.start() if nxt else len(text)
            section = text[:end].rstrip("\n") + "\n" + "\n".join(lines) + "\n"
            text = section + ("\n" + text[end:] if nxt else "")
        else:
            sep = "\n\n" if text.strip() else ""
            text = text.rstrip("\n") + f"{sep}{LEARNED_HEADING}\n\n{MARKER}\n" + "\n".join(lines) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return written
