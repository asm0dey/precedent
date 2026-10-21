# Principles in every session — design

Settled 2026-10-21 in a brainstorming session. Vocabulary is defined in
`CONTEXT.md`.

## Intent

The user keeps seeing agents fail in the same systematic ways, whatever the
project. Two examples prompted this:

- `pgrep -f` / `pkill -f` match the polling shell's own command line, so a
  `while pgrep -f X` loop never exits.
- `rm`, `mv`, `cp` may be aliased to `-i`; the y/n prompt is invisible in a
  non-interactive shell, so the call stalls or skips the overwrite.

Today these live in `~/.claude/rules/shell-safety.md`. That is not good enough:
it is not systematic, it is outside precedent's control, agents still ignore
it sometimes, and agents other than Claude Code never see it. With precedent in
place there is no need to add anything to `~/.claude/rules/`; nothing in this
feature writes there or reads from there.

Precedent already has the right entry: a **principle**. One recorded without
`--topic` is a global instruction (`cmd_principle` allows it), and re-running
`principle --id X` with new wording replaces the statement under the same id.
What is wrong is where principles are shown:

- They print in an informational section of the brief (`== standing
  principles ==`), where they read as context rather than orders.
- They print only for a tagged project. In an empty directory the brief is
  empty, both session-start hooks exit early, and the agent gets nothing at
  all from precedent.

## Goals

- Every principle (topic or topicless) reaches the agent in every session, in
  every directory, as part of the **standing orders**.
- Sessions that never run the session-start hook still get them.
- The user can retire a principle.

## Non-goals

- A new entry type, new `rule` commands, or a separate `why` field. Principles
  carry their reason in the statement.
- The agent proposing principles unprompted for agent-behaviour failures. The
  user adds them: by typing the command, or by asking the agent in a sentence,
  in which case the agent drafts it, shows it, and runs it on confirmation.
- Importing `~/.claude/rules/`. The two shell-safety principles are added by
  hand after this ships; deleting the file is the user's call.
- Re-delivering principles on the mid-session re-prime in `register.ts`. They
  are already in context from session start.

## Anti-goals

- An empty directory still getting nothing.
- Principles silently missing because the store could not be read.
- `standing-orders` creating the store directory as a side effect of printing
  (the reason it runs without a `Store` today — see its docstring).

## Constraints

Unchanged: PEP 723 + `uv run`, no new dependencies, Linux/macOS/Windows,
`selftest` is the test entry point, `journal.jsonl` append-only, `SCHEMA` stays
`1` (new journal ops are additive), `rebuild` must reproduce the graph from the
journal.

## 1. Principles move into standing orders

- `standing-orders` prints the existing banner, then every active principle as
  a further bullet of the same list, statement followed by `#<id>`.
- The `== standing principles ==` section is removed from the brief
  (`_print_lessons_and_principles` keeps printing lessons).
- `standing-orders` reads the store only if it already exists, read-only. No
  store: banner alone, as today, and nothing is created.
- Store exists but cannot be read (previous engine, unopenable `graph.db`,
  newer journal schema, missing `graphdblite`): banner plus a visible line
  `principles could not be read: <reason>`, exit 0. The brief survives and the
  gap is not silent. *(Builder's call, settled during review.)*
- `check`'s "PRINCIPLE in play" line is unchanged.

## 2. Hooks print standing orders even with an empty brief

Both `session-start.sh` and `session-start.ps1`:

- non-empty brief: as today — brief, then standing orders (now with
  principles).
- empty brief: standing orders alone, under the same `PRECEDENT` header line.
- every other early exit (no CLI, no `uv`, CLI failure) stays as today.
- The statusline marker keeps its meaning: written only when the brief was
  non-empty. An orders-only session does not light the badge.

## 3. Sessions without the hook

Some sessions never run the hook: a manual install with no hook wired, a
machine without `uv`, possibly Codex or Cursor through ACR (not verified).

- The precedent skill's frontmatter `description` (always in context, unlike
  the skill body) gains a trigger: when no `PRECEDENT` block is in context,
  run `standing-orders` before the first shell command and follow what it
  prints.
- Principles themselves are not put in the description: it is static shipped
  text, and principles live in the user's store.
- The description stays within the harness's length limit and keeps its
  existing triggering phrases; the ACR manifest's description, generated from
  the same source, carries the same trigger.

## 4. Retiring a principle

- `principle --id <id> --retire --because "<why>"` marks the principle
  retired. `--because` is required with `--retire` (same flag name as
  `regret`); `--statement` is not.
- The reason is stored on the node and in the journal entry. A principle
  dropped without a reason leaves no way to tell, when the pattern comes back,
  whether it was dropped on purpose.
- Logged as its own journal entry and handled in `replay.py`, so `rebuild`
  reproduces it, reason included.
- A retired principle is not deleted. It stops printing in standing orders,
  stops appearing in `check`, and is still counted as known by `suggest`, so
  it is not offered again as a promotion candidate.
- Re-recording a retired id with `principle --id <id> --statement ...` makes
  it active again.

## 5. Tests and docs

- `selftest`: `standing-orders` with no store prints the banner and creates
  nothing; with a store, prints active principles and omits retired ones;
  with an unreadable store, prints the visible line and exits 0; `--retire`
  without `--because` is refused; retire
  survives `rebuild`; retired principles are absent from `check`; the brief
  no longer prints `== standing principles ==`.
- Hook behaviour with an empty brief is covered wherever the hooks are
  already tested (`evals/`, `tests/`); if they are not covered today, a check
  that the `.sh` hook emits the orders in an empty directory.
- `CONTEXT.md`: update **Principle** and **Standing orders** — principles are
  now part of the standing orders, and a topicless principle is a global
  agent instruction.
- `SKILL.md` and `README.md`: document `--retire` and where principles now
  appear.

## Left to the builder

- Exact output wording, and the journal op name for retiring.
- How `status` is stored on the `Principle` node (existing principles have
  none and count as active).
