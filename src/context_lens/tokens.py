"""Token counting: exact through Anthropic's count_tokens endpoint, else an estimate.

Exact counts are taken once per whole file; sections inside a file are then
apportioned by character share, so a 200-section CLAUDE.md costs one API call.
"""

from __future__ import annotations

import hashlib
import os

DEFAULT_MODEL = "claude-opus-5-5"
# Markdown-heavy instruction files under the Opus 4.7+ tokenizer. Only used
# when no API credentials are available; reports label these counts "≈".
CHARS_PER_TOKEN_ESTIMATE = 3.4


class TokenCounter:
    def __init__(self, model: str = DEFAULT_MODEL, mode: str = "auto") -> None:
        self.model = model
        self._cache: dict[str, int] = {}
        self._client = None
        self._overhead = 0
        self.mode = "estimate"
        if mode == "estimate":
            return
        try:
            from anthropic import Anthropic

            has_creds = any(
                os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
            ) or os.path.isdir(os.path.expanduser("~/.config/anthropic"))
            if not has_creds:
                raise RuntimeError("no Anthropic credentials found")
            self._client = Anthropic()
            self._overhead = self._raw_count(".") - 1
            self.mode = "exact"
        except Exception as exc:  # noqa: BLE001 - any failure falls back to estimates
            if mode == "exact":
                raise SystemExit(
                    f"--tokens exact needs `pip install context-lens[exact]` and an API key ({exc})"
                ) from exc

    @property
    def exact(self) -> bool:
        return self.mode == "exact"

    def _raw_count(self, text: str) -> int:
        resp = self._client.messages.count_tokens(  # type: ignore[union-attr]
            model=self.model, messages=[{"role": "user", "content": text}]
        )
        return resp.input_tokens

    def count(self, text: str) -> int:
        if not text:
            return 0
        key = hashlib.sha256(text.encode()).hexdigest()
        if key not in self._cache:
            if self.exact:
                self._cache[key] = max(self._raw_count(text) - self._overhead, 0)
            else:
                self._cache[key] = round(len(text) / CHARS_PER_TOKEN_ESTIMATE)
        return self._cache[key]

    def apportion(self, total_tokens: int, total_chars: int, part_chars: int) -> int:
        if total_chars == 0:
            return 0
        return round(total_tokens * part_chars / total_chars)
