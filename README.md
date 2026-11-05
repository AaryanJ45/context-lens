# context-lens

Measure what Claude Code loads into context before you type a word, find the
parts of your `CLAUDE.md` that aren't pulling their weight, and prove a slimmer
version works just as well.

Every Claude Code session starts by loading your instruction files (`CLAUDE.md`,
imports, rules, auto memory, the skill listing) on top of its own system prompt.
That block is written to the prompt cache once and re-read on **every turn**,
and in a busy repo `CLAUDE.md` tends to grow faster than anyone prunes it.

## Install

```sh
pipx install git+https://github.com/AaryanJ45/context-lens
# exact token counts via Anthropic's count_tokens endpoint (needs an API key):
pipx install "context-lens[exact] @ git+https://github.com/AaryanJ45/context-lens"
```

Python 3.10+. The core has no dependencies.

## Profile a repo

```sh
context-lens profile path/to/repo --html report.html --json profile.json
```

It reports:

- **Measured startup context** from your own Claude Code transcripts
  (`~/.claude/projects/*/*.jsonl`): the first response of each session carries
  the startup tokens exactly as billed (input + cache writes + cache reads).
- **Which files are loaded**, at startup or on demand, following Claude Code's
  memory hierarchy: user and project `CLAUDE.md` up the directory tree,
  `CLAUDE.local.md`, `@imports`, `.claude/rules/`, auto memory, the skill
  listing, and nested `CLAUDE.md` files.
- **Every section's cost**, split by heading (and by list item when one
  heading holds a long numbered list), with the date it was last edited.
- **Signals that a section can move or go:**
  - `history`: struck-through text and "corrected on <date>" notes (git keeps these)
  - `reference`: repeats a doc in the repo, often one it names, so a pointer would do
  - `scoped:<dir>/`: only about one directory, so it can load on demand from a nested `CLAUDE.md`
  - paths it references that no longer exist
- **Cost per session** at API list prices, and the share caused by instruction files.
- **Growth** of `CLAUDE.md` across its git history.

Token counts are estimates (marked `≈`) unless the `exact` extra is installed
and credentials are available. Exact mode makes one `count_tokens` call per file.

Everything runs locally. Transcripts and file contents never leave your machine
except for the optional `count_tokens` calls.

## Compact a CLAUDE.md

```sh
context-lens compact path/to/repo                       # proposes, writes nothing into the repo
context-lens compact path/to/repo --backend claude-code # bill the model calls to your Claude subscription
context-lens compact path/to/repo --from-plan context-lens-out/plan.json --apply
```

Three steps, each one checkable:

1. **Layout.** One call reads the file's outline and proposes path-scoped rule
   files (`.claude/rules/<slug>.md` with `paths:` globs). Claude Code loads those
   only when it touches a matching file, so they cost nothing at startup. Globs
   that match no tracked file are reported.
2. **Decisions.** Sections go out in parallel batches. Each comes back as `keep`,
   `condense`, `move` (to a rule file), `pointer` (to a repo doc that already
   holds it) or `drop` (obsolete), with the rewritten text.
3. **Coverage.** Every backticked fact in the original (paths, commands,
   identifiers, flag keys, thresholds) must reappear in the output it was sent
   to, or in the doc it points at, or be declared dropped with a reason.
   Sections with unexplained losses are asked again once; anything still
   missing is listed under "Lost" in the report.

Output goes to `context-lens-out/`: the proposed files under `files/`,
`plan.json` (every decision, editable, re-assemble with `--from-plan`) and
`report.md` (before/after tokens, coverage, every decision with its reason).
`--apply` writes the files into the repo; review the diff in git.

**Backends.** `--backend api` uses the Anthropic SDK and `ANTHROPIC_API_KEY`
(install the `exact` extra). `--backend claude-code` runs headless `claude -p`
from an empty directory with settings, MCP servers, skills and tools off, so
each call carries about 1k tokens of overhead and bills to your subscription.
`auto` (the default) picks the API when a key is set, else Claude Code.

## Prove it still works

```sh
context-lens eval gen path/to/repo --plan context-lens-out/plan.json --n 30 --out probes.json
context-lens eval run path/to/repo --probes probes.json --after context-lens-out/files
```

1. **Probes.** For a sample of sections, a model writes a realistic task an
   agent only gets right by following that section's rule, naming real files,
   plus a rubric taken from the *original* text. The sample is spread across
   headings (one long catalog can't fill it), favours sections the compaction
   moved, pointed or condensed, and keeps about 20% untouched as controls.
   A probe that names the doc holding its rule is rejected, since it would hand
   the agent the answer. Probes are JSON; edit them before running.
2. **Runs.** Each probe runs as a fresh headless Claude Code session in a
   throwaway git worktree, once on the repo as committed (`before`) and once
   with the compacted files laid over it (`after`). Sessions can read and
   search but not edit or run commands; user-level settings, hooks and MCP
   servers are left out so the only difference is the instruction files.
3. **Grading.** A separate judge call sees the original rule, the rubric and
   the answer, never which variant wrote it.

The report gives pass rate, startup tokens (the first turn's input, as
billed), total tokens, cost and turns per variant, then pairs each probe
across variants and lists every failure with the files the session read.

## Roadmap

- [x] `profile`: measure startup context and score sections
- [x] `compact`: propose a slim root `CLAUDE.md` plus on-demand rule files and
      doc pointers, with a coverage check that every original rule lands somewhere
- [x] `eval`: run rule-probing tasks headlessly under both versions and compare
      startup tokens, cost and rule pass rate
- [ ] Claude Code plugin wrapping the CLI

## License

MIT
