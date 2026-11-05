"""Find the instruction files Claude Code puts in context for a working directory.

Mirrors Claude Code's memory hierarchy:
  * user memory          ~/.claude/CLAUDE.md
  * project memory       CLAUDE.md / .claude/CLAUDE.md in the cwd and every ancestor
  * local memory         CLAUDE.local.md in the same places
  * imports              `@path` references inside any of the above (max depth 5)
  * rules                .claude/rules/**/*.md (startup unless scoped with `paths:`)
  * auto memory          ~/.claude/projects/<encoded cwd>/memory/MEMORY.md
  * skill listing        name + description of every skill (bodies load on demand)
  * nested CLAUDE.md     CLAUDE.md files below the cwd, loaded on demand
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

STARTUP = "startup"
ON_DEMAND = "on-demand"

MAX_IMPORT_DEPTH = 5
_IMPORT_RE = re.compile(r"(?<![\w`@/])@((?:~/|\.{1,2}/|/)?[\w.\-][\w.\-/]*)")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


@dataclass
class ContextFile:
    path: Path
    kind: str
    loaded: str
    text: str
    imported_by: Path | None = None
    scope: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text)


def encode_project_dir(path: Path) -> str:
    """Claude Code's folder name for a project under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _strip_code(text: str) -> str:
    """Drop fenced blocks and inline code: Claude Code does not resolve imports there."""
    out, in_fence = [], False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(_INLINE_CODE_RE.sub("", line))
    return "\n".join(out)


def _resolve_imports(file: ContextFile, home: Path, seen: set[Path], depth: int) -> list[ContextFile]:
    if depth >= MAX_IMPORT_DEPTH:
        return []
    found: list[ContextFile] = []
    for ref in _IMPORT_RE.findall(_strip_code(file.text)):
        target = (home / ref[2:]) if ref.startswith("~/") else (file.path.parent / ref)
        target = target.resolve()
        if target in seen or not target.is_file():
            continue
        text = _read(target)
        if text is None:
            continue
        seen.add(target)
        child = ContextFile(target, "import", file.loaded, text, imported_by=file.path)
        found.append(child)
        found.extend(_resolve_imports(child, home, seen, depth + 1))
    return found


def _frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    meta: dict[str, str] = {}
    key = None
    for line in match.group(1).splitlines():
        if re.match(r"^[A-Za-z_][\w-]*:", line):
            key, _, value = line.partition(":")
            key = key.strip()
            meta[key] = value.strip()
        elif key and line.strip():
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta


def _skill_listing(skill_dirs: list[Path]) -> str:
    """What the model sees about skills at startup: one line per skill."""
    lines = []
    for base in skill_dirs:
        if not base.is_dir():
            continue
        for skill in sorted(base.glob("*/SKILL.md")):
            meta = _frontmatter(_read(skill) or "")
            name = meta.get("name", skill.parent.name)
            desc = meta.get("description", "").strip("\"'")
            lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def _git_files(repo: Path, pattern: str) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", pattern],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return [p for p in repo.rglob(pattern) if "node_modules" not in p.parts]
    return [repo / line for line in out.splitlines() if line]


def _md_excludes(cwd: Path, home: Path) -> list[str]:
    """`claudeMdExcludes` from the user, project and local settings files."""
    patterns: list[str] = []
    for path in (home / ".claude" / "settings.json", cwd / ".claude" / "settings.json",
                 cwd / ".claude" / "settings.local.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("claudeMdExcludes"), list):
            patterns += [p for p in data["claudeMdExcludes"] if isinstance(p, str) and p]
    return patterns


def discover(cwd: Path, home: Path | None = None) -> list[ContextFile]:
    cwd = cwd.resolve()
    home = (home or Path.home()).resolve()
    seen: set[Path] = set()
    files: list[ContextFile] = []
    excludes = _md_excludes(cwd, home)

    def add(path: Path, kind: str, loaded: str, scope: list[str] | None = None) -> None:
        path = path.resolve()
        if path in seen or not path.is_file():
            return
        if kind != "auto-memory" and any(fnmatch.fnmatchcase(str(path), p) for p in excludes):
            return
        text = _read(path)
        if text is None:
            return
        seen.add(path)
        cf = ContextFile(path, kind, loaded, text, scope=scope or [])
        files.append(cf)
        files.extend(_resolve_imports(cf, home, seen, 0))

    add(home / ".claude" / "CLAUDE.md", "user", STARTUP)

    # Ancestors first (outermost to innermost), matching how they stack in context.
    for directory in reversed([cwd, *cwd.parents]):
        if directory == home.parent or directory == Path(directory.anchor):
            continue
        add(directory / "CLAUDE.md", "project", STARTUP)
        add(directory / ".claude" / "CLAUDE.md", "project", STARTUP)
        add(directory / "CLAUDE.local.md", "local", STARTUP)

    rules_dir = cwd / ".claude" / "rules"
    if rules_dir.is_dir():
        for rule in sorted(rules_dir.rglob("*.md")):
            paths = _frontmatter(_read(rule) or "").get("paths", "")
            scope = [p.strip(" -[]\"'") for p in re.split(r"[,\s]+", paths) if p.strip(" -[]\"'")]
            add(rule, "rule", ON_DEMAND if scope else STARTUP, scope)

    memory = home / ".claude" / "projects" / encode_project_dir(cwd) / "memory" / "MEMORY.md"
    add(memory, "auto-memory", STARTUP)

    listing = _skill_listing([home / ".claude" / "skills", cwd / ".claude" / "skills"])
    if listing:
        files.append(ContextFile(Path("<skill listing>"), "skill-listing", STARTUP, listing))

    for nested in sorted(_git_files(cwd, "*CLAUDE.md")):
        if nested.resolve() == (cwd / "CLAUDE.md").resolve() or nested.name != "CLAUDE.md":
            continue
        rel = nested.parent.relative_to(cwd).as_posix()
        add(nested, "nested", ON_DEMAND, [f"{rel}/**"])

    return files
