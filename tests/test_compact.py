import json
import subprocess
from pathlib import Path

from context_lens.compact import Plan, assemble, batches, build_plan, coverage, glob_matches, summarize
from context_lens.facts import facts_in
from context_lens.llm import Backend
from context_lens.tokens import TokenCounter

CLAUDE_MD = """# Project

## Always
Run `make check` before you finish.

## Frontend
Use `Button` from `@acme/ui` in `components/`. ~~Old: use `LegacyButton`.~~

## Flags
- `FLAG_A` (`flag-a`) gates search. See `docs/flags.md`.

## History
~~Everything here was removed.~~
"""


class FakeBackend(Backend):
    """Answers layout and decision calls by section title, like a scripted model."""

    name = "fake"

    def __init__(self, lose_fact: bool = False):
        super().__init__("fake-model")
        self.lose_fact = lose_fact
        self.prompts: list[str] = []

    def ask_json(self, system, prompt, schema):
        self.prompts.append(prompt)
        if "files" in schema["properties"]:
            return {"files": [{"path": ".claude/rules/frontend.md", "paths": ["components/**"], "purpose": "UI work"}]}
        out = []
        for line in prompt.splitlines():
            if not line.startswith("<section id="):
                continue
            sid = line.split('"')[1]
            title = prompt.split(line, 1)[1].splitlines()[1]
            if "Frontend" in title:
                text = "## Frontend\nUse `Button` in `components/`." if self.lose_fact and "previous attempt" not in prompt \
                    else "## Frontend\nUse `Button` from `@acme/ui` in `components/`."
                out.append({"id": sid, "action": "move", "dest": ".claude/rules/frontend.md", "text": text, "reason": "UI only"})
            elif "Flags" in title:
                out.append({"id": sid, "action": "pointer", "doc": "docs/flags.md", "text": "Flags: read `docs/flags.md` first.", "reason": "doc"})
            elif "History" in title:
                out.append({"id": sid, "action": "drop", "reason": "struck"})
            else:
                out.append({"id": sid, "action": "keep", "reason": "global"})
        return {"decisions": out}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "components").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "components" / "A.tsx").write_text("x")
    (repo / "docs" / "flags.md").write_text("`FLAG_A` (`flag-a`) gates search.")
    (repo / "CLAUDE.md").write_text(CLAUDE_MD)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    return repo


def test_facts_skip_struck_text_and_fences():
    text = "Use `alpha` and `beta`. ~~Was `gamma`.~~\n```\n`delta`\n```\n`alpha` again, `x`."
    assert facts_in(text) == ["alpha", "beta"]
    assert facts_in("never a `` `$${x}` `` template, or `y()`") == ["`$${x}`", "y()"]


def test_glob_matching(tmp_path):
    repo = _repo(tmp_path)
    assert glob_matches(repo, ["components/**"]) == 1
    assert glob_matches(repo, ["**/*.md"]) == 2
    assert glob_matches(repo, ["backend/**"]) == 0


def test_compact_moves_points_and_drops_without_losing_facts(tmp_path):
    repo = _repo(tmp_path)
    counter = TokenCounter(mode="estimate")
    plan = build_plan(FakeBackend(), repo, repo / "CLAUDE.md", counter, jobs=1, log=lambda *_: None)
    out = assemble(plan)
    root, rules = out["CLAUDE.md"], out[".claude/rules/frontend.md"]
    assert "`make check`" in root and "Flags: read `docs/flags.md` first." in root
    assert "Button" not in root and "removed" not in root
    assert rules.startswith('---\npaths:\n  - "components/**"\n---\n') and "`@acme/ui`" in rules
    assert "`.claude/rules/frontend.md`" in root  # the scoped-rules index
    lost, _ = coverage(plan, out, repo)
    assert lost == []
    report = summarize(plan, out, counter, repo)
    assert "0 lost without a reason" in report


def test_lost_facts_trigger_one_repair_pass(tmp_path):
    repo = _repo(tmp_path)
    fake = FakeBackend(lose_fact=True)
    plan = build_plan(fake, repo, repo / "CLAUDE.md", TokenCounter(mode="estimate"), jobs=1, log=lambda *_: None)
    assert any("A previous attempt lost these facts" in p and "`@acme/ui`" in p for p in fake.prompts)
    assert coverage(plan, assemble(plan), repo)[0] == []


def test_invalid_decisions_fall_back_to_keep(tmp_path):
    repo = _repo(tmp_path)

    class Bad(FakeBackend):
        def ask_json(self, system, prompt, schema):
            data = super().ask_json(system, prompt, schema)
            for d in data.get("decisions", []):
                if d["action"] == "move":
                    d["dest"] = ".claude/rules/nope.md"
                if d["action"] == "pointer":
                    d["doc"] = "docs/missing.md"
            return data

    plan = build_plan(Bad(), repo, repo / "CLAUDE.md", TokenCounter(mode="estimate"), jobs=1, log=lambda *_: None)
    kinds = {plan.decisions[i.id].action for i in plan.items if "Frontend" in i.title or "Flags" in i.title}
    assert kinds == {"keep"}


def test_plan_round_trips_through_json(tmp_path):
    repo = _repo(tmp_path)
    plan = build_plan(FakeBackend(), repo, repo / "CLAUDE.md", TokenCounter(mode="estimate"), jobs=1, log=lambda *_: None)
    again = Plan.from_json(plan.to_json())
    assert assemble(again) == assemble(plan)
    assert json.loads(plan.to_json())["layout"][0]["matches"] == 1


def test_batches_keep_a_heading_together():
    from context_lens.compact import Item

    mk = lambda i, title, tok: Item(f"s{i}", title, "", tok, "core", [], None, 0.0, [])
    items = [mk(0, "A", 7000)] + [mk(i, f"B › {i}", 900) for i in range(1, 6)] + [mk(9, "C", 100)]
    got = [[i.id for i in b] for b in batches(items)]
    assert got[0] == ["s0"] and got[1] == ["s1", "s2", "s3", "s4", "s5", "s9"]
