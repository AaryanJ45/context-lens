"""context-lens command line: one command, `doctor`.

With no options it runs the checkup. Every follow-up a fix calls for is a flag
on the same command, so there is nothing else to learn:

  context-lens doctor                     checkup, local only
  context-lens doctor --rules             + did past sessions follow your rules
  context-lens doctor --learn             + turn what you keep correcting into rules
  context-lens doctor --add-rules 1,3     add learned rules to CLAUDE.md
  context-lens doctor --compact           propose a slim CLAUDE.md (writes nothing into the repo)
  context-lens doctor --apply-local       use that proposal in this checkout only
  context-lens doctor --apply             write that proposal into the repo
  context-lens doctor --undo-local        back to the committed CLAUDE.md
  context-lens doctor --verify quick      prove the proposal is still followed

`doctor` can be left out: `context-lens --compact` means the same thing.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from . import __version__

DEFAULT_OUT = ".context-lens"
RULES_JUDGE_MODEL = "claude-haiku-5-5"
VERIFY_JUDGE_MODEL = "claude-sonnet-5-5"
VERIFY_SIZES = {"quick": (10, 1), "full": (30, 3)}  # probes, runs per probe
ACTIONS = ("compact", "apply", "apply_local", "undo_local", "local_status", "verify", "verify_report", "add_rules")


def _fail(msg: str, code: int = 2) -> int:
    print(f"context-lens: {msg}", file=sys.stderr)
    return code


def _show(html: Path, args: argparse.Namespace) -> None:
    """Point at the report, and open it in the browser when someone is at the terminal (or --open)."""
    url = html.resolve().as_uri()
    should_open = args.open if args.open is not None else sys.stdout.isatty()
    opened = False
    if should_open:
        import webbrowser

        try:
            opened = webbrowser.open(url)
        except Exception:  # noqa: BLE001 - no browser is not an error
            opened = False
    print(f"\n{'Opened the report in your browser' if opened else 'See the full report in your browser'}: {url}")


def _backend(args: argparse.Namespace, model: str):
    from .llm import pick_backend

    return pick_backend(args.backend, model)


# --- checkup ---------------------------------------------------------------------

def _checkup(repo: Path, out: Path, args: argparse.Namespace) -> int:
    from .doctor import render_text, run_doctor, to_json
    from .doctor_html import render_doctor_html

    doc = run_doctor(repo, days=args.days, token_mode=args.tokens, model=args.count_model)
    if args.rules or args.rules_dry_run:
        code = _rules(doc, out, args)
        if code is not None:
            return code
    if args.learn or args.learn_dry_run:
        code = _learn(doc, out, args)
        if code is not None:
            return code
    def write() -> None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "doctor.json").write_text(to_json(doc), encoding="utf-8")
        (out / "doctor.html").write_text(render_doctor_html(doc), encoding="utf-8")

    write()
    print(render_text(doc))
    if _offer_learn(doc, args):
        _learn(doc, out, args)
        if doc.lessons:
            from .doctor import render_lessons

            print("\n" + "\n".join(render_lessons(doc)))
            print(f"\n{doc.privacy}")
        write()
    print(f"\nData for scripts: {out / 'doctor.json'}")
    _show(out / "doctor.html", args)
    return 0


def _rules(doc, out: Path, args: argparse.Namespace) -> int | None:
    """Rule adherence. Returns an exit code to stop early, or None to carry on to the report."""
    from .doctor import add_rules
    from .llm import BackendError
    from .rules import build_payload, candidate_rules, judge, pick_sessions, select_checkable, write_dry_run

    model = args.judge_model or RULES_JUDGE_MODEL
    candidates = candidate_rules(doc.profile)
    sessions = pick_sessions(doc.activity, args.rules_sessions)
    if not candidates or not sessions:
        print("context-lens: rule check skipped: no startup rules or no sessions with tool calls", file=sys.stderr)
        return None
    if args.rules_dry_run:
        path = out / "rules-payloads.jsonl"
        payloads = [build_payload(a, candidates[: args.rules_max]) for a in sessions]
        write_dry_run(candidates, payloads, path)
        print(f"{1 + len(payloads)} requests written to {path}, redacted. Nothing was sent. "
              "Read them, then run with --rules to send.")
        return 0
    try:
        backend = _backend(args, model)
        rules = select_checkable(backend, candidates, args.rules_max)
    except BackendError as exc:
        print(f"context-lens: rule check skipped: {exc}", file=sys.stderr)
        return None
    if not rules:
        print("context-lens: rule check skipped: none of your rules can be checked from command lines", file=sys.stderr)
        return None
    payloads = [build_payload(a, rules) for a in sessions]
    sent = sum(len(p.prompt) for p in payloads)
    print(f"Judging {len(rules)} of {len(candidates)} rules (the rest are about code content, which command lines "
          f"don't show) against {len(payloads)} sessions on {model} ({sent:,} redacted characters)…",
          file=sys.stderr)
    results, failed = judge(backend, rules, payloads, jobs=args.jobs)
    u = backend.usage
    cost = f", ${u.cost_usd:.2f}" if u.cost_usd else ""
    note = f"; {failed} session(s) failed to judge" if failed else ""
    add_rules(doc, results, len(candidates) - len(rules),
              f"The rule check sent your rule text and {len(payloads) - failed} redacted session excerpts ({sent:,} "
              f"characters: prompts and one line per tool call, no tool output or file contents) to {model} via "
              f"{backend.name} ({u.calls} calls{cost}{note}).")
    return None


def _offer_learn(doc, args: argparse.Namespace) -> bool:
    """After a plain checkup at a terminal, ask once whether to learn from the corrections it found.

    Nothing is sent unless the answer is yes; scripts, pipes and Claude itself never see the question
    (the HTML report suggests --learn instead).
    """
    from .doctor import CORRECTIONS_LOOK
    from .learn import gather

    if args.learn or args.learn_dry_run or args.no_input or doc.lessons:
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    found = gather(doc.activity)
    if len(found) < CORRECTIONS_LOOK:
        return False
    sessions = len({c.session_id for c in found})
    print(f"\nYou corrected Claude {len(found)} times in {sessions} sessions.")
    try:
        answer = input("Learn rules from them? Sends redacted excerpts to a model, about $0.25. [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def _learn(doc, out: Path, args: argparse.Namespace) -> int | None:
    """Corrections to rules. Returns an exit code to stop early, or None to carry on to the report."""
    from .doctor import add_lessons
    from .learn import build_prompt, gather, learn, rule_index, save, write_dry_run
    from .llm import BackendError

    corrections = gather(doc.activity)
    if not corrections:
        print("context-lens: nothing to learn: no corrections in this window (try a longer --days)", file=sys.stderr)
        return None
    rules = rule_index(doc.profile)
    prompt, ids = build_prompt(corrections, rules)
    if args.learn_dry_run:
        path = out / "learn-payload.jsonl"
        write_dry_run(prompt, path)
        print(f"1 request ({len(ids)} corrections, {len(prompt):,} characters, redacted) written to {path}. "
              "Nothing was sent. Read it, then run with --learn to send.")
        return 0
    print(f"Learning from {len(ids)} corrections on {args.model} ({len(prompt):,} redacted characters)…", file=sys.stderr)
    try:
        backend = _backend(args, args.model)
        lessons = learn(backend, corrections, rules)
    except BackendError as exc:
        print(f"context-lens: learning skipped: {exc}", file=sys.stderr)
        return None
    save(lessons, out / "learn" / "lessons.json")
    u = backend.usage
    cost = f", ${u.cost_usd:.2f}" if u.cost_usd else ""
    add_lessons(doc, lessons,
                f"Learning sent {len(ids)} redacted corrections (your words and one line per action Claude took "
                f"just before, {len(prompt):,} characters) and your rule titles to {args.model} via {backend.name} "
                f"({u.calls} call{cost}).")
    return None


def _add_rules(repo: Path, out: Path, args: argparse.Namespace) -> int:
    from .learn import add_rules, load

    path = out / "learn" / "lessons.json"
    if not path.is_file():
        return _fail("no learned rules yet; run `context-lens doctor --learn` first")
    lessons = load(path)
    try:
        picks = [int(x) for x in args.add_rules.split(",") if x.strip()]
        written = add_rules(lessons, picks, repo, args.add_to)
    except ValueError as exc:
        return _fail(f"--add-rules takes lesson numbers like 1,3 ({exc})")
    if not written:
        print("Those rules are already there; nothing to add.")
        return 0
    for target, lines in written.items():
        print(f"Added {len(lines)} rule(s) to {target}:")
        for line in lines:
            print(f"  {line}")
    print("They load from the next session. Review with `git diff` before committing.")
    return 0


# --- compact and apply -------------------------------------------------------------

def _compact(repo: Path, out: Path, args: argparse.Namespace) -> int:
    """--compact proposes; --apply / --apply-local re-assemble from the saved plan and write it."""
    from .compact import Plan, assemble, build_plan, summarize, write_outputs
    from .llm import BackendError
    from .tokens import TokenCounter
    from .transcripts import sessions_for

    target = (repo / args.file).resolve()
    if not target.is_file():
        return _fail(f"{target} not found")
    cdir = out / "compact"
    plan_path = Path(args.plan).expanduser() if args.plan else cdir / "plan.json"
    counter = TokenCounter(args.count_model, args.tokens)
    if args.compact and not args.plan:
        try:
            backend = _backend(args, args.model)
        except BackendError as exc:
            return _fail(str(exc))
        plan = build_plan(backend, repo, target, counter, jobs=args.jobs, repair=not args.no_repair)
        u = backend.usage
        cost = f", ${u.cost_usd:.2f} list price" if u.cost_usd else ""
        print(f"{u.calls} calls, {u.input_tokens:,} in / {u.output_tokens:,} out{cost}")
    elif plan_path.is_file():
        # Re-assembling from the plan picks up any edits made to plan.json, with no model calls.
        plan = Plan.from_json(plan_path.read_text(encoding="utf-8"))
    else:
        return _fail(f"no proposal at {plan_path}; run `context-lens doctor --compact` first")
    outputs = assemble(plan, index_rules=not args.no_index)
    sessions = sessions_for(repo, args.days)
    measured = int(statistics.median(s.startup_tokens for s in sessions)) if sessions else None
    report = summarize(plan, outputs, counter, repo, measured)
    cdir.mkdir(parents=True, exist_ok=True)
    write_outputs(cdir / "files", outputs)
    if plan_path != cdir / "plan.json" or args.compact:
        (cdir / "plan.json").write_text(plan.to_json(), encoding="utf-8")
    (cdir / "report.md").write_text(report, encoding="utf-8")
    print(report.split("## Decisions")[0].rstrip())
    print(f"\nProposed files in {cdir / 'files'}; plan in {cdir / 'plan.json'}; report in {cdir / 'report.md'}")
    if args.apply:
        for path in write_outputs(repo, outputs):
            print(f"applied {path}")
        print("Review with `git diff`. Run `context-lens doctor --verify quick` before committing: it compares "
              "the committed version against the proposal.")
    elif args.apply_local:
        from .local import LocalModeError, enable

        try:
            written = enable(repo, target, outputs)
        except LocalModeError as exc:
            return _fail(str(exc))
        for path in written:
            print(f"applied locally {path}")
        print(f"{args.file} is unchanged and still committed; this checkout loads the slim version from the next "
              "session. Undo with `context-lens doctor --undo-local`.")
    elif args.compact:
        print("Nothing was written into the repo. Next: `context-lens doctor --apply-local` to try it in this "
              "checkout only, or `--apply` to change the repo.")
    return 0


# --- local mode ----------------------------------------------------------------------

def _local(repo: Path, args: argparse.Namespace) -> int:
    from .local import disable, status

    st = status(repo)
    if args.undo_local:
        removed = disable(repo)
        print(f"local mode off; removed {len(removed)} file(s)" if st.active else "local mode is not on")
        return 0
    if not st.active:
        if not args.quiet:
            print("local mode is off: this checkout loads the committed CLAUDE.md")
        return 0
    if st.stale:
        rel = st.original.relative_to(repo)
        print(f"context-lens local mode: {rel} changed since the slim copy was made, so CLAUDE.local.md "
              f"may be missing new rules. Read {rel} for anything newer, and re-run "
              "`context-lens doctor --compact --apply-local` (or `context-lens doctor --undo-local`).")
        return 1
    if not args.quiet:
        print(f"local mode on: {st.original.relative_to(repo)} is excluded, "
              f"{len(st.files)} local file(s) load instead; up to date")
    return 0


# --- verify ---------------------------------------------------------------------------

def _verify(repo: Path, out: Path, args: argparse.Namespace) -> int:
    """Write probes from the saved plan, then run them against the committed and proposed files."""
    from .compact import Plan
    from .eval import eval_report, generate_probes, load_probes, run_eval, save_probes
    from .eval_html import render_eval_html
    from .llm import BackendError

    plan_path = Path(args.plan).expanduser() if args.plan else out / "compact" / "plan.json"
    after = out / "compact" / "files"
    if not plan_path.is_file() or not (after / "CLAUDE.md").is_file():
        return _fail("nothing to verify yet; run `context-lens doctor --compact` first")
    n, reps = VERIFY_SIZES[args.verify]
    reps = args.reps or reps
    probes_path = out / "probes.json"
    try:
        if args.reuse_probes and probes_path.is_file():
            probes = load_probes(probes_path)
        else:
            plan = Plan.from_json(plan_path.read_text(encoding="utf-8"))
            probes = generate_probes(_backend(args, args.model), repo, plan, n, jobs=args.jobs, seed=args.seed,
                                     review=not args.no_review)
            save_probes(probes_path, probes)
            print(f"{len(probes)} probes written to {probes_path}")
        judge_backend = _backend(args, args.judge_model or VERIFY_JUDGE_MODEL)
    except BackendError as exc:
        return _fail(str(exc))
    if args.only:
        wanted = set(args.only.split(","))
        probes = [p for p in probes if p.id in wanted]
    variants = {"before": None, "after": after.resolve()}
    print(f"{len(probes)} probes × {len(variants)} variants × {reps} run(s) on {args.run_model}")
    edir = out / "eval"
    edir.mkdir(parents=True, exist_ok=True)
    partial = edir / "results.partial.jsonl"
    lock = threading.Lock()

    def save(res) -> None:  # each session as it finishes, so a stopped run keeps what it paid for
        with lock, partial.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(res)) + "\n")

    results = run_eval(repo, probes, variants, judge_backend, model=args.run_model, reps=reps, jobs=args.jobs,
                       budget=args.budget, keep_worktrees=args.keep_worktrees, on_result=save)
    (edir / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    report = eval_report(probes, results, args.run_model)
    (edir / "report.md").write_text(report, encoding="utf-8")
    (edir / "report.html").write_text(render_eval_html(probes, results, args.run_model), encoding="utf-8")
    print(report.split("## Probes")[0].rstrip())
    print(f"\nResults in {edir / 'results.json'}; report in {edir / 'report.md'}")
    _show(edir / "report.html", args)
    return 0


def _verify_report(out: Path, args: argparse.Namespace) -> int:
    """Rebuild the verify report from saved (or partial) results, optionally anonymized for sharing."""
    from .eval import eval_report, load_probes
    from .eval_html import anonymize, load_results, render_eval_html

    edir = out / "eval"
    results_path = edir / "results.json" if (edir / "results.json").is_file() else edir / "results.partial.jsonl"
    if not results_path.is_file() or not (out / "probes.json").is_file():
        return _fail("no verify results yet; run `context-lens doctor --verify quick` first")
    probes = load_probes(out / "probes.json")
    results = load_results(results_path)
    seen = {r.probe_id for r in results}
    probes = [p for p in probes if p.id in seen]
    if args.anonymize:
        probes, results = anonymize(probes, results)
    html = edir / ("report-shareable.html" if args.anonymize else "report.html")
    html.write_text(render_eval_html(probes, results, args.run_model, summary_only=args.summary_only),
                    encoding="utf-8")
    print(eval_report(probes, results, args.run_model).split("## Probes")[0].rstrip())
    _show(html, args)
    return 0


# --- entry point -------------------------------------------------------------------------

def _doctor(args: argparse.Namespace) -> int:
    chosen = [a for a in ACTIONS if getattr(args, a)]
    if len(chosen) > 1 and set(chosen) not in ({"compact", "apply"}, {"compact", "apply_local"}):
        flags = ", ".join("--" + a.replace("_", "-") for a in chosen)
        return _fail(f"pick one of {flags} (only --compact combines with --apply or --apply-local)")
    repo = Path(args.path).expanduser().resolve()
    if not repo.is_dir():
        return _fail(f"{repo} is not a directory")
    out = Path(args.out).expanduser()
    out = out if out.is_absolute() else repo / out
    if args.compact or args.apply or args.apply_local:
        return _compact(repo, out, args)
    if args.undo_local or args.local_status:
        return _local(repo, args)
    if args.verify:
        return _verify(repo, out, args)
    if args.verify_report:
        return _verify_report(out, args)
    if args.add_rules:
        return _add_rules(repo, out, args)
    return _checkup(repo, out, args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="context-lens", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="check how efficiently Claude Code runs in a repo, and fix it",
                       description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    d.add_argument("path", nargs="?", default=".", help="repo root (default: .)")
    d.add_argument("--out", default=DEFAULT_OUT, help=f"where everything is written (default: {DEFAULT_OUT}, in the repo)")
    d.add_argument("--days", type=int, default=30, help="transcript window in days (default: 30)")
    d.add_argument("--backend", choices=["auto", "api", "claude-code"], default="auto",
                   help="model calls: api = ANTHROPIC_API_KEY; claude-code = headless `claude -p` on your subscription")
    d.add_argument("--jobs", type=int, default=4, help="parallel model calls or sessions (default: 4)")

    g = d.add_argument_group("rule adherence (adds to the checkup)")
    g.add_argument("--rules", action="store_true",
                   help="also judge past sessions against your rules (sends redacted excerpts to a model)")
    g.add_argument("--rules-dry-run", action="store_true",
                   help="write the exact redacted requests --rules would send, and send nothing")
    g.add_argument("--rules-max", type=int, default=20, help="most rules to judge (default: 20)")
    g.add_argument("--rules-sessions", type=int, default=8, help="sessions to sample (default: 8)")

    g = d.add_argument_group("learn from your corrections")
    g.add_argument("--learn", action="store_true",
                   help="turn corrections you repeat into rules (sends your redacted words to a model)")
    g.add_argument("--learn-dry-run", action="store_true", help="write the exact request --learn would send, and send nothing")
    g.add_argument("--add-rules", metavar="N,M", help="add learned rules (numbers from --learn) to their CLAUDE.md")
    g.add_argument("--no-input", action="store_true", help="never ask a question (for scripts)")
    g.add_argument("--add-to", choices=["repo", "local", "personal"],
                   help="where --add-rules writes: repo CLAUDE.md, CLAUDE.local.md, or ~/.claude/CLAUDE.md "
                        "(default: what each rule is about)")

    g = d.add_argument_group("shrink CLAUDE.md")
    g.add_argument("--compact", action="store_true", help="propose a slim CLAUDE.md plus on-demand rule files")
    g.add_argument("--apply-local", action="store_true",
                   help="use the proposal in this checkout only; the committed file is untouched")
    g.add_argument("--apply", action="store_true", help="write the proposal into the repo")
    g.add_argument("--undo-local", action="store_true", help="go back to the committed CLAUDE.md")
    g.add_argument("--local-status", action="store_true", help="is local mode on, and is it stale after a pull")
    g.add_argument("--file", default="CLAUDE.md", help="instruction file to compact (default: CLAUDE.md)")
    g.add_argument("--plan", metavar="PLAN", help="use this plan.json instead of the saved one")
    g.add_argument("--no-repair", action="store_true", help="skip the second pass on lost facts")
    g.add_argument("--no-index", action="store_true", help="don't list the scoped rule files in CLAUDE.md")

    g = d.add_argument_group("prove the proposal still works")
    g.add_argument("--verify", choices=sorted(VERIFY_SIZES), metavar="{quick,full}",
                   help="quick: 10 probes × 1 run (~20 sessions); full: 30 × 3 (~180 sessions)")
    g.add_argument("--verify-report", action="store_true", help="rebuild the verify report from saved results")
    g.add_argument("--anonymize", action="store_true", help="with --verify-report: drop all rule text, for sharing")
    g.add_argument("--summary-only", action="store_true", help="with --verify-report: leave out per-probe detail")
    g.add_argument("--reuse-probes", action="store_true", help="run the probes already in probes.json")
    g.add_argument("--run-model", default="claude-sonnet-5-5", help="model the probe sessions run on")
    g.add_argument("--reps", type=int, help="runs per probe (overrides quick/full)")
    g.add_argument("--budget", type=float, default=3.0, help="max USD per probe session (default: 3)")
    g.add_argument("--only", help="comma-separated probe ids")
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--no-review", action="store_true", help="skip the probe rubric review pass")
    g.add_argument("--keep-worktrees", action="store_true")

    g = d.add_argument_group("models and tokens")
    g.add_argument("--model", default="claude-opus-5-5", help="model that writes the proposal and probes")
    g.add_argument("--judge-model", help=f"judge (default: {RULES_JUDGE_MODEL} for --rules, {VERIFY_JUDGE_MODEL} for --verify)")
    g.add_argument("--tokens", choices=["auto", "exact", "estimate"], default="auto")
    g.add_argument("--count-model", default="claude-opus-5-5", help="model used for exact token counts")
    g.add_argument("--quiet", action="store_true", help="with --local-status: print only when stale")

    g = d.add_argument_group("report")
    g.add_argument("--open", dest="open", action="store_true", default=None,
                   help="open the HTML report when done (the default when run in a terminal)")
    g.add_argument("--no-open", dest="open", action="store_false", help="don't open the report")
    d.set_defaults(func=_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("doctor", "-h", "--help", "--version"):
        argv.insert(0, "doctor")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
