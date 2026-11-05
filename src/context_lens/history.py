"""How an instruction file grew over its git history."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class SizePoint:
    commit: str
    when: datetime
    bytes: int


def size_history(repo: Path, rel_path: str, limit: int = 400) -> list[SizePoint]:
    try:
        log = subprocess.run(
            ["git", "-C", str(repo), "log", f"-{limit}", "--format=%H %cI", "--", rel_path],
            capture_output=True, text=True, check=True,
        ).stdout.split("\n")
    except (OSError, subprocess.CalledProcessError):
        return []
    commits = [line.split(" ", 1) for line in log if line.strip()]
    if not commits:
        return []
    batch = "".join(f"{sha}:{rel_path}\n" for sha, _ in commits)
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch-check=%(objectsize)"],
            input=batch, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return []
    points = []
    for (sha, when), size in zip(commits, out):
        if size.isdigit():  # "missing" when the file was deleted in that commit
            points.append(SizePoint(sha[:10], datetime.fromisoformat(when), int(size)))
    return sorted(points, key=lambda p: p.when)
