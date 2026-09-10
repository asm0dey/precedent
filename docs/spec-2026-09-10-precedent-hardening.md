# precedent hardening — spec

Settled 2026-09-10 in a grilling session. Everything below was decided
explicitly; the reasoning for the five largest lives in `docs/adr/`.
Vocabulary is defined in `CONTEXT.md`.

Three subsystems, three plans. This spec covers all three; the plan in
`docs/superpowers/plans/2026-09-10-precedent-cli-hardening.md` implements
subsystem A only.

## Global constraints

- Python `>=3.12`. One file, PEP 723 inline metadata, no durable venv
  (recorded precedent: `keep-uv-run-pep-723-no-durable-venv`).
- Dependencies: `grafeo`, `filelock`. Nothing else.
- Must run on Linux, macOS and Windows.
- `precedent.py selftest` is the only test entry point, and must still end
  with the graph's node count unchanged.
- `journal.jsonl` is append-only. Never edit or reorder lines.

## A. CLI hardening

### A1 — Show, don't decide

- Delete `close_matches`, `refuse_drift`, `asserted_distinct`, the
  `--new-tag` flag, `tags_distinct` emission, and `maintain`'s pair scan.
- `replay_entry` keeps its `tags_distinct` no-op branch so existing
  journals replay.
- Exact string equality stays for **matching** (resolving a lookup).
  **Detecting** that two different words mean the same thing moves to the
  model, which gets the vocabulary printed to it.
- `maintain` keeps a strict check: normalise case, `-`, `_` and trailing
  `s`, then compare exactly. That is identity after trivial normalisation,
  not a similarity guess.
- A zero-hit `check --topic X` prints the topic vocabulary. `clear` is
  never printed when the topic was never seen.

### A2 — Verdict correctness

- `check`'s `revived` query filters `d.status='active'`. A regretted
  decision must stop firing `CONFLICT`.
- New `LESSON` verdict: `--chose` matching a `Lesson.instead` reports the
  regret that recommends it.
- The norm prints its newest decision date beside the project count.
  Grafeo's confirmed function list has no `max()`; use `collect()` and take
  the max in Python.
- `maintain`'s contradiction check groups by decision id, not
  project+topic, so one decision choosing two options is not a clash.
- `apply_tag_merge` has a dead `s.q(...) if False else None` line. Remove.
- `check`'s `ack` must match the acknowledged divergence for *that norm*,
  not any `despite` on the topic in this project.
- `Principle` gains an `ABOUT` edge to `Topic`; `check` matches on the edge
  instead of `pr.statement CONTAINS $topic`.

### A3 — Identity

- A Project is identified by its **portable id**: the git remote, plus a
  `#`-prefixed subpath for a module inside it. Absent when there is no
  remote; never guessed from a home-relative path.
- `Project.id` remains the native path of first sighting. `Project.paths`
  collects every native path seen. Reads *and* writes resolve through the
  portable id.
- `portable` is computed lazily on `brief` and `record` and backfilled onto
  existing nodes. `brief` may now write to an existing node; it must still
  never create one.
- Liveness checks every path in `Project.paths` and reports a third state,
  "not on this machine", distinct from gone.

### A4 — Portability

- `filelock` replaces `fcntl`. `import fcntl` at module level currently
  makes `--help` fail on Windows.
- A pointer file replaces the symlink in `relocate`/`cmd_init`, on every
  platform.
- Every journal line carries `"v": 1`. `rebuild` refuses — stops, does not
  skip — a line whose version exceeds what it knows, naming the entry.

## B. Packaging and distribution

- Layout: `scripts/precedent.py` stays. Add `adapters/claude/` and move
  `SKILL.md`, `commands/`, `hooks/` into it.
- `precedent.py standing-orders` prints the banner once; every adapter
  shells out to it.
- `.ps1` twin of the session-start hook, plus a shell-pinned `hooks.json`
  per platform.
- Ship both manifests. `agent-plugin.yaml` (ACR) is the source;
  `plugin.json` (Claude Code) is generated from it.
- GitHub Actions matrix — ubuntu, macos, windows — running `selftest`.
- Reframe README/SKILL from "a Claude Code skill" to a vendor-neutral
  decision store.
- Document that the lock is local and a synced store is not.

## C. Measurement

- Capture evals: assert `record` ran with a rationale traceable to the
  user's own words, graded independently.
- A negative case: nothing settled in the conversation, nothing recorded.
- Codex priming measured by running eval 0 against a Codex session with the
  same grader. Nonce test only if that fails.
- If Codex's session-start hook does not reach the model: static
  `AGENTS.md` / Cursor rules carrying the standing orders. No cached brief
  — a stale brief is a graph that lies.
