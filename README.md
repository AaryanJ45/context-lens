# context-lens

A checkup for Claude Code that learns from your mistakes. One command reads
your instruction files and your past sessions and tells you how efficiently
Claude runs in a repo: what it loads before you type a word, where sessions
waste tokens, which tools and skills sit unused, and whether Claude actually
follows your `CLAUDE.md`. Then it ranks what to fix, and opens a visual report
in your browser.

**It learns from your corrections.** Every time you rejected an action,
interrupted Claude or said "no, don't…", you were stating a rule your
`CLAUDE.md` doesn't have. `--learn` finds those moments, groups the ones you
keep repeating, and writes the missing rule. It also catches the case where
your own instructions say the opposite of what you keep asking for, which is
usually why Claude keeps getting it wrong. `--add-rules` writes the rules you
pick. When the fix
is a smaller `CLAUDE.md`, the same command writes it (`--compact`) and proves
it still works (`--verify`).

```sh
context-lens doctor          # or just: context-lens
```

**Private by default.** Everything except the optional rule check runs on your
machine and reads only local files. The rule check sends your rule text plus
short, redacted excerpts (prompts and one line per tool call, never tool output
or file contents) to a model, and `--rules-dry-run` shows you every byte first.
See [Privacy](#privacy).

Every Claude Code session starts by loading your instruction files (`CLAUDE.md`,
imports, rules, auto memory, the skill listing) on top of its own system prompt.
That block is written to the prompt cache once and re-read on **every turn**,
and in a busy repo `CLAUDE.md` tends to grow faster than anyone prunes it.

## Results

Run on a production repo whose root `CLAUDE.md` had grown past 100 KB:
`compact` proposed a slim root file plus five path-scoped rule files, and
`eval` ran 29 rule probes × 3 runs × 2 variants (174 headless sessions on
Sonnet 5.5). Rule names are withheld; the chart below is generated with
`context-lens doctor --verify-report --anonymize --summary-only`.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/eval-dark.png">
  <img alt="Before vs after: startup context 63.5k to 17.0k tokens, input tokens per session 377k to 221k, first-run cost per session $0.40 to $0.26, rules followed 87% to 90%; per-rule pass rates mostly identical" src="docs/images/eval-light.png">
</picture>

What it shows, and what it doesn't:

- **No measurable loss in rule-following.** 76/87 runs passed on the original,
  78/87 on the compacted version; the 95% intervals overlap. Of 87 paired runs,
  3 passed only before and 5 only after.
- **The saving is context, every session.** 46k fewer tokens ride along on
  every turn. Over a long session (say 80 turns) that is about 3.7M fewer
  cache-read tokens, roughly $1 at list price.
- **Cost depends on cache warmth.** A large prefix that is already cached is
  cheap to re-read, and compacted sessions take ~2 more turns opening rule
  files, so back-to-back sessions saved nothing here ($0.15 vs $0.20).
- **Probes are short, read-only tasks** (~10 turns). In long sessions the fixed
  startup cost is a smaller share of the total.

## Install

```sh
pipx install git+https://github.com/AaryanJ45/context-lens
# exact token counts via Anthropic's count_tokens endpoint (needs an API key):
pipx install "context-lens[exact] @ git+https://github.com/AaryanJ45/context-lens"
```

Python 3.10+. The core has no dependencies.

## Use it inside Claude Code

The repo is also a Claude Code plugin marketplace. In any session:

```
/plugin install context-lens --marketplace AaryanJ45/context-lens
```

(On Claude Code before 2.1.275: `/plugin marketplace add AaryanJ45/context-lens`,
then `/plugin install context-lens@context-lens`.)

That adds one command, `/context-lens:doctor`. It runs the checkup, opens the
report in your browser, explains the top fixes, and offers each next step (the
rule check, shrinking `CLAUDE.md`, verifying it) when it fits. Anything that
costs money or changes files is confirmed with you first.

The plugin needs Python 3.10+. It uses an installed `context-lens` if there is
one, else `uvx` or `pipx`, else installs a private copy into
`~/.cache/context-lens` on first use. Outputs go to `.context-lens/` in your
repo; add it to `.gitignore`.

## One command

Everything is `context-lens doctor` (or plain `context-lens`) plus a flag:

```sh
context-lens doctor                     # checkup: local only, no model calls
context-lens doctor --rules-dry-run     # write what --rules would send; send nothing
context-lens doctor --rules             # + did past sessions follow your rules
context-lens doctor --learn             # + turn what you keep correcting into rules
context-lens doctor --add-rules 1,3     # add learned rules to CLAUDE.md
context-lens doctor --compact           # propose a slim CLAUDE.md (writes nothing into the repo)
context-lens doctor --apply-local       # use the proposal in this checkout only
context-lens doctor --apply             # write the proposal into the repo
context-lens doctor --undo-local        # back to the committed CLAUDE.md
context-lens doctor --verify quick      # prove the proposal is still followed (or: full)
```

Pass a path to check a repo other than the current directory. Everything is
written to `.context-lens/` in the repo; add it to `.gitignore`.

## The checkup

It prints one line per area and a ranked list of fixes, each with the evidence
behind it, then **opens `.context-lens/doctor.html` in your browser** (when run
in a terminal; `--open` / `--no-open` override) and prints its `file://` link.
`doctor.json` has the same data for scripts. Every number comes from your own files, over the last 30 days
(`--days`):

| Area | What it measures | Where from |
|---|---|---|
| **Startup context** | Tokens loaded before your first message, how much is instruction files, the costliest sections, stale paths | Instruction files + the first response of each session, as billed |
| **Wasted work** | Tool outputs over 5k tokens that sit in context for the rest of the session, failed calls and the commands behind them, the same failing call repeated, re-reads of unchanged files | Tool calls and results in transcripts |
| **Tools and MCP** | MCP servers configured but never called, skills listed every session but never used | `~/.claude.json`, `.mcp.json`, skill folders, transcripts |
| **Session health** | Share of sessions that ran out of room and were compacted, peak context, context added per turn | Per-turn usage in transcripts |
| **Corrections** | How often you rejected an action, interrupted, or pushed back, and on what; with `--learn`, the rules those corrections add up to | Rejections (with the reason you typed), interruptions, prompts that push back |
| **Rule adherence** (`--rules`) | Whether sessions followed each rule that a log of actions can show, with the line that proves it | A model reading redacted excerpts |

Each area is marked *Good*, *Worth a look* or *Fix this* from plain thresholds
(for example, instruction files over 25k tokens, or more than 10% of tool
calls failing), and the report shows the numbers behind every mark. There is
no single score: it would hide which number moved.

### Startup context in detail

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

### Rule adherence

Two steps, both part of `--rules`:

1. **Pick the checkable rules.** The 80 largest startup sections go to the
   model as rule text, and it keeps the ones a log of actions can show being
   followed: which commands to run, git and branch workflow, tests, which tools
   or skills to use, asking before an action. Rules about the *content* of code
   (styling, which helper to use) are counted as "not judged" rather than
   guessed at, because command lines don't show them.
2. **Judge recent sessions.** About 8 recent sessions that did real work
   (`--rules-sessions`), one call each, against up to 20 of those rules
   (`--rules-max`). Each rule comes back *followed*, *violated* or *didn't come
   up*, with the excerpt line that shows it.

The judge defaults to `claude-haiku-5-5` (`--judge-model`) and uses the same
backends as `compact`: your API key, or headless Claude Code on your
subscription. A run over 8 sessions costs a few cents. Rules that never come up
are candidates to load on demand; rules often violated are candidates to make
shorter, more concrete, or enforced by a hook.

## Learn from your corrections: `--learn`

```sh
context-lens doctor --learn-dry-run     # write the exact request; send nothing
context-lens doctor --learn             # one model call
context-lens doctor --add-rules 1,3     # write lessons 1 and 3
```

1. **Find corrections (local).** Three kinds, each paired with the few
   actions Claude took just before it:
   - **Rejected actions**, with the reason you typed when saying no. Declining
     to answer a clarifying question doesn't count.
   - **Interruptions**, with what you said next. A bare "continue" doesn't count.
   - **Pushback**: prompts that open with "no", "don't", "stop", "I told you",
     "why did you"…
   Rejections are also no longer counted as failed tool calls, since you
   chose them.
2. **Group and write rules (one call, opt-in).** The model groups corrections
   that mean the same thing. It writes a rule only for something you said twice
   or more, or once as a general rule ("never…", "always…"). One-off
   redirections ("also add a chart") are set aside. Each lesson says where it
   belongs: your **personal** `~/.claude/CLAUDE.md` when it's about your
   machine or working style, else the repo's `CLAUDE.md`. Each also says when
   you already have the rule (then Claude isn't following it; a hook can fix
   that) or when **an existing rule says the opposite**, which is the likeliest
   reason Claude keeps doing it.
3. **Add what you pick.** `--add-rules 1,3` appends those rules under one
   "Learned from corrections" heading in each target file and skips any already
   there. `--add-to repo|local|personal` overrides where they go (`local` is
   your uncommitted `CLAUDE.local.md`). It never commits.

The default model is `--model` (Opus 5.5). One call over a month of
corrections costs about $0.25.

## Privacy

Transcripts hold code, commands and sometimes secrets, so context-lens treats
them that way.

- **`doctor` without `--rules` is local only.** It reads files on disk and
  sends nothing anywhere (except `count_tokens` calls in exact mode, which carry
  your instruction files, not transcripts). The report says
  "Local only: nothing left this machine."
- **`--learn` sends your corrections, not transcripts:** your words when you
  corrected Claude (capped at 400 characters each), the one-line actions just
  before each (200 characters each), and the titles and first words of your
  existing rules. It sends at most 150 corrections, all redacted as below.
- **`--rules` sends short excerpts, not transcripts.** Per sampled session: your
  prompts and one line per tool call (the command, the file path, the search
  pattern), each line capped at 300 characters, the session capped at 12,000.
  Never sent: tool output, file contents, diffs, or Claude's replies.
- **Secrets are redacted before anything is sent:** API keys (Anthropic,
  OpenAI, GitHub, AWS, Slack, Stripe, and others), JWTs and bearer tokens,
  passwords in URLs and `NAME=value` assignments, private keys, email
  addresses, long random strings, and your home directory path. Redaction is a
  best-effort filter, so:
- **You can read every byte first.** `--rules-dry-run` and `--learn-dry-run`
  write the exact requests to `.context-lens/` and send nothing.
- **The report records what was sent:** how many excerpts, how many
  characters, to which model, through which backend.

## Shrink CLAUDE.md: `--compact`

```sh
context-lens doctor --compact                          # proposes, writes nothing into the repo
context-lens doctor --compact --backend claude-code    # bill the model calls to your Claude subscription
context-lens doctor --apply                            # write the saved proposal into the repo
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

Output goes to `.context-lens/compact/`: the proposed files under `files/`,
`plan.json` (every decision, editable) and `report.md` (before/after tokens,
coverage, every decision with its reason). `--apply` and `--apply-local`
re-assemble from `plan.json` with no model calls, so your edits to it are
picked up. `--apply` writes the files into the repo; review the diff in git.

**Backends.** `--backend api` uses the Anthropic SDK and `ANTHROPIC_API_KEY`
(install the `exact` extra). `--backend claude-code` runs headless `claude -p`
from an empty directory with settings, MCP servers, skills and tools off, so
each call carries about 1k tokens of overhead and bills to your subscription.
`auto` (the default) picks the API when a key is set, else Claude Code.

### Try it in your checkout only

Not ready to change the `CLAUDE.md` your team pulls? `--apply-local` switches
just your checkout to the slim version and leaves the committed file alone:

```sh
context-lens doctor --apply-local    # this checkout loads the slim version from the next session
context-lens doctor --local-status   # on/off, and whether a pull has made the slim copy stale
context-lens doctor --undo-local     # back to the committed CLAUDE.md, every local file removed
```

The slim root goes in `CLAUDE.local.md`, the rule files in
`.claude/rules/context-lens-local/`, and `.claude/settings.local.json` gets a
`claudeMdExcludes` entry so Claude Code skips the long original. All of them
are listed in `.git/info/exclude`, so `git status` stays clean and nothing can
be committed by accident. The original keeps updating with every pull; when it
changes, `--local-status` exits 1, the checkup flags it as a fix, and the
plugin's SessionStart hook tells the session to read the original for newer
rules until you re-make it.
`doctor` honours `claudeMdExcludes`, so it measures what you actually load.

## Prove it still works: `--verify`

```sh
context-lens doctor --verify quick   # 10 probes × 1 run, ~20 sessions, ~10 min
context-lens doctor --verify full    # 30 probes × 3 runs, ~180 sessions, ~1 hour
context-lens doctor --verify-report  # rebuild the report from saved or partial results
```

Run it before committing an `--apply`: it compares the committed `CLAUDE.md`
against the proposal.

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
across variants and lists every failure with the files the session read, and
opens in your browser like the checkup. Results are saved as each session
finishes, so a stopped run keeps what it paid for. `--anonymize` on
`--verify-report` drops every rule's text for a chart you can share.

## Roadmap

- [x] One command, `doctor`: a checkup across startup context, wasted work,
      tools and MCP, session health and rule adherence, with fixes ranked and a
      visual report (replaced `profile`, `compact`, `eval` and `local`)
- [x] `--compact`: propose a slim root `CLAUDE.md` plus on-demand rule files and
      doc pointers, with a coverage check that every original rule lands somewhere
- [x] `--learn`: turn the corrections you repeat into rules, and flag
      instructions that contradict them
- [x] `--verify`: run rule-probing tasks headlessly under both versions and compare
      startup tokens, cost and rule pass rate
- [x] Claude Code plugin wrapping the CLI

## License

MIT
