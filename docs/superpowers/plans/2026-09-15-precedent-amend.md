# `precedent amend` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `precedent amend <id>`, a third write op that fixes the *wording* of a decision already recorded, distinct from `record --supersedes`, which models a decision that *changed*.

**Architecture:** A new append-only journal op, `{"op":"amend","id":...}`, carrying only the fields being reworded. `replay_entry` folds it onto the existing `Decision` node with a `MATCH ... SET` — never a `MERGE`, so an amendment can never fabricate a decision. Append-only survives because the amendment is itself an appended fact. The decision id never changes.

**Tech Stack:** Python ≥3.12, `graphdblite`, `uv run` with PEP 723 inline metadata, `argparse`. Tests are `_check_*` functions inside `scripts/precedent.py`, run by `precedent.py selftest` — there is no pytest in this repo.

**Spec:** [github.com/asm0dey/precedent#10](https://github.com/asm0dey/precedent/issues/10) — "No way to amend a recorded decision's wording". The one design question it leaves open (what happens to the title slug embedded in the id) was settled before this plan was written; see Global Constraints.

## Global Constraints

Every task's requirements implicitly include this section.

- **`SCHEMA` stays `1`.** Do not bump it. `replay_entry` refuses any line whose `v` exceeds `SCHEMA` by raising `JournalTooNew`, which stops the whole replay at that line. Bumping to 2 would make an older precedent refuse *every* line written afterwards, plain `record` lines included. Leaving it at 1 means an older precedent hits the unknown op `amend`, raises `ValueError`, and `replay_journal` reports it as one skipped line and keeps going — visible, loud, and scoped to the one entry it cannot read.
- **The decision id never changes, stale slug and all.** The id is identity; the slug inside it is an accident of how it was minted. Same split as `Project.portable` (identity) versus `Project.id` (a native path that is only an alias) — `docs/adr/0002`. Anything already quoting the old id keeps resolving: a `SUPERSEDES` edge, a rule file, a commit message.
- **Amendable fields are exactly `title`, `statement`, `rationale`.** `topic`, `chose`, `rejected`, `scope` and the project are *what was decided*, not how it was described; changing one of those is a different decision, which is `record --supersedes`.
- **An absent field means "unchanged", never "cleared".** `amend --title` must not blank a rationale.
- **Journal stays append-only.** No line is ever edited or reordered. The amendment is a new line.
- **Matching is exact string equality** everywhere (`docs/adr/0001`). No fuzzy id resolution — if the id is wrong, say so and exit non-zero.
- **The id is a `--id` flag, not a positional.** Issue #10 sketches `amend <id>`; every other command in this CLI takes its id as `--id` (`record`, `regret`, `principle`), and matching the file it lives in beats matching the sketch. Do not "fix" this back.
- **One dependency.** `graphdblite`, declared in the PEP 723 header. Do not add another.
- **Windows is supported** (`#support-windows-not-posix-only-1789027385`). No POSIX-only paths, no `/tmp` literals — use `tempfile`.
- **Cypher is built from a whitelist, never from user input.** The only f-string interpolation into a query is field *names* drawn from the `AMENDABLE` tuple; every *value* goes through a parameter.

---

### Task 1: The graph fold — `apply_amend` and its replay branch

The op that folds an amendment onto an existing decision, plus the replay branch that makes it survive a `rebuild`. No CLI surface yet — this task is testable on its own through `selftest`.

**Files:**
- Modify: `scripts/precedent.py` — add `AMENDABLE` beside `SCOPES` (~line 43), add `apply_amend` after `apply_regret` (~line 819), add the `amend` branch to `replay_entry` (~line 1562, beside the `regret` branch), add `_check_amend` after `_check_journal` (~line 2311), register it in `cmd_selftest` (~line 2880)
- Test: `scripts/precedent.py::_check_amend`, run by `uv run scripts/precedent.py selftest`

**Interfaces:**
- Consumes: `Store.q`, `write_decision`, `replay_entry`, `today()`, `SCHEMA` — all already in the file.
- Produces:
  - `AMENDABLE: tuple[str, ...]` = `("title", "statement", "rationale")` — Task 2 iterates it to build both the parser flags and the payload.
  - `apply_amend(s: Store, e: dict) -> None` — `e` carries `"id"` plus any subset of `AMENDABLE`. Task 2 calls it after journalling.
  - `_check_amend(s: Store) -> None`.

- [ ] **Step 1: Write the failing test**

Add after `_check_journal` (which ends with the `selftest-noversion` cleanup, ~line 2311):

```python
def _check_amend(s: Store) -> None:
    """An amendment must rewrite the words and nothing else.

    `check` prints the title first, so a misleading title is the expensive
    error — it is the part that gets quoted back as precedent. What makes
    amending safe to reach for instead of superseding is that it cannot
    touch what was decided: an amendment able to move a CHOSE edge would be
    a silent rewrite of history in the one tool whose value is that history
    is not rewritten.
    """
    d = {"id": "selftest-amend", "title": "narrow title", "statement": "s",
         "rationale": "r", "scope": "architecture", "created": today(),
         "project_id": "selftest-amend-proj", "project_name": "selftest",
         "tags": [], "topics": ["selftest-amend-topic"],
         "chose": ["selftest-amend-opt"], "rejected": [], "supersedes": []}
    try:
        write_decision(s, d)
        apply_amend(s, {"id": "selftest-amend", "title": "wider title"})
        assert s.q("""MATCH (d:Decision {id:'selftest-amend'})
                      RETURN d.title AS t, d.statement AS st,
                             d.rationale AS r""") \
            == [{"t": "wider title", "st": "s", "r": "r"}], \
            "an absent field means unchanged, never cleared"
        assert s.q("""MATCH (:Decision {id:'selftest-amend'})-[:CHOSE]->(o:Option)
                      RETURN o.name AS n""") == [{"n": "selftest-amend-opt"}], \
            "an amendment must not touch what was decided"
        assert s.q("MATCH (d:Decision {id:'selftest-amend'}) RETURN d.status AS st") \
            == [{"st": "active"}], "an amendment is not a supersession"

        # Replayed from a journal line it must land identically: the graph is
        # rebuilt from these lines and from nothing else, so an op that works
        # only when called directly is an amendment that disappears on the
        # next `rebuild` — and the old wording comes back.
        replay_entry(s, {"op": "amend", "id": "selftest-amend",
                         "rationale": "replayed reason", "v": SCHEMA})
        assert s.q("""MATCH (d:Decision {id:'selftest-amend'})
                      RETURN d.rationale AS r""") == [{"r": "replayed reason"}]

        # An id that is not there must not be invented. A rewording is not a
        # decision, and a bare node carrying only a title would be precedent
        # nobody ever recorded — quoted back with full confidence.
        replay_entry(s, {"op": "amend", "id": "selftest-amend-ghost",
                         "title": "ghost", "v": SCHEMA})
        assert s.q("MATCH (d:Decision {id:'selftest-amend-ghost'}) RETURN d.id AS id") \
            == [], "amend must never create a Decision"

        # An empty amendment is a no-op, not a query with an empty SET clause
        # (which does not parse). cmd_amend refuses this case up front, but a
        # journal line is not under its control.
        apply_amend(s, {"id": "selftest-amend"})
        assert s.q("""MATCH (d:Decision {id:'selftest-amend'})
                      RETURN d.title AS t""") == [{"t": "wider title"}]
    finally:
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-amend' DETACH DELETE n")
        for name in ("selftest-amend-topic", "selftest-amend-opt"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""",
                {"name": name})
```

Register it in `cmd_selftest`, on the line after `_check_journal(s)`:

```python
    _check_journal(s)
    _check_amend(s)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run scripts/precedent.py selftest`
Expected: FAIL with `NameError: name 'apply_amend' is not defined`

- [ ] **Step 3: Write the minimal implementation**

Add beside `SCOPES`, after line 43 (`SCOPES = (...)`):

```python
# The fields an amendment may rewrite: how the decision was DESCRIBED.
# Everything else on a Decision — its topics, its options, its scope, the
# project it belongs to — is WHAT WAS DECIDED, and changing one of those is a
# different decision, which is `record --supersedes`. Also the whitelist that
# makes apply_amend's f-string SET clause safe.
AMENDABLE = ("title", "statement", "rationale")
```

Add after `apply_regret` (which ends with the `MERGE (l)-[:REGRETS]->(d)` block, ~line 819):

```python
def apply_amend(s: Store, e: dict) -> None:
    """Fold an amendment onto the decision it rewords.

    MATCH, never MERGE. An amendment describes a decision that already
    exists; a MERGE would fabricate a bare node carrying nothing but a new
    title whenever the `record` line above it was unreadable — inventing
    precedent out of a rewording, which `check` would then quote with no
    rationale and no options beside it. `project_portable` refuses to create
    a node for the same reason. Journal order guarantees the `record` line
    is replayed first, so a miss here means that line was skipped, and
    `replay_journal` has already reported it.

    Only the keys present are written: an absent key means "unchanged", so
    `amend --title` cannot silently blank a rationale that took a year to
    earn. An amendment with no fields is a no-op rather than an empty SET
    clause, which does not parse.

    The SET clause is interpolated, its values are not: field names come
    from AMENDABLE and nowhere else, every value goes through a parameter.
    """
    fields = {k: e[k] for k in AMENDABLE if k in e}
    if not fields:
        return
    clause = ", ".join(f"d.{k}=${k}" for k in fields)
    s.q(f"MATCH (d:Decision {{id:$id}}) SET {clause}", {"id": e["id"], **fields})
```

Add the replay branch to `replay_entry`, immediately after the `regret` branch (`elif e["op"] == "regret": apply_regret(s, e)`):

```python
    elif e["op"] == "amend":
        apply_amend(s, e)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run scripts/precedent.py selftest`
Expected: PASS — `selftest ok (N nodes, unchanged)`. The unchanged node count is the check that `_check_amend` cleaned up after itself.

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: fold an amendment onto a decision without superseding it"
```

---

### Task 2: The CLI surface — `cmd_amend` and its parser

The subcommand a user reaches. Refuses an unknown id and an empty amendment, prints was/now for every field it changed, and journals before it mutates.

**Files:**
- Modify: `scripts/precedent.py` — add `cmd_amend` after `cmd_record` (~line 771, before `cmd_regret`), add the `amend` subparser in `build_parser` after the `record` block (~line 2937), add `"amend": True` to the `_check_lock_modes` expected table (~line 2262)
- Test: `scripts/precedent.py::_check_lock_modes`, run by `uv run scripts/precedent.py selftest`

**Interfaces:**
- Consumes: `AMENDABLE` and `apply_amend` from Task 1; `Store.log`, `Store.q`.
- Produces: `cmd_amend(a, s: Store) -> None`, wired as `fn` on the `amend` subparser with `writes=True`. The journal line shape is `{"ts":..., "op":"amend", "id":..., <subset of AMENDABLE>, "v":1}`.

- [ ] **Step 1: Write the failing test**

`_check_lock_modes` compares the *whole* parser mapping against a hand-written table, so it is already the failing test for a new subcommand — it just needs the expected entry. Add `"amend": True` to the writers block of the `expected` dict in `_check_lock_modes` (~line 2262):

```python
        # writers
        "record": True, "tag": True, "regret": True, "principle": True,
        "rebuild": True, "selftest": True,
        "amend": True,          # rewords a decision in place
        "brief": True,          # the portable-id backfill mutates
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run scripts/precedent.py selftest`
Expected: FAIL with `command lock modes changed`, the `got` mapping having no `amend` key and the `expected` mapping having one.

- [ ] **Step 3: Write the minimal implementation**

Add after `cmd_record` (which ends with the untagged-project note), before `cmd_regret`:

```python
def cmd_amend(a, s: Store) -> None:
    """Same decision, better words.

    Distinct from supersede on purpose, and the distinction is the whole
    point. `--supersedes` models a decision that CHANGED: it writes a second
    decision, marks the first superseded, and `check` carries both, because
    when you revisit the call in two years the old reasoning is the most
    valuable thing in the graph. A decision that was merely DESCRIBED badly
    has no such history, and superseding one asserts a change that never
    happened — in the field `check` prints first and quotes back.

    Only the words change; see AMENDABLE for why that is the line.

    The id never changes, including the stale title slug inside it. The id is
    identity and the slug within it is an accident of how it was minted —
    the same split as Project.portable against Project.id (docs/adr/0002).
    Anything already holding the old id keeps resolving: a SUPERSEDES edge,
    a rule file, a commit message.

    Journal first, then mutate, exactly as `record` does: a crash between the
    two costs a replay, not the amendment.
    """
    rows = s.q("""MATCH (d:Decision {id:$id})
                  RETURN d.title AS title, d.statement AS statement,
                         d.rationale AS rationale""", {"id": a.id})
    if not rows:
        # Exact matching, like everywhere else (docs/adr/0001). Guessing at a
        # near id here would amend the wrong decision, which is the one
        # outcome worse than amending none.
        print(f"no decision with id {a.id!r} — ids are printed by `check --topic <topic>`"
              f" and by `brief`, after the '#'")
        raise SystemExit(2)
    was = rows[0]
    fields = {k: getattr(a, k) for k in AMENDABLE if getattr(a, k)}
    if not fields:
        print("nothing to amend — pass at least one of "
              + ", ".join(f"--{k}" for k in AMENDABLE))
        raise SystemExit(2)

    payload = {"id": a.id, **fields}
    s.log("amend", payload)
    apply_amend(s, payload)

    print(f"amended {a.id}")
    # Both halves, because the user cannot see the graph and this is a write
    # to the text that gets quoted as their own words. A wrong entry is worse
    # than a missing one, so the confirmation shows what it replaced.
    for k, new in fields.items():
        print(f"  {k}")
        print(f"    was: {was[k] or '-'}")
        print(f"    now: {new}")
```

Add the subparser in `build_parser`, directly after the `record` block (which ends `proj(r); r.set_defaults(writes=True, fn=cmd_record)`):

```python
    am = sub.add_parser("amend",
                        help="reword a decision — same decision, better words")
    am.add_argument("--id", required=True,
                    help="the decision id, as printed by `check` and `brief` after the '#'")
    am.add_argument("--title", default="")
    am.add_argument("--statement", default="")
    am.add_argument("--rationale", default="")
    am.set_defaults(writes=True, fn=cmd_amend)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run scripts/precedent.py selftest`
Expected: PASS — `selftest ok (N nodes, unchanged)`

- [ ] **Step 5: Exercise the command end-to-end against a throwaway store**

The selftest never runs `cmd_amend` itself — it tests the fold beneath it. Prove the CLI path, the refusals and the journal line, in a store that is not the real one:

```bash
export PRECEDENT_HOME=$(mktemp -d)
uv run scripts/precedent.py --home "$PRECEDENT_HOME" record \
  --title "notify4j-core for multi-channel booking notifications" \
  --topic notifications --chose notify4j-core --rationale "one library, many channels" \
  --id amend-demo
uv run scripts/precedent.py --home "$PRECEDENT_HOME" amend --id amend-demo \
  --title "notify4j-core for all outbound third-party delivery"
uv run scripts/precedent.py --home "$PRECEDENT_HOME" check --topic notifications
uv run scripts/precedent.py --home "$PRECEDENT_HOME" rebuild
uv run scripts/precedent.py --home "$PRECEDENT_HOME" check --topic notifications
uv run scripts/precedent.py --home "$PRECEDENT_HOME" amend --id no-such-id --title x; echo "exit=$?"
uv run scripts/precedent.py --home "$PRECEDENT_HOME" amend --id amend-demo; echo "exit=$?"
cat "$PRECEDENT_HOME/journal.jsonl"
```

Expected:
- `amended amend-demo`, then `title` / `was: notify4j-core for multi-channel booking notifications` / `now: notify4j-core for all outbound third-party delivery`
- both `check` runs print the **new** title with the **unchanged** id `#amend-demo`, and the rationale still reads `one library, many channels` — the rebuild is the half that proves the journal carries it
- `no decision with id 'no-such-id'` and `exit=2`
- `nothing to amend — pass at least one of --title, --statement, --rationale` and `exit=2`
- the journal holds two lines, the second `{"ts":...,"op":"amend","id":"amend-demo","title":"notify4j-core for all outbound third-party delivery","v":1}`

Then `unset PRECEDENT_HOME` and delete the temporary directory.

- [ ] **Step 6: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: add \`amend\` for rewording a decision in place"
```

---

### Task 3: Documentation — the op, the term, and when to reach for it

Without this the feature is unreachable: nobody types these commands, so an agent that has not been told amend exists will keep superseding. `references/schema.md` states outright that a new op must be added to it and to `replay_entry` together, so this is a correctness requirement, not a courtesy.

**Files:**
- Modify: `references/schema.md` — the `op` list (~line 139) and the journal-format section
- Modify: `CONTEXT.md` — add **Amendment** to the Decisions vocabulary, after **Supersession** (~line 34)
- Modify: `README.md:216-222` — the "When precedent does not simply apply" table
- Modify: `adapters/claude/skills/precedent/SKILL.md` — the trigger table (~line 89) and the 3a table (~line 233)
- Modify: `docs/setup.md:159-172` — the command table
- Create: `docs/adr/0008-an-amendment-keeps-the-id.md`

**Interfaces:**
- Consumes: the journal line shape and CLI flags settled in Tasks 1 and 2.
- Produces: no code. `docs/adr/0008-an-amendment-keeps-the-id.md` becomes the citable reference for the id decision.

- [ ] **Step 1: Document the op in `references/schema.md`**

Replace the `op` sentence (~line 139, beginning `` `op` is `record`… ``):

```markdown
`op` is `record`, `amend`, `project_tags`, `project_portable`, `tag_merge`, `regret`
or `principle`; the pre-tag `project_type` and the retired `tags_distinct` are still
replayed so old journals keep working. The list is exhaustive: `replay_entry`
raises on an op it does not know rather than counting a silent no-op as a
successful replay, so a new op must be added there and here together.
`precedent.py rebuild` replays the file in order, so entries must stay
append-only — editing or reordering lines rewrites history.
```

Then add, after the `project_portable` paragraph that closes the section:

````markdown
`amend` rewords a decision that is already recorded — same decision, better
words — and carries `id` plus any subset of `title`, `statement` and
`rationale`:

```json
{"ts":"2026-09-15T11:04:02","op":"amend","id":"notify4j-core-for-multi-channel-booking-notifica-1789206712",
 "title":"notify4j-core for all outbound third-party delivery","v":1}
```

A key that is absent is unchanged, never cleared, so an amendment to a title
cannot blank a rationale. Nothing else on the decision is amendable: topics,
options, scope and project are *what was decided*, and changing one of those
is a different decision, recorded with `record --supersedes`.

It replays as `MATCH ... SET` — never a `MERGE`, for the same reason
`project_portable` does not: an amendment must not fabricate a decision out of
a rewording when the `record` line above it was unreadable. Journal order puts
that line first, so a miss means it was skipped and already reported.

The id does not change, including the now-stale title slug inside it. See
`docs/adr/0008`.

Amending does NOT bump `v`. An older precedent replaying a journal that
contains an `amend` line raises on the unknown op, and `replay_journal`
reports that one line as skipped and continues; bumping the schema would
instead make it refuse every line written from that point on, `record`
lines included.
````

Also extend the `Decision` row's note under **Nodes** — after the
`Decision.despite` paragraph (~line 22), add:

```markdown
`title`, `statement` and `rationale` are the only properties `amend` rewrites;
`status`, `scope` and `created` are not amendable, and neither are any of a
decision's edges.
```

- [ ] **Step 2: Add the term to `CONTEXT.md`**

Insert after the **Supersession** entry, before **Regret**:

```markdown
**Amendment**:
A correction to how a Decision was *described*, leaving what was decided
untouched. Distinct from Supersession, which records that the decision itself
changed: an amendment asserts no history, because none happened. The id
survives it.
_Avoid_: edit, fix, correction, revision
```

- [ ] **Step 3: Add the row to `README.md`**

Replace the table and its lead-in at `README.md:213-222`:

```markdown
Four situations get confused with each other, and each wants a different record. You say the
ordinary sentence; the agent picks the right one.

| The situation | What gets recorded |
|---|---|
| The precedent is sound, but **this project is genuinely different** | a decision *despite* the precedent |
| The choice was right then and is **wrong now, here** | a decision that *supersedes* the old one |
| The choice was **wrong everywhere**, and you repeated it | a *regret*, which inverts the norm |
| The choice was right — you just **wrote it down badly** | an *amendment*, which rewords it in place |
```

Then add after the paragraph beginning *"The third exists because repetition…"* and its
`REGRET:` block:

```markdown
The fourth looks like the smallest and is the one worth getting right. `check` prints the title
first, so the title is the part quoted back as your own reasoning — and a decision titled more
narrowly than it is gets read as not-applicable, re-litigated, and answered a second time. An
amendment fixes the words without inventing a change of mind, and the id keeps resolving.
```

- [ ] **Step 4: Teach the agent when to reach for it in `SKILL.md`**

In `adapters/claude/skills/precedent/SKILL.md`, add a row to the trigger table
(after the `--supersedes` row, ~line 94):

```markdown
| "that's not what I meant", "the title is too narrow", "reword that" | a rewording, not a new decision | `amend` |
```

Replace the paragraph immediately below that table (beginning *"The two that get
confused are the third and fifth"*):

```markdown
Three of these get confused. Changing your mind in one project is `--supersedes`:
the old choice stays a norm everywhere else. Deciding the *pattern* was wrong is
`regret`: it marks every project that made that choice and stops the graph
arguing for it again. Deciding the *words* were wrong is `amend`: nothing about
the choice changed, so nothing is superseded and no second entry appears. Ask
which one they mean when the sentence is ambiguous — "we're moving off mongo" is
either of the first two, and "that's not really what I decided" is usually the
third rather than a new decision.
```

Add a row to the 3a table (~line 235, after the `regret` row):

```markdown
| The choice was right — the **record of it reads wrong** | `amend --id <id> --title "<better words>"` | Only the wording; no second decision, no supersession, id unchanged |
```

Then add, after the paragraph beginning *"The first is the one people skip"*:

```markdown
The fourth is the one to reach for whenever the correction is to a description
rather than to a choice, and the cheap mistake is superseding instead. A
supersession writes a second decision and makes `check` carry both — an
assertion that the call was revisited. For a rewording that history never
happened, and it is asserted in the line `check` quotes first. Amend takes
`--title`, `--statement` and `--rationale`; anything else — a different option,
a different topic — is a different decision, so that really is `--supersedes`.
```

- [ ] **Step 5: Add the row to `docs/setup.md`**

Insert after the `record` row in the command table (~line 162):

```markdown
| `amend` | reword a decision already recorded — same decision, better words (`--id --title --statement --rationale`) |
```

- [ ] **Step 6: Write the ADR**

Create `docs/adr/0008-an-amendment-keeps-the-id.md`:

````markdown
# 0008 — An amendment keeps the id, stale slug and all

## Status

Accepted, 2026-09-15.

## Context

A decision id embeds a slug of the title it was minted from:

```
notify4j-core-for-multi-channel-booking-notifica-1789206712
                             ^^^^^^^
```

`amend` rewords a decision without superseding it (issue #10). The moment a
title can change, the slug inside the id is a description of a title that no
longer exists, and something has to be decided about it: leave it, or mint a
new id and point the old one at it.

## Decision

The id does not change. The slug inside it is allowed to rot.

## Consequences

The id is identity; the slug within it is an accident of how it was minted, in
the same way `Project.id` is only the native path a project was first seen at
while `Project.portable` is what the project *is* (`docs/adr/0002`). This
project has already answered the general form of this question once, and
answered it the same way: a human-readable string is a display fact, never the
identity.

Everything already holding an id keeps resolving — a `SUPERSEDES` edge, a
`DIVERGES_FROM` edge, a `Principle`'s `DERIVED_FROM`, a rule file, a commit
message, a GitHub issue. None of these are enumerable, which is the argument:
an id that can change is an id that can be dangling somewhere nobody will
check.

The rejected alternative was to regenerate the id and keep the old one as an
alias. It costs an alias lookup on every `MATCH (d:Decision {id:$x})` in the
file, a second journal op shape, and a mapping that can itself go stale — all
to keep a cosmetic substring honest in a string the user reads as opaque.
Regenerating with no alias was rejected outright: every existing reference
would silently stop matching, and a silent no-op is this tool's worst failure
mode.

The visible cost is that `#notify4j-core-for-multi-channel-booking-notifica-…`
can sit beside the title *"notify4j-core for all outbound third-party
delivery"* and look inconsistent. That is the intended reading — the id is a
handle, not a summary.
````

- [ ] **Step 7: Verify the docs against the implementation**

Run: `uv run scripts/precedent.py amend --help`
Expected: the flags listed match the `docs/setup.md` row exactly — `--id`, `--title`, `--statement`, `--rationale`.

Run: `grep -n 'amend' references/schema.md CONTEXT.md README.md docs/setup.md adapters/claude/skills/precedent/SKILL.md docs/adr/0008-an-amendment-keeps-the-id.md`
Expected: a hit in every one of the six files.

Run: `uv run scripts/precedent.py selftest`
Expected: PASS — confirms the docs edits did not disturb the code.

- [ ] **Step 8: Commit**

```bash
git add references/schema.md CONTEXT.md README.md docs/setup.md \
        adapters/claude/skills/precedent/SKILL.md \
        docs/adr/0008-an-amendment-keeps-the-id.md
git commit -m "docs: document \`amend\`, and why it keeps the id"
```

---

## Deliberately not in scope

- **No `precedent-amend` slash-command skill.** The other write commands have one, but a slash command is not how this gets used: the trigger is the user saying *"that's not what I meant"* mid-conversation, which the main `SKILL.md` table now covers. Add one if amending turns out to be something people go looking for on purpose.
- **No amending of topics, options or scope.** That is `record --supersedes` and must stay so — see `AMENDABLE`.
- **No `unamend` or amendment history.** The journal already holds every amendment in order; `rebuild` replays them and the last one wins. A UI over that history is worth building when somebody wants to read it.
- **No version bump or release.** This repo releases in its own `chore: release X` commits; bumping `agent-plugin.yaml` and regenerating the two manifests with `scripts/gen-plugin-json.py` is that separate step.
