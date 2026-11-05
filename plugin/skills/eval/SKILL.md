---
name: eval
description: Check that a compacted CLAUDE.md is still followed as well as the original, by running realistic rule-probing tasks as headless Claude Code sessions against both versions and grading them blind. Use after /context-lens:compact, or when the user asks whether a slimmer CLAUDE.md still works, or wants before and after numbers.
---

# Evaluate a compaction

Needs the output of `/context-lens:compact` in `.context-lens/compact/`, a git
repo, and the `claude` CLI. The **original** CLAUDE.md must still be the
committed one: each run starts from a fresh worktree of `HEAD` (before) and
the same worktree with the proposed files laid over it (after). If the
compaction is already committed, before and after are the same, so stop and
say so.

## Choose a size and confirm

Every probe run is a full headless session billed to the user's subscription
(or API key), so state the cost and ask:

| | probes | runs | sessions | time |
|---|---:|---:|---:|---|
| quick | 10 | 1 | 20 | ~10 min |
| full | 30 | 3 | ~180 | ~1 hour |

A quick run shows whether anything is badly broken; only a full run gives a
pass rate worth quoting.

## 1. Write probes

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" eval gen . --plan .context-lens/compact/plan.json --n 10 --out .context-lens/probes.json
```

Use `--n 30` for a full run. List the probes briefly (rule and task, one line
each); the user may edit `.context-lens/probes.json` before running.

## 2. Run

```sh
sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" eval run . --probes .context-lens/probes.json --after .context-lens/compact/files --reps 1 --out .context-lens/eval
```

Use `--reps 3` for a full run. It runs far longer than one shell call's
timeout: start it in the background and wait. Results are saved as each
session finishes (`results.partial.jsonl`); if it stops early, rebuild the
report from them with
`eval report --probes .context-lens/probes.json --results .context-lens/eval/results.partial.jsonl --html .context-lens/eval/report.html`.

## 3. Report

Read `.context-lens/eval/report.md` and give: pass rate before and after,
startup tokens, tokens per session and first-run cost, then each failure with
the judge's reason. Be honest about noise: with one run per probe a difference
of one or two probes means nothing, and later runs share a warm prompt cache,
which makes the original look cheaper than it is for a cold session. Offer to
open `.context-lens/eval/report.html`. To publish the chart without the repo's
rules, add `--anonymize` to `eval report`.
