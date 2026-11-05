import json
from pathlib import Path

from context_lens.discovery import ON_DEMAND, STARTUP, discover, encode_project_dir
from context_lens.pricing import price, static_context_cost
from context_lens.profile import build_profile
from context_lens.sections import split_sections
from context_lens.transcripts import read_session


def test_sections_follow_headings_and_split_big_lists():
    rules = "\n".join(f"{i}. Rule {i} " + "x" * 400 for i in range(1, 21))
    text = f"# Guide\nintro\n## Small\nshort\n## Rules\n{rules}\n## Tail\nend\n"
    titles = [s.title for s in split_sections(text)]
    assert titles[:2] == ["Guide", "Guide › Small"]
    assert "Guide › Rules › 1. Rule 1 " + "x" * 50 in [t[: len("Guide › Rules › 1. Rule 1 ") + 50] for t in titles]
    assert sum(t.startswith("Guide › Rules › ") for t in titles) == 20
    assert titles[-1] == "Guide › Tail"


def test_headings_inside_code_fences_are_ignored():
    text = "# A\n```\n# not a heading\n```\n# B\n"
    assert [s.title for s in split_sections(text)] == ["A", "B"]


def test_discovery_finds_hierarchy_imports_and_nested(tmp_path: Path):
    home, repo = tmp_path / "home", tmp_path / "home" / "work" / "repo"
    (home / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (repo / "backend").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("user rules")
    (home / ".claude" / "skills" / "deploy" / "SKILL.md").write_text("---\nname: deploy\ndescription: Ship it\n---\nbody")
    (repo / "CLAUDE.md").write_text("see @docs/style.md and `@not/an/import.md`")
    (repo / "docs").mkdir()
    (repo / "docs" / "style.md").write_text("style guide")
    (repo / "backend" / "CLAUDE.md").write_text("backend rules")
    memory = home / ".claude" / "projects" / encode_project_dir(repo.resolve()) / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- a memory")

    found = {(f.kind, f.loaded): f for f in discover(repo, home)}
    assert ("user", STARTUP) in found
    assert ("project", STARTUP) in found
    assert found[("import", STARTUP)].path.name == "style.md"
    assert ("auto-memory", STARTUP) in found
    assert "deploy: Ship it" in found[("skill-listing", STARTUP)].text
    assert found[("nested", ON_DEMAND)].scope == ["backend/**"]


def _write_transcript(path: Path, cwd: str) -> None:
    def rec(i, usage, sidechain=False):
        return {
            "type": "assistant", "isSidechain": sidechain, "sessionId": "s1", "cwd": cwd,
            "timestamp": f"2026-11-0{i}T10:00:00Z",
            "message": {"id": f"m{i}", "model": "claude-opus-5-5", "usage": usage},
        }

    lines = [
        {"type": "user", "cwd": cwd},
        rec(1, {"input_tokens": 2, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 500,
                "output_tokens": 10, "cache_creation": {"ephemeral_1h_input_tokens": 1000}}),
        rec(1, {"input_tokens": 2, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 500,
                "output_tokens": 10}),  # same message id: counted once
        rec(2, {"input_tokens": 1, "cache_read_input_tokens": 1500, "output_tokens": 5}),
        rec(3, {"input_tokens": 99, "output_tokens": 99}, sidechain=True),
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines))


def test_transcript_startup_and_turns(tmp_path: Path):
    f = tmp_path / "s1.jsonl"
    _write_transcript(f, "/repo")
    s = read_session(f)
    assert s.startup_tokens == 1502
    assert s.turns == 2
    assert s.cache_write_1h == 1000 and s.cache_read == 2000
    expected = (3 * 4.00 + 1000 * 4.00 * 2 + 2000 * 0.20 + 15 * 20.00) / 1e6
    assert abs(s.cost_usd - expected) < 1e-12


def test_pricing_longest_prefix_and_static_cost():
    assert price("claude-opus-5-5")[0] == 4.00
    assert price("claude-opus-5")[0] == 5.00
    assert price("gpt-whatever") is None
    assert static_context_cost("claude-opus-5-5", 1_000_000, 1) == 8.00
    assert abs(static_context_cost("claude-opus-5-5", 1_000_000, 11) - 10.00) < 1e-9


def test_profile_end_to_end(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    flags = "\n".join(f"- flag_{i} does a thing for rollout number {i} with careful notes" for i in range(40))
    (repo / "docs" / "flags.md").write_text(flags)
    (repo / "CLAUDE.md").write_text(
        "# Rules\n## Flags\nSee `docs/flags.md`.\n" + flags + "\n## History\n~~old rule that was wrong~~ " * 3
    )
    projects = tmp_path / "projects" / "x"
    projects.mkdir(parents=True)
    _write_transcript(projects / "s1.jsonl", str(repo.resolve()))
    prof = build_profile(repo, days=10_000, token_mode="estimate", home=tmp_path / "home", projects_dir=tmp_path / "projects")
    hints = {s.section.title: s.hint for _, s in prof.all_sections()}
    assert hints["Rules › Flags"] == "reference"
    assert hints["Rules › History"] == "history"
    assert prof.measured_startup == 1502
