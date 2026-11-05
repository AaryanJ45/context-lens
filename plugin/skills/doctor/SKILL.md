---
name: doctor
description: Check how efficiently Claude Code runs in this repo and fix it - startup context (CLAUDE.md, rules, memory, skill listing), what the user keeps correcting Claude about (and the CLAUDE.md rules that would stop it), wasted work in past sessions (oversized tool output, failing and repeated calls), unused MCP servers and skills, compactions, and optionally whether past sessions followed the CLAUDE.md rules; then shrink CLAUDE.md into on-demand rule files and prove the slim version still works. Use when the user asks why sessions are slow, expensive or forgetful, why Claude keeps making the same mistake, what rules they are missing, whether their setup is efficient, what is using up their context, wants to shrink or clean up their CLAUDE.md, or wants before and after numbers.
---

# Doctor

Everything is one command, `doctor`, with flags. Run every step from the repo
root (the directory Claude Code sessions start in). Outputs go to
`.context-lens/` in the repo, which belongs in `.gitignore`.

## 1. Checkup (always first)

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --open
```

It runs locally in seconds and makes no model calls. `--open` opens
`.context-lens/doctor.html` in the user's browser. Then answer in a few lines,
not a dump of the output:

1. **One line per area** that is not "Good", with its key number.
2. **The top three fixes** from "WHAT TO DO", each with the evidence line that
   makes it concrete (the section, command or server named).
3. **The total** tokens per session that could stay out of context.
4. **Tell them the full visual report is open in their browser**, and give the
   `file://` link from the last line of the output in case it didn't open.
5. **Offer the next step** that fits. If the Corrections area shows repeated
   corrections, offer learning (step 2) first: it is the most useful one. Offer
   the rule check (step 2b) if they care whether Claude follows their rules,
   and shrinking CLAUDE.md (step 3) if startup context is the top fix.

## 2. Learn from corrections (opt-in)

This sends data to a model, so say so first: their words from each time they
rejected, interrupted or pushed back on Claude, the one-line actions just
before each, and their rule titles, all redacted. It costs about $0.25. To show
them exactly what would be sent:
`sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --learn-dry-run`

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --learn --open
```

List each lesson from "LEARNED FROM YOUR CORRECTIONS" with how many times they
said it and one quote. Then:

- **Contradicts R…**: their own instructions say the opposite of what they keep
  asking for. Show that rule's text and offer to edit it yourself; this is the
  fix that matters most.
- **Already in R…**: the rule exists and isn't followed. Offer to make it more
  concrete, or to enforce it with a hook.
- **New**: ask which to add, then run `doctor . --add-rules <numbers>`.
  Personal lessons go to `~/.claude/CLAUDE.md`, repo lessons to the repo's
  `CLAUDE.md`, and `--add-to local` keeps them out of git. Show what was
  written. Never commit.

## 2b. Rule adherence (opt-in)


This is the one check that sends data to a model. Say so before running it:
it sends their rule text, plus their prompts and one line per tool call
(commands, file paths) from about 8 recent sessions, with secrets redacted.
It never sends tool output or file contents. It costs a few cents. To show
them exactly what would be sent, without sending anything:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --rules-dry-run
```

That writes `.context-lens/rules-payloads.jsonl`. To run the check:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --rules --open
```

Report the rules with the lowest adherence and their evidence lines. Say that
rules about code content (styling, which helper to use) are not judged,
because command lines don't show them.

## 3. Shrink CLAUDE.md

This makes model calls: usually 5 to 25 of them, a few dollars at list price
for a 30k-token file. With no `ANTHROPIC_API_KEY` set they run as headless
`claude -p` on the user's own subscription. Unless the user already asked,
confirm first.

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --compact
```

It takes several minutes, longer than one shell call's timeout, so run it in
the background and wait. It writes nothing into the repo. Read
`.context-lens/compact/report.md` and tell the user:

- startup tokens before and after
- each new rule file, with its `paths:` globs and what it holds
- **every** entry under "Lost (review these)", in full; the user decides on each
- a short summary of "Dropped on purpose"

Point out any rule file whose globs match almost every file: it loads in
nearly every session, so it saves less than the table suggests.

To change a decision, edit `.context-lens/compact/plan.json`. The apply flags
below re-assemble from the plan, so the edits are picked up with no new model
calls.

**Apply only when the user says so.** Ask which way, unless they already said:

- **Locally (recommended for trying it out).** Only this checkout switches to
  the slim version. The committed `CLAUDE.md` is untouched, nothing appears in
  `git status`, and teammates are unaffected:
  `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --apply-local`. It takes
  effect from the **next** session. `--local-status` checks it and
  `--undo-local` removes it completely. After a pull changes `CLAUDE.md`, the
  slim copy is stale; the plugin warns at session start, and the checkup flags
  it too.
- **In the repo.** This rewrites the tracked files for everyone:
  `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --apply`. Never commit.
  Show `git diff --stat`, and suggest step 4 **before** committing.

## 4. Prove it still works

This needs the proposal from step 3, a git repo, and the `claude` CLI. The
**original** CLAUDE.md must still be the committed one. Every probe is a full
headless session, so state the cost and ask:

| | sessions | time |
|---|---:|---|
| `--verify quick` | ~20 | ~10 min |
| `--verify full` | ~180 | ~1 hour |

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" doctor . --verify quick --open
```

Run it in the background and wait. Results are saved as each session finishes.
If the run stops early, `doctor . --verify-report --open` rebuilds the report
from what finished.

Report the pass rate before and after, startup tokens, tokens and first-run
cost per session, then each failure with the judge's reason. Be honest about
noise: with one run per probe, a difference of one or two probes means
nothing. Later runs also share a warm prompt cache, which makes the original
look cheaper than it is for a cold session. To share the chart without the
repo's rules, use `--verify-report --anonymize --summary-only`.
