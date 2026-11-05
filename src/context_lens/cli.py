"""context-lens command line."""

from __future__ import annotations

import argparse
import statistics
import threading
import sys
from pathlib import Path

from . import __version__
from .profile import build_profile
from .report import render_text, to_json


def _profile(args: argparse.Namespace) -> int:
    repo = Path(args.path).expanduser()
    if not repo.is_dir():
        print(f"context-lens: {repo} is not a directory", file=sys.stderr)
        return 2
    prof = build_profile(repo, days=args.days, token_mode=args.tokens, model=args.model)
    if args.json:
        Path(args.json).write_text(to_json(prof), encoding="utf-8")
    if args.html:
        from .html import render_html

        Path(args.html).write_text(render_html(prof), encoding="utf-8")
    print(render_text(prof))
    if args.json:
        print(f"\nJSON written to {args.json}")
    if args.html:
        print(f"HTML report written to {args.html}")
    return 0


def _compact(args: argparse.Namespace) -> int:
    from .compact import Plan, assemble, build_plan, summarize, write_outputs
    from .llm import BackendError, pick_backend
    from .tokens import TokenCounter
    from .transcripts import sessions_for

    repo = Path(args.path).expanduser().resolve()
    target = (repo / args.file).resolve()
    if not target.is_file():
        print(f"context-lens: {target} not found", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser().resolve()
    counter = TokenCounter(args.count_model, args.tokens)
    if args.from_plan:
        plan = Plan.from_json(Path(args.from_plan).read_text(encoding="utf-8"))
    else:
        try:
            backend = pick_backend(args.backend, args.model)
        except BackendError as exc:
            print(f"context-lens: {exc}", file=sys.stderr)
            return 2
        plan = build_plan(backend, repo, target, counter, jobs=args.jobs, repair=not args.no_repair)
        u = backend.usage
        cost = f", ${u.cost_usd:.2f} list price" if u.cost_usd else ""
        print(f"{u.calls} calls, {u.input_tokens:,} in / {u.output_tokens:,} out{cost}")
    outputs = assemble(plan, index_rules=not args.no_index)
    sessions = sessions_for(repo, args.days)
    measured = int(statistics.median(s.startup_tokens for s in sessions)) if sessions else None
    report = summarize(plan, outputs, counter, repo, measured)
    out.mkdir(parents=True, exist_ok=True)
    write_outputs(out / "files", outputs)
    (out / "plan.json").write_text(plan.to_json(), encoding="utf-8")
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report.split("## Decisions")[0].rstrip())
    print(f"\nProposed files in {out / 'files'}; plan in {out / 'plan.json'}; report in {out / 'report.md'}")
    if args.apply:
        for path in write_outputs(repo, outputs):
            print(f"applied {path}")
    return 0


def _eval_gen(args: argparse.Namespace) -> int:
    from .compact import Plan
    from .eval import generate_probes, save_probes
    from .llm import BackendError, pick_backend

    repo = Path(args.path).expanduser().resolve()
    plan = Plan.from_json(Path(args.plan).read_text(encoding="utf-8"))
    try:
        backend = pick_backend(args.backend, args.model)
    except BackendError as exc:
        print(f"context-lens: {exc}", file=sys.stderr)
        return 2
    probes = generate_probes(backend, repo, plan, args.n, jobs=args.jobs, seed=args.seed, review=not args.no_review)
    save_probes(Path(args.out), probes)
    print(f"{len(probes)} probes written to {args.out}; review and edit them before `eval run`.")
    return 0


def _eval_run(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    from .eval import eval_report, load_probes, run_eval
    from .llm import BackendError, pick_backend

    repo = Path(args.path).expanduser().resolve()
    probes = load_probes(Path(args.probes))
    if args.only:
        wanted = set(args.only.split(","))
        probes = [p for p in probes if p.id in wanted]
    after = Path(args.after).expanduser().resolve()
    if not (after / "CLAUDE.md").is_file():
        print(f"context-lens: {after} has no CLAUDE.md (pass the compact output's files/ dir)", file=sys.stderr)
        return 2
    try:
        judge = pick_backend(args.backend, args.judge_model)
    except BackendError as exc:
        print(f"context-lens: {exc}", file=sys.stderr)
        return 2
    variants = {"before": None, "after": after}
    print(f"{len(probes)} probes × {len(variants)} variants × {args.reps} rep(s) on {args.model}")
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    partial = out / "results.partial.jsonl"
    lock = threading.Lock()

    def save(res) -> None:  # each session as it finishes, so a stopped run keeps what it paid for
        with lock, partial.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(res)) + "\n")

    results = run_eval(repo, probes, variants, judge, model=args.model, reps=args.reps, jobs=args.jobs,
                       budget=args.budget, keep_worktrees=args.keep_worktrees, on_result=save)
    (out / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    report = eval_report(probes, results, args.model)
    (out / "report.md").write_text(report, encoding="utf-8")
    from .eval_html import render_eval_html

    (out / "report.html").write_text(render_eval_html(probes, results, args.model), encoding="utf-8")
    print(report.split("## Probes")[0].rstrip())
    print(f"\nResults in {out / 'results.json'}; report in {out / 'report.md'} and {out / 'report.html'}")
    return 0


def _eval_report(args: argparse.Namespace) -> int:
    from .eval import eval_report, load_probes
    from .eval_html import anonymize, load_results, render_eval_html

    probes = load_probes(Path(args.probes))
    results = load_results(Path(args.results))
    seen = {r.probe_id for r in results}
    probes = [p for p in probes if p.id in seen]
    if args.anonymize:
        probes, results = anonymize(probes, results)
    Path(args.html).write_text(render_eval_html(probes, results, args.model), encoding="utf-8")
    print(eval_report(probes, results, args.model).split("## Probes")[0].rstrip())
    print(f"\nHTML report written to {args.html}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="context-lens", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("profile", help="measure what Claude Code loads at startup in a repo")
    p.add_argument("path", nargs="?", default=".", help="repo or working directory (default: .)")
    p.add_argument("--days", type=int, default=30, help="transcript window in days (default: 30)")
    p.add_argument("--tokens", choices=["auto", "exact", "estimate"], default="auto")
    p.add_argument("--model", default="claude-opus-5-5", help="model used for exact token counts")
    p.add_argument("--json", metavar="FILE", help="also write the full profile as JSON")
    p.add_argument("--html", metavar="FILE", help="also write a self-contained HTML report")
    p.set_defaults(func=_profile)

    c = sub.add_parser("compact", help="propose a smaller startup CLAUDE.md with nothing lost")
    c.add_argument("path", nargs="?", default=".", help="repo root (default: .)")
    c.add_argument("--file", default="CLAUDE.md", help="instruction file to compact (default: CLAUDE.md)")
    c.add_argument("--backend", choices=["auto", "api", "claude-code"], default="auto",
                   help="api = ANTHROPIC_API_KEY; claude-code = headless `claude -p` on your subscription")
    c.add_argument("--model", default="claude-opus-5-5", help="model that makes the decisions")
    c.add_argument("--jobs", type=int, default=4, help="parallel model calls (default: 4)")
    c.add_argument("--out", default="context-lens-out", help="where proposed files, plan and report go")
    c.add_argument("--from-plan", metavar="PLAN", help="re-assemble from a saved plan.json, no model calls")
    c.add_argument("--apply", action="store_true", help="also write the proposed files into the repo")
    c.add_argument("--no-repair", action="store_true", help="skip the second pass on lost facts")
    c.add_argument("--no-index", action="store_true", help="don't list the scoped rule files in CLAUDE.md")
    c.add_argument("--tokens", choices=["auto", "exact", "estimate"], default="auto")
    c.add_argument("--count-model", default="claude-opus-5-5", help="model used for exact token counts")
    c.add_argument("--days", type=int, default=30, help="transcript window for the measured baseline")
    c.set_defaults(func=_compact)

    e = sub.add_parser("eval", help="check a compacted CLAUDE.md still gets followed")
    esub = e.add_subparsers(dest="eval_command", required=True)
    g = esub.add_parser("gen", help="write rule probes from a compact plan")
    g.add_argument("path", nargs="?", default=".", help="repo root (default: .)")
    g.add_argument("--plan", required=True, help="plan.json from `compact`")
    g.add_argument("--n", type=int, default=30, help="sections to probe (default: 30)")
    g.add_argument("--out", default="probes.json")
    g.add_argument("--backend", choices=["auto", "api", "claude-code"], default="auto")
    g.add_argument("--model", default="claude-opus-5-5", help="model that writes the probes")
    g.add_argument("--jobs", type=int, default=4)
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--no-review", action="store_true", help="skip the rubric review pass")
    g.set_defaults(func=_eval_gen)
    r = esub.add_parser("run", help="run probes against the original and the compacted files")
    r.add_argument("path", nargs="?", default=".", help="repo root (default: .)")
    r.add_argument("--probes", required=True)
    r.add_argument("--after", required=True, help="the compact output's files/ directory")
    r.add_argument("--model", default="claude-sonnet-5-5", help="model the probe sessions run on")
    r.add_argument("--judge-model", default="claude-sonnet-5-5")
    r.add_argument("--backend", choices=["auto", "api", "claude-code"], default="auto", help="judge backend")
    r.add_argument("--reps", type=int, default=1)
    r.add_argument("--jobs", type=int, default=3, help="parallel sessions (default: 3)")
    r.add_argument("--budget", type=float, default=3.0, help="max USD per session (default: 3)")
    r.add_argument("--only", help="comma-separated probe ids")
    r.add_argument("--out", default="context-lens-eval")
    r.add_argument("--keep-worktrees", action="store_true")
    r.set_defaults(func=_eval_run)
    rp = esub.add_parser("report", help="rebuild the report from saved (or partial) results")
    rp.add_argument("--probes", required=True)
    rp.add_argument("--results", required=True, help="results.json or results.partial.jsonl")
    rp.add_argument("--model", default="claude-sonnet-5-5")
    rp.add_argument("--html", default="report.html")
    rp.add_argument("--anonymize", action="store_true",
                    help="label probes Rule 01, Rule 02, … and drop all rule text, answers and file names")
    rp.set_defaults(func=_eval_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
