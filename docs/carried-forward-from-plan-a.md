# Carried forward from the CLI hardening plan (subsystem A)

Written 2026-09-10 when plan A completed. Every item below was found by a review,
triaged as ship-it or parked, and deliberately not fixed. Plan B should triage them
again — several are cheapest to fix alongside work it already does.

## Parked with rulings

Ruling 27: PARKED — `cmd_maintain` now holds its read lock across up to one `git` subprocess per
portable-less project. Each call is bounded at 5s but N of them are not, and a concurrent WRITER
queues behind it up to the 30s acquire bound. Real, but `maintain` is a manual command never on the
hook path, readers still share, and this store has a handful of projects. Cost if wrong: on a large
pre-branch store on a slow filesystem, a `record` run concurrently with `maintain` fails with the
lock-timeout SystemExit instead of waiting. Visible and actionable when it happens.

Ruling 28: PARKED — `selftest` run WITHOUT `--home` now enumerates every Project node and shells out
to `git remote get-url` in the user's real project directories. Read-only, no graph or filesystem
mutation, node count balances including on the failure path. Cost if wrong: selftest gets slower in
proportion to project count and touches directories a self-test arguably should not. Worth revisiting
if selftest ever runs unattended against a real store — but Ruling 19 means it never has here.

Ruling 29: PARKED — `_check_decisions` still has no `try`/`finally`, the exact shape of fix-wave
finding 3 in a sibling function. Not named in the fix dispatch, found by the re-reviewer. Real: a
failing assert mid-check leaks its nodes and the NEXT run reports the node-count failure against the
wrong check. Parked rather than fixed because the process allows one fix wave and one re-review, and
adding a second wave for a test-hygiene gap with no dependent work would be the controller
re-litigating its own process. Cheapest possible follow-up; belongs in plan B alongside the CI matrix.


## Deferred minors, by task

- Task 1: minor (deferred): `_check_detect_project`/`_check_decisions` now rmdir /tmp/precedent-selftest-proj,
  which the pre-refactor selftest never did — a small behaviour change beyond pure code motion, net-positive.
- Task 1: minor (deferred): `import shutil as _sh` now duplicated as a local import in two checks.

Ruling 10 (Task 2): `_check_store(s)` must not release/reacquire the Store's own lock.
- Task 2: minor (deferred): if `__enter__`'s `GrafeoDB(...)` raises after the lock is acquired,
  `__exit__` never runs and the release is left to process exit. Harmless in a single-shot CLI;
  a longer-lived Store caller would need try/finally. Pre-existing shape, not introduced here.
- Task 2: minor (deferred): `raise SystemExit(...)` inside `except filelock.Timeout` chains the
  original traceback; `from None` would give the CLI user one clean message.
- Task 2: minor (deferred): `_check_store` re-imports `filelock` already imported at module scope.
- Task 2: minor (deferred): `holder.stdout.readline()` has no timeout — a holder that never prints
  `held` hangs the check instead of failing. Plan-mandated (brief-verbatim block).

- Task 2b: minor (deferred): `_check_lock_modes` introspects argparse privates (`p._actions`,
  `_SubParsersAction`, `sp.get_default`). No public API enumerates subparsers; same acceptance as
  filelock's `_current_mode`. Flagged for awareness only.
- Task 3: minor (deferred): `_ensure_removable` inspects directories via `path.iterdir()` after an
  `os.access(W_OK)` check, so a directory that is writable but NOT readable raises an uncaught
  PermissionError instead of the clean entry-naming SystemExit. Outside the root-owned-directory
- Task 3: minor (deferred): findings 1, 3 and 4 (.lock prefix filter, `--home` refusal guard,
  removability pre-check) were verified only by ad-hoc runs, not durable selftest assertions. Only
  2, 5, 6, 7 landed as persisted asserts. Deliberately deferred rather than taking a third round:
- Task 5: minor (deferred): `cmd_tag`'s docstring and the `--merge` branch comment were reworded
  beyond Step 4's verbatim text — justified (they referenced the deleted `refuse_drift`/`--new-tag`
  and would have been actively misleading), but it is why insertions came in at 54 against the
- Task 6: minor (deferred): loop variable `r` reused across two sequential non-nested loops in
  `cmd_check` (:650 and :664).

- Task 7: minor (deferred): the `[r for r in endorsed if r["why"]]` guard is live only for `--because ""`;
  inherited verbatim from the pre-existing `regrets` query, not introduced here.
- Task 7: minor (deferred): test cleanup deletes both Lessons explicitly, then a `STARTS WITH 'selftest-v'`
  sweep that would have caught them anyway. Harmless redundancy.
- Task 8: complete (commits d5dd476..3842b38, review clean, no fix round). Both print branches updated
- Task 8: minor (deferred): `last[:7]` assumes `created` is an ISO date. Every current write path stamps
  it via `today()`, but `write_decision`'s `d.get("created", ...)` override would let a future writer
  pass a non-ISO string and the slice would silently truncate to nonsense rather than fail.
- Task 9: minor (deferred): a decision with zero chosen options never appears in `contradictions_in`
  because the MATCH requires a CHOSE edge. Pre-existing — the pre-change query had the same
  requirement — not introduced here.

## Known unverified

- Ruling 21's macOS path fix is correct by construction but has NEVER executed on macOS.
- NOTHING in plan A has run on Windows, which was its headline goal. filelock replacing
  the POSIX lock module, the pointer file replacing the symlink, and as_posix() in the
  portable id are all reasoned, not executed. The CI matrix in plan B is what closes this.
- The selftest harness itself was POSIX-only until the final fix wave; a Windows CI run
  would have failed on fixtures before reaching any product code.
