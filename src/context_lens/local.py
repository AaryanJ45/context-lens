"""Use a compacted CLAUDE.md in one checkout without changing what is committed.

`compact --apply` rewrites the tracked CLAUDE.md, which every teammate then
pulls. Local mode leaves the tracked file exactly as it is, on disk and in git,
and switches this checkout over with files git never sees:

  * ``CLAUDE.local.md`` next to the original holds the slim root text
    (Claude Code loads it at startup like CLAUDE.md, but it is personal).
  * ``.claude/rules/context-lens-local/`` holds the scoped rule files, in their
    own folder so they never collide with rule files the repo already tracks.
  * ``.claude/settings.local.json`` gets the original's absolute path in
    ``claudeMdExcludes``, so Claude Code stops loading the long version.
  * ``.git/info/exclude`` lists all of the above, so nothing shows in
    ``git status`` and nothing can be committed by accident.

A state file records the original's sha256. When a pull changes the original,
the slim copy is stale (it lacks whatever was added); ``status`` says so and the
plugin's SessionStart hook tells the session. ``off`` removes exactly what
``on`` wrote and the checkout is back to the committed CLAUDE.md.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

LOCAL_RULES_DIR = ".claude/rules/context-lens-local"
STATE_FILE = ".claude/context-lens-local.json"
SETTINGS_FILE = ".claude/settings.local.json"
EXCLUDE_HEADER = "# context-lens local mode (remove with `context-lens doctor --undo-local`)"
MARKER = "<!-- context-lens local mode: generated from {src} (sha256 {sha}). Do not commit. -->"


class LocalModeError(Exception):
    pass


@dataclass
class Status:
    active: bool
    original: Path | None = None
    stale: bool = False
    files: tuple[str, ...] = ()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remap(outputs: dict[str, str], target_rel: str) -> dict[str, str]:
    """Move the compact outputs to their local-only paths, rewriting references to rule files."""
    local_root = (Path(target_rel).parent / "CLAUDE.local.md").as_posix()
    moved = {rel: f"{LOCAL_RULES_DIR}/{Path(rel).name}" for rel in outputs if rel != target_rel}
    out: dict[str, str] = {}
    for rel, text in outputs.items():
        for old, new in moved.items():
            text = text.replace(f"`{old}`", f"`{new}`")
        out[moved.get(rel, local_root)] = text
    return out


def _git_exclude(repo: Path) -> Path:
    try:
        raw = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LocalModeError(f"{repo} is not a git checkout") from exc
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _tracked(repo: Path, rel: str) -> bool:
    res = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", rel], capture_output=True)
    return res.returncode == 0


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LocalModeError(f"{path} is not valid JSON; fix it first") from exc
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _set_exclude(repo: Path, patterns: list[str]) -> None:
    """Replace our block in .git/info/exclude (an empty list removes it)."""
    path = _git_exclude(repo)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if EXCLUDE_HEADER in lines:
        start = lines.index(EXCLUDE_HEADER)
        end = start + 1
        while end < len(lines) and lines[end].strip():
            end += 1
        del lines[start:end]
        while lines and not lines[-1].strip():
            lines.pop()
    if patterns:
        lines += (["", EXCLUDE_HEADER] if lines else [EXCLUDE_HEADER]) + [f"/{p}" for p in patterns]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def _set_excludes_setting(repo: Path, original: Path, add: bool) -> None:
    path = repo / SETTINGS_FILE
    data = _read_json(path)
    current = [p for p in data.get("claudeMdExcludes", []) if p != str(original)]
    if add:
        current.append(str(original))
    if current:
        data["claudeMdExcludes"] = current
    else:
        data.pop("claudeMdExcludes", None)
    if data:
        _write_json(path, data)
    elif path.is_file():
        path.unlink()  # held nothing but our entry


def enable(repo: Path, target: Path, outputs: dict[str, str]) -> list[Path]:
    """Write the compact outputs as local-only files and switch Claude Code over to them."""
    repo, target = repo.resolve(), target.resolve()
    target_rel = target.relative_to(repo).as_posix()
    if not _tracked(repo, target_rel):
        raise LocalModeError(f"{target_rel} is not tracked by git; local mode keeps a tracked file untouched")
    if status(repo).active:
        disable(repo)
    files = remap(outputs, target_rel)
    root_rel = next(rel for rel in files if rel.endswith("CLAUDE.local.md"))
    root = repo / root_rel
    if root.is_file() and not root.read_text(encoding="utf-8").startswith("<!-- context-lens local mode"):
        raise LocalModeError(f"{root_rel} already exists and is yours; move it aside first")
    for rel in files:
        if _tracked(repo, rel):
            raise LocalModeError(f"{rel} is tracked by git; refusing to overwrite it")
    sha = sha256(target)
    files[root_rel] = MARKER.format(src=target_rel, sha=sha) + "\n\n" + files[root_rel]
    written = []
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    _set_excludes_setting(repo, target, add=True)
    _write_json(repo / STATE_FILE, {"original": target_rel, "sha256": sha, "files": sorted(files)})
    _set_exclude(repo, [root_rel, f"{LOCAL_RULES_DIR}/", SETTINGS_FILE, STATE_FILE])
    return written


def status(repo: Path) -> Status:
    repo = repo.resolve()
    state = _read_json(repo / STATE_FILE)
    if not state.get("original"):
        return Status(active=False)
    original = repo / state["original"]
    stale = not original.is_file() or sha256(original) != state.get("sha256")
    return Status(True, original, stale, tuple(state.get("files", [])))


def disable(repo: Path) -> list[Path]:
    """Remove every file local mode wrote; the committed CLAUDE.md loads again."""
    repo = repo.resolve()
    st = status(repo)
    if not st.active:
        return []
    removed = []
    for rel in st.files:
        path = repo / rel
        if path.is_file() and not _tracked(repo, rel):
            path.unlink()
            removed.append(path)
    rules = repo / LOCAL_RULES_DIR
    if rules.is_dir() and not any(rules.iterdir()):
        rules.rmdir()
    _set_excludes_setting(repo, st.original, add=False)
    (repo / STATE_FILE).unlink()
    _set_exclude(repo, [])
    return removed
