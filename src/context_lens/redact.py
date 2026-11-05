"""Scrub secrets and personal details from text before it leaves the machine.

Only `doctor --rules` sends anything to a model, and only short excerpts:
the user's prompts and one line per tool call (a command, a file path, a
search pattern), never tool output or file contents. Each line passes through
`redact` first. This is a best-effort filter, not a guarantee, which is why
`--rules-dry-run` writes the exact payloads to disk for review first.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

_PATTERNS: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<private-key>"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), "<anthropic-key>"),
    (re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{20,}"), "<api-key>"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), "<github-token>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<aws-key>"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), "<slack-token>"),
    (re.compile(r"\b(?:lin_api|phc|phx|npm|glpat|pk_live|sk_live|rk_live)_[A-Za-z0-9_\-]{16,}"), "<api-key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "<jwt>"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}"), r"\1 <token>"),
    (re.compile(r"(?i)(://)[^/\s:@]+:[^/\s@]+@"), r"\1<user>:<password>@"),
    # NAME=value / "name": "value" / --name value where the name says secret.
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIALS?)[A-Z0-9_]*)"
                r"(\s*[=:]\s*|\"\s*:\s*\")(['\"]?)[^\s'\"]{6,}"), r"\1\2\3<redacted>"),
    (re.compile(r"(?i)(--?(?:password|passwd|token|api-key|secret)[ =])\S+"), r"\1<redacted>"),
    (re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+"), "<email>"),
    (re.compile(r"\b[0-9a-f]{32,}\b", re.I), "<hex>"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}"), lambda m: "<blob>" if _looks_random(m.group()) else m.group()),
]


def _looks_random(s: str) -> bool:
    """A key mixes digits and both cases and has at most a couple of slashes; a path doesn't."""
    return (s.count("/") <= 2 and any(c.isdigit() for c in s)
            and any(c.isupper() for c in s) and any(c.islower() for c in s))


def redact(text: str, home: Path | None = None) -> str:
    home_s = str(home or Path.home())
    if home_s and home_s != "/":
        text = text.replace(home_s, "~")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text
