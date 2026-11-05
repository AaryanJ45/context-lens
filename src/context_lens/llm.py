"""Two ways to ask Claude for structured JSON: the Anthropic API or a headless Claude Code.

* ``api``          Anthropic SDK with ANTHROPIC_API_KEY, billed to that key.
* ``claude-code``  ``claude -p`` on this machine, billed to the user's Claude
                   subscription. Run from an empty directory with settings,
                   MCP servers, skills and tools switched off, so the call
                   carries about 1k tokens of overhead instead of the user's
                   whole startup context.
* ``auto``         the API when a key is set, else Claude Code when ``claude``
                   is on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from threading import Lock

DEFAULT_MODEL = "claude-opus-5-5"
MAX_OUTPUT_TOKENS = 32000
CLAUDE_CODE_TIMEOUT_S = 900


class BackendError(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def add(self, inp: int, out: int, cost: float = 0.0) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += inp
            self.output_tokens += out
            self.cost_usd += cost


class Backend:
    name = "base"

    def __init__(self, model: str) -> None:
        self.model = model
        self.usage = Usage()

    def ask_json(self, system: str, prompt: str, schema: dict) -> dict:
        raise NotImplementedError


class ApiBackend(Backend):
    name = "api"

    def __init__(self, model: str) -> None:
        super().__init__(model)
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise BackendError("the api backend needs `pip install context-lens[exact]`") from exc
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            raise BackendError("the api backend needs ANTHROPIC_API_KEY")
        self._client = Anthropic()

    def ask_json(self, system: str, prompt: str, schema: dict) -> dict:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=[{"name": "answer", "description": "Return the answer.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": "answer"},
        )
        self.usage.add(resp.usage.input_tokens, resp.usage.output_tokens)
        for block in resp.content:
            if block.type == "tool_use":
                return dict(block.input)
        raise BackendError(f"no structured answer (stop_reason={resp.stop_reason})")


class ClaudeCodeBackend(Backend):
    name = "claude-code"

    def __init__(self, model: str) -> None:
        super().__init__(model)
        self._bin = shutil.which("claude")
        if not self._bin:
            raise BackendError("the claude-code backend needs the `claude` CLI on PATH")

    def ask_json(self, system: str, prompt: str, schema: dict) -> dict:
        cmd = [
            self._bin, "-p",
            "--model", self.model,
            "--system-prompt", system,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--tools", "",
            "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
        with tempfile.TemporaryDirectory(prefix="context-lens-") as empty:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, cwd=empty, timeout=CLAUDE_CODE_TIMEOUT_S
            )
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BackendError(f"claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}") from exc
        usage = out.get("usage") or {}
        self.usage.add(
            usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0),
            usage.get("output_tokens", 0),
            out.get("total_cost_usd") or 0.0,
        )
        if out.get("is_error") or out.get("structured_output") is None:
            raise BackendError(f"claude -p returned no structured output: {str(out.get('result'))[:500]}")
        return out["structured_output"]


def pick_backend(name: str, model: str = DEFAULT_MODEL) -> Backend:
    if name == "api":
        return ApiBackend(model)
    if name == "claude-code":
        return ClaudeCodeBackend(model)
    errors = []
    for cls in (ApiBackend, ClaudeCodeBackend):
        try:
            return cls(model)
        except BackendError as exc:
            errors.append(str(exc))
    raise BackendError("no backend available: " + "; ".join(errors))
