"""Per-section signals that say how much of a file is pulling its weight."""

from __future__ import annotations

import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .sections import Section

_STRIKE_RE = re.compile(r"~~(.+?)~~", re.S)
_CORRECTION_RE = re.compile(r"\bcorrected\s+20\d\d-\d\d-\d\d", re.I)
_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
_DATE_RE = re.compile(r"\b20\d\d-\d\d-\d\d\b")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_PATH_EXT_RE = re.compile(r"\.(tsx?|jsx?|mjs|cjs|py|md|json|ya?ml|toml|css|scss|sh|sql|go|rs|swift|txt|ini|cfg)$")
_WORD_RE = re.compile(r"[a-z0-9_]+")
SHINGLE = 8

HISTORY_STRUCK_SHARE = 0.10
HISTORY_CORRECTIONS = 2
REFERENCE_DUP_SHARE = 0.30
# A section that names a doc and paraphrases it is a copy even at lower overlap.
REFERENCE_POINTED_SHARE = 0.15
SCOPED_SHARE = 0.70
IGNORED_SCOPES = {".", "node_modules", "docs", ".github"}


@dataclass
class SectionStats:
    section: Section
    tokens: int = 0
    struck_chars: int = 0
    corrections: int = 0
    tickets: int = 0
    dates: int = 0
    path_refs: list[str] = field(default_factory=list)
    missing_refs: list[str] = field(default_factory=list)
    top_dirs: Counter = field(default_factory=Counter)
    dup_doc: str | None = None
    dup_share: float = 0.0
    last_edited: datetime | None = None
    hint: str = "core"

    @property
    def scope_dir(self) -> str | None:
        if not self.top_dirs:
            return None
        top, n = self.top_dirs.most_common(1)[0]
        if top in IGNORED_SCOPES:
            return None
        return top if n / sum(self.top_dirs.values()) >= SCOPED_SHARE and n >= 2 else None


class RepoIndex:
    """Tracked files, used to tell live path references from stale ones."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True
            ).stdout
            self.files = out.splitlines()
        except (OSError, subprocess.CalledProcessError):
            self.files = [
                p.relative_to(repo).as_posix()
                for p in repo.rglob("*")
                if p.is_file() and "node_modules" not in p.parts and ".git" not in p.parts
            ]
        self.dirs = {"/".join(f.split("/")[:i]) for f in self.files for i in range(1, f.count("/") + 1)}
        self.by_name: dict[str, list[str]] = defaultdict(list)
        for f in self.files:
            self.by_name[f.rsplit("/", 1)[-1]].append(f)

    def resolve(self, ref: str) -> str | None:
        ref = ref.strip("/")
        if ref in self.dirs or ref in self.by_name.get(ref.rsplit("/", 1)[-1], []):
            return ref
        name = ref.rsplit("/", 1)[-1]
        for f in self.by_name.get(name, []):
            if f.endswith("/" + ref) or f == ref:
                return f
        if "/" in ref:
            return next((d for d in self.dirs if d.endswith("/" + ref)), None)
        matches = self.by_name.get(name, [])
        return matches[0] if matches else None


def _path_refs(text: str) -> list[str]:
    refs = []
    for raw in _CODE_SPAN_RE.findall(text):
        ref = raw.strip().rstrip(".,;:)")
        ref = re.sub(r":\d+(-\d+)?$", "", ref)  # file.py:12
        ref = ref.split("::")[0]
        if not ref or ref[0] in "@<$-~/" or "://" in ref or any(c in ref for c in " *{}()<>|=\"'"):
            continue
        if ref.startswith("./"):
            ref = ref[2:]
        if re.search(r"\.[A-Z]", ref) or any(len(part) <= 1 for part in ref.strip("/").split("/")):
            continue  # Table.Header/Cell, N/A: names and prose, not paths
        if "/" in ref or _PATH_EXT_RE.search(ref):
            refs.append(ref)
    return refs


def _shingles(text: str) -> set[int]:
    words = _WORD_RE.findall(text.lower())
    return {hash(" ".join(words[i : i + SHINGLE])) for i in range(max(len(words) - SHINGLE + 1, 0))}


class DocCorpus:
    """Shingles of the repo's other markdown, to find sections that repeat a doc."""

    def __init__(self, index: RepoIndex, exclude: set[Path]) -> None:
        self.docs: dict[str, set[int]] = {}
        skip_names = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "GEMINI.md"}
        for rel in index.files:
            if not rel.endswith(".md") or rel.rsplit("/", 1)[-1] in skip_names:
                continue
            path = (index.repo / rel).resolve()
            if path in exclude:
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                self.docs[rel] = _shingles(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue

    def best_match(self, text: str, named: list[str] | None = None) -> tuple[str | None, float, bool]:
        """Best-overlapping doc, its share, and whether the section names that doc.

        A doc the section itself points at wins when it clears the lower bar.
        """
        mine = _shingles(text)
        if len(mine) < 20:
            return None, 0.0, False
        shares = {rel: len(mine & theirs) / len(mine) for rel, theirs in self.docs.items()}
        if not shares:
            return None, 0.0, False
        pointed = [(shares[r], r) for r in set(named or []) if shares.get(r, 0) >= REFERENCE_POINTED_SHARE]
        if pointed:
            share, rel = max(pointed)
            return rel, share, True
        rel = max(shares, key=shares.get)
        return rel, shares[rel], False


def blame_times(path: Path) -> list[datetime | None]:
    """Author time of every line, or [] when the file is not in git."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "blame", "--line-porcelain", "--", path.name],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    times: list[datetime | None] = []
    current: datetime | None = None
    for line in out.splitlines():
        if line.startswith("author-time "):
            current = datetime.fromtimestamp(int(line.split()[1]), tz=timezone.utc)
        elif line.startswith("\t"):
            times.append(current)
    return times


def analyze_section(
    section: Section, tokens: int, index: RepoIndex, corpus: DocCorpus | None, blame: list[datetime | None]
) -> SectionStats:
    st = SectionStats(section=section, tokens=tokens)
    text = section.text
    st.struck_chars = sum(len(m) + 4 for m in _STRIKE_RE.findall(text))
    st.corrections = len(_CORRECTION_RE.findall(text))
    st.tickets = len(set(_TICKET_RE.findall(text)))
    st.dates = len(_DATE_RE.findall(text))
    st.path_refs = sorted(set(_path_refs(text)))
    named_docs: list[str] = []
    for ref in st.path_refs:
        resolved = index.resolve(ref)
        if resolved is None and (index.repo / ref.split("/")[0]).exists():
            resolved = ref  # untracked or ignored on disk (build output, local config)
        if resolved and resolved.endswith(".md"):
            named_docs.append(resolved)
        if resolved is None:
            st.missing_refs.append(ref)
        else:
            st.top_dirs[resolved.split("/", 1)[0] if "/" in resolved else "."] += 1
    pointed = False
    if corpus:
        st.dup_doc, st.dup_share, pointed = corpus.best_match(text, named_docs)
    span = [t for t in blame[section.start_line - 1 : section.end_line] if t]
    st.last_edited = max(span) if span else None

    if section.chars and st.struck_chars / section.chars >= HISTORY_STRUCK_SHARE or st.corrections >= HISTORY_CORRECTIONS:
        st.hint = "history"
    elif st.dup_share >= (REFERENCE_POINTED_SHARE if pointed else REFERENCE_DUP_SHARE):
        st.hint = "reference"
    elif st.scope_dir and st.scope_dir != ".":
        st.hint = f"scoped:{st.scope_dir}/"
    return st
