---
name: profile
description: Measure what Claude Code loads into context before the first message in this repo (CLAUDE.md files, imports, rules, auto memory, the skill listing), which sections cost the most, and what that costs per session. Use when the user asks why sessions start with so many tokens, whether their CLAUDE.md is too big, or what is using up their context.
---

# Profile startup context

Run from the repo root (the directory Claude Code sessions start in):

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" profile . --json .context-lens/profile.json --html .context-lens/report.html
```

It reads the user's own Claude Code transcripts for this directory, so the
startup number is what they were actually billed, not an estimate. It runs in
seconds and makes no model calls.

Then answer in a few lines, not a dump of the output:

1. **Measured startup context** (median tokens before the first message) and
   how much of it comes from instruction files versus Claude Code itself.
2. **The five most expensive sections** with their tokens and signal:
   `history` (struck-through or "corrected" notes git already keeps),
   `reference` (repeats a doc in the repo), `scoped:<dir>/` (only about one
   directory). These are the candidates to move or cut.
3. **Cost per session** from instruction files, if transcripts exist.
4. **Next step:** if instruction files are over about 10k tokens, suggest
   `/context-lens:compact`. If they are small, say so and stop.

Token counts marked `≈` are estimates; exact counts need `ANTHROPIC_API_KEY`
and the `exact` extra. Mention that `.context-lens/` holds the outputs and
belongs in `.gitignore`. Offer to open `.context-lens/report.html`.
