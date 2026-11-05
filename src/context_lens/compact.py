"""Propose a smaller startup CLAUDE.md without losing an instruction.

Three steps, each checkable:

1. **Layout.** One call reads the file's outline and proposes path-scoped rule
   files (``.claude/rules/<slug>.md`` with ``paths:`` globs). Claude Code loads
   those only when it touches a matching file, so they cost nothing at startup.
2. **Decisions.** Sections go out in parallel batches. Each comes back as
   keep, condense, move (to a rule file), pointer (to a repo doc that already
   holds it) or drop (obsolete), with rewritten text where needed.
3. **Coverage.** Every backticked fact in every section must reappear in the
   output it was sent to, or in the doc it points at, or be declared dropped
   with a reason. Sections with unexplained losses are asked again once with
   the list; whatever is still missing is reported, never silently lost.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .analyze import DocCorpus, RepoIndex, analyze_section, blame_times
from .facts import facts_in, missing
from .llm import Backend
from .sections import split_sections
from .tokens import TokenCounter

ACTIONS = ("keep", "condense", "move", "pointer", "drop")
BATCH_TOKENS = 8000
GROUP_MAX_TOKENS = 24000
RULES_DIR = ".claude/rules"
TOP_DIRS_SHOWN = 60

SYSTEM = """You compact the instruction files Claude Code loads into every session (CLAUDE.md).
The goal is fewer tokens at startup with no instruction lost. Every token you keep
is paid on every turn of every session, so cut hard, but an agent that follows
the compacted files must behave exactly as one that followed the original.

What to cut: narrative history, dates and ticket IDs that only record provenance,
incident stories and measurements kept as evidence, struck-through (~~text~~)
passages, "corrected on <date>" notes (keep only the current state), repeated
rationale, and examples that restate the rule. Keep one short "why" only where it
stops a likely mistake. Never invent, soften or generalise a rule.

Copy every backticked span you keep (paths, commands, identifiers, flag keys,
thresholds) exactly as written, backticks included."""

LAYOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": ".claude/rules/<slug>.md"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "purpose": {"type": "string", "description": "one line: what work loads it"},
                },
                "required": ["path", "paths", "purpose"],
            },
        }
    },
    "required": ["files"],
}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "dest": {"type": "string", "description": "move: one of the rule file paths"},
                    "doc": {"type": "string", "description": "pointer: repo-relative doc that holds the content"},
                    "text": {"type": "string"},
                    "dropped_facts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"fact": {"type": "string"}, "why": {"type": "string"}},
                            "required": ["fact", "why"],
                        },
                    },
                    "reason": {"type": "string"},
                },
                "required": ["id", "action", "reason"],
            },
        }
    },
    "required": ["decisions"],
}


@dataclass
class Item:
    id: str
    title: str
    text: str
    tokens: int
    hint: str
    docs: list[str]
    dup_doc: str | None
    dup_share: float
    facts: list[str]

    @property
    def group(self) -> str:
        return self.title.rsplit(" › ", 1)[0] if " › " in self.title else self.title


@dataclass
class RuleFile:
    path: str
    paths: list[str]
    purpose: str
    matches: int = 0


@dataclass
class Decision:
    id: str
    action: str
    reason: str = ""
    dest: str = ""
    doc: str = ""
    text: str = ""
    dropped_facts: list[dict] = field(default_factory=list)
    note: str = ""


@dataclass
class Loss:
    id: str
    title: str
    action: str
    facts: list[str]


@dataclass
class Plan:
    target: str
    model: str
    backend: str
    items: list[Item]
    layout: list[RuleFile]
    decisions: dict[str, Decision]

    def to_json(self) -> str:
        return json.dumps(
            {
                "target": self.target, "model": self.model, "backend": self.backend,
                "items": [asdict(i) for i in self.items],
                "layout": [asdict(r) for r in self.layout],
                "decisions": [asdict(self.decisions[i.id]) for i in self.items if i.id in self.decisions],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> "Plan":
        data = json.loads(raw)
        return cls(
            data["target"], data["model"], data["backend"],
            # Facts are re-read from the text so a parser fix applies to saved plans.
            [Item(**{**i, "facts": facts_in(i["text"])}) for i in data["items"]],
            [RuleFile(**r) for r in data["layout"]],
            {d["id"]: Decision(**d) for d in data["decisions"]},
        )


# ---------------------------------------------------------------- collection


def collect_items(repo: Path, target: Path, counter: TokenCounter) -> list[Item]:
    text = target.read_text(encoding="utf-8")
    index = RepoIndex(repo)
    corpus = DocCorpus(index, {target.resolve()})
    blame = blame_times(target)
    total = counter.count(text)
    items = []
    for n, sec in enumerate(split_sections(text)):
        st = analyze_section(sec, counter.apportion(total, len(text), sec.chars), index, corpus, blame)
        docs = sorted({r for r in st.path_refs if r.endswith(".md") and index.resolve(r)})
        items.append(
            Item(f"s{n}", sec.title, sec.text, st.tokens, st.hint, docs, st.dup_doc, round(st.dup_share, 2),
                 facts_in(sec.text))
        )
    return items


def _top_dirs(repo: Path) -> list[str]:
    index = RepoIndex(repo)
    counts: dict[str, int] = {}
    for f in index.files:
        if "/" in f:
            counts[f.split("/", 1)[0]] = counts.get(f.split("/", 1)[0], 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:TOP_DIRS_SHOWN]
    return [f"{d}/ ({n} files)" for d, n in ranked]


def glob_matches(repo: Path, globs: list[str]) -> int:
    files = RepoIndex(repo).files
    regexes = [re.compile(glob_to_re(g)) for g in globs]
    return sum(1 for f in files if any(r.fullmatch(f) for r in regexes))


def glob_to_re(glob: str) -> str:
    out, i = "", 0
    glob = glob.lstrip("./")
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        elif glob[i] == "{":
            j = glob.find("}", i)
            alts = glob[i + 1 : j].split(",") if j > i else [glob[i]]
            out, i = out + "(?:" + "|".join(re.escape(a) for a in alts) + ")", (j + 1 if j > i else i + 1)
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return out


# -------------------------------------------------------------------- layout


def _outline(items: list[Item]) -> str:
    rows = []
    for it in items:
        extra = f" hint={it.hint}" if it.hint != "core" else ""
        if it.docs:
            extra += " docs=" + ",".join(it.docs[:3])
        rows.append(f"{it.id} [{it.tokens} tok]{extra} {it.title}")
    return "\n".join(rows)


def plan_layout(backend: Backend, repo: Path, items: list[Item], target_rel: str) -> list[RuleFile]:
    prompt = f"""File: {target_rel}

Outline (id, estimated tokens, signals, title):
{_outline(items)}

Top-level directories of the repo:
{chr(10).join(_top_dirs(repo))}

Propose the path-scoped rule files this content should be split into. Claude Code
loads `.claude/rules/<slug>.md` only when it reads or edits a file matching one of
the `paths` globs, so content that only matters for some directories costs nothing
at startup. Use 2-8 files, named by the area of the codebase (frontend, backend,
tests, ...), with globs relative to the repo root that match real directories above
(for example "src/ui/**" or "api/**/*.py"). Do not create a file for content
that applies to every task; that stays in {target_rel}. Return only the files."""
    data = backend.ask_json(SYSTEM, prompt, LAYOUT_SCHEMA)
    files = []
    for raw in data.get("files", []):
        path = raw.get("path", "").strip().lstrip("./")
        if not path.startswith(RULES_DIR + "/") or not path.endswith(".md"):
            path = f"{RULES_DIR}/{Path(path).stem or 'scoped'}.md"
        globs = [g.strip() for g in raw.get("paths", []) if g.strip()]
        if globs:
            files.append(RuleFile(path, globs, raw.get("purpose", ""), glob_matches(repo, globs)))
    return files


# ----------------------------------------------------------------- decisions


def batches(items: list[Item]) -> list[list[Item]]:
    """Pack sections into batches, keeping one heading's items together where it fits."""
    groups: list[list[Item]] = []
    for it in items:
        if groups and groups[-1][0].group == it.group and sum(i.tokens for i in groups[-1]) < GROUP_MAX_TOKENS:
            groups[-1].append(it)
        else:
            groups.append([it])
    out: list[list[Item]] = []
    for g in groups:
        size = sum(i.tokens for i in g)
        if out and sum(i.tokens for i in out[-1]) + size <= BATCH_TOKENS:
            out[-1].extend(g)
        else:
            out.append(list(g))
    return out


def _render_items(batch: list[Item]) -> str:
    parts = []
    for it in batch:
        attrs = f'id="{it.id}" tokens="{it.tokens}" signal="{it.hint}"'
        if it.docs:
            attrs += f' names_docs="{",".join(it.docs)}"'
        if it.dup_doc and it.dup_share >= 0.1:
            attrs += f' overlaps_doc="{it.dup_doc}" overlap="{it.dup_share:.0%}"'
        parts.append(f"<section {attrs}>\n{it.text}\n</section>")
    return "\n\n".join(parts)


def _decision_prompt(batch: list[Item], items: list[Item], layout: list[RuleFile], target_rel: str,
                     feedback: str = "") -> str:
    rules = "\n".join(f"- {r.path}  paths={r.paths}  ({r.purpose})" for r in layout) or "- (none)"
    return f"""File being compacted: {target_rel}

Whole-file outline, for context:
{_outline(items)}

Path-scoped rule files available as move destinations:
{rules}

Decide every section below. Actions:
- keep: applies to nearly every task here and is already tight. Leave text empty.
- condense: applies to nearly every task; text is the shorter rewrite.
- move: only matters when working in the files a rule file covers. dest is that
  rule file's path; text is the rewrite, or empty to move it verbatim.
- pointer: reference material another repo doc already holds (see names_docs /
  overlaps_doc), or a catalog the agent needs only when working on that topic.
  doc is the repo-relative doc path; text is a one or two line replacement saying
  when to read it, or empty when an earlier section's pointer already covers this one.
- drop: obsolete; everything in it is struck through, removed, or superseded.

Keep each section's heading line (#, ##, ...) and list numbering at the start of
its text. For every backticked span in a section that your text (or, for pointer,
the doc) will not contain, add it to dropped_facts with a short why.
{feedback}
Sections:

{_render_items(batch)}"""


def _clean(raw: dict, ids: set[str], layout_paths: set[str], repo: Path) -> Decision | None:
    sid = raw.get("id", "")
    if sid not in ids:
        return None
    d = Decision(
        sid, raw.get("action", "keep"), raw.get("reason", ""), raw.get("dest", "") or "",
        (raw.get("doc", "") or "").strip().lstrip("./"), raw.get("text", "") or "", raw.get("dropped_facts") or [],
    )
    if d.action not in ACTIONS:
        d.action, d.note = "keep", f"unknown action {raw.get('action')!r}, kept"
    if d.action == "move" and d.dest not in layout_paths:
        d.action, d.note = "keep", f"move to unknown file {d.dest!r}, kept"
    if d.action == "pointer" and not (repo / d.doc).is_file():
        d.action, d.note = "keep", f"pointer to missing doc {d.doc!r}, kept"
    if d.action == "condense" and not d.text.strip():
        d.action = "keep"
    return d


def decide(backend: Backend, repo: Path, items: list[Item], layout: list[RuleFile], target_rel: str,
           jobs: int = 4, only: list[list[Item]] | None = None, feedback: dict[str, str] | None = None,
           log=print) -> dict[str, Decision]:
    layout_paths = {r.path for r in layout}
    todo = only or batches(items)

    def run(batch: list[Item]) -> dict[str, Decision]:
        fb = ""
        if feedback:
            notes = [f"- {it.id}: {feedback[it.id]}" for it in batch if it.id in feedback]
            if notes:
                fb = "\nA previous attempt lost these facts. Include them or list each in dropped_facts:\n" + "\n".join(notes) + "\n"
        ids = {it.id for it in batch}
        try:
            data = backend.ask_json(SYSTEM, _decision_prompt(batch, items, layout, target_rel, fb), DECISION_SCHEMA)
        except Exception as exc:  # noqa: BLE001 - a failed batch keeps its sections unchanged
            log(f"  batch {batch[0].id}..{batch[-1].id} failed, kept as-is: {exc}")
            return {it.id: Decision(it.id, "keep", note=f"batch failed: {exc}") for it in batch}
        got = {}
        for raw in data.get("decisions", []):
            d = _clean(raw, ids, layout_paths, repo)
            if d:
                got[d.id] = d
        for it in batch:
            got.setdefault(it.id, Decision(it.id, "keep", note="no decision returned, kept"))
        log(f"  decided {batch[0].id}..{batch[-1].id} ({sum(i.tokens for i in batch)} tok)")
        return got

    out: dict[str, Decision] = {}
    with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        for result in pool.map(run, todo):
            out.update(result)
    return out


# ------------------------------------------------------------------ assembly


def _frontmatter(globs: list[str]) -> str:
    return "---\npaths:\n" + "".join(f'  - "{g}"\n' for g in globs) + "---\n"


def assemble(plan: Plan, index_rules: bool = True) -> dict[str, str]:
    """Output files keyed by repo-relative path."""
    root: list[str] = []
    moved: dict[str, list[str]] = {r.path: [] for r in plan.layout}
    for it in plan.items:
        d = plan.decisions.get(it.id, Decision(it.id, "keep"))
        body = d.text.strip() if d.text.strip() else it.text.strip()
        if d.action == "keep":
            root.append(it.text.strip())
        elif d.action == "condense":
            root.append(body)
        elif d.action == "move":
            moved[d.dest].append(body)
        elif d.action == "pointer" and d.text.strip():
            root.append(d.text.strip())
    used = [r for r in plan.layout if moved[r.path]]
    if index_rules and used:
        lines = ["## Scoped rules", "", "Loaded automatically when you touch matching files; read one first when planning work there."]
        lines += [f"- `{r.path}`: {r.purpose} ({', '.join(r.paths)})" for r in used]
        root.append("\n".join(lines))
    files = {plan.target: "\n\n".join(p for p in root if p) + "\n"}
    for r in used:
        title = Path(r.path).stem.replace("-", " ").title()
        files[r.path] = _frontmatter(r.paths) + f"\n# {title} rules\n\n" + "\n\n".join(moved[r.path]) + "\n"
    return files


def coverage(plan: Plan, outputs: dict[str, str], repo: Path) -> tuple[list[Loss], list[Loss]]:
    """(lost, declared): facts gone from the outputs, split by whether the model said why."""
    lost, declared = [], []
    doc_cache: dict[str, str] = {}
    for it in plan.items:
        d = plan.decisions.get(it.id, Decision(it.id, "keep"))
        if d.action == "keep" or not it.facts:
            continue
        hay: list[str] = []
        if d.action in ("condense", "pointer"):
            hay.append(outputs.get(plan.target, ""))
        if d.action == "move":
            hay.append(outputs.get(d.dest, ""))
        if d.action == "pointer" and d.doc:
            if d.doc not in doc_cache:
                doc_cache[d.doc] = (repo / d.doc).read_text(encoding="utf-8", errors="replace")
            hay.append(doc_cache[d.doc])
        gone = missing(it.facts, hay)
        said = {str(x.get("fact", "")).strip().strip("`") for x in d.dropped_facts}
        unexplained = [f for f in gone if f not in said]
        explained = [f for f in gone if f in said]
        if unexplained:
            lost.append(Loss(it.id, it.title, d.action, unexplained))
        if explained:
            declared.append(Loss(it.id, it.title, d.action, explained))
    return lost, declared


def build_plan(backend: Backend, repo: Path, target: Path, counter: TokenCounter, jobs: int = 4,
               repair: bool = True, log=print) -> Plan:
    target_rel = target.resolve().relative_to(repo.resolve()).as_posix()
    items = collect_items(repo, target, counter)
    log(f"{len(items)} sections, ≈{sum(i.tokens for i in items):,} tokens; planning layout…")
    layout = plan_layout(backend, repo, items, target_rel)
    for r in layout:
        log(f"  {r.path}  {r.paths}  matches {r.matches} files")
    log(f"deciding {len(batches(items))} batches with {backend.name} ({backend.model})…")
    plan = Plan(target_rel, backend.model, backend.name, items, layout, decide(backend, repo, items, layout, target_rel, jobs, log=log))
    if repair:
        lost, _ = coverage(plan, assemble(plan), repo)
        if lost:
            log(f"repairing {len(lost)} sections with unexplained losses…")
            by_id = {i.id: i for i in items}
            feedback = {l.id: ", ".join(f"`{f}`" for f in l.facts) for l in lost}
            retry = [[by_id[l.id]] for l in lost]
            plan.decisions.update(decide(backend, repo, items, layout, target_rel, jobs, retry, feedback, log))
    return plan


# -------------------------------------------------------------------- report


def summarize(plan: Plan, outputs: dict[str, str], counter: TokenCounter, repo: Path,
              measured_startup: int | None = None) -> str:
    before = sum(i.tokens for i in plan.items)
    after_root = counter.count(outputs[plan.target])
    scoped = {p: counter.count(t) for p, t in outputs.items() if p != plan.target}
    lost, declared = coverage(plan, outputs, repo)
    approx = "" if counter.exact else "≈"
    by_action: dict[str, list[int]] = {a: [0, 0] for a in ACTIONS}
    for it in plan.items:
        d = plan.decisions.get(it.id, Decision(it.id, "keep"))
        by_action[d.action][0] += 1
        by_action[d.action][1] += it.tokens
    cut = before - after_root
    lines = [
        f"# context-lens compact: {plan.target}",
        "",
        f"Backend {plan.backend}, model {plan.model}. Token counts are {'exact' if counter.exact else 'estimates (≈)'}.",
        "",
        "| | tokens |",
        "|---|---:|",
        f"| `{plan.target}` before | {approx}{before:,} |",
        f"| `{plan.target}` after | {approx}{after_root:,} |",
        f"| **Cut from startup** | **{approx}{cut:,} ({cut / max(before, 1):.0%})** |",
    ]
    for p, t in scoped.items():
        lines.append(f"| `{p}` (on demand) | {approx}{t:,} |")
    if measured_startup:
        lines += ["", f"Measured median startup context was {measured_startup:,} tokens; this projects "
                  f"{approx}{measured_startup - cut:,} ({cut / measured_startup:.0%} less)."]
    lines += ["", "| action | sections | tokens in |", "|---|---:|---:|"]
    lines += [f"| {a} | {n} | {approx}{t:,} |" for a, (n, t) in by_action.items() if n]
    total_facts = sum(len(i.facts) for i in plan.items)
    lines += ["", "## Coverage", "",
              f"{total_facts} backticked facts in the original. "
              f"{sum(len(l.facts) for l in lost)} lost without a reason, "
              f"{sum(len(l.facts) for l in declared)} dropped with a stated reason."]
    if lost:
        lines += ["", "### Lost (review these)", ""]
        lines += [f"- {l.id} {l.title} [{l.action}]: " + ", ".join(f"`{f}`" for f in l.facts) for l in lost]
    if declared:
        lines += ["", "### Dropped on purpose", ""]
        by_id = {i.id: plan.decisions[i.id] for i in plan.items if i.id in plan.decisions}
        for l in declared:
            whys = {str(x.get("fact", "")).strip("`"): x.get("why", "") for x in by_id[l.id].dropped_facts}
            lines += [f"- {l.id} {l.title}: " + "; ".join(f"`{f}` ({whys.get(f, '')})" for f in l.facts)]
    lines += ["", "## Decisions", ""]
    for it in plan.items:
        d = plan.decisions.get(it.id, Decision(it.id, "keep"))
        where = f" → `{d.dest or d.doc}`" if d.dest or d.doc else ""
        note = f" _{d.note}_" if d.note else ""
        lines.append(f"- **{it.id}** {d.action}{where} · {it.title} ({approx}{it.tokens}): {d.reason}{note}")
    return "\n".join(lines) + "\n"


def write_outputs(base: Path, outputs: dict[str, str]) -> list[Path]:
    written = []
    for rel, text in outputs.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
