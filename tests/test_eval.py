import json
import subprocess
from pathlib import Path

from context_lens.compact import Decision, Item, Plan
from context_lens.eval import (
    Probe, RunResult, eval_report, load_probes, make_variant, parse_stream, pick_sections, remove_variant, save_probes,
)


def _plan(actions):
    items = [Item(f"s{i}", f"T{i}", "x", 200, "core", [], None, 0.0, []) for i in range(len(actions))]
    return Plan("CLAUDE.md", "m", "fake", items, [], {f"s{i}": Decision(f"s{i}", a) for i, a in enumerate(actions)})


def test_pick_sections_prefers_changed_and_keeps_controls():
    plan = _plan(["keep"] * 10 + ["move"] * 10 + ["pointer"] * 10 + ["drop"] * 5)
    chosen = pick_sections(plan, 10)
    acts = [plan.decisions[i.id].action for i in chosen]
    assert len(chosen) == 10 and acts.count("keep") == 2 and "drop" not in acts
    assert [i.id for i in chosen] == sorted((i.id for i in chosen), key=lambda s: int(s[1:]))


def test_pick_sections_spreads_across_headings():
    items = [Item(f"s{i}", f"Catalog › f{i}", "x", 200, "core", [], None, 0.0, []) for i in range(40)]
    items += [Item(f"s{40 + i}", f"Rules › r{i}", "x", 200, "core", [], None, 0.0, []) for i in range(5)]
    items += [Item("s99", "Naming", "x", 200, "core", [], None, 0.0, [])]
    decisions = {i.id: Decision(i.id, "keep" if i.id == "s99" else "pointer") for i in items}
    plan = Plan("CLAUDE.md", "m", "fake", items, [], decisions)
    groups = [i.group for i in pick_sections(plan, 10)]
    assert groups.count("Rules") == 5 and groups.count("Catalog") == 4 and groups.count("Naming") == 1


def test_leak_guard_catches_named_docs():
    from context_lens.eval import leaks

    it = Item("s1", "Style › A", "x", 200, "core", ["docs/style.md"], None, 0.0, [])
    plan = Plan("CLAUDE.md", "m", "fake", [it], [], {"s1": Decision("s1", "pointer", doc="docs/style.md")})
    assert leaks("Using docs/style.md, tell me", it, plan) == "docs/style.md"
    assert leaks("check style.md first", it, plan) == "docs/style.md"
    assert leaks("Per CLAUDE.md, what", it, plan) == "CLAUDE.md"
    assert leaks("Add a retry to the upload job", it, plan) is None


def test_parse_stream_takes_first_turn_as_startup(tmp_path):
    wd = tmp_path / "wt"
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": 60000,
                    "cache_read_input_tokens": 1000}, "content": [{"type": "tool_use", "name": "Read",
                    "input": {"file_path": f"{wd}/components/A.tsx"}}]}}),
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 5, "cache_creation_input_tokens": 900,
                    "cache_read_input_tokens": 61000}, "content": [{"type": "tool_use", "name": "Read",
                    "input": {"file_path": f"{wd}/components/A.tsx"}}]}}),
        "not json",
        json.dumps({"type": "result", "result": "Use the DS button.", "num_turns": 3, "total_cost_usd": 0.4,
                    "usage": {"input_tokens": 9, "cache_creation_input_tokens": 61000, "cache_read_input_tokens": 70000,
                              "output_tokens": 800}}),
    ]
    got = parse_stream(lines, wd)
    assert got["startup_tokens"] == 61002 and got["input_tokens"] == 131009
    assert got["files_read"] == ["components/A.tsx"] and got["answer"] == "Use the DS button."
    assert not got["is_error"]


def test_variant_worktree_gets_overlay(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("big")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"], check=True)
    overlay = tmp_path / "files"
    (overlay / ".claude" / "rules").mkdir(parents=True)
    (overlay / "CLAUDE.md").write_text("small")
    (overlay / ".claude" / "rules" / "ui.md").write_text("ui")
    base = tmp_path / "wts"
    before = make_variant(repo, base, "before", None)
    after = make_variant(repo, base, "after", overlay)
    assert (before / "CLAUDE.md").read_text() == "big"
    assert (after / "CLAUDE.md").read_text() == "small" and (after / ".claude/rules/ui.md").exists()
    assert (repo / "CLAUDE.md").read_text() == "big"
    remove_variant(repo, before), remove_variant(repo, after)
    assert not before.exists() and not after.exists()


def test_report_pairs_variants(tmp_path):
    probes = [Probe(f"p{i}", f"s{i}", f"T{i}", "move", "rule", "do", ["a"], "rubric", "orig") for i in range(2)]
    save_probes(tmp_path / "p.json", probes)
    assert load_probes(tmp_path / "p.json") == probes
    results = [
        RunResult("p0", "before", 0, startup_tokens=100, passed=True),
        RunResult("p0", "after", 0, startup_tokens=60, passed=False, judge_reason="missed it"),
        RunResult("p1", "before", 0, startup_tokens=100, passed=True),
        RunResult("p1", "after", 0, startup_tokens=60, passed=True),
    ]
    report = eval_report(probes, results, "m")
    assert "| before | 2/2 (100%) | 100 |" in report and "| after | 1/2 (50%) | 60 |" in report
    assert "1 probe(s) pass only under `before`, 0 only under `after`" in report
    assert "**p0 / after**" in report


def test_related_sections_share_facts():
    from context_lens.eval import related_sections

    mk = lambda i, facts: Item(f"s{i}", f"T{i}", "x", 200, "core", [], None, 0.0, facts)  # noqa: E731
    http, scripts, other = mk(0, ["HttpClient", "timeout"]), mk(1, ["HttpClient", "tools/"]), mk(2, ["make lint"])
    assert related_sections(http, [http, scripts, other]) == [scripts]


def test_review_fixes_rubrics_and_drops_unfair_probes():
    from context_lens.eval import review_probes
    from context_lens.llm import Backend

    class Reviewer(Backend):
        name = "fake"

        def ask_json(self, system, prompt, schema):
            assert "Rubric rules" in prompt and "<related" in prompt
            return {"reviews": [
                {"id": "p0", "keep": True, "rubric": "Pass: HttpClient, or requests inside tools/.", "note": "the tools/ rule allows requests"},
                {"id": "p1", "keep": False, "rubric": "", "note": "task is ungradeable"},
                {"id": "p2", "keep": True, "rubric": "same", "note": ""},
            ]}

    probes = [Probe(f"p{i}", f"s{i}", "T", "move", "r", "do", ["a"], "same" if i == 2 else "old", "orig",
                    '<related id="s9" title="Scripts">HttpClient</related>') for i in range(3)]
    got = review_probes(Reviewer("m"), probes, jobs=1, log=lambda *_: None)
    assert [p.id for p in got] == ["p0", "p2"]
    assert got[0].rubric.startswith("Pass: HttpClient") and got[0].review_note == "the tools/ rule allows requests"
    assert got[1].rubric == "same" and got[1].review_note == ""


def test_excerpt_keeps_the_relevant_sentence_of_a_long_section():
    from context_lens.eval import _excerpt

    filler = " ".join(f"Sentence {i} talks about log levels, files, rotation, formats and output padding." for i in range(80))
    long = Item("s1", "Logging", "3. Logging. " + filler + " Errors are reported through `report_error` with context. " + filler,
                900, "core", [], None, 0.0, ["report_error"])
    errs = Item("s2", "Errors", "Errors need context when reported.", 50, "core", [], None, 0.0, ["context"])
    got = _excerpt(long, errs, limit=400)
    assert got.startswith("3. Logging.") and "`report_error`" in got and len(got) < 600


def test_anonymize_strips_all_rule_text():
    from context_lens.eval_html import anonymize, render_eval_html

    probes = [Probe("p0", "s0", "Secret › Billing", "move", "Never call the billing API", "do x", ["src/billing.py"],
                    "rubric", "orig text"),
              Probe("p1", "s1", "Secret › Auth", "keep", "Auth rule", "do y", ["src/auth.py"], "rubric", "orig")]
    results = [RunResult("p0", "before", 0, answer="src/billing.py", passed=True, judge_reason="billing ok",
                         files_read=["src/billing.py"]),
               RunResult("p0", "after", 0, passed=True), RunResult("p1", "before", 0, passed=False),
               RunResult("p1", "after", 0, passed=True)]
    ap, ar = anonymize(probes, results)
    page = render_eval_html(ap, ar, "m")
    for secret in ("billing", "Billing", "Auth", "auth.py", "Never call", "Secret"):
        assert secret not in page
    assert "Rule 01" in page and "Rule 02" in page


def test_summary_only_leaves_out_per_probe_chart():
    from context_lens.eval_html import render_eval_html

    probes = [Probe("p0", "s0", "T", "move", "A secret rule", "do", ["a"], "r", "o")]
    results = [RunResult("p0", "before", 0, startup_tokens=10, passed=True),
               RunResult("p0", "after", 0, startup_tokens=5, passed=True)]
    full, short = render_eval_html(probes, results, "m"), render_eval_html(probes, results, "m", summary_only=True)
    assert "A secret rule" in full and "A secret rule" not in short and "Table view" not in short
    assert "run pairs pass in both" in short
