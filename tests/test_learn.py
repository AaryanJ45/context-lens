import json
from pathlib import Path

from context_lens.activity import read_activity
from context_lens.learn import LEARNED_HEADING, Lesson, add_rules, build_prompt, gather, learn
from tests.test_doctor import FakeBackend


def _write(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records))


def _assistant(i: int, name: str, inp: dict) -> dict:
    return {"type": "assistant", "sessionId": "s", "timestamp": f"2026-11-0{1 + i % 8}T10:00:00Z",
            "message": {"id": f"m{i}", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}}


def _result(i: int, text: str, error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": text, "is_error": error}]}}


def _prompt(text: str) -> dict:
    return {"type": "user", "timestamp": "2026-11-02T10:00:00Z", "message": {"content": text}}


REJECT = ("The user doesn't want to proceed with this tool use. The tool use was rejected. "
          "To tell you how to proceed, the user said: {}")


def test_corrections_rejections_interruptions_and_pushback(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    _write(f, [
        _prompt("fix the tests"),
        _assistant(1, "Bash", {"command": "npx jest"}),
        _result(1, REJECT.format("don't run jest locally, CI does it"), error=True),
        _assistant(2, "AskUserQuestion", {"q": "which?"}),
        _result(2, REJECT.format("The user wants to clarify these questions."), error=True),
        _assistant(3, "Bash", {"command": "npm run lint"}),
        _result(3, "ok"),
        _prompt("[Request interrupted by user for tool use]"),
        _prompt("no lint either, CI runs it"),
        _assistant(4, "Edit", {"file_path": "/r/a.py"}),
        _result(4, "ok"),
        _prompt("[Request interrupted by user]"),
        _prompt("continue"),
        _prompt("No, I told you to use pnpm"),
        _prompt("great, now add a chart"),
    ])
    act = read_activity(f)
    kinds = [(c.kind, c.said) for c in act.corrections]
    assert kinds == [
        ("rejected", "don't run jest locally, CI does it"),
        ("interrupted", "no lint either, CI runs it"),
        ("interrupted", "continue"),
        ("pushback", "No, I told you to use pnpm"),
    ]
    assert act.corrections[0].before[-1] == "Bash: npx jest"
    # A rejection is the user's decision, not a failed call.
    assert act.errors == 0 and sum(c.rejected for c in act.calls) == 2
    # "continue" after an interruption is a nudge, not a correction.
    assert [c.said for c in gather([act])] and "continue" not in [c.said for c in gather([act])]


def test_prompt_labels_and_redacts(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    _write(f, [_prompt("deploy"), _assistant(1, "Bash", {"command": "export API_TOKEN=abcdef123456 && deploy"}),
               _result(1, REJECT.format("never deploy from my laptop, mail me at a@b.com"), error=True)])
    prompt, ids = build_prompt(gather([read_activity(f)]), {"R1": "Deploys: use the pipeline"})
    assert list(ids) == ["C1"]
    assert "abcdef123456" not in prompt and "a@b.com" not in prompt
    assert "developer earlier: deploy" in prompt and "agent: Bash: export API_TOKEN=<redacted>" in prompt
    assert "R1: Deploys: use the pipeline" in prompt


def test_learn_maps_ids_and_labels(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    _write(f, [_prompt("go"), _assistant(1, "Bash", {"command": "npx jest"}),
               _result(1, REJECT.format("no jest locally"), error=True),
               _assistant(2, "Bash", {"command": "npx jest a"}),
               _result(2, REJECT.format("I said no jest"), error=True)])
    corrections = gather([read_activity(f)])
    backend = FakeBackend([{"lessons": [
        {"title": "No local jest", "rule": "Never run jest locally.", "corrections": ["C1", "C2", "C99"],
         "covered_by": "", "conflicts_with": "R1", "scope": "personal"},
        {"title": "Empty", "rule": "x", "corrections": ["C99"], "covered_by": "", "conflicts_with": "", "scope": "repo"},
    ]}])
    [lesson] = learn(backend, corrections, {"R1": "Testing: run jest once before saying tests pass"})
    assert lesson.count == 2 and lesson.scope == "personal"
    assert lesson.conflicts_with == "R1 (Testing)" and lesson.covered_by == ""


def test_add_rules_goes_into_its_own_section_once(tmp_path: Path):
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text(f"# Rules\n\n{LEARNED_HEADING}\n\n- old one\n\n## Later\nkeep me last\n")
    lessons = [Lesson("Pnpm", "Use pnpm, not npm.", "repo"), Lesson("Jest", "Never run jest locally.", "personal")]
    written = add_rules(lessons, [1, 2], repo, home=home)
    assert set(written) == {repo / "CLAUDE.md", home / ".claude" / "CLAUDE.md"}
    text = (repo / "CLAUDE.md").read_text()
    assert text.index("- Use pnpm, not npm.") < text.index("## Later") and text.rstrip().endswith("keep me last")
    assert LEARNED_HEADING in (home / ".claude" / "CLAUDE.md").read_text()
    assert add_rules(lessons, [1], repo, home=home) == {}  # already there
    add_rules(lessons, [1], repo, to="local", home=home)
    assert "- Use pnpm, not npm." in (repo / "CLAUDE.local.md").read_text()
