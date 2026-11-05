"""Put discovery, token counts, section signals and transcripts together."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .analyze import DocCorpus, RepoIndex, SectionStats, analyze_section, blame_times
from .discovery import STARTUP, ContextFile, discover
from .history import SizePoint, size_history
from .pricing import static_context_cost
from .sections import split_sections
from .tokens import TokenCounter
from .transcripts import SessionUsage, sessions_for

ANALYZED_KINDS = {"user", "project", "local", "import", "rule", "nested", "auto-memory"}


@dataclass
class FileReport:
    file: ContextFile
    tokens: int
    sections: list[SectionStats] = field(default_factory=list)

    @property
    def label(self) -> str:
        return str(self.file.path)


@dataclass
class Profile:
    repo: Path
    token_mode: str
    model: str
    files: list[FileReport]
    sessions: list[SessionUsage]
    history: list[SizePoint]
    days: int

    @property
    def startup_files(self) -> list[FileReport]:
        return [f for f in self.files if f.file.loaded == STARTUP]

    @property
    def startup_file_tokens(self) -> int:
        return sum(f.tokens for f in self.startup_files)

    @property
    def measured_startup(self) -> int | None:
        vals = [s.startup_tokens for s in self.sessions]
        return int(statistics.median(vals)) if vals else None

    @property
    def median_turns(self) -> int | None:
        vals = [s.turns for s in self.sessions]
        return int(statistics.median(vals)) if vals else None

    @property
    def sessions_per_day(self) -> float | None:
        if not self.sessions:
            return None
        span = (self.sessions[-1].started - self.sessions[0].started).days + 1
        return len(self.sessions) / max(span, 1)

    def file_cost_per_session(self, tokens: int) -> float | None:
        turns = self.median_turns
        model = self.sessions[-1].model if self.sessions else self.model
        return static_context_cost(model, tokens, turns) if turns else None

    def all_sections(self) -> list[tuple[FileReport, SectionStats]]:
        return [(f, s) for f in self.files for s in f.sections]


def build_profile(
    repo: Path, days: int = 30, token_mode: str = "auto", model: str = "claude-opus-5-5",
    home: Path | None = None, projects_dir: Path | None = None,
) -> Profile:
    repo = repo.resolve()
    counter = TokenCounter(model, token_mode)
    files = discover(repo, home)
    index = RepoIndex(repo)
    corpus = DocCorpus(index, {f.path.resolve() for f in files if f.path.is_absolute()})

    reports: list[FileReport] = []
    for cf in files:
        total = counter.count(cf.text)
        fr = FileReport(cf, total)
        if cf.kind in ANALYZED_KINDS:
            blame = blame_times(cf.path)
            for sec in split_sections(cf.text):
                tokens = counter.apportion(total, cf.chars, sec.chars)
                fr.sections.append(analyze_section(sec, tokens, index, corpus, blame))
        reports.append(fr)

    root = repo / "CLAUDE.md"
    history = size_history(repo, "CLAUDE.md") if root.is_file() else []
    return Profile(repo, counter.mode, model, reports, sessions_for(repo, days, projects_dir), history, days)
