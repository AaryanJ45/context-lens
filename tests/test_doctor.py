import json
from pathlib import Path

from context_lens.activity import read_activity
from context_lens.doctor import FIX, LOOK, run_doctor
from context_lens.doctor_html import render_doctor_html
from context_lens.llm import Backend
from context_lens.redact import redact
from context_lens.rules import Rule, build_payload, judge, select_checkable


def _session(path: Path, cwd: str, steps: list[tuple[str, dict, str, bool]], compact: bool = False) -> None:
    """steps: (tool name, input, result text, is_error)."""
    recs: list[dict] = [{"type": "user", "cwd": cwd, "sessionId": path.stem, "timestamp": "2026-11-01T10:00:00Z",
                         "message": {"role": "user", "content": "please fix the build"}},
                        {"type": "user", "cwd": cwd, "message": {"role": "user",
                                                                  "content": "<command-name>/ship</command-name>"}}]
    for i, (name, inp, result, err) in enumerate(steps):
        recs.append({"type": "assistant", "cwd": cwd, "sessionId": path.stem, "timestamp": "2026-11-01T10:00:00Z",
                     "message": {"id": f"m{i}", "model": "claude-opus-5-5",
                                 "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000 + 500 * i, "output_tokens": 5},
                                 "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})
        recs.append({"type": "user", "cwd": cwd, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": result, "is_error": err}]}})
        if compact and i == 1:
            recs.append({"type": "system", "subtype": "compact_boundary"})
    path.write_text("\n".join(json.dumps(r) for r in recs))


def test_activity_counts_failures_loops_large_outputs_and_rereads(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    big = "x" * 40_000
    _session(f, "/repo", [
        ("Read", {"file_path": "/repo/a.py"}, "small", False),
        ("Read", {"file_path": "/repo/a.py"}, "y" * 3_400, False),   # unchanged: a re-read
        ("Edit", {"file_path": "/repo/a.py"}, "ok", False),
        ("Read", {"file_path": "/repo/a.py"}, "z", False),            # changed since: fine
        ("Bash", {"command": "npm test"}, "boom", True),
        ("Bash", {"command": "npm test"}, "boom", True),              # same failing call again
        ("Bash", {"command": "cat huge.log"}, big, False),
    ], compact=True)
    a = read_activity(f)
    assert len(a.calls) == 7 and a.errors == 2
    assert a.rereads == 1 and a.reread_tokens == 1_000
    assert a.retry_loops == 1
    assert a.large_results == 1
    assert a.compactions == 1
    assert a.commands["ship"] == 1
    assert a.prompts == ["please fix the build"]
    assert a.context[0] == 1010 and a.peak_context == 1010 + 500 * 6
    assert "Bash: npm test" in a.timeline and "  -> Bash failed" in a.timeline


def test_redact_scrubs_secrets_but_keeps_paths():
    text = ("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY "
            "sk-ant-api03-abcdefghijklmnop ghp_abcdefghijklmnopqrstuvwxyz1234 a@b.com "
            "postgres://u:pw@db/x --password hunter22 /home/me/proj "
            "cd ~/Documents/Github/MobileAppUpdated/components/fleet/map/view")
    out = redact(text, home=Path("/home/me"))
    for secret in ("wJalr", "sk-ant-api03", "ghp_", "a@b.com", "u:pw", "hunter22", "/home/me"):
        assert secret not in out
    assert "~/proj" in out
    assert "MobileAppUpdated/components/fleet/map/view" in out


def test_doctor_end_to_end_flags_startup_waste_and_idle_mcp(tmp_path: Path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    (home / ".claude" / "skills" / "ship").mkdir(parents=True)
    (home / ".claude" / "skills" / "ship" / "SKILL.md").write_text("---\nname: ship\ndescription: Ship it\n---\n")
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# Rules\n" + "".join(f"## R{i}\n" + "Always do the thing. " * 300 for i in range(16)))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"linear": {}, "unused-server": {}}}))
    projects = tmp_path / "projects" / "x"
    projects.mkdir(parents=True)
    huge = "x" * 80_000
    _session(projects / "s1.jsonl", str(repo.resolve()), [
        ("mcp__linear__get_issue", {"id": "A-1"}, "ok", False),
        ("Bash", {"command": "cat all.log"}, huge, False),
        ("Bash", {"command": "make"}, "err", True),
    ])
    doc = run_doctor(repo, days=10_000, token_mode="estimate", home=home, projects_dir=tmp_path / "projects")
    titles = {f.title: f for f in doc.findings}
    startup = next(f for t, f in titles.items() if t.startswith("Instruction files load"))
    assert startup.severity == FIX
    assert any(t.startswith("Tool outputs over 5k") for t in titles)
    assert any(t.startswith("33% of tool calls fail") for t in titles)
    idle = next(f for t, f in titles.items() if "MCP server" in t)
    assert idle.severity == LOOK and idle.evidence == ["unused-server"]
    assert doc.mcp_servers == {"linear": 1, "unused-server": 0}
    assert doc.privacy.startswith("Local only")
    html = render_doctor_html(doc)
    assert "<title>Context Lens Doctor</title>" in html and "unused-server" in html


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, answers):
        super().__init__("fake")
        self.answers = list(answers)
        self.prompts: list[str] = []

    def ask_json(self, system, prompt, schema):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_rules_select_judge_and_payload_never_holds_tool_output(tmp_path: Path):
    f = tmp_path / "s.jsonl"
    _session(f, "/repo", [("Bash", {"command": "git commit -m wip TOKEN=abcdef123456"}, "SECRET OUTPUT", False)])
    act = read_activity(f)
    rules = [Rule("R1", "Commits", "CLAUDE.md:1", 100, "Write detailed commits."),
             Rule("R2", "Styling", "CLAUDE.md:9", 90, "Use tokens for colors.")]
    selector = FakeBackend([{"checkable": ["R1"]}])
    assert [r.id for r in select_checkable(selector, rules, 5)] == ["R1"]

    payload = build_payload(act, rules[:1])
    assert "SECRET OUTPUT" not in payload.prompt
    assert "abcdef123456" not in payload.prompt
    assert "git commit" in payload.prompt and "USER: please fix the build" in payload.prompt

    line = payload.lines.index(next(ln for ln in payload.lines if ln.startswith("Bash: git commit"))) + 1
    backend = FakeBackend([
        {"verdicts": [{"rule": "R1", "verdict": "violated", "line": line, "why": "one-word message"}]},
        RuntimeError("judge down"),
    ])
    results, failed = judge(backend, rules[:1], [payload, payload], jobs=1)
    assert failed == 1
    r = results[0]
    assert (r.followed, r.violated, r.not_applicable) == (0, 1, 0)
    assert r.evidence[0].line.startswith("Bash: git commit") and r.adherence == 0.0
