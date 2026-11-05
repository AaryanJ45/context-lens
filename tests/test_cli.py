import webbrowser
from pathlib import Path

import pytest

from context_lens import cli
from context_lens.doctor import FIX, check_local


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# Rules\n## Git\nCommit often.\n")
    return repo


@pytest.fixture
def opened(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: urls.append(url) or True)
    return urls


def test_bare_command_runs_the_checkup_and_points_at_the_report(tmp_path, capsys, opened):
    repo = _repo(tmp_path)
    assert cli.main([str(repo), "--tokens", "estimate", "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "context-lens doctor" in out
    assert (repo / ".context-lens" / "doctor.html").is_file()
    assert "See the full report in your browser: file://" in out
    assert opened == []  # captured stdout is not a terminal: link only


def test_open_flag_opens_the_report(tmp_path, capsys, opened):
    repo = _repo(tmp_path)
    assert cli.main(["doctor", str(repo), "--tokens", "estimate", "--days", "1", "--open"]) == 0
    assert opened == [(repo / ".context-lens" / "doctor.html").resolve().as_uri()]
    assert "Opened the report in your browser" in capsys.readouterr().out


def test_conflicting_actions_are_refused(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert cli.main([str(repo), "--compact", "--verify", "quick"]) == 2
    assert "pick one of --compact, --verify" in capsys.readouterr().err


def test_apply_and_verify_need_a_proposal_first(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert cli.main([str(repo), "--apply-local"]) == 2
    assert "run `context-lens doctor --compact` first" in capsys.readouterr().err
    assert cli.main([str(repo), "--verify", "quick"]) == 2
    assert "run `context-lens doctor --compact` first" in capsys.readouterr().err


def test_local_status_when_off(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert cli.main([str(repo), "--local-status"]) == 0
    assert "local mode is off" in capsys.readouterr().out


def test_stale_local_copy_is_a_fix(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "context-lens-local.json").write_text(
        '{"original": "CLAUDE.md", "sha256": "0000", "files": ["CLAUDE.local.md"]}')
    metrics: dict[str, str] = {}
    [finding] = check_local(repo, metrics)
    assert finding.severity == FIX and "--compact --apply-local" in finding.fix
    assert metrics["local slim copy"] == "stale"


def test_add_rules_needs_learn_first_then_writes(tmp_path, capsys):
    from context_lens.learn import Lesson, save

    repo = _repo(tmp_path)
    assert cli.main([str(repo), "--add-rules", "1"]) == 2
    assert "run `context-lens doctor --learn` first" in capsys.readouterr().err
    save([Lesson("Pnpm", "Use pnpm, not npm.", "repo")], repo / ".context-lens" / "learn" / "lessons.json")
    assert cli.main([str(repo), "--add-rules", "1"]) == 0
    assert "- Use pnpm, not npm." in (repo / "CLAUDE.md").read_text()
    assert cli.main([str(repo), "--add-rules", "7"]) == 2


def _corrected_repo(tmp_path: Path) -> tuple[Path, Path]:
    import json

    repo = _repo(tmp_path)
    projects = tmp_path / "projects" / "x"
    projects.mkdir(parents=True)
    recs = [{"type": "user", "cwd": str(repo.resolve()), "sessionId": "s", "timestamp": "2026-11-01T10:00:00Z",
             "message": {"content": "start"}}]
    for i in range(6):
        recs += [{"type": "assistant", "cwd": str(repo.resolve()), "timestamp": "2026-11-01T10:00:00Z",
                  "message": {"id": f"m{i}", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash",
                                                          "input": {"command": "npx jest"}}]}},
                 {"type": "user", "message": {"content": "no, don't run jest locally"}}]
    (projects / "s.jsonl").write_text("\n".join(json.dumps(r) for r in recs))
    return repo, tmp_path / "projects"


@pytest.mark.parametrize("answer, learned", [("y", True), ("", False)])
def test_terminal_run_offers_learn(tmp_path, monkeypatch, capsys, opened, answer, learned):
    repo, projects = _corrected_repo(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    calls = []
    monkeypatch.setattr(cli, "_learn", lambda doc, out, args: calls.append(1))
    from context_lens import doctor

    real = doctor.run_doctor
    monkeypatch.setattr(doctor, "run_doctor", lambda repo, **kw: real(repo, projects_dir=projects, home=tmp_path, **kw))
    assert cli.main([str(repo), "--tokens", "estimate", "--days", "10000", "--no-open"]) == 0
    assert "You corrected Claude 6 times in 1 sessions." in capsys.readouterr().out
    assert bool(calls) is learned


def test_no_question_when_not_a_terminal_or_no_input(tmp_path, monkeypatch, capsys, opened):
    repo, projects = _corrected_repo(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked a question"))
    from context_lens import doctor

    real = doctor.run_doctor
    monkeypatch.setattr(doctor, "run_doctor", lambda repo, **kw: real(repo, projects_dir=projects, home=tmp_path, **kw))
    assert cli.main([str(repo), "--tokens", "estimate", "--days", "10000", "--no-open"]) == 0
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    assert cli.main([str(repo), "--tokens", "estimate", "--days", "10000", "--no-open", "--no-input"]) == 0
    assert "Learn rules from them?" not in capsys.readouterr().out
