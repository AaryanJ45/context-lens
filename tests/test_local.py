import json
import subprocess
from pathlib import Path

import pytest

from context_lens.discovery import discover
from context_lens.local import LocalModeError, disable, enable, remap, status

ORIGINAL = "# Project\n\nRun `make check`.\n\n## Frontend\nUse `Button` from `@acme/ui`.\n"
OUTPUTS = {
    "CLAUDE.md": "# Project\n\nRun `make check`.\n\n## Scoped rules\n\n- `.claude/rules/frontend.md`: UI work\n",
    ".claude/rules/frontend.md": '---\npaths:\n  - "components/**"\n---\n\nUse `Button` from `@acme/ui`.\n',
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".claude" / "rules").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text(ORIGINAL)
    (repo / ".claude" / "rules" / "frontend.md").write_text("tracked rule, not ours\n")
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


def test_remap_moves_rules_aside_and_rewrites_references():
    out = remap(OUTPUTS, "CLAUDE.md")
    assert set(out) == {"CLAUDE.local.md", ".claude/rules/context-lens-local/frontend.md"}
    assert "`.claude/rules/context-lens-local/frontend.md`" in out["CLAUDE.local.md"]


def test_enable_leaves_git_clean_and_the_original_untouched(tmp_path):
    repo = _repo(tmp_path)
    enable(repo, repo / "CLAUDE.md", OUTPUTS)

    assert (repo / "CLAUDE.md").read_text() == ORIGINAL
    assert (repo / ".claude/rules/frontend.md").read_text() == "tracked rule, not ours\n"
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    settings = json.loads((repo / ".claude/settings.local.json").read_text())
    assert settings["claudeMdExcludes"] == [str((repo / "CLAUDE.md").resolve())]
    assert "context-lens local mode" in (repo / "CLAUDE.local.md").read_text()

    st = status(repo)
    assert st.active and not st.stale


def test_profile_sees_the_slim_version(tmp_path):
    repo = _repo(tmp_path)
    enable(repo, repo / "CLAUDE.md", OUTPUTS)
    paths = {f.path for f in discover(repo, home=tmp_path / "home")}
    assert (repo / "CLAUDE.md").resolve() not in paths
    assert (repo / "CLAUDE.local.md").resolve() in paths


def test_a_pull_that_changes_the_original_marks_it_stale(tmp_path):
    repo = _repo(tmp_path)
    enable(repo, repo / "CLAUDE.md", OUTPUTS)
    (repo / "CLAUDE.md").write_text(ORIGINAL + "\nNew rule.\n")
    assert status(repo).stale


def test_disable_restores_the_checkout(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".claude/settings.local.json").write_text('{"permissions": {"allow": ["Bash(ls)"]}}\n')
    exclude = repo / ".git/info/exclude"
    before_exclude = exclude.read_text()
    enable(repo, repo / "CLAUDE.md", OUTPUTS)
    disable(repo)

    assert not (repo / "CLAUDE.local.md").exists()
    assert not (repo / ".claude/rules/context-lens-local").exists()
    assert json.loads((repo / ".claude/settings.local.json").read_text()) == {"permissions": {"allow": ["Bash(ls)"]}}
    assert exclude.read_text() == before_exclude
    assert not status(repo).active


def test_enable_twice_replaces_the_first(tmp_path):
    repo = _repo(tmp_path)
    enable(repo, repo / "CLAUDE.md", OUTPUTS)
    enable(repo, repo / "CLAUDE.md", OUTPUTS)
    settings = json.loads((repo / ".claude/settings.local.json").read_text())
    assert len(settings["claudeMdExcludes"]) == 1
    assert (repo / ".git/info/exclude").read_text().count("context-lens local mode") == 1


def test_refuses_to_overwrite_a_personal_claude_local_md(tmp_path):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.local.md").write_text("my notes\n")
    with pytest.raises(LocalModeError):
        enable(repo, repo / "CLAUDE.md", OUTPUTS)
    assert (repo / "CLAUDE.local.md").read_text() == "my notes\n"
