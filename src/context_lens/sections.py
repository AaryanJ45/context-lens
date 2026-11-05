"""Split an instruction file into sections a person can reason about.

Sections follow markdown headings. A section that is still large and is built
from a top-level list (a long "1. ... 2. ..." list of rules) is split
further, one section per item, because that is where the bulk usually hides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TOP_ITEM_RE = re.compile(r"^(\d+)[.)]\s+(.*)|^[-*]\s+(.*)")
SPLIT_ITEMS_ABOVE_CHARS = 6000
TITLE_MAX = 70


@dataclass
class Section:
    title: str
    level: int
    start_line: int  # 1-based, inclusive
    end_line: int    # 1-based, inclusive
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


def _short(title: str) -> str:
    title = re.sub(r"\*\*|__|`|~~", "", title).strip()
    return title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1].rstrip() + "…"


def _split_items(section: Section) -> list[Section] | None:
    lines = section.text.splitlines()
    starts: list[tuple[int, str]] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if in_fence or i == 0:
            continue
        m = _TOP_ITEM_RE.match(line)
        if m:
            label = f"{m.group(1)}. {m.group(2)}" if m.group(1) else (m.group(3) or "")
            starts.append((i, label))
    if len(starts) < 3:
        return None
    parts: list[Section] = []
    first = starts[0][0]
    if first > 1:
        head = "\n".join(lines[:first])
        parts.append(Section(section.title, section.level, section.start_line, section.start_line + first - 1, head))
    for n, (i, label) in enumerate(starts):
        j = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        parts.append(
            Section(
                f"{section.title} › {_short(label)}",
                section.level + 1,
                section.start_line + i,
                section.start_line + j - 1,
                "\n".join(lines[i:j]),
            )
        )
    return parts


def split_sections(text: str) -> list[Section]:
    lines = text.splitlines()
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    cur_title, cur_level, cur_start = "(preamble)", 0, 0
    in_fence = False

    def close(end: int) -> None:
        body = "\n".join(lines[cur_start:end])
        if body.strip():
            sections.append(Section(cur_title, cur_level, cur_start + 1, end, body))

    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        m = None if in_fence else _HEADING_RE.match(line)
        if not m:
            continue
        close(i)
        level, title = len(m.group(1)), _short(m.group(2))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        cur_title = " › ".join(t for _, t in stack[-2:]) if level > 1 else title
        cur_level, cur_start = level, i
    close(len(lines))

    out: list[Section] = []
    for s in sections:
        split = _split_items(s) if s.chars > SPLIT_ITEMS_ABOVE_CHARS else None
        out.extend(split or [s])
    return out
