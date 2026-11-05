---
name: compact
description: Propose a smaller startup CLAUDE.md by moving directory-specific rules into path-scoped .claude/rules files that load only when matching files are touched, pointing to docs that already hold reference content, and cutting history, with a check that no path, command or identifier is lost. Use when the user wants to shrink, slim down, split or clean up their CLAUDE.md, or after /context-lens:profile shows it is large.
---

# Compact CLAUDE.md

This makes model calls (one layout call plus one per batch of sections,
usually 5 to 25 calls, a few dollars at list price for a 30k-token file). With
no `ANTHROPIC_API_KEY` set they run as headless `claude -p` on the user's own
subscription. Unless the user already asked you to compact, say that and
confirm first.

## 1. Propose

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" compact . --out .context-lens/compact
```

Add `--file path/to/CLAUDE.md` for a file other than the root one. It takes
several minutes, longer than one shell call's timeout, so run it in the
background and wait for it to finish. It writes nothing into the repo.

## 2. Report

Read `.context-lens/compact/report.md` and tell the user:

- startup tokens before and after, and the projected session total
- each new rule file with its `paths:` globs and what it holds
- **every** entry under "Lost (review these)", in full; these are facts the
  rewrite dropped without a reason, and the user must decide on each one
- a short summary of "Dropped on purpose"

Show the new root file (`.context-lens/compact/files/CLAUDE.md`) and the rule
files, or a diff against the current `CLAUDE.md`. Point out any rule file whose
globs match almost every file: it loads in nearly every session, so it saves
less than the table suggests.

## 3. Adjust (optional)

To change a decision, edit `.context-lens/compact/plan.json` (each decision
has `action`, `dest`, `doc` and `text`), then re-assemble without new model
calls:

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" compact . --from-plan .context-lens/compact/plan.json --out .context-lens/compact
```

## 4. Apply only when the user says so

Ask which way, unless they already said:

- **Locally (recommended for trying it out).** Only this checkout switches to
  the slim version. The committed `CLAUDE.md` is untouched on disk and in git,
  nothing appears in `git status`, and teammates are unaffected:

  ```sh
  sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" compact . --from-plan .context-lens/compact/plan.json --out .context-lens/compact --apply-local
  ```

  It writes `CLAUDE.local.md`, rule files under `.claude/rules/context-lens-local/`,
  a `claudeMdExcludes` entry in `.claude/settings.local.json` and lines in
  `.git/info/exclude`. Tell the user it takes effect in the **next** session, that
  `run.sh local status` checks it and `run.sh local off` undoes it completely, and
  that after a pull changes `CLAUDE.md` the slim copy is stale (the plugin warns
  at session start) and should be re-made with step 1 and `--apply-local`.

- **In the repo.** Rewrites the tracked files for everyone:

  ```sh
  sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" compact . --from-plan .context-lens/compact/plan.json --out .context-lens/compact --apply
  ```

  Never commit. After applying, show `git diff --stat` and suggest
  `/context-lens:eval` **before** committing: eval compares the committed version
  against the proposal, so it has to run while the original is still what is
  committed.

Never apply either way without explicit approval.
