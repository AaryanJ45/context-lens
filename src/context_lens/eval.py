"""Prove a compacted CLAUDE.md works as well as the original.

1. **Probes.** For a sample of sections, a model writes a realistic task that an
   agent only gets right by following that section's rule, naming real files in
   the repo, plus a rubric taken from the *original* text. Probes favour sections
   the compaction touched (moved, pointed, condensed) and keep some untouched
   ones as controls. They are saved as JSON so a person can edit them.
2. **Runs.** Each probe runs as a fresh headless Claude Code session in a
   throwaway git worktree, once per variant: ``before`` (the repo as committed)
   and ``after`` (the compacted files laid over it). Sessions may read and search
   but not edit or run commands. Each run records startup tokens (the first
   turn's input), total tokens, cost, turns and the files it read.
3. **Grading.** A separate judge call sees the original rule, the rubric and the
   answer, never which variant produced it, and returns pass or fail.

The report pairs every probe across variants, so "after passed 27/30, before
28/30" comes with exactly which probes changed.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import statistics
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .analyze import RepoIndex
from .compact import Item, Plan, glob_to_re
from .llm import Backend

DEFAULT_RUN_MODEL = "claude-sonnet-5-5"
RUN_TOOLS = "Read,Grep,Glob"
RUN_BUDGET_USD = 3.0
RUN_TIMEOUT_S = 900
GEN_BATCH = 6
CONTROL_SHARE = 0.2
MIN_SECTION_TOKENS = 60
FILES_SHOWN = 40
RELATED_SECTIONS = 3
RELATED_CHARS = 2500

GEN_SYSTEM = """You write evaluation probes for coding-agent instruction files.
A probe is a realistic request a developer would type to Claude Code in this
repository. An agent answers it correctly only if it follows one specific rule
from the instruction file. The probe must never quote, name or hint at the rule:
it tests whether the agent knows and applies it unprompted."""

GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "probes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section_id": {"type": "string"},
                    "testable": {"type": "boolean", "description": "false when no task can check this rule"},
                    "rule": {"type": "string", "description": "the rule under test, one sentence"},
                    "prompt": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "rubric": {"type": "string", "description": "what a passing answer must do and must not do"},
                },
                "required": ["section_id", "testable", "rule", "prompt", "files", "rubric"],
            },
        }
    },
    "required": ["probes"],
}

RUBRIC_RULES = """Rubric rules:
- Grade what the answer would change (the code, files, config, or the decision it
  commits to), never what it explains. Do not require it to name the rule, cite a
  ticket, explain why, or mention evidence.
- The rule lives in a whole instruction file. Where a related section (given
  below) prescribes a different but compatible way to satisfy the same case, the
  rubric must accept it. Example: if one rule says "call external APIs through
  the shared HTTP client" and another says "scripts in tools/ may use requests
  directly", a task in tools/ passes with requests.
- State the pass condition and the fail condition concretely. Fail only what the
  rule forbids or a required step it leaves out.
- A clarifying question passes only when the task is genuinely ambiguous given the
  named files and the question names that ambiguity."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "keep": {"type": "boolean", "description": "false drops a probe that cannot be graded fairly"},
                    "rubric": {"type": "string", "description": "the corrected rubric, or the original if fine"},
                    "note": {"type": "string", "description": "what changed and why, empty if nothing"},
                },
                "required": ["id", "keep", "rubric", "note"],
            },
        }
    },
    "required": ["reviews"],
}

JUDGE_SYSTEM = """You grade a coding agent's answer against one rule from its
instruction file. Pass when the change the answer commits to follows the rule as
the rubric states it, including any compatible approach a related section
prescribes. Grade the change, not the explanation: an answer that does the right
thing without saying why passes. Fail an answer that breaks the rule, leaves out a
step the rule requires, or commits to no change, except a clarifying question that
names a real ambiguity in the task. Judge only the rule under test."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"pass": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["pass", "reason"],
}

ANSWER_SUFFIX = (
    "\n\nDon't edit any files. Look at the code you need, then reply with exactly what "
    "you would change: which files, and the code."
)


@dataclass
class Probe:
    id: str
    section_id: str
    section_title: str
    action: str
    rule: str
    prompt: str
    files: list[str]
    rubric: str
    original: str
    related: str = ""
    review_note: str = ""


@dataclass
class RunResult:
    probe_id: str
    variant: str
    rep: int
    answer: str = ""
    startup_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    files_read: list[str] = field(default_factory=list)
    error: str = ""
    passed: bool | None = None
    judge_reason: str = ""


# -------------------------------------------------------------------- probes


def _spread(items: list[Item], k: int, rng: random.Random) -> list[Item]:
    """Round-robin across headings, so one long catalog can't fill the sample."""
    groups: dict[str, list[Item]] = {}
    for it in items:
        groups.setdefault(it.group, []).append(it)
    for g in groups.values():
        rng.shuffle(g)
    order = list(groups)
    rng.shuffle(order)
    out: list[Item] = []
    while len(out) < k and any(groups.values()):
        for g in order:
            if groups[g] and len(out) < k:
                out.append(groups[g].pop())
    return out


def pick_sections(plan: Plan, n: int, seed: int = 0) -> list[Item]:
    """Mostly sections the compaction changed, plus some untouched ones as controls."""
    rng = random.Random(seed)
    usable = [i for i in plan.items if i.tokens >= MIN_SECTION_TOKENS and plan.decisions.get(i.id)]
    changed = [i for i in usable if plan.decisions[i.id].action in ("move", "pointer", "condense")]
    kept = [i for i in usable if plan.decisions[i.id].action == "keep"]
    n_control = min(len(kept), max(1, round(n * CONTROL_SHARE))) if kept else 0
    chosen = _spread(changed, n - n_control, rng) + _spread(kept, n_control, rng)
    order = {i.id: k for k, i in enumerate(plan.items)}
    return sorted(chosen, key=lambda i: order[i.id])


def leaks(prompt: str, item: Item, plan: Plan) -> str | None:
    """A doc the prompt names that holds this rule: it would hand the agent the answer."""
    d = plan.decisions.get(item.id)
    for doc in {*(item.docs or []), *([d.doc] if d and d.doc else []), plan.target}:
        if doc and (doc in prompt or Path(doc).name in prompt):
            return doc
    return None


def _candidate_files(index: RepoIndex, item: Item, plan: Plan) -> list[str]:
    d = plan.decisions[item.id]
    rule = next((r for r in plan.layout if r.path == d.dest), None)
    globs = rule.paths if rule else []
    regexes = [re.compile(glob_to_re(g)) for g in globs]
    pool = [f for f in index.files if any(r.fullmatch(f) for r in regexes)] if regexes else []
    named = [f for f in index.files if any(f.endswith(ref.strip("/")) for ref in item.facts if "/" in ref or "." in ref)]
    rng = random.Random(item.id)
    sample = named[:FILES_SHOWN // 2] + rng.sample(pool, min(len(pool), FILES_SHOWN // 2))
    return list(dict.fromkeys(sample))


_WORD_RE = re.compile(r"[a-z][a-z0-9_]{3,}")
FACT_WEIGHT = 3.0


def _words(item: Item) -> set[str]:
    return set(_WORD_RE.findall(item.text.lower()))


def related_sections(item: Item, items: list[Item], k: int = RELATED_SECTIONS) -> list[Item]:
    """The sections a rubric for `item` must not contradict.

    Scored by shared vocabulary weighted by rarity (two rules that both talk about
    "retries" or "timeouts" are related even with no backticked fact in common)
    plus a bonus per shared fact.
    """
    import math

    vocab = {o.id: _words(o) for o in items}
    df: dict[str, int] = {}
    for words in vocab.values():
        for w in words:
            df[w] = df.get(w, 0) + 1
    n = len(items)
    idf = {w: math.log(n / c) for w, c in df.items()}
    mine, my_facts = vocab[item.id], set(item.facts)
    scored = []
    for o in items:
        if o.id == item.id:
            continue
        shared = mine & vocab[o.id]
        score = sum(idf[w] for w in shared if df[w] <= n / 4) + FACT_WEIGHT * len(my_facts & set(o.facts))
        if score > 0:
            scored.append((score, o))
    scored.sort(key=lambda t: -t[0])
    return [o for _, o in scored[:k]]


# Not after a list number ("3. Logging"): that is the heading, not a sentence end.
_SENTENCE_RE = re.compile(r"(?<=[.;!?])(?<!^\d\.)(?<!^\d\d\.)\s+|\n+")


def _excerpt(other: Item, item: Item, limit: int = RELATED_CHARS) -> str:
    """The sentences of `other` that share rare words or facts with `item`, in order."""
    if len(other.text) <= limit:
        return other.text
    mine = _words(item)
    facts = set(item.facts)
    sentences = [x for x in _SENTENCE_RE.split(other.text) if x.strip()]
    scored = []
    for k, sent in enumerate(sentences):
        words = set(_WORD_RE.findall(sent.lower()))
        hits = len(words & mine) + FACT_WEIGHT * sum(1 for f in facts if f in sent)
        # Per square-root length: a run-on sentence must not win just by containing more words.
        scored.append((hits / (len(sent) ** 0.5), k, sent))
    keep = {0}  # the heading / opening line says what the section is
    used = len(sentences[0])
    for score, k, sent in sorted(scored, key=lambda t: (-t[0], t[1])):
        if score <= 0 or used + len(sent) > limit:
            continue
        keep.add(k)
        used += len(sent)
    out, last = [], -1
    for k in sorted(keep):
        if last >= 0 and k != last + 1:
            out.append("…")
        out.append(sentences[k].strip())
        last = k
    return " ".join(out)


def _related_text(item: Item, items: list[Item]) -> str:
    parts = [f'<related id="{o.id}" title="{o.title}">\n{_excerpt(o, item)}\n</related>'
             for o in related_sections(item, items)]
    return "\n".join(parts)


def review_probes(backend: Backend, probes: list[Probe], jobs: int = 4, log=print) -> list[Probe]:
    """A second read of every rubric against its related sections; fixes conflicts, drops unfair probes."""
    groups = [probes[i : i + GEN_BATCH] for i in range(0, len(probes), GEN_BATCH)]

    def run(group: list[Probe]) -> list[Probe]:
        blocks = [
            f'<probe id="{p.id}">\n<rule_source>\n{p.original}\n</rule_source>\n{p.related}\n'
            f"<task>\n{p.prompt}\n</task>\n<rubric>\n{p.rubric}\n</rubric>\n</probe>"
            for p in group
        ]
        prompt = (
            "Review each probe's rubric before it is used to grade answers. Fix any rubric that "
            "breaks the rules below, especially one that would fail an answer a related section "
            "requires or allows, or one that grades the explanation instead of the change. Set "
            "keep=false only when the task itself cannot be graded fairly.\n\n"
            + RUBRIC_RULES + "\n\n" + "\n\n".join(blocks)
        )
        try:
            data = backend.ask_json(GEN_SYSTEM, prompt, REVIEW_SCHEMA)
        except Exception as exc:  # noqa: BLE001 - an unreviewed probe is still usable
            log(f"  review {group[0].id}.. failed, kept as written: {exc}")
            return group
        by_id = {r.get("id"): r for r in data.get("reviews", [])}
        out = []
        for p in group:
            r = by_id.get(p.id)
            if r and not r.get("keep", True):
                log(f"  {p.id} dropped in review: {r.get('note', '')}")
                continue
            if r and r.get("rubric", "").strip() and r["rubric"].strip() != p.rubric.strip():
                p.rubric, p.review_note = r["rubric"].strip(), r.get("note", "")
            out.append(p)
        changed = sum(1 for p in out if p.review_note)
        log(f"  reviewed {group[0].id}..{group[-1].id}: {changed} rubric(s) fixed")
        return out

    reviewed: list[Probe] = []
    with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        for got in pool.map(run, groups):
            reviewed.extend(got)
    return reviewed


def generate_probes(backend: Backend, repo: Path, plan: Plan, n: int, jobs: int = 4, seed: int = 0,
                    review: bool = True, log=print) -> list[Probe]:
    index = RepoIndex(repo)
    sections = pick_sections(plan, n, seed)
    groups = [sections[i : i + GEN_BATCH] for i in range(0, len(sections), GEN_BATCH)]

    def run(group: list[Item]) -> list[Probe]:
        blocks = []
        for it in group:
            files = "\n".join(_candidate_files(index, it, plan)) or "(none listed; pick real paths you are sure exist)"
            related = _related_text(it, plan.items)
            blocks.append(f'<section id="{it.id}" title="{it.title}">\n{it.text}\n</section>\n'
                          + (f"<related_to_{it.id}>\n{related}\n</related_to_{it.id}>\n" if related else "")
                          + f"<real_files_for_{it.id}>\n{files}\n</real_files_for_{it.id}>")
        prompt = (
            "Write one probe per section below. The task must touch real files from the list "
            "given for that section (put them in `files` and mention them in the prompt), be "
            "something a developer would plausibly ask, and be answerable by reading code "
            "without running anything. The agent will be told not to edit and to describe its "
            "change. The rubric states the concrete behaviour the rule requires, taken from the "
            "section text, and follows the rubric rules below. Never point the agent at the instruction file or at a doc that holds the "
            "rule (no 'using docs/x.md', no 'check the guidelines'): it must know or find the rule "
            "itself. `files` are code files the task touches, not docs. Mark testable=false for a section whose rule no task can check "
            "(pure history, a generated block, a note about tooling the agent never uses).\n\n"
            + RUBRIC_RULES + "\n\n" + "\n\n".join(blocks)
        )
        try:
            data = backend.ask_json(GEN_SYSTEM, prompt, GEN_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            log(f"  probe batch {group[0].id}.. failed: {exc}")
            return []
        by_id = {i.id: i for i in group}
        out = []
        for raw in data.get("probes", []):
            it = by_id.get(raw.get("section_id", ""))
            if not it or not raw.get("testable"):
                continue
            leaked = leaks(raw.get("prompt", ""), it, plan)
            if leaked:
                log(f"  {it.id}: probe names {leaked}, which holds the rule; skipped")
                continue
            files = [f for f in raw.get("files", []) if (repo / f).exists()]
            if not files:
                log(f"  {it.id}: probe named no existing file, skipped")
                continue
            out.append(Probe("", it.id, it.title, plan.decisions[it.id].action, raw["rule"], raw["prompt"],
                             files, raw["rubric"], it.text, _related_text(it, plan.items)))
        log(f"  probes for {group[0].id}..{group[-1].id}: {len(out)}")
        return out

    probes: list[Probe] = []
    with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        for got in pool.map(run, groups):
            probes.extend(got)
    for k, p in enumerate(probes):
        p.id = f"p{k:02d}"
    return review_probes(backend, probes, jobs, log) if review else probes


def save_probes(path: Path, probes: list[Probe]) -> None:
    path.write_text(json.dumps([asdict(p) for p in probes], indent=2), encoding="utf-8")


def load_probes(path: Path) -> list[Probe]:
    return [Probe(**p) for p in json.loads(path.read_text(encoding="utf-8"))]


# ------------------------------------------------------------------ variants


def make_variant(repo: Path, base: Path, name: str, overlay: Path | None, ref: str = "HEAD") -> Path:
    """A detached worktree of `ref`, with `overlay`'s files copied over it."""
    path = base / name
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "-q", str(path), ref], check=True)
    if overlay:
        for src in overlay.rglob("*"):
            if src.is_file():
                dst = path / src.relative_to(overlay)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    # Instructions can point at docs shipped inside node_modules; share it read-only.
    if (repo / "node_modules").is_dir() and not (path / "node_modules").exists():
        os.symlink(repo / "node_modules", path / "node_modules")
    return path


def remove_variant(repo: Path, path: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)], capture_output=True)


# ---------------------------------------------------------------------- runs


def parse_stream(lines: list[str], workdir: Path) -> dict:
    """Startup tokens, totals and files read from `claude -p --output-format stream-json`."""
    first_usage, seen, files, result = None, set(), [], {}
    # macOS temp dirs show up both as /var/... and /private/var/...
    roots = sorted({str(workdir), str(workdir.resolve())}, key=len, reverse=True)
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            if first_usage is None and msg.get("usage"):
                first_usage = msg["usage"]
            for block in msg.get("content") or []:
                if block.get("type") == "tool_use" and block.get("name") == "Read":
                    fp = str((block.get("input") or {}).get("file_path", ""))
                    rel = fp
                    for root in roots:
                        if fp.startswith(root + "/"):
                            rel = fp[len(root) + 1 :]
                            break
                    if rel and rel not in seen:
                        seen.add(rel)
                        files.append(rel)
        elif ev.get("type") == "result":
            result = ev
    u = first_usage or {}
    total = result.get("usage") or {}
    return {
        "startup_tokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
        "input_tokens": total.get("input_tokens", 0) + total.get("cache_creation_input_tokens", 0) + total.get("cache_read_input_tokens", 0),
        "output_tokens": total.get("output_tokens", 0),
        "cost_usd": result.get("total_cost_usd") or 0.0,
        "turns": result.get("num_turns") or 0,
        "answer": result.get("result") or "",
        "is_error": bool(result.get("is_error")) or not result,
        "files_read": files,
    }


def run_probe(workdir: Path, probe: Probe, variant: str, rep: int, model: str, budget: float) -> RunResult:
    claude = shutil.which("claude")
    res = RunResult(probe.id, variant, rep)
    if not claude:
        res.error = "claude CLI not on PATH"
        return res
    cmd = [
        claude, "-p", "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--tools", RUN_TOOLS,
        # Project + local settings only: the user's hooks and plugins stay out of eval runs.
        "--setting-sources", "project,local",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--no-session-persistence",
        "--max-budget-usd", str(budget),
    ]
    try:
        proc = subprocess.run(cmd, input=probe.prompt + ANSWER_SUFFIX, capture_output=True, text=True,
                              cwd=workdir, timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        res.error = "timed out"
        return res
    parsed = parse_stream(proc.stdout.splitlines(), workdir)
    for key in ("startup_tokens", "input_tokens", "output_tokens", "cost_usd", "turns", "answer", "files_read"):
        setattr(res, key, parsed[key])
    if parsed["is_error"] and not res.answer:
        res.error = (proc.stderr or "no result")[:300]
    return res


def judge(backend: Backend, probe: Probe, res: RunResult) -> None:
    if res.error or not res.answer.strip():
        res.passed, res.judge_reason = False, res.error or "no answer"
        return
    prompt = f"""<rule_source>
{probe.original}
</rule_source>
{probe.related}

Rule under test: {probe.rule}

Rubric: {probe.rubric}

Task the agent was given:
{probe.prompt}

<answer>
{res.answer}
</answer>"""
    try:
        data = backend.ask_json(JUDGE_SYSTEM, prompt, JUDGE_SCHEMA)
        res.passed, res.judge_reason = bool(data.get("pass")), data.get("reason", "")
    except Exception as exc:  # noqa: BLE001
        res.passed, res.judge_reason = None, f"judge failed: {exc}"


def run_eval(repo: Path, probes: list[Probe], variants: dict[str, Path | None], judge_backend: Backend,
             model: str = DEFAULT_RUN_MODEL, reps: int = 1, jobs: int = 3, budget: float = RUN_BUDGET_USD,
             keep_worktrees: bool = False, on_result=None, log=print) -> list[RunResult]:
    base = Path(tempfile.mkdtemp(prefix="context-lens-eval-"))
    dirs = {name: make_variant(repo, base, name, overlay) for name, overlay in variants.items()}
    tasks = [(p, v, r) for r in range(reps) for p in probes for v in variants]
    results: list[RunResult] = []
    try:
        def one(task: tuple[Probe, str, int]) -> RunResult:
            p, v, r = task
            res = run_probe(dirs[v], p, v, r, model, budget)
            judge(judge_backend, p, res)
            if on_result:
                on_result(res)
            mark = {True: "pass", False: "FAIL", None: "?"}[res.passed]
            log(f"  {p.id} {v:<6} {mark}  startup {res.startup_tokens:,}  ${res.cost_usd:.2f}  {res.turns} turns")
            return res

        with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
            results = list(pool.map(one, tasks))
    finally:
        if not keep_worktrees:
            for d in dirs.values():
                remove_variant(repo, d)
            shutil.rmtree(base, ignore_errors=True)
    return results


# -------------------------------------------------------------------- report


def eval_report(probes: list[Probe], results: list[RunResult], model: str) -> str:
    variants = list(dict.fromkeys(r.variant for r in results))
    lines = [f"# context-lens eval", "", f"{len(probes)} probes, run model {model}.", "",
             "| variant | pass rate | median startup tokens | mean total tokens | mean cost | mean turns |",
             "|---|---:|---:|---:|---:|---:|"]
    for v in variants:
        rs = [r for r in results if r.variant == v]
        graded = [r for r in rs if r.passed is not None]
        passed = sum(1 for r in graded if r.passed)
        lines.append(
            f"| {v} | {passed}/{len(graded)} ({passed / max(len(graded), 1):.0%}) "
            f"| {int(statistics.median([r.startup_tokens for r in rs] or [0])):,} "
            f"| {int(statistics.mean([r.input_tokens for r in rs] or [0])):,} "
            f"| ${statistics.mean([r.cost_usd for r in rs] or [0]):.2f} "
            f"| {statistics.mean([r.turns for r in rs] or [0]):.1f} |"
        )
    if len(variants) == 2:
        a, b = variants
        key = lambda r: (r.probe_id, r.rep)  # noqa: E731
        ra = {key(r): r for r in results if r.variant == a}
        rb = {key(r): r for r in results if r.variant == b}
        only_a = [k for k in ra if k in rb and ra[k].passed and rb[k].passed is False]
        only_b = [k for k in ra if k in rb and rb[k].passed and ra[k].passed is False]
        lines += ["", f"Paired: {len(only_a)} probe(s) pass only under `{a}`, {len(only_b)} only under `{b}`."]
    by_probe = {p.id: p for p in probes}
    lines += ["", "## Probes", "", "| probe | section | action | " + " | ".join(variants) + " |",
              "|---|---|---|" + "---|" * len(variants)]
    for p in probes:
        cells = []
        for v in variants:
            rs = [r for r in results if r.probe_id == p.id and r.variant == v]
            cells.append(" ".join({True: "✓", False: "✗", None: "?"}[r.passed] for r in rs))
        lines.append(f"| {p.id} | {p.section_title[:60]} | {p.action} | " + " | ".join(cells) + " |")
    fails = [r for r in results if r.passed is False]
    if fails:
        lines += ["", "## Failures", ""]
        for r in fails:
            p = by_probe[r.probe_id]
            read = ", ".join(r.files_read[:6]) or "nothing"
            lines.append(f"- **{r.probe_id} / {r.variant}** ({p.rule}): {r.judge_reason} Read: {read}.")
    return "\n".join(lines) + "\n"
