# Principles in Every Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every active principle reaches the agent as part of the standing orders in every session and every directory, and a principle can be retired with a reason.

**Architecture:** `standing-orders` gains a read-only, never-creating look into the store and appends active principles to its banner; the brief stops printing them. Both session-start hooks emit the orders even when the brief is empty. The `precedent` skill description tells hook-less sessions to run `standing-orders`. Retirement is a new journal op on the existing `Principle` node.

**Tech Stack:** Python (PEP 723, `uv run`), graphdblite (openCypher), bash + PowerShell hooks.

**Spec:** `docs/superpowers/specs/2026-10-21-global-principles-design.md`

`P` below = `adapters/claude/skills/precedent/scripts/precedent_cli`.
Selftest: `uv run adapters/claude/skills/precedent/scripts/precedent.py --home "$(mktemp -d)/s" selftest` (never the default home — see `.github/workflows/test.yml:47`).

## Global Constraints

- PEP 723 + `uv run`; no new dependencies.
- Must run on Linux, macOS and Windows; CI runs selftest and the hook tests on all three.
- `journal.jsonl` is append-only; `SCHEMA` stays `1` — the retire op is additive.
- `rebuild` must reproduce the graph from the journal.
- `standing-orders` must never create the store directory.
- Nothing reads or writes `~/.claude/rules/`.

## Review Focus

1. **Retiring an id that does not exist** (typo) — expected: refused with an error naming the id, nothing journalled. Test in Task 1.
2. **Pointer file broken / store relocated** — `resolve_home` raises `SystemExit` on a bad pointer; `standing-orders` must report it as the visible "could not be read" line, not crash the hook. Test in Task 2 (unreadable-store case covers the same handler; add a bad-pointer case).
3. **Store directory exists but has no `graph.db`** (journal only, or empty dir) — expected: banner alone, nothing created. Test in Task 2.
4. **Hook in a directory with no store at all** (fresh user) — expected: standing orders printed, no store created, no statusline marker. Test in Task 3.
5. **Retire then re-record the same id** — expected: active again, prints again, reason cleared. Test in Task 1.

---

### Task 1: Retire a principle

**Files:**
- Modify: `P/writing.py` (`cmd_principle`, ~line 287)
- Modify: `P/cli.py:88-93` (principle subparser)
- Modify: `P/replay.py:150-171` (`_replay_principle`, new op in `_REPLAY_OPS`)
- Modify: `P/reading.py:327` (`check`'s principle query)
- Test: `P/selftest/checks_graph.py` (new `_check_principle_retire`), registered in `P/selftest/__init__.py`

**Interfaces:**
- Produces: `Principle` node fields `retired` (date string, or null when active) and `retired_because` (string or null). **Active ⇔ `pr.retired IS NULL`.** Task 2 relies on this predicate.
- Produces: journal op `"principle_retire"` with payload `{"id": str, "because": str}`.

- [ ] **Step 1: Write the failing test** `_check_principle_retire(s: Store)` in `checks_graph.py`, using id `selftest-retire` and calling `cmd_principle` with an `argparse.Namespace` (as the existing principle check builds its args). Assertions:
  - record `--statement "x"` → node exists with `retired` null.
  - retire with `because="obsolete"` → `MATCH (pr:Principle {id:'selftest-retire'}) RETURN pr.retired AS r, pr.retired_because AS b` gives non-null `r`, `b == "obsolete"`.
  - retire with `because=""` → `SystemExit`; retire of id `selftest-retire-missing` → `SystemExit` whose message contains that id.
  - `replay_entry(s, {"op": "principle_retire", "id": "selftest-retire", "because": "again"})` sets `retired_because == "again"` (the replay path, without `rebuild`).
  - `cmd_check` on a topic the principle is linked to prints no `PRINCIPLE in play` line while retired.
  - re-record with `--statement "y"` → `retired` and `retired_because` are null again.
  - cleanup: `DETACH DELETE` every node with id starting `selftest-retire`, and any topic it created.

- [ ] **Step 2: Run selftest; expect FAIL** (`--retire` unknown / `principle_retire` not replayed).

- [ ] **Step 3: Implement**
  - `cli.py`: `--statement` becomes `default=""`; add `--retire` (`store_true`) and `--because` (`default=""`, help "why it no longer holds — required with --retire").
  - `cmd_principle`: when `a.retire`: refuse (`SystemExit("error: ...")`) if `a.because` is blank or no `Principle {id}` exists; else `s.log("principle_retire", {"id", "because"})`, apply, print `principle <id> retired: <because>`. When not retiring: refuse blank `--statement`; the existing `SET` also sets `pr.retired=null, pr.retired_because=null`.
  - `replay.py`: `_replay_principle` clears the same two fields; new `_replay_principle_retire` sets `retired=<entry ts date>`, `retired_because=$because`; register under `"principle_retire"`. Share the apply query between `cmd_principle` and replay as `apply_principle_retire(s, id, because, date)` in `writing.py` (the `apply_regret`/`apply_amend` pattern).
  - `reading.py:327`: add `WHERE pr.retired IS NULL` to the governing-principle query. `suggest`'s `known` set (`reading.py:379`) stays unfiltered on purpose.
  - Register `_check_principle_retire(s)` in `cmd_selftest`.

- [ ] **Step 4: Run selftest; expect `selftest ok`.**

- [ ] **Step 5: Commit** `feat: retire a principle with a reason`.

### Task 2: Principles print in standing orders, not the brief

**Files:**
- Modify: `P/admin.py:104-115` (`cmd_standing_orders`)
- Modify: `P/reading.py:99-114` (drop the principles half of `_print_lessons_and_principles`; rename to `_print_lessons`)
- Test: `P/selftest/checks_store.py` (`_check_standing_orders`, ~line 600)

**Interfaces:**
- Consumes: active predicate `pr.retired IS NULL` (Task 1).
- Produces: `standing-orders` output = banner, then one line per active principle, `- <statement>  #<id>`, ordered by `pr.created`; or, when the store cannot be read, banner then `- principles could not be read: <first line of the error>`. Exit 0 in every case. Task 3's hook test greps for the principle statement in this output.

- [ ] **Step 1: Write the failing tests** in `_check_standing_orders` (each in its own `tempfile.TemporaryDirectory()`, passing `argparse.Namespace(home=str(path))`):
  - `cmd_standing_orders(None)` still works and keeps every existing assertion.
  - home that does not exist → output equals the bare banner; `not path.exists()` afterwards.
  - home that exists but has no `graph.db` → bare banner; no `graph.db` created.
  - store with active principle `selftest-so-a` ("Never pgrep -f in a poll loop.") and retired `selftest-so-r` ("Retired text.") → output contains the first, not the second, and the line `- Never pgrep -f in a poll loop.  #selftest-so-a`.
  - `graph.db` containing junk bytes (`b"not a database"`) → output contains `principles could not be read:`; no exception escapes.
  - home holding a `location` pointer file with empty content → same visible line.
  - a brief for a tagged temp project no longer contains `== standing principles ==`.

- [ ] **Step 2: Run selftest; expect FAIL.**

- [ ] **Step 3: Implement**
  - `cmd_standing_orders(a)`: `home = pathlib.Path(a.home) if a else HOME`. Print the banner first. Then: `resolve_home(home)`; if `<resolved>/graph.db` is not a file, stop. Else open `Store(resolved, write=False)` and query `MATCH (pr:Principle) WHERE pr.retired IS NULL RETURN pr.id AS id, pr.statement AS stmt ORDER BY pr.created`. Wrap resolve + open + query in one `except (SystemExit, Exception) as exc` that prints the visible line; one comment saying why a hook-facing command must not fail. Update the docstring: it now reads an existing store read-only, still never creates one.
  - Check that `Store(write=False)` on an existing store creates nothing new; `Store.__init__` calls `home.mkdir(exist_ok=True)`, harmless only because the graph-exists check runs first — keep that order.
  - `reading.py`: remove the principle query and its section; update the caller at `reading.py:166`.

- [ ] **Step 4: Run selftest; expect `selftest ok`.**

- [ ] **Step 5: Commit** `feat: principles print in standing orders`.

### Task 3: Hooks emit standing orders when the brief is empty

**Files:**
- Modify: `adapters/claude/hooks/session-start.sh:44-75`
- Modify: `adapters/claude/hooks/session-start.ps1:48-90`
- Test: `tests/hook-finds-cli.sh`, `.github/workflows/test.yml` (Windows hook step)

**Interfaces:**
- Consumes: `standing-orders` output (Task 2).

- [ ] **Step 1: Write the failing test** — in `tests/hook-finds-cli.sh`, after the seeding block, record a topicless principle: `principle --id hook-test-p --statement "HOOK-TEST-PRINCIPLE"`. Add a case: run the repository-layout hook from `$WORK/empty` (a new empty directory, no decisions). Require: output contains `PRECEDENT` and `HOOK-TEST-PRINCIPLE`; no marker file at `$CLAUDE_CONFIG_DIR/.precedent-primed/<mangled $WORK/empty>` (same mangling as `session-start.sh:22`). Add a second case with `PRECEDENT_HOME="$WORK/nostore"`: output contains `PRECEDENT`, and `$WORK/nostore` does not exist afterwards. Existing layout cases must also show `HOOK-TEST-PRINCIPLE`.

- [ ] **Step 2: Run `bash tests/hook-finds-cli.sh`; expect FAIL** on the empty-directory case.

- [ ] **Step 3: Implement**
  - `session-start.sh`: replace `[[ -n "$BRIEF" ]] || exit 0` with: write the marker only when `$BRIEF` is non-empty; always fetch `$ORDERS`; in the heredoc, print the `$BRIEF` paragraph only when non-empty (no blank double gap). Update the top-of-file "Silent unless…" comment.
  - `session-start.ps1`: the same three changes.
  - `test.yml` Windows step: after the existing assertion, run the hook from a new empty temp directory and `throw` unless the output matches `PRECEDENT`.

- [ ] **Step 4: Run `bash tests/hook-finds-cli.sh` and `bash tests/statusline-badge.sh`; expect both pass.**

- [ ] **Step 5: Commit** `feat: hooks print standing orders in every directory`.

### Task 4: Hook-less trigger and docs

**Files:**
- Modify: `adapters/claude/skills/precedent/SKILL.md` (frontmatter `description`, line 3; "Standing orders" section ~line 60)
- Modify: `CONTEXT.md:70-72` (**Principle**), `CONTEXT.md:155-157` (**Standing orders**)
- Modify: `README.md` (where `principle` is documented; add `--retire --because`)
- Test: `P/selftest/checks_store.py` (`_check_standing_orders`)

- [ ] **Step 1: Write the failing test** in `_check_standing_orders`: read `CLI.parent.parent / "SKILL.md"`, take the `description:` frontmatter value, and assert `len(desc) <= 1024` and `"standing-orders" in desc`.

- [ ] **Step 2: Run selftest; expect FAIL.**

- [ ] **Step 3: Implement** — replace the description with this exact text (933 chars; it keeps every existing trigger):

  > Remember and reuse the user's architectural, business, and tooling decisions across every project, in a queryable graph. Use whenever the user settles a non-trivial choice (database, framework, auth, deployment, pricing, process) so it gets recorded; starts work in a project you have not briefed on, including one whose decisions are only visible in its manifests, CI and docs; weighs options or asks "which should I use" or "what did I do last time"; says a past choice was a mistake, bit them, or they would not repeat it; knowingly departs from how they usually do it; or asks what they already decided, whether a choice is consistent with their other projects, or what decisions they still owe this project. Trigger it even when the user does not mention decisions or the graph. If no PRECEDENT block is in context, run this skill's scripts/precedent.py standing-orders before your first shell command and follow what it prints.

  Then the docs:
  - `SKILL.md` "Standing orders" section: principles now print as part of them; topicless principles are global agent instructions the user adds by asking (draft, show, run on confirmation — never offered unprompted); document `principle --id X --retire --because "..."`.
  - `CONTEXT.md` **Principle**: also covers a topicless, user-added agent instruction; principles are delivered in the standing orders; retired principles stay in the graph. **Standing orders**: now include every active principle.
  - `README.md`: `--retire --because`.
  - ACR needs no manifest change: skill descriptions ship inside `SKILL.md`, which `agent-plugin.yaml` already lists. Run `uv run --locked --no-build scripts/gen-plugin-json.py --check` to confirm.

- [ ] **Step 4: Run selftest and the manifest check; expect both pass.**

- [ ] **Step 5: Commit** `docs: principles in standing orders; hook-less trigger`.
