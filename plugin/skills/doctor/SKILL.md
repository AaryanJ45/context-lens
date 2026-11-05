---
name: doctor
description: Check how efficiently Claude Code runs in this repo and what to fix, in one pass - startup context (CLAUDE.md, rules, memory, skill listing), wasted work in past sessions (oversized tool output, failing and repeated calls), unused MCP servers and skills, compactions, and optionally whether past sessions followed the CLAUDE.md rules. Use when the user asks why sessions are slow, expensive or forgetful, whether their setup is efficient, what is using up their context, or wants a checkup of their Claude Code setup.
---

# Doctor

Run from the repo root (the directory Claude Code sessions start in):

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor .
```

It reads the user's own Claude Code transcripts and config for this directory
and makes no model calls. It writes `.context-lens/doctor.html` and
`.context-lens/doctor.json`.

Then answer in a few lines, not a dump of the output:

1. **One line per area** that is not "Good", with its key number.
2. **The top three fixes** from "WHAT TO DO", each with the evidence line that
   makes it concrete (the section, command or server named).
3. **The total** tokens per session that could stay out of context.
4. **Next step:** if startup context is the top fix, offer
   `/context-lens:compact`. Offer to open `.context-lens/doctor.html`.

## Rule adherence (opt-in)

Only if the user asks whether Claude follows their rules, or agrees when you
offer it. It is the one check that sends data to a model, so say so before
running it: their rule text, plus their prompts and one line per tool call
(commands, file paths) from about 8 recent sessions, secrets redacted. Never
tool output or file contents. To show them exactly what would be sent first:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --rules-dry-run
```

That writes `.context-lens/rules-payloads.jsonl` and sends nothing. To run it:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --rules
```

Report the rules with the lowest adherence and their evidence lines, and say
that rules about code content (styling, which helper to use) are not judged,
because command lines don't show them.

`.context-lens/` holds the outputs and belongs in `.gitignore`.
