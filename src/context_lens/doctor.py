"""`doctor`: one pass over a repo's instruction files and past sessions, ending in a ranked list of fixes.

Five areas, each built from numbers the report shows rather than a score that
hides them:

* startup context   what loads before the first message (the old `profile`)
* wasted work       oversized tool output, failing and repeated calls
* tools and MCP     configured servers and listed skills that never get used
* session health    compactions and how big context gets
* rule adherence    optional (`--rules`): whether sessions followed the rules

Everything except rule adherence is computed locally from files on disk.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .analyze import _PATH_EXT_RE
from .activity import LARGE_RESULT_TOKENS, SessionActivity, mcp_server, read_activity
from .discovery import STARTUP
from .profile import Profile, build_profile
from .report import _rel, savings_buckets
from .transcripts import transcript_paths

FIX, LOOK, OK = "fix", "look", "ok"
SEVERITY_ORDER = {FIX: 0, LOOK: 1, OK: 2}
AREAS = ["Startup context", "Wasted work", "Tools and MCP", "Session health", "Corrections", "Rule adherence"]

STARTUP_LOOK, STARTUP_FIX = 10_000, 25_000
LARGE_OUTPUT_LOOK, LARGE_OUTPUT_FIX = 5_000, 20_000  # tokens per session
ERROR_RATE_LOOK, ERROR_RATE_FIX = 0.05, 0.10
COMPACTED_SHARE_LOOK = 0.25
SKILL_LISTING_LOOK = 1_500
ADHERENCE_FIX = 0.70
TOP_EVIDENCE = 5


@dataclass
class Finding:
    area: str
    severity: str
    title: str
    detail: str
    fix: str
    tokens_per_session: int | None = None
    evidence: list[str] = field(default_factory=list)


@dataclass
class Doctor:
    profile: Profile
    activity: list[SessionActivity]
    findings: list[Finding]
    metrics: dict[str, dict[str, str]]
    mcp_servers: dict[str, int]
    rules: list = field(default_factory=list)  # rules.RuleResult, when --rules ran
    lessons: list = field(default_factory=list)  # learn.Lesson, when --learn ran
    sent: list[str] = field(default_factory=list)  # one line per opt-in check that called a model

    @property
    def privacy(self) -> str:
        if not self.sent:
            return "Local only: nothing left this machine."
        return " ".join(self.sent) + " Everything else ran locally."

    def area_status(self, area: str) -> str:
        sev = [f.severity for f in self.findings if f.area == area]
        return min(sev, key=SEVERITY_ORDER.get) if sev else OK

    def ranked(self) -> list[Finding]:
        return sorted((f for f in self.findings if f.severity != OK),
                      key=lambda f: (SEVERITY_ORDER[f.severity], -(f.tokens_per_session or 0)))

    @property
    def recoverable(self) -> int:
        return sum(f.tokens_per_session or 0 for f in self.findings if f.severity != OK)


def _n(v: float | int | None) -> str:
    return "n/a" if v is None else f"{v:,.0f}"


def _median(vals: list[float]) -> float | None:
    return statistics.median(vals) if vals else None


# --- startup -----------------------------------------------------------------

def _skill_names(p: Profile) -> list[str]:
    for f in p.files:
        if f.file.kind == "skill-listing":
            return [ln.split(":", 1)[0].strip("- ").strip() for ln in f.file.text.splitlines() if ":" in ln]
    return []


def check_startup(p: Profile, acts: list[SessionActivity]) -> tuple[list[Finding], dict[str, str]]:
    out: list[Finding] = []
    est = "" if p.token_mode == "exact" else "≈"
    files = p.startup_file_tokens
    metrics = {"instruction files at startup": f"{est}{files:,}"}
    if p.measured_startup:
        metrics["measured startup (median)"] = f"{p.measured_startup:,}"
    secs = sorted(((f, s) for f, s in p.all_sections() if f.file.loaded == STARTUP), key=lambda fs: -fs[1].tokens)
    if files >= STARTUP_LOOK:
        buckets = savings_buckets(p)
        movable = sum(buckets.values())
        share = f" ({files / p.measured_startup:.0%} of what each session starts with)" if p.measured_startup else ""
        out.append(Finding(
            "Startup context", FIX if files >= STARTUP_FIX else LOOK,
            f"Instruction files load {est}{files:,} tokens before your first message",
            f"They are written to the cache once and re-read on every turn{share}. "
            f"{est}{movable:,} tokens are history, copies of repo docs, or rules about one directory.",
            "Run `context-lens doctor --compact` to move directory-specific rules into on-demand rule files and "
            "replace copied docs with pointers; it checks that every path and command still lands somewhere.",
            movable or None,
            [f"{est}{s.tokens:,}  {s.hint:<14} {s.section.title}  ({_rel(f.file.path, p.repo)}:{s.section.start_line})"
             for f, s in secs[:TOP_EVIDENCE]],
        ))
    # A bare filename ("Button.tsx") or a slash-joined name ("console.log/warn") is often an example
    # or an identifier, so only report what is clearly a file path.
    missing = sorted({r for _, s in secs for r in s.missing_refs if "/" in r and _PATH_EXT_RE.search(r)})
    if missing:
        out.append(Finding(
            "Startup context", LOOK, f"{len(missing)} paths named in instruction files no longer exist",
            "Claude follows these references and finds nothing, or trusts a rule about code that has moved.",
            "Update or delete the stale references.", None, missing[:TOP_EVIDENCE],
        ))

    skills = _skill_names(p)
    listing = next((f.tokens for f in p.files if f.file.kind == "skill-listing"), 0)
    if skills and acts:
        used = {c.key for a in acts for c in a.calls if c.name == "Skill"} | {n for a in acts for n in a.commands}
        used |= {u.split(":")[-1] for u in used}
        unused = [s for s in skills if s not in used and s.split(":")[-1] not in used]
        metrics["skills listed / used"] = f"{len(skills)} / {len(skills) - len(unused)}"
        if listing >= SKILL_LISTING_LOOK and len(unused) > len(skills) / 2:
            out.append(Finding(
                "Tools and MCP", LOOK, f"{len(unused)} of {len(skills)} listed skills were never used",
                f"Every session carries the skill listing ({est}{listing:,} tokens) so Claude knows what it can invoke.",
                "Move skills you never use out of ~/.claude/skills (or into a project that needs them).",
                int(listing * len(unused) / len(skills)), unused[:TOP_EVIDENCE * 2],
            ))
    return out, metrics


def check_local(repo: Path, metrics: dict[str, str]) -> list[Finding]:
    """A compact applied with --apply-local goes stale when a pull changes the committed CLAUDE.md."""
    from .local import status

    st = status(repo)
    if not st.active:
        return []
    metrics["local slim copy"] = "stale" if st.stale else "on, up to date"
    if not st.stale:
        return []
    rel = _rel(st.original, repo)
    return [Finding(
        "Startup context", FIX, f"Your local slim copy is older than {rel}",
        f"{rel} changed (probably a pull) since this checkout switched to the slim version, so sessions here "
        "may be missing the newest rules.",
        "Re-make it with `context-lens doctor --compact --apply-local`, or go back with "
        "`context-lens doctor --undo-local`.",
    )]


# --- wasted work ---------------------------------------------------------------

def check_waste(acts: list[SessionActivity]) -> tuple[list[Finding], dict[str, str]]:
    if not acts:
        return [], {}
    n = len(acts)
    calls = [c for a in acts for c in a.calls]
    errors = [c for c in calls if c.error]
    rate = len(errors) / len(calls) if calls else 0.0
    large = [c for c in calls if c.result_tokens >= LARGE_RESULT_TOKENS]
    large_per = sum(c.result_tokens for c in large) / n
    loops = sum(a.retry_loops for a in acts)
    reread = sum(a.reread_tokens for a in acts)
    metrics = {
        "tool calls": f"{len(calls):,}",
        "failed calls": f"{len(errors):,} ({rate:.1%})",
        "oversized outputs": f"{len(large):,} (≈{large_per:,.0f} tokens/session)",
        "repeated failing calls": f"{loops:,}",
    }
    out: list[Finding] = []
    if large_per >= LARGE_OUTPUT_LOOK:
        by_cmd = Counter()
        for c in large:
            by_cmd[f"{c.name}: {c.key[:90]}"] += c.result_tokens
        out.append(Finding(
            "Wasted work", FIX if large_per >= LARGE_OUTPUT_FIX else LOOK,
            f"Tool outputs over 5k tokens add ≈{large_per:,.0f} tokens per session",
            f"{len(large)} results over 5k tokens. Each stays in context, re-read every later turn, until compaction.",
            "Cap noisy commands (`| head`, `| tail -50`, `--quiet`), read files with offset/limit, and send wide "
            "searches to a subagent so only its summary comes back. A one-line CLAUDE.md rule naming the worst "
            "commands works well.",
            int(large_per), [f"≈{t:,}  {k}" for k, t in by_cmd.most_common(TOP_EVIDENCE)],
        ))
    if calls and rate >= ERROR_RATE_LOOK:
        top = Counter(f"{c.name}: {c.key[:90]}" for c in errors)
        out.append(Finding(
            "Wasted work", FIX if rate >= ERROR_RATE_FIX else LOOK, f"{rate:.0%} of tool calls fail",
            f"{len(errors):,} of {len(calls):,} calls returned an error; each costs a turn to notice and retry.",
            "Look at the repeat offenders below. A wrong path, a missing tool or a permission rule usually "
            "explains most of them, and one line in CLAUDE.md fixes it for every later session.",
            None, [f"{k}  ×{v}" for k, v in top.most_common(TOP_EVIDENCE)],
        ))
    if loops >= 3:
        out.append(Finding(
            "Wasted work", LOOK, f"Claude retried the same failing call {loops} times",
            "The same tool with the same input failed twice or more in a row.",
            "Tell Claude in CLAUDE.md what to do instead for these, or allow the command in settings if a "
            "permission denial is what failed.", None,
        ))
    if reread:
        out.append(Finding(
            "Wasted work", LOOK, f"Unchanged files were read again (≈{reread // n:,} tokens per session)",
            f"{sum(a.rereads for a in acts)} re-reads of a file that hadn't changed since the last read.",
            "Usually a sign context was compacted mid-task; see Session health.", reread // n,
        ))
    return out, metrics


# --- tools and MCP -------------------------------------------------------------

def configured_mcp(repo: Path, home: Path | None = None) -> list[str]:
    """MCP servers this repo's sessions start with: user, local (per-project) and .mcp.json scopes."""
    home = home or Path.home()
    names: list[str] = []

    def load(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    cfg = load(home / ".claude.json")
    names += list((cfg.get("mcpServers") or {}).keys())
    proj = (cfg.get("projects") or {}).get(str(repo.resolve())) or {}
    names += list((proj.get("mcpServers") or {}).keys())
    names += list((load(repo / ".mcp.json").get("mcpServers") or {}).keys())
    return list(dict.fromkeys(names))


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def check_tools(repo: Path, acts: list[SessionActivity], home: Path | None = None) -> tuple[list[Finding], dict[str, int]]:
    used = Counter()
    for a in acts:
        for c in a.calls:
            server = mcp_server(c.name)
            if server:
                used[_norm(server)] += 1
    servers = {s: used.get(_norm(s), 0) for s in configured_mcp(repo, home)}
    idle = [s for s, n in servers.items() if n == 0]
    out: list[Finding] = []
    if acts and idle:
        out.append(Finding(
            "Tools and MCP", LOOK, f"{len(idle)} MCP server(s) configured but never called",
            f"No tool from these servers ran in {len(acts)} sessions. Each one still starts with every session "
            "and adds its tools to what Claude can see.",
            "Remove them with `claude mcp remove <name>`, or move them to the projects that use them.",
            None, idle,
        ))
    return out, servers


# --- session health ------------------------------------------------------------

def check_sessions(acts: list[SessionActivity]) -> tuple[list[Finding], dict[str, str]]:
    if not acts:
        return [], {}
    compacted = [a for a in acts if a.compactions]
    share = len(compacted) / len(acts)
    peaks = [a.peak_context for a in acts if a.context]
    growth = [(a.context[-1] - a.context[0]) / (len(a.context) - 1) for a in acts if len(a.context) > 1]
    metrics = {
        "sessions": f"{len(acts):,}",
        "compacted": f"{len(compacted)} ({share:.0%})",
        "peak context (median)": _n(_median(peaks)),
        "context added per turn (median)": _n(_median(growth)),
    }
    out: list[Finding] = []
    if share >= COMPACTED_SHARE_LOOK:
        out.append(Finding(
            "Session health", LOOK, f"{share:.0%} of sessions ran out of room and were compacted",
            "Compaction summarises the conversation, and detail from before it is gone: Claude re-reads files "
            "and can forget instructions you gave early on.",
            "Start a fresh session (/clear) between unrelated tasks, keep oversized outputs out (see Wasted "
            "work), and hand broad searches to subagents.",
            None, [f"{a.started:%Y-%m-%d}  {a.compactions}×  peak {a.peak_context:,}" for a in compacted[-TOP_EVIDENCE:] if a.started],
        ))
    return out, metrics


# --- corrections -------------------------------------------------------------------

CORRECTIONS_LOOK = 5


def check_corrections(acts: list[SessionActivity]) -> tuple[list[Finding], dict[str, str]]:
    from .learn import best_examples, gather

    if not acts:
        return [], {}
    found = gather(acts)
    kinds = Counter(c.kind for c in found)
    sessions = len({c.session_id for c in found})
    metrics = {
        "corrections": f"{len(found)} in {sessions} sessions",
        "rejected / interrupted / pushback": f"{kinds['rejected']} / {kinds['interrupted']} / {kinds['pushback']}",
    }
    if len(found) < CORRECTIONS_LOOK:
        return [], metrics
    return [Finding(
        "Corrections", LOOK, f"You corrected Claude {len(found)} times in {sessions} sessions",
        "Rejected actions, interruptions and pushback (\u201cno\u201d, \u201cdon't\u201d, \u201cI told you\u201d). "
        "Some are one-off redirections; the ones you repeat are rules your CLAUDE.md is missing.",
        "Run `context-lens doctor --learn` to group the repeats and write the missing rules "
        "(sends your redacted words to a model; `--learn-dry-run` shows exactly what first).",
        None, [f"{c.kind}: \u201c{c.said[:110]}\u201d" for c in best_examples(found, TOP_EVIDENCE)],
    )], metrics


# --- rules -----------------------------------------------------------------------

def check_rules(results: list, uncheckable: int = 0) -> tuple[list[Finding], dict[str, str]]:
    if not results:
        return [], {}
    applied = [r for r in results if r.applied]
    followed = sum(r.followed for r in results)
    total = sum(r.applied for r in results)
    metrics = {
        "rules judged": f"{len(results)}",
        "about code content (not judged)": f"{uncheckable}",
        "came up in sampled sessions": f"{len(applied)}",
        "verdicts followed": f"{followed}/{total} ({followed / total:.0%})" if total else "n/a",
    }
    out: list[Finding] = []
    for r in sorted(applied, key=lambda r: (r.adherence or 0, -r.violated)):
        if r.adherence is not None and r.adherence < ADHERENCE_FIX and r.violated >= 1:
            out.append(Finding(
                "Rule adherence", FIX if r.violated >= 2 else LOOK,
                f"{r.rule.id} “{r.rule.title}” followed {r.followed} of {r.applied} times",
                f"From {r.rule.source}. Verdicts come from a model reading command lines and prompts only.",
                "Make the rule shorter and more concrete (name the command or file), or enforce it with a hook "
                "or lint so it doesn't depend on Claude remembering.",
                None, [f"{e.verdict}: {e.line[:120]}  ({e.why})" for e in r.evidence if e.verdict == "violated"][:3],
            ))
    never = [r for r in results if not r.applied]
    if never:
        tokens = sum(r.rule.tokens for r in never)
        out.append(Finding(
            "Rule adherence", LOOK, f"{len(never)} rules never came up in the sampled sessions",
            "None of the sampled sessions did anything these rules govern. A rule that rarely applies still "
            "loads every session; with a small sample, read the list before acting on it.",
            "Candidates to move into path-scoped rule files that load only when relevant (`context-lens doctor --compact`).",
            tokens, [f"≈{r.rule.tokens:,}  {r.rule.title}  ({r.rule.source})" for r in never[:TOP_EVIDENCE]],
        ))
    return out, metrics


# --- assemble --------------------------------------------------------------------

def run_doctor(repo: Path, days: int = 30, token_mode: str = "auto", model: str = "claude-opus-5-5",
               home: Path | None = None, projects_dir: Path | None = None) -> Doctor:
    repo = repo.resolve()
    profile = build_profile(repo, days=days, token_mode=token_mode, model=model, home=home, projects_dir=projects_dir)
    acts = [read_activity(path) for path in transcript_paths(repo, days, projects_dir)]
    acts = sorted((a for a in acts if a.calls or a.prompts), key=lambda a: a.started.timestamp() if a.started else 0)
    findings: list[Finding] = []
    metrics: dict[str, dict[str, str]] = {}
    f, metrics["Startup context"] = check_startup(profile, acts)
    findings += f
    findings += check_local(repo, metrics["Startup context"])
    f, metrics["Wasted work"] = check_waste(acts)
    findings += f
    f, servers = check_tools(repo, acts, home)
    findings += f
    metrics["Tools and MCP"] = {**{k: v for k, v in metrics["Startup context"].items() if k.startswith("skills")},
                                "MCP servers configured": f"{len(servers)}",
                                "MCP servers used": f"{sum(1 for n in servers.values() if n)}"}
    metrics["Startup context"].pop("skills listed / used", None)
    f, metrics["Session health"] = check_sessions(acts)
    findings += f
    f, metrics["Corrections"] = check_corrections(acts)
    findings += f
    return Doctor(profile, acts, findings, metrics, servers)


def add_rules(doc: Doctor, results: list, uncheckable: int, sent: str) -> None:
    f, doc.metrics["Rule adherence"] = check_rules(results, uncheckable)
    doc.findings += f
    doc.rules = results
    doc.sent.append(sent)


def add_lessons(doc: Doctor, lessons: list, sent: str) -> None:
    """Findings from --learn: one per lesson, numbered as --add-rules takes them."""
    doc.lessons = lessons
    doc.sent.append(sent)
    doc.metrics.setdefault("Corrections", {})["rules learned"] = (
        f"{sum(1 for l in lessons if not l.covered_by)} new, {sum(1 for l in lessons if l.covered_by)} already written")
    # The local summary finding is replaced by the specific ones.
    doc.findings = [f for f in doc.findings if not (f.area == "Corrections" and f.title.startswith("You corrected"))]
    for n, l in enumerate(lessons, 1):
        quotes = [f"\u201c{e['said'][:110]}\u201d" for e in l.examples if e["said"]][:3]
        if l.conflicts_with:
            doc.findings.append(Finding(
                "Corrections", FIX, f"Your instructions contradict what you keep telling Claude: {l.title} ({l.count}×)",
                f"Rule {l.conflicts_with} says the opposite of what you asked for {l.count} times, so Claude "
                f"follows the file. What you keep saying: {l.rule}",
                f"Change {l.conflicts_with.split(' ')[0]} to match. `context-lens doctor --add-rules {n}` would add "
                "the new rule but leave the old one in place, so edit the old rule first.",
                None, quotes,
            ))
        elif l.covered_by:
            doc.findings.append(Finding(
                "Corrections", LOOK, f"You keep correcting Claude on a rule you already have: {l.title} ({l.count}×)",
                f"Your rule {l.covered_by} says this, but you still had to say it {l.count} times.",
                "Make that rule shorter and more concrete, move it nearer the top, or enforce it with a hook.",
                None, quotes,
            ))
        else:
            where = "your personal ~/.claude/CLAUDE.md" if l.scope == "personal" else "this repo's CLAUDE.md"
            doc.findings.append(Finding(
                "Corrections", FIX if l.count >= 2 else LOOK, f"Add a rule: {l.title} (you said it {l.count}×)",
                f"Suggested for {where}: {l.rule}",
                f"`context-lens doctor --add-rules {n}` adds it. Pick several with `--add-rules 1,3`.",
                None, quotes,
            ))


def render_lessons(doc: Doctor) -> list[str]:
    lines = [f"LEARNED FROM YOUR CORRECTIONS ({len(doc.lessons)})"]
    for n, l in enumerate(doc.lessons, 1):
        tag = (f"contradicts {l.conflicts_with}" if l.conflicts_with else f"already in {l.covered_by}"
               if l.covered_by else ("personal" if l.scope == "personal" else "repo"))
        lines.append(f"{n:>2}. [{l.count}×, {tag}] {l.rule}")
    new = [str(n) for n, l in enumerate(doc.lessons, 1) if not l.covered_by and not l.conflicts_with]
    if new:
        lines.append(f"    Add them with `context-lens doctor --add-rules {','.join(new)}` (or pick some).")
    return lines


def render_text(doc: Doctor) -> str:
    icon = {FIX: "✗", LOOK: "!", OK: "✓"}
    lines = [f"context-lens doctor · {doc.profile.repo}", f"{len(doc.activity)} sessions in the last {doc.profile.days} days", ""]
    for area in AREAS:
        if area not in doc.metrics:
            continue
        m = doc.metrics[area]
        if not m:
            lines.append(f"  - {area:<16} no sessions in this window")
            continue
        summary = " · ".join(f"{k} {v}" for k, v in m.items())
        lines.append(f"  {icon[doc.area_status(area)]} {area:<16} {summary}")
    if "Rule adherence" not in doc.metrics:
        lines.append("  - Rule adherence   not checked (add --rules; sends redacted excerpts to a model)")
    if doc.lessons:
        lines.append("")
        lines += render_lessons(doc)
    lines.append("")
    ranked = doc.ranked()
    if not ranked:
        lines.append("Nothing to fix. Startup is lean and sessions are clean.")
    else:
        lines.append(f"WHAT TO DO ({len(ranked)}, most important first)")
        for i, f in enumerate(ranked, 1):
            save = f"  [≈{f.tokens_per_session:,} tokens/session]" if f.tokens_per_session else ""
            lines.append(f"{i:>2}. [{f.severity}] {f.title}{save}")
            lines.append(f"    {f.fix}")
            for e in f.evidence[:3]:
                lines.append(f"      · {e}")
        if doc.recoverable:
            lines.append("")
            lines.append(f"≈{doc.recoverable:,} tokens per session could stay out of context.")
    lines.append("")
    lines.append(doc.privacy)
    return "\n".join(lines)


def to_json(doc: Doctor) -> str:
    from .report import to_json as profile_json
    from .rules import results_to_json

    data = {
        "repo": str(doc.profile.repo),
        "days": doc.profile.days,
        "areas": {a: {"status": doc.area_status(a), "metrics": doc.metrics.get(a, {})} for a in AREAS if a in doc.metrics},
        "findings": [asdict(f) for f in doc.ranked()],
        "recoverable_tokens_per_session": doc.recoverable,
        "mcp_servers": doc.mcp_servers,
        "sessions": [
            {"session_id": a.session_id, "started": a.started.isoformat() if a.started else None,
             "tool_calls": len(a.calls), "failed_calls": a.errors, "compactions": a.compactions,
             "peak_context": a.peak_context, "large_output_tokens": a.large_result_tokens}
            for a in doc.activity
        ],
        "rules": results_to_json(doc.rules),
        "lessons": [asdict(l) for l in doc.lessons],
        "startup": json.loads(profile_json(doc.profile)),
        "privacy": doc.privacy,
    }
    return json.dumps(data, indent=2, default=str)
