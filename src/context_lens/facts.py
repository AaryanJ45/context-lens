"""The concrete references in an instruction file, and a check that none went missing.

A "fact" is a backticked span: a path, a command, a component or flag name, a
threshold. Prose can be reworded freely, but if `make check` or
`docs/api.md` disappears from every output, an instruction went with
it. Spans inside ~~struck-through~~ text are history and are not counted.
"""

from __future__ import annotations

import re

_STRIKE_RE = re.compile(r"~~.+?~~", re.S)
# CommonMark: a span opens and closes with the same number of backticks (`` a `b` ``).
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
MIN_FACT_CHARS = 3


def facts_in(text: str) -> list[str]:
    """Distinct backticked spans outside struck-through text and code fences, in order."""
    kept, in_fence = [], False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append(line)
    live = _STRIKE_RE.sub(" ", "\n".join(kept))
    seen: dict[str, None] = {}
    spans = (raw.strip() for line in live.splitlines() for _, raw in _CODE_SPAN_RE.findall(line))
    for fact in spans:
        if len(fact) >= MIN_FACT_CHARS and re.search(r"[A-Za-z0-9]", fact):
            seen.setdefault(fact, None)
    return list(seen)


def missing(facts: list[str], haystacks: list[str]) -> list[str]:
    return [f for f in facts if not any(f in h for h in haystacks)]
