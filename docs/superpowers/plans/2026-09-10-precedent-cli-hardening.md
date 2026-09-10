# precedent CLI Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `precedent.py` run on Windows, stop it silently mis-answering `check`, and identify a project by its git remote rather than its filesystem path.

**Architecture:** One file, `scripts/precedent.py`, stays one file. Three shapes of change: heuristics are deleted and replaced by printing the vocabulary for the model to judge (ADR 0001); the Project primary key gains a portable alias resolved on read and write (ADR 0002); and two platform-bound mechanisms — the lock and the store relocation — are swapped for portable ones (ADR 0003, 0004). `cmd_selftest` is split into named check functions first, so each later task has a place to put a focused test and each function balances its own node count.

**Tech Stack:** Python 3.12+, PEP 723 inline metadata, `grafeo` (openCypher 9.0 dialect), `filelock`, `uv run`.

**Spec:** `docs/spec-2026-09-10-precedent-hardening.md` — this plan implements subsystem **A** only. B (packaging) and C (measurement) are separate plans.

## Global Constraints

- Python `>=3.12`. One file, PEP 723 inline metadata, no durable venv.
- Dependencies are exactly `grafeo` and `filelock`. Add nothing else.
- Must run on Linux, macOS and Windows. No `fcntl`, no `os.symlink`, no `os.sep` string surgery in new code.
- `uv run scripts/precedent.py selftest` is the only test entry point, and must end with `selftest ok (N nodes, unchanged)`.
- `journal.jsonl` is append-only. Never edit or reorder existing lines.
- Grafeo dialect rules from `references/schema.md`: alias every returned expression; `NOT (n)-[:R]->()` does not parse, use `NOT EXISTS { MATCH ... }`; parameters are positional dicts. `max()` is **not** in the confirmed-working list — use `collect()` and reduce in Python.
- Run the whole selftest against a scratch store, never the real one:
  `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`

---

## Phase 1 — Test harness and portability

These three tasks unblock everything else and are independently shippable.

### Task 1: Split `cmd_selftest` into named checks

**Files:**
- Modify: `scripts/precedent.py:997-1157` (`cmd_selftest`)

**Interfaces:**
- Produces: `_check_helpers()`, `_check_detect_project()`, `_check_drift()`, `_check_projects(s)`, `_check_decisions(s)` — each takes no arguments or a `Store`, asserts, and cleans up every node it created. Later tasks add `_check_store(s)`, `_check_journal(s)`, `_check_verdicts(s)`, `_check_identity(s)`.

- [ ] **Step 1: Extract the existing blocks verbatim into functions**

Cut each block out of `cmd_selftest` and paste it into a function of its own, above `cmd_selftest`. Move the cleanup lines that belong to each block into that block's function, so every function leaves the graph exactly as it found it:

- `_check_helpers()` — the `slug(...)` and `csv(...)` asserts. No graph access.
- `_check_detect_project()` — the `/nonexistent` assert and the `/tmp/precedent-selftest-proj` build/listing asserts, including the `build.zig` / `shard.yml` / `src` / `node_modules` create-and-remove.
- `_check_drift()` — both `close_matches` assert loops. No graph access.
- `_check_projects(s)` — the `tags_of` / `neighbours` / `min_shared` asserts and the containment block (`root`, `mod`, `sibling`), plus the `DETACH DELETE` cleanup for those three projects and the `selftest-monorepo` tag.
- `_check_decisions(s)` — `write_decision` for `selftest-1`, the supersede assert for `selftest-2`, the `despite` asserts for `selftest-3`, and the `apply_regret` block. Ends with the `n.id STARTS WITH 'selftest'` cleanup, the two project deletes, and the orphan Topic/Option/Tag sweep.

- [ ] **Step 2: Rewrite `cmd_selftest` as the runner**

```python
def cmd_selftest(a, s: Store) -> None:
    """One runnable check over the paths that contain real logic.

    Each check cleans up after itself, so the node count is the invariant
    that catches a check which forgot to.
    """
    before = s.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
    _check_helpers()
    _check_detect_project()
    _check_drift()
    _check_projects(s)
    _check_decisions(s)
    after = s.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
    assert after == before, f"selftest changed node count {before} -> {after}"
    print(f"selftest ok ({before} nodes, unchanged)")
```

- [ ] **Step 3: Run it and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: `selftest ok (N nodes, unchanged)`

- [ ] **Step 4: Verify each check balances on its own**

Temporarily comment out every call in `cmd_selftest` except one, run, and confirm `unchanged`. Repeat for each of the five. Uncomment all when done. A check that only balances when its neighbours run is a check that leaks.

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "refactor: split selftest into named checks that balance individually"
```

---

### Task 2: `filelock` replaces `fcntl`

**Files:**
- Modify: `scripts/precedent.py:1-4` (PEP 723 header), `:25` (`import fcntl`), `:41-63` (`Store`)
- Test: `scripts/precedent.py` — new `_check_store(s)`

**Interfaces:**
- Consumes: `_check_store(s)` slot in `cmd_selftest` from Task 1.
- Produces: `Store` with the same `__enter__` / `__exit__` contract. `Store.lock_path` — `pathlib.Path` of the lock file, used by the test.

- [ ] **Step 1: Write the failing test**

Add above `cmd_selftest`, and add `_check_store(s)` to the runner list:

```python
def _check_store(s: Store) -> None:
    """The lock must actually exclude a second process.

    A lock that silently does not lock is the failure mode grafeo#405
    documents: 120 writes across 6 processes, 60 stored, no errors raised.
    Two Store objects in one process would not prove it — filelock returns
    the same instance for a given path — so this forks.
    """
    import subprocess
    import filelock

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time, filelock;"
         "l = filelock.FileLock(sys.argv[1]);"
         "l.acquire();"
         "print('held', flush=True);"
         "time.sleep(5)",
         str(s.lock_path)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        contended = filelock.FileLock(str(s.lock_path))
        try:
            contended.acquire(timeout=0.5)
            raise AssertionError("lock did not exclude a second process")
        except filelock.Timeout:
            pass
    finally:
        holder.kill()
        holder.wait()
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL — `ModuleNotFoundError: No module named 'filelock'`, because it is not in the PEP 723 header yet.

- [ ] **Step 3: Declare the dependency**

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["grafeo", "filelock"]
# ///
```

- [ ] **Step 4: Swap the lock**

Delete `import fcntl` at line 25. Replace the lock handling in `Store`:

```python
class Store:
    def __init__(self, home: pathlib.Path = HOME):
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "graph.db"
        self.journal = home / "journal.jsonl"
        self.lock_path = home / ".lock"
        # filelock, not fcntl: fcntl does not exist on Windows, and it is
        # imported at module scope, so the whole CLI failed to start there.
        # Hand-rolling the fcntl/msvcrt shim means owning a concurrency
        # primitive on an OS this project does not run and cannot test.
        self._lock = filelock.FileLock(str(self.lock_path))

    def __enter__(self):
        self._lock.acquire()
        import grafeo

        self.db = grafeo.GrafeoDB(str(self.db_path))
        return self

    def __exit__(self, *exc):
        try:
            self.db.close()
        except Exception:
            pass
        self._lock.release()
        return False
```

Add `import filelock` to the import block, alphabetically after `argparse`.

- [ ] **Step 5: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 6: Verify the module imports with no `fcntl`**

Run: `uv run python -c "import ast,sys; src=open('scripts/precedent.py').read(); assert 'fcntl' not in src, 'fcntl still referenced'; print('clean')"`
Expected: `clean`

- [ ] **Step 7: Commit**

```bash
git add scripts/precedent.py
git commit -m "fix: use filelock so the CLI starts on Windows"
```

---

### Task 3: A pointer file replaces the relocation symlink

**Files:**
- Modify: `scripts/precedent.py:41-47` (`Store.__init__`), `:925-971` (`relocate`), `:973-990` (`cmd_init`), `:1128-1145` (the `init` asserts inside the Task 1 checks)

**Interfaces:**
- Consumes: `Store.lock_path` from Task 2.
- Produces: `resolve_home(home: pathlib.Path) -> pathlib.Path` — follows a `location` pointer file exactly once and returns the real store directory. `POINTER = "location"`.

- [ ] **Step 1: Write the failing test**

Add to `_check_store(s)`, after the lock assertions:

```python
    # A pointer file, not a symlink: os.symlink raises WinError 1314 without
    # Developer Mode, which made `init` unavailable on Windows for its whole
    # purpose. Resolution follows the pointer exactly once — a pointer inside
    # the target is data, not a second hop.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        default, target = pathlib.Path(tmp) / "default", pathlib.Path(tmp) / "target"
        default.mkdir()
        (default / "journal.jsonl").write_text('{"op":"noop"}\n')
        relocate(target, default=default)
        assert not (default / "journal.jsonl").exists(), "payload must move"
        assert (target / "journal.jsonl").exists(), "payload must arrive"
        assert (default / POINTER).read_text().strip() == str(target)
        assert resolve_home(default) == target, "pointer must resolve"
        (target / POINTER).write_text(str(default))
        assert resolve_home(default) == target, "resolution must not loop"
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL with `NameError: name 'POINTER' is not defined`

- [ ] **Step 3: Add the pointer constant and resolver**

Below `DEFAULT_HOME`:

```python
POINTER = "location"
```

Above `class Store`:

```python
def resolve_home(home: pathlib.Path) -> pathlib.Path:
    """Follow a relocation pointer, once.

    The default path is wired into the SessionStart hook and every slash
    command, and PRECEDENT_HOME is unset in the hook's environment, so an
    env var cannot move the store. A symlink can, but needs Developer Mode
    on Windows. A file holding a path needs neither.

    Exactly one hop: a pointer found inside the target is a stale file, not
    an instruction, and following it is how a relocation loop starts.
    """
    pointer = home / POINTER
    if not pointer.is_file():
        return home
    return pathlib.Path(pointer.read_text().strip()).expanduser()
```

- [ ] **Step 4: Resolve in `Store.__init__`**

Replace the first three lines of `Store.__init__`:

```python
    def __init__(self, home: pathlib.Path = HOME):
        home.mkdir(parents=True, exist_ok=True)
        self.home = home = resolve_home(home)
        home.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 5: Rewrite `relocate` to write the pointer**

Replace the symlink handling. The `is_symlink` branch and the final `symlink_to` both go; everything from the `if target == default:` guard through the payload move stays as it is, except that `default` is no longer removed — it now holds the pointer:

```python
def relocate(target: pathlib.Path, default: pathlib.Path = DEFAULT_HOME) -> str:
    """Keep the store somewhere else, and leave a pointer at the default path."""
    import shutil

    target.mkdir(parents=True, exist_ok=True)
    if target == default:
        return f"store: {target}  (the default location)"

    default.mkdir(parents=True, exist_ok=True)
    payload = [f for f in default.iterdir()
               if f.name not in (".lock", POINTER)]
    occupied = [f for f in target.iterdir()
                if f.name not in (".lock", POINTER)]
    if payload and occupied:
        raise SystemExit(
            f"both {default} and {target} hold a store; refusing to merge.\n"
            f"  merging is a journal concatenation, so do it deliberately:\n"
            f"    cat {default}/journal.jsonl >> {target}/journal.jsonl\n"
            f"    mv {default} {default}.bak\n"
            f"  then re-run this, and `precedent.py rebuild`.")
    for f in payload:
        shutil.move(str(f), str(target / f.name))
    (default / POINTER).write_text(str(target) + "\n")
    moved = f"  moved {len(payload)} file(s) from the default location\n" if payload else ""
    return f"store: {target}\n{moved}  {default}/{POINTER} points here"
```

- [ ] **Step 6: Rewrite `cmd_init`'s no-argument branch**

```python
def cmd_init(a) -> None:
    """Runs without a Store: opening one would create the default directory
    before this command has decided where the store belongs."""
    if not a.location:
        d = DEFAULT_HOME
        real = resolve_home(d)
        if real != d:
            print(f"store: {real}  (via {d}/{POINTER})")
        elif d.exists():
            print(f"store: {d}  (the default location)")
        else:
            print(f"no store yet; it will be created at {d}")
        if os.environ.get("PRECEDENT_HOME"):
            print(f"  note: PRECEDENT_HOME is set to {os.environ['PRECEDENT_HOME']},"
                  " which overrides the above for this shell only —"
                  " the SessionStart hook will not see it.")
        return
    print(relocate(pathlib.Path(a.location).expanduser().resolve()))
```

- [ ] **Step 7: Delete the old symlink asserts**

In the check function Task 1 extracted them into, remove every assertion mentioning `is_symlink()`. They are replaced by Step 1's block.

- [ ] **Step 8: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 9: Verify no symlink calls remain**

Run: `grep -n "symlink" scripts/precedent.py`
Expected: no output

- [ ] **Step 10: Commit**

```bash
git add scripts/precedent.py
git commit -m "fix: relocate the store with a pointer file, not a symlink"
```

---

## Phase 2 — Journal safety and show-don't-decide

### Task 4: Every journal line carries a schema version

**Files:**
- Modify: `scripts/precedent.py:34-36` (constants), `:68-74` (`Store.log`), `:853-886` (`cmd_rebuild`), `:888-923` (`replay_entry`)

**Interfaces:**
- Produces: `SCHEMA = 1`; `class JournalTooNew(Exception)`; `replay_entry` raises `JournalTooNew` for an entry whose `v` exceeds `SCHEMA`.

- [ ] **Step 1: Write the failing test**

Add above `cmd_selftest`, and add `_check_journal(s)` to the runner list:

```python
def _check_journal(s: Store) -> None:
    """A journal written by a newer precedent must stop the replay, not
    half-succeed. `rebuild` is the escape hatch that makes a v0.5 graph
    engine an acceptable dependency; an escape hatch that silently
    half-works is not one.

    This exercises replay_entry directly rather than cmd_rebuild, because
    rebuild starts by deleting every node and selftest must leave the
    graph untouched.
    """
    assert SCHEMA == 1
    try:
        replay_entry(s, {"op": "record", "v": SCHEMA + 1, "id": "selftest-future"})
        raise AssertionError("a future schema version must not replay")
    except JournalTooNew as exc:
        assert "selftest-future" in str(exc), exc
    # An entry with no version is pre-versioning, and replays as v1.
    assert s.q("MATCH (d:Decision {id:'selftest-future'}) RETURN d.id AS id") == []
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL with `NameError: name 'SCHEMA' is not defined`

- [ ] **Step 3: Add the constant and the exception**

Below `SCOPES`:

```python
SCHEMA = 1          # journal line format; bump only on a breaking change


class JournalTooNew(Exception):
    """A journal line written by a newer precedent than this one."""
```

- [ ] **Step 4: Stamp every written line**

In `Store.log`, add `"v": SCHEMA` to the object:

```python
    def log(self, op: str, payload: dict) -> None:
        """Journal first, then mutate. A crash between the two costs a replay, not data."""
        with open(self.journal, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "v": SCHEMA, "op": op, **payload}) + "\n")
            f.flush()
            os.fsync(f.fileno())
```

- [ ] **Step 5: Refuse a future version on replay**

As the first statement in `replay_entry`:

```python
def replay_entry(s: Store, e: dict) -> None:
    # A line with no "v" predates versioning and is v1 by definition. A line
    # from the future cannot be interpreted by guessing which keys it has,
    # which is exactly what the rest of this function does.
    if e.get("v", 1) > SCHEMA:
        raise JournalTooNew(
            f"entry {e.get('id', e.get('op', '?'))!r} is schema v{e['v']},"
            f" this precedent understands v{SCHEMA} — upgrade before replaying")
```

- [ ] **Step 6: Make `cmd_rebuild` stop rather than skip**

In `cmd_rebuild`, add a handler *above* the existing `except Exception` so a
future-version line is fatal rather than counted as one skipped entry:

```python
        try:
            replay_entry(s, e)
        except JournalTooNew as exc:
            raise SystemExit(f"stopping at line {lineno}: {exc}")
        except Exception as exc:
            skipped.append(f"line {lineno}: {e.get('op', '?')} — {type(exc).__name__}: {exc}")
            continue
```

- [ ] **Step 7: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 8: Verify a real rebuild still works**

```bash
cp -r ~/.local/share/precedent /tmp/precedent-backup
uv run scripts/precedent.py rebuild
uv run scripts/precedent.py brief --project .
```
Expected: `rebuilt N journal entries`, and the brief still lists 7 decisions. Old unversioned lines replay as v1.

- [ ] **Step 9: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: version journal lines and refuse to replay the future"
```

---

### Task 5: Delete the similarity heuristics

Implements ADR 0001. Deletes ~80 lines and adds ~20.

**Files:**
- Delete: `scripts/precedent.py:243-275` (`close_matches`), `:277-289` (`asserted_distinct`), `:291-321` (`refuse_drift`)
- Modify: `:737-789` (`cmd_tag`), `:803-822` (`cmd_maintain` drift section), `:888-923` (`replay_entry`), `:1213-1222` (the `tag` subparser), and the `_check_drift()` function from Task 1

**Interfaces:**
- Consumes: `vocabulary(s)` (unchanged, `:155`).
- Produces: `normalised(tag: str) -> str` — lowercases, strips `-`/`_`/space, and drops a trailing `s` on words longer than three characters.

- [ ] **Step 1: Replace the drift test**

Replace the entire body of `_check_drift()`:

```python
def _check_drift() -> None:
    """Identity after trivial normalisation is a fact. Similarity is a
    judgment, and it moved to the model — difflib scored a shared prefix
    and called web-frontend and web-backend the same thing, 7 of 11
    realistic pairs wrong. See docs/adr/0001.
    """
    for a_, b_ in [("data_pipeline", "data-pipeline"), ("Data-Pipeline", "data-pipeline"),
                   ("backend-apis", "backend-api"), ("telegram bot", "telegram-bot")]:
        assert normalised(a_) == normalised(b_), f"spelling variant missed: {a_} ~ {b_}"
    for a_, b_ in [("web-frontend", "web-backend"), ("mobile-ios", "mobile-android"),
                   ("telegram-bot", "telegram-api"), ("python", "python3"),
                   ("backend", "backend-api"), ("ml-training", "ml-serving"),
                   ("cli", "clip")]:
        assert normalised(a_) != normalised(b_), f"false match: {a_} ~ {b_}"
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL with `NameError: name 'normalised' is not defined`

- [ ] **Step 3: Add `normalised` and delete the three functions**

Replace `close_matches`, `asserted_distinct` and `refuse_drift` — all three, entirely — with:

```python
def normalised(tag: str) -> str:
    """Case, separators and a trailing plural are spelling, not meaning.

    This is the whole of the automated drift check. It reports identity
    after trivial normalisation, which is a fact; whether two genuinely
    different words mean the same thing is a judgment, and it belongs to
    the caller, which has the vocabulary, the repository and the
    conversation in front of it.
    """
    flat = re.sub(r"[-_ ]+", "", tag.lower())
    return flat[:-1] if len(flat) > 3 and flat.endswith("s") else flat
```

- [ ] **Step 4: Stop refusing tags; show the vocabulary instead**

In `cmd_tag`, delete the `refuse_drift(s, add, a.new_tag)` line. At the end of
the add/remove branch, replace the single print with:

```python
    print(f"{info['name']} tags: {', '.join(tags_of(s, info['id'])) or 'none'}")
    known = vocabulary(s)
    if known:
        # Printed on every write, not only on request: precedent is ranked by
        # exact tag overlap, so a near-duplicate hides half the history from
        # the other half. The caller can see it here and merge.
        print("\ntags in use across all projects:")
        for r in known:
            print(f"  {r['tag']:<20} {r['projects']} project(s)")
        print("  a near-duplicate above splits your history —"
              " merge with: precedent.py tag --merge <from> --into <to>")
```

- [ ] **Step 5: Drop the `--new-tag` flag**

In `main`, remove this line from the `tag` subparser:

```python
    tg.add_argument("--new-tag", action="store_true",
                    help="confirm a tag resembling an existing one really means something else")
```

- [ ] **Step 6: Replace `maintain`'s pair scan**

Replace the `tags = [...]` / `pairs = {...}` / print block in `cmd_maintain`:

```python
    tags = [r["tag"] for r in vocabulary(s)]
    groups: dict[str, list[str]] = {}
    for t in tags:
        groups.setdefault(normalised(t), []).append(t)
    dupes = [sorted(v) for v in groups.values() if len(v) > 1]
    print(f"\n== tags that differ only in spelling ({len(dupes)}) ==")
    if not dupes:
        print("  none")
    for names in sorted(dupes):
        print(f"  {' / '.join(repr(n) for n in names)}"
              f" — merge with: precedent.py tag --merge {names[1]} --into {names[0]}")
    print(f"\n== the whole tag vocabulary ({len(tags)}) ==")
    for r in vocabulary(s):
        print(f"  {r['tag']:<20} {r['projects']} project(s)")
    print("  two of these that mean the same thing split your history."
          " Spelling variants are listed above; the rest is a judgment call.")
```

- [ ] **Step 7: Keep the legacy journal op replaying**

Confirm `replay_entry` still contains this branch, and extend its comment. It
must survive even though nothing writes `tags_distinct` any more, or journals
written before this change stop replaying:

```python
    elif e["op"] == "tags_distinct":
        pass  # legacy: written by the removed drift guard, kept so old
              # journals still replay. Never emitted now. See docs/adr/0001.
```

- [ ] **Step 8: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 9: Verify the deletions and that a real journal still replays**

```bash
grep -n "close_matches\|refuse_drift\|asserted_distinct\|new_tag" scripts/precedent.py
uv run scripts/precedent.py rebuild
uv run scripts/precedent.py maintain
```
Expected: the grep prints nothing; rebuild reports every entry replayed with none skipped; `maintain` prints the two new sections.

- [ ] **Step 10: Commit**

```bash
git add scripts/precedent.py
git commit -m "refactor: delete the similarity heuristics, print the vocabulary instead"
```

---

### Task 6: A zero-hit `check` prints the topic vocabulary

**Files:**
- Modify: `scripts/precedent.py:155-160` (beside `vocabulary`), `:605-640` (`cmd_check` head)

**Interfaces:**
- Consumes: `Store.q`.
- Produces: `topic_vocabulary(s: "Store") -> list[dict]` with keys `topic`, `decisions`.

- [ ] **Step 1: Write the failing test**

Add above `cmd_selftest`, and add `_check_verdicts(s)` to the runner list:

```python
def _check_verdicts(s: Store) -> None:
    """`clear` must mean 'I searched and your history is silent', never
    'you typed a word I have never seen'. Reproduced before the fix:
    `check --topic database` against a graph holding the same decision
    under `persistence` printed `new ground` and then `clear`.
    """
    d = {"id": "selftest-v1", "title": "T", "statement": "T",
         "rationale": "concurrent writers", "scope": "architecture",
         "created": today(), "project_id": "/tmp/precedent-selftest-v",
         "project_name": "selftest-v", "tags": ["selftest-v-tag"],
         "topics": ["selftest-persistence"], "chose": ["postgres"],
         "rejected": ["sqlite"], "supersedes": []}
    write_decision(s, d)
    try:
        topics = [r["topic"] for r in topic_vocabulary(s)]
        assert "selftest-persistence" in topics, topics
    finally:
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-v' DETACH DELETE n")
        s.q("""MATCH (p:Project {id:'/tmp/precedent-selftest-v'}) DETACH DELETE p""")
        for name in ("selftest-persistence", "postgres", "sqlite", "selftest-v-tag"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option OR n:Tag) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL with `NameError: name 'topic_vocabulary' is not defined`

- [ ] **Step 3: Add the helper**

Directly below `vocabulary`:

```python
def topic_vocabulary(s: "Store") -> list[dict]:
    """Topics already in use, commonest first — the menu `check` picks from.

    Topics are the key `check` matches on, and matching is exact. The
    caller invents the word fresh each session, so a synonym returns
    nothing; showing the vocabulary is how it corrects itself.
    """
    return s.q("""MATCH (d:Decision)-[:ABOUT]->(t:Topic)
                  WHERE d.status='active'
                  RETURN t.name AS topic, count(DISTINCT d) AS decisions
                  ORDER BY decisions DESC, topic""")
```

- [ ] **Step 4: Return the vocabulary instead of a verdict on a miss**

In `cmd_check`, replace the `if not rows:` block. Return before any verdict —
every verdict query keys on the same topic string and would be empty too, so
printing `clear` after a miss is the exact failure being fixed:

```python
    if not rows:
        print("  nothing recorded under this exact word.")
        known = topic_vocabulary(s)
        if known:
            print("\n== topics in use ==")
            for r in known:
                print(f"  {r['topic']:<24} {r['decisions']} decision(s)")
            print("\n  matching is exact, so a synonym finds nothing. If one of"
                  " these is the same question, re-run with it. If none is,"
                  " this is new ground.")
        else:
            print("  no decisions recorded anywhere yet — this is new ground.")
        return
```

- [ ] **Step 5: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 6: Verify against the real graph**

```bash
uv run scripts/precedent.py check --topic database --chose sqlite
```
Expected: `nothing recorded under this exact word`, then a topic list containing `file-locking`, `project-identity`, `platform-support` and the rest. The word `clear` must not appear.

- [ ] **Step 7: Commit**

```bash
git add scripts/precedent.py
git commit -m "fix: show the topic vocabulary on a miss instead of reporting clear"
```

---

## Phase 3 — Verdict correctness

`check` is the command the whole tool exists for, and five separate things in it
report confidently wrong answers. Each task here is independently reviewable.

### Task 7: A regret must stop firing CONFLICT and start firing LESSON

Reproduced before the fix: two projects chose mongo rejecting postgres; after
`regret --topic persistence --chose mongo --instead postgres`, running
`check --topic persistence --chose postgres` printed two `CONFLICT` lines and
never mentioned the lesson. The verb argues against you the moment you take its
own advice.

**Files:**
- Modify: `scripts/precedent.py:651-657` (`revived`), `:686-687` (the `clear` guard)
- Test: `_check_verdicts(s)` from Task 6

**Interfaces:**
- Consumes: `write_decision`, `apply_regret`, `topic_vocabulary` from earlier tasks.
- Produces: no new names; `cmd_check` gains an `endorsed` local.

- [ ] **Step 1: Write the failing test**

Append to `_check_verdicts(s)`, inside a new `try`/`finally` after the existing block:

```python
    import argparse as _argparse
    import contextlib
    import io

    base = {"title": "T", "statement": "T", "rationale": "flexible schema",
            "scope": "architecture", "created": today(),
            "tags": ["selftest-v-tag"], "topics": ["selftest-persistence"],
            "chose": ["mongo"], "rejected": ["postgres"], "supersedes": []}
    write_decision(s, {**base, "id": "selftest-v2",
                       "project_id": "/tmp/precedent-selftest-v2", "project_name": "vA"})
    write_decision(s, {**base, "id": "selftest-v3",
                       "project_id": "/tmp/precedent-selftest-v3", "project_name": "vB"})
    apply_regret(s, {"id": "selftest-v-lesson", "topic": "selftest-persistence",
                     "option": "mongo", "because": "schema drift",
                     "instead": "postgres",
                     "decisions": ["selftest-v2", "selftest-v3"], "created": today()})
    try:
        out = io.StringIO()
        args = _argparse.Namespace(topic="selftest-persistence",
                                   chose="postgres", project="/tmp")
        with contextlib.redirect_stdout(out):
            cmd_check(args, s)
        text = out.getvalue()
        assert "CONFLICT" not in text, f"a regretted rejection must not fire CONFLICT:\n{text}"
        assert "LESSON" in text, f"the lesson that recommends this must fire:\n{text}"
        assert "schema drift" in text, text
        assert "clear —" not in text, text
    finally:
        s.q("MATCH (l:Lesson {id:'selftest-v-lesson'}) DETACH DELETE l")
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-v' DETACH DELETE n")
        for pid in ("/tmp/precedent-selftest-v2", "/tmp/precedent-selftest-v3"):
            s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": pid})
        for name in ("selftest-persistence", "mongo", "postgres", "selftest-v-tag"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option OR n:Tag) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL — `AssertionError: a regretted rejection must not fire CONFLICT`

- [ ] **Step 3: Filter the revived query on status**

```python
    revived = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                           (d)-[:REJECTED]->(:Option {name:$o}),
                           (d)-[:IN_PROJECT]->(p:Project)
                     WHERE d.status='active'
                     RETURN p.name AS pname, d.title AS title, d.rationale AS why""",
                  {"t": topic, "o": a.chose})
```

- [ ] **Step 4: Report the lesson that recommends this option**

Insert directly after the `regrets` loop and before `revived`:

```python
    # The mirror of REGRET. Without it, taking a lesson's own advice is met
    # with the rejections recorded by the decisions that lesson regrets.
    endorsed = s.q("""MATCH (l:Lesson)
                      OPTIONAL MATCH (l)-[:REGRETS]->(d:Decision)
                      WHERE l.topic=$t AND l.instead=$o
                      RETURN l.option AS was, l.statement AS why, count(d) AS n""",
                   {"t": topic, "o": a.chose})
    endorsed = [r for r in endorsed if r["why"]]
    for r in endorsed:
        print(f"  LESSON: you chose '{r['was']}' for this in {r['n']} project(s),"
              f" concluded it was a mistake, and now prefer this —")
        print(f"     \"{r['why']}\"")
```

- [ ] **Step 5: Include it in the `clear` guard**

```python
    if not revived and not diverged and not regrets and not endorsed:
        print("  clear — no rejection history, no regret, no divergence from your norm")
```

- [ ] **Step 6: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/precedent.py
git commit -m "fix: a regret silences its own conflicts and endorses its replacement"
```

---

### Task 8: The norm reports how old it is

**Files:**
- Modify: `scripts/precedent.py:659-664` (`norm` query), `:672-681` (the divergence prints)
- Test: `_check_verdicts(s)`

**Interfaces:**
- Consumes: `cmd_check`'s `norm` local.
- Produces: no new names.

- [ ] **Step 1: Write the failing test**

Insert into the Task 7 `try:` block, before the `finally:`, a second assertion
using the same fixture from the other direction:

```python
        out2 = io.StringIO()
        args2 = _argparse.Namespace(topic="selftest-persistence",
                                    chose="sqlite", project="/tmp")
        s.q("MATCH (d:Decision) WHERE d.id STARTS WITH 'selftest-v' SET d.status='active'")
        with contextlib.redirect_stdout(out2):
            cmd_check(args2, s)
        div = out2.getvalue()
        assert "DIVERGENCE" in div, div
        assert f"last: {today()[:7]}" in div, f"the norm must carry its age:\n{div}"
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL — `AssertionError: the norm must carry its age`

- [ ] **Step 3: Collect the dates**

`references/schema.md` lists the confirmed-working functions and `max()` is not
among them, so aggregate with `collect()` and reduce in Python:

```python
    norm = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                        (d)-[:CHOSE]->(o:Option),
                        (d)-[:IN_PROJECT]->(p:Project)
                  WHERE d.status='active' AND o.name <> $o
                  RETURN o.name AS other, count(DISTINCT p) AS n,
                         collect(d.created) AS dates
                  ORDER BY n DESC LIMIT 3""", {"t": topic, "o": a.chose})
```

- [ ] **Step 4: Print the age beside the count**

Replace both branches of the divergence print:

```python
    for r in diverged:
        # A count with no date weighs a choice from 2019 in a dead repo exactly
        # as heavily as one from last month. The reader can discount it; the
        # tool should not decide the history expired.
        last = max([d for d in r["dates"] if d], default="")
        age = f", last: {last[:7]}" if last else ""
        if ack:
            print(f"  DIVERGENCE from '{r['other']}' ({r['n']} projects{age}) —"
                  f" acknowledged here: \"{ack[0]['why']}\"")
        else:
            print(f"  DIVERGENCE: you chose '{r['other']}' for this in"
                  f" {r['n']} other projects{age}")
```

- [ ] **Step 5: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: print how old a norm is beside its project count"
```

---

### Task 9: A single decision choosing two options is not a contradiction

`maintain` reports `caching: redis,memcached — supersede one` for one decision
that legitimately chose both. `--chose` accepts a comma-separated list, so this
is a false positive on valid input.

**Files:**
- Modify: `scripts/precedent.py:791-801` (`apply_tag_merge`), `:803-813` (`cmd_maintain` head)
- Test: new `_check_maintain(s)`, added to the `cmd_selftest` runner list

**Interfaces:**
- Produces: `contradictions_in(s: "Store") -> list[dict]` with keys `project`, `topic`, `decisions` (a `dict` of decision id to chosen options).

- [ ] **Step 1: Write the failing test**

```python
def _check_maintain(s: Store) -> None:
    """One decision choosing redis AND memcached is a stack, not a clash.
    Two decisions in one project choosing differently is the real thing.
    """
    base = {"title": "T", "statement": "T", "rationale": "r",
            "scope": "architecture", "created": today(),
            "project_id": "/tmp/precedent-selftest-m", "project_name": "m",
            "tags": [], "topics": ["selftest-caching"],
            "rejected": [], "supersedes": []}
    try:
        write_decision(s, {**base, "id": "selftest-m1", "chose": ["redis", "memcached"]})
        assert contradictions_in(s) == [], "a multi-option decision is not a clash"
        write_decision(s, {**base, "id": "selftest-m2", "chose": ["hazelcast"]})
        clash = contradictions_in(s)
        assert len(clash) == 1 and clash[0]["topic"] == "selftest-caching", clash
        assert set(clash[0]["decisions"]) == {"selftest-m1", "selftest-m2"}, clash
    finally:
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-m' DETACH DELETE n")
        s.q("MATCH (p:Project {id:'/tmp/precedent-selftest-m'}) DETACH DELETE p")
        for name in ("selftest-caching", "redis", "memcached", "hazelcast"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL with `NameError: name 'contradictions_in' is not defined`

- [ ] **Step 3: Extract the grouping and key it on the decision**

Above `cmd_maintain`:

```python
def contradictions_in(s: "Store") -> list[dict]:
    """Two live decisions in one project answering one topic differently.

    Grouped by decision id, not by project+topic: `record --chose redis,memcached`
    is one decision picking a stack, and reporting it as a clash asks the user
    to supersede a decision that is correct.
    """
    rows = s.q("""MATCH (d:Decision)-[:ABOUT]->(t:Topic),
                        (d)-[:CHOSE]->(o:Option),
                        (d)-[:IN_PROJECT]->(p:Project)
                  WHERE d.status='active'
                  RETURN p.name AS pname, t.name AS topic, d.id AS did,
                         collect(DISTINCT o.name) AS opts""")
    grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
    for r in rows:
        grouped.setdefault((r["pname"], r["topic"]), {})[r["did"]] = sorted(r["opts"])
    out = []
    for (pname, topic), decisions in sorted(grouped.items()):
        if len(decisions) < 2:
            continue
        if len({tuple(v) for v in decisions.values()}) < 2:
            continue        # two decisions, same answer: duplicated, not conflicting
        out.append({"project": pname, "topic": topic, "decisions": decisions})
    return out
```

- [ ] **Step 4: Use it in `cmd_maintain`**

Replace the query and `clashes` lines at the top of `cmd_maintain`:

```python
    clashes = contradictions_in(s)
    print(f"== contradictions: same project, same topic, two live answers ({len(clashes)}) ==")
    for r in clashes:
        print(f"  {r['project']}/{r['topic']} — supersede one:")
        for did, opts in sorted(r["decisions"].items()):
            print(f"     {','.join(opts)}  #{did}")
```

- [ ] **Step 5: Delete the dead line in `apply_tag_merge`**

Remove entirely:

```python
    s.q("""MATCH (p:Project)-[r:TAGGED]->(:Tag {name:$f})
           DELETE r""", {"f": frm}) if False else None
```

- [ ] **Step 6: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/precedent.py
git commit -m "fix: group contradictions by decision so a chosen stack is not a clash"
```

---

### Task 10: An acknowledged divergence answers one norm, not the topic

`check`'s `ack` lookup finds any decision in this project carrying `despite` on
this topic, so a single acknowledged exception silences every divergence warning
there — including ones it never addressed.

**Files:**
- Modify: `scripts/precedent.py:665-681` (`ack` query and the divergence loop)
- Test: `_check_verdicts(s)`

**Interfaces:**
- Consumes: the `(Decision)-[:DIVERGES_FROM]->(Decision)` edge written by `write_decision:365-371`.
- Produces: no new names; `ack` moves inside the `diverged` loop.

- [ ] **Step 1: Write the failing test**

Add to `_check_verdicts(s)`'s Task 7 `try:` block:

```python
        # Two norms on one topic: mongo (2 projects) and cassandra (2 projects).
        # The exception is recorded against the mongo one only, so the
        # cassandra warning must still fire unacknowledged.
        for n, pid in (("selftest-v4", "/tmp/precedent-selftest-v4"),
                       ("selftest-v5", "/tmp/precedent-selftest-v5")):
            write_decision(s, {**base, "id": n, "chose": ["cassandra"],
                               "rejected": [], "supersedes": [],
                               "project_id": pid, "project_name": n})
        write_decision(s, {**base, "id": "selftest-v6", "chose": ["sqlite"],
                           "rejected": [], "supersedes": [],
                           "despite": "single user, no concurrency",
                           "diverges_from": ["selftest-v2"],
                           "project_id": "/tmp", "project_name": "here"})
        out3 = io.StringIO()
        with contextlib.redirect_stdout(out3):
            cmd_check(_argparse.Namespace(topic="selftest-persistence",
                                          chose="sqlite", project="/tmp"), s)
        scoped = out3.getvalue()
        warnings = [l for l in scoped.splitlines() if "DIVERGENCE" in l]
        mongo = [l for l in warnings if "mongo" in l]
        cass = [l for l in warnings if "cassandra" in l]
        assert mongo and "acknowledged here" in mongo[0], f"{warnings}"
        assert cass and "acknowledged here" not in cass[0], \
            f"an unrelated norm must not be reported as acknowledged: {cass}"
```

Extend that block's `finally:` with the two extra project deletes
(`/tmp/precedent-selftest-v4`, `/tmp/precedent-selftest-v5`), the `/tmp` project
delete, and `"cassandra"` and `"sqlite"` in the orphan-option sweep.

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL — the unrelated norm is reported as acknowledged

- [ ] **Step 3: Scope the lookup to the norm being diverged from**

Delete the standalone `ack = s.q(...)` block. Inside the `for r in diverged:`
loop, above the `last =` line, add:

```python
        # Scoped to THIS norm: the exception was argued out against a specific
        # prior decision, and letting it answer every warning on the topic is
        # how one acknowledged divergence hides three unacknowledged ones.
        ack = s.q("""MATCH (d:Decision)-[:ABOUT]->(:Topic {name:$t}),
                           (d)-[:IN_PROJECT]->(:Project {id:$pid}),
                           (d)-[:DIVERGES_FROM]->(:Decision)-[:CHOSE]->(:Option {name:$other})
                     WHERE d.status='active' AND d.despite IS NOT NULL
                     RETURN d.despite AS why LIMIT 1""",
                  {"t": topic, "pid": str(pathlib.Path(a.project).resolve()),
                   "other": r["other"]})
```

- [ ] **Step 4: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "fix: scope an acknowledged divergence to the norm it answers"
```

---

### Task 11: Principles match on an edge, not a substring

`check` finds principles with `pr.statement CONTAINS $topic`, so topic `auth`
matches a principle whose prose mentions `author`. `Principle` has no `ABOUT`
edge to match on properly.

There are currently **zero** `Principle` nodes in the graph, so no migration is
needed; verify with the command in Step 6 before starting.

**Files:**
- Modify: `scripts/precedent.py:469-478` (`cmd_principle`), `:683-686` (`violated`), `:729-733` (`cmd_suggest` promote hint), `:1207-1212` (`principle` subparser), `:914-920` (`replay_entry` principle branch)
- Modify: `references/schema.md` (edge list and node table)
- Test: `_check_verdicts(s)`

**Interfaces:**
- Consumes: `csv()`, `Store.log`.
- Produces: `(Principle)-[:ABOUT]->(Topic)` edge; `principle --topic` argument; journal `principle` entries gain a `topics` list.

- [ ] **Step 1: Write the failing test**

Add a self-contained block to `_check_verdicts(s)`:

```python
    import argparse as _argparse
    import contextlib
    import io

    d = {"id": "selftest-p1", "title": "T", "statement": "T", "rationale": "r",
         "scope": "architecture", "created": today(),
         "project_id": "/tmp/precedent-selftest-p", "project_name": "p",
         "tags": [], "topics": ["selftest-auth"], "chose": ["oidc"],
         "rejected": [], "supersedes": []}
    try:
        write_decision(s, d)
        # The prose contains the topic word as a substring. Only the edge
        # should decide, so this principle must NOT surface for selftest-auth.
        s.q("""MERGE (pr:Principle {id:'selftest-p'})
               SET pr.statement='Prefer one selftest-author per module.',
                   pr.created=$c""", {"c": today()})
        s.q("MERGE (t:Topic {name:'selftest-p-other'})")
        s.q("""MATCH (pr:Principle {id:'selftest-p'}),(t:Topic {name:'selftest-p-other'})
               MERGE (pr)-[:ABOUT]->(t)""")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_check(_argparse.Namespace(topic="selftest-auth",
                                          chose="oidc", project="/tmp"), s)
        text = out.getvalue()
        assert "PRINCIPLE" not in text, \
            f"'selftest-author' in the prose must not match topic 'selftest-auth':\n{text}"

        s.q("""MATCH (pr:Principle {id:'selftest-p'}),(t:Topic {name:'selftest-auth'})
               MERGE (pr)-[:ABOUT]->(t)""")
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            cmd_check(_argparse.Namespace(topic="selftest-auth",
                                          chose="oidc", project="/tmp"), s)
        assert "PRINCIPLE" in out2.getvalue(), \
            f"a principle with the topic edge must surface:\n{out2.getvalue()}"
    finally:
        s.q("MATCH (pr:Principle {id:'selftest-p'}) DETACH DELETE pr")
        s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-p' DETACH DELETE n")
        s.q("MATCH (p:Project {id:'/tmp/precedent-selftest-p'}) DETACH DELETE p")
        for name in ("selftest-auth", "selftest-p-other", "oidc"):
            s.q("""MATCH (n) WHERE (n:Topic OR n:Option) AND n.name = $name
                     AND NOT EXISTS { MATCH (n)<--() } DETACH DELETE n""", {"name": name})
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL — `AssertionError: 'selftest-author' in the prose must not match
topic 'selftest-auth'`. The existing `pr.statement CONTAINS $t` query matches
the substring.

- [ ] **Step 3: Accept and store a topic on a principle**

```python
def cmd_principle(a, s: Store) -> None:
    p = {"id": a.id, "statement": a.statement, "derived_from": csv(a.derived_from),
         "topics": [t.lower() for t in csv(a.topic)]}
    s.log("principle", p)
    s.q("""MERGE (pr:Principle {id:$id}) SET pr.statement=$statement, pr.created=$created""",
        {**p, "created": today()})
    for topic in p["topics"]:
        s.q("MERGE (t:Topic {name:$n})", {"n": topic})
        s.q("""MATCH (pr:Principle {id:$id}),(t:Topic {name:$n})
               MERGE (pr)-[:ABOUT]->(t)""", {"id": a.id, "n": topic})
    for did in p["derived_from"]:
        s.q("""MATCH (pr:Principle {id:$id}),(d:Decision {id:$did})
               MERGE (pr)-[:DERIVED_FROM]->(d)""", {"id": a.id, "did": did})
    print(f"principle {a.id}: {a.statement}")
    if not p["topics"]:
        print("  note: no --topic, so `check` will never surface this principle.")
```

Add to the `principle` subparser in `main`:

```python
    pr.add_argument("--topic", default="", help="comma-separated topics this governs")
```

- [ ] **Step 4: Replay the topics**

In `replay_entry`'s `principle` branch, after the `MERGE (pr:Principle ...)`:

```python
        for topic in e.get("topics", []):
            s.q("MERGE (t:Topic {name:$n})", {"n": topic})
            s.q("""MATCH (pr:Principle {id:$id}),(t:Topic {name:$n})
                   MERGE (pr)-[:ABOUT]->(t)""", {"id": e["id"], "n": topic})
```

- [ ] **Step 5: Match on the edge in `cmd_check`**

```python
    violated = s.q("""MATCH (pr:Principle)-[:ABOUT]->(:Topic {name:$t})
                      RETURN pr.id AS id, pr.statement AS stmt""", {"t": topic})
```

- [ ] **Step 6: Update the promote hint and the schema doc**

In `cmd_suggest`, add the flag to the printed command:

```python
        print(f"     promote: precedent.py principle --id {pid}"
              f" --topic {r['topic']}"
              f" --statement \"For {r['topic']}, use {r['opt']}.\"")
```

In `references/schema.md`, add `(Principle)-[:ABOUT]->(Topic)` to the edge
block, and note that a principle without it is invisible to `check`.

- [ ] **Step 7: Confirm no principles need migrating, then verify**

```bash
uv run scripts/precedent.py cypher "MATCH (pr:Principle) RETURN count(pr) AS n"
uv run scripts/precedent.py --home /tmp/precedent-plan selftest
grep -n "CONTAINS" scripts/precedent.py
```
Expected: `{'n': 0}`; selftest passes; the grep prints nothing.

- [ ] **Step 8: Commit**

```bash
git add scripts/precedent.py references/schema.md
git commit -m "fix: match principles on a topic edge, not a substring of their prose"
```

---

## Phase 4 — Identity

Implements ADR 0002. A repository checked out on two machines is currently two
Projects sharing no precedent, and `init`'s headline use — a synced store — is
the configuration that produces it.

### Task 12: Derive a portable id from the git remote

**Files:**
- Modify: `scripts/precedent.py:119-141` (beside `detect_project`)
- Test: new `_check_identity(s)`, added to the `cmd_selftest` runner list

**Interfaces:**
- Produces:
  - `normalise_remote(url: str) -> str | None` — `github.com/owner/repo`, lowercased, scheme/credentials/port/`.git` removed.
  - `portable_id(root: pathlib.Path) -> str | None` — `normalise_remote` of `origin`, plus `#/<subpath>` when `root` is below the repository top level. `None` when there is no git, no `origin`, or no `git` binary.

- [ ] **Step 1: Write the failing test**

```python
def _check_identity(s: Store) -> None:
    """A home-relative id was rejected: /home/u/src/api and C:\\dev\\api are the
    same repo at different relative paths, and two different projects can sit
    at the same relative path on two machines. The remote is the identity.
    """
    for raw, want in [
        ("https://github.com/asm0dey/precedent.git", "github.com/asm0dey/precedent"),
        ("git@github.com:asm0dey/precedent.git", "github.com/asm0dey/precedent"),
        ("ssh://git@github.com/asm0dey/precedent.git", "github.com/asm0dey/precedent"),
        ("https://user:token@github.com/o/r.git", "github.com/o/r"),
        ("ssh://git@host:2222/o/r.git", "host/o/r"),
        ("https://GitHub.com/Asm0dey/Precedent", "github.com/asm0dey/precedent"),
        ("", None),
    ]:
        assert normalise_remote(raw) == want, f"{raw!r} -> {normalise_remote(raw)!r}"

    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "repo"
        (root / "mod").mkdir(parents=True)
        run = lambda *a: subprocess.run(["git", "-C", str(root), *a],
                                        capture_output=True, check=True)
        assert portable_id(root) is None, "no git dir yet"
        run("init", "-q")
        assert portable_id(root) is None, "a repo with no origin has no portable id"
        run("remote", "add", "origin", "git@github.com:asm0dey/precedent.git")
        assert portable_id(root) == "github.com/asm0dey/precedent"
        assert portable_id(root / "mod") == "github.com/asm0dey/precedent#/mod"
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL with `NameError: name 'normalise_remote' is not defined`

- [ ] **Step 3: Implement both functions**

Insert directly above `detect_project`:

```python
def normalise_remote(url: str) -> str | None:
    """Reduce any shape git hands back to `host/owner/repo`.

    Lowercased so two clones agree. That loses case on a local-path remote,
    which is an acceptable trade for an identity that has to match across
    machines.
    """
    url = url.strip()
    if not url:
        return None
    url = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", url)   # https:// ssh:// git://
    url = re.sub(r"^[^/@]*@", "", url)                       # git@ or user:token@
    host, sep, path = url.partition("/")
    if ":" in host:
        host, _, extra = host.partition(":")
        if not extra.isdigit():                              # scp-style host:owner/repo
            path = f"{extra}/{path}" if sep else extra
    url = f"{host}/{path}".rstrip("/") if path else host
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower() or None


def portable_id(root: pathlib.Path) -> str | None:
    """The identity that survives a machine, a clone location and an OS.

    None is a fine answer — a project with no remote is correctly
    machine-local. A guessed id is not: it silently merges the histories of
    two unrelated projects, which is the failure a wrong tag causes.
    """
    import subprocess

    def git(*args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(root), *args],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    remote = normalise_remote(git("remote", "get-url", "origin") or "")
    if not remote:
        return None
    top = git("rev-parse", "--show-toplevel")
    if not top:
        return remote
    try:
        sub = root.resolve().relative_to(pathlib.Path(top).resolve())
    except ValueError:
        return remote
    # as_posix keeps the key identical on Windows; a backslash here would
    # split the graph exactly the way the native path already does.
    return remote if str(sub) == "." else f"{remote}#/{sub.as_posix()}"
```

- [ ] **Step 4: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: derive a portable project id from the git remote"
```

---

### Task 13: Resolve a project by its portable id, on reads and writes

**Files:**
- Modify: `scripts/precedent.py:143-153` (`tags_of`, `project_info`), `:162-207` (`enclosing`, `contained`, `same_tree`, `effective_tags`), `:323-334` (`upsert_project`, `attach_tags`), `:482-493` (`worth_backfilling`), `:665` (`cmd_check`'s `ack` pid)
- Modify: `references/schema.md` (Project properties, Containment section)
- Test: `_check_identity(s)`

**Interfaces:**
- Consumes: `portable_id` from Task 12.
- Produces:
  - `project_info` returns `{"id", "path", "portable", "paths", "name", "contents", "tags"}` where **`id` is the graph key** (the native path of first sighting) and **`path` is this machine's path**. Everything that queries a node uses `id`; all path arithmetic uses `path` / `paths`.
  - `paths_of(row: dict) -> list[str]` — splits the newline-delimited `Project.paths` property.
  - `enclosing(s, info)` / `contained(s, info)` / `same_tree(s, info)` now take the info dict, not an id string.

- [ ] **Step 1: Write the failing test**

Append to `_check_identity(s)`:

```python
    # Two machines, one repo: the second must resolve onto the first node,
    # for reads AND writes. Read-only resolution finds the existing node
    # while writes create a second one, fragmenting the graph a little more
    # with every machine and every session, invisibly.
    linux, windows = "/tmp/precedent-selftest-i", "C:\\dev\\precedent-selftest-i"
    pp = "github.com/asm0dey/selftest-i"
    try:
        a_info = upsert_project(s, {"id": linux, "path": linux, "name": "i",
                                    "portable": pp})
        b_info = upsert_project(s, {"id": windows, "path": windows, "name": "i",
                                    "portable": pp})
        assert b_info["id"] == linux, "the second sighting must resolve onto the first"
        assert s.q("MATCH (p:Project {portable:$pp}) RETURN count(p) AS n",
                   {"pp": pp}) == [{"n": 1}], "one repo, one node"
        row = s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths", {"id": linux})[0]
        assert set(paths_of(row)) == {linux, windows}, row
    finally:
        s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": linux})
        s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": windows})
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL — `AssertionError: the second sighting must resolve onto the first`

- [ ] **Step 3: Add the path-list helper**

Above `tags_of`:

```python
def paths_of(row: dict) -> list[str]:
    """Project.paths is newline-delimited rather than a list property.

    Every consumer already filters in Python — containment reads all
    projects and compares there — so a string costs nothing and does not
    depend on the graph engine supporting list properties.
    """
    return [p for p in (row.get("paths") or "").split("\n") if p]
```

- [ ] **Step 4: Resolve in `upsert_project`**

```python
def upsert_project(s: Store, info: dict) -> dict:
    """Resolve by portable id first, then fall back to the native path.

    The returned dict's `id` is the graph key, which may be a path from
    another machine. `path` stays this machine's, because containment is
    still derived from paths and must be derived from local ones.
    """
    local, portable = info["path"], info.get("portable")
    key = local
    if portable:
        rows = s.q("MATCH (p:Project {portable:$pp}) RETURN p.id AS id LIMIT 1",
                   {"pp": portable})
        if rows:
            key = rows[0]["id"]
    s.q("""MERGE (p:Project {id:$id}) SET p.name=$name, p.seen=$seen""",
        {"id": key, "name": info["name"], "seen": today()})
    if portable:
        s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp", {"id": key, "pp": portable})
    row = s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths", {"id": key})[0]
    known = paths_of(row)
    if local not in known:
        known.append(local)
        s.q("MATCH (p:Project {id:$id}) SET p.paths=$paths",
            {"id": key, "paths": "\n".join(sorted(known))})
    info["id"], info["paths"] = key, known
    return info
```

- [ ] **Step 5: Make `project_info` carry both**

```python
def project_info(s: "Store", path: str, extra_tags: str = "") -> dict:
    info = detect_project(pathlib.Path(path))
    info["path"] = info["id"]
    info["portable"] = portable_id(pathlib.Path(info["path"]))
    if info["portable"]:
        rows = s.q("""MATCH (p:Project {portable:$pp})
                      RETURN p.id AS id, p.paths AS paths LIMIT 1""",
                   {"pp": info["portable"]})
        if rows:
            info["id"], info["paths"] = rows[0]["id"], paths_of(rows[0])
    info.setdefault("paths", [info["path"]])
    info["tags"] = sorted(set(tags_of(s, info["id"])) | set(csv(extra_tags)))
    return info
```

- [ ] **Step 6: Point the path arithmetic at local paths**

Change the three containment helpers to take the info dict and compare against
every path a project is known at:

```python
def enclosing(s: "Store", info: dict) -> list[dict]:
    """Projects that physically contain this one, outermost first."""
    sep = os.sep
    here = info["path"]
    rows = s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name, p.paths AS paths")
    out = [r for r in rows
           if r["id"] != info["id"]
           and any(here.startswith(p.rstrip(sep) + sep) for p in paths_of(r) or [r["id"]])]
    return sorted(out, key=lambda r: len(r["id"]))


def contained(s: "Store", info: dict) -> list[dict]:
    """Projects physically inside this one — the modules of a monorepo."""
    sep = os.sep
    here = info["path"].rstrip(sep) + sep
    rows = s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name, p.paths AS paths")
    return sorted((r for r in rows
                   if r["id"] != info["id"]
                   and any(p.startswith(here) for p in paths_of(r) or [r["id"]])),
                  key=lambda r: r["id"])


def same_tree(s: "Store", info: dict) -> set[str]:
    """This project plus everything above and below it in the filesystem."""
    return ({info["id"]}
            | {r["id"] for r in enclosing(s, info)}
            | {r["id"] for r in contained(s, info)})
```

- [ ] **Step 7: Update every caller**

- `effective_tags(s, info)` — `for anc in enclosing(s, info):`
- `neighbours(s, info, ...)` — `skip = same_tree(s, info)`
- `cmd_brief` — `above, below = enclosing(s, info), contained(s, info)`
- `worth_backfilling(s, info)` — `pathlib.Path(info["path"]) / ".git"`
- `cmd_check`'s `ack` — replace the inline `str(pathlib.Path(a.project).resolve())` with `project_info(s, a.project)["id"]`, computed once above the verdict block
- `cmd_record` / `cmd_tag` / `cmd_suggest` — no change; they already pass `info` through `upsert_project`, which now returns the resolved `id`
- `write_decision` — `upsert_project(s, {"id": d["project_id"], "path": d.get("project_path", d["project_id"]), "name": d["project_name"], "portable": d.get("portable")})`, then use the returned `id` for the `IN_PROJECT` edge
- `_check_projects(s)` — the fixtures build info dicts by hand and must gain a
  `path`. Every `upsert_project(s, {"id": pid, "name": name})` becomes
  `upsert_project(s, {"id": pid, "path": pid, "name": name})`, and every
  `neighbours(s, {"id": x, "tags": [...]})` becomes
  `neighbours(s, {"id": x, "path": x, "tags": [...]})`. The containment asserts
  change to the new signatures: `enclosing(s, {"id": mod, "path": mod})`,
  `contained(s, {"id": root, "path": root})`,
  `enclosing(s, {"id": sibling, "path": sibling})`, and
  `effective_tags(s, {"id": mod, "path": mod, "tags": ["selftest-java"]})`

- [ ] **Step 8: Journal the portable id**

In `cmd_record`'s `d` dict, add `"portable": info["portable"]` and
`"project_path": info["path"]`. `replay_entry` needs no change — it passes the
whole entry to `write_decision`, and an old entry without those keys falls back
to the native path.

- [ ] **Step 9: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS — including the pre-existing containment asserts in
`_check_projects`, which exercise `root` / `mod` / `sibling` through the new
signatures.

- [ ] **Step 10: Verify against the real graph and update the schema doc**

```bash
uv run scripts/precedent.py rebuild
uv run scripts/precedent.py brief --project .
uv run scripts/precedent.py cypher "MATCH (p:Project) RETURN p.name AS name, p.portable AS portable, p.paths AS paths"
```
Expected: rebuild replays cleanly; the brief still shows 7 decisions here and 4
inherited from `my-decisions`; `precedent` reports a `portable` value and
`my-decisions` reports its own.

In `references/schema.md`, add `portable` and `paths` to the `Project`
properties row, and rewrite the Containment section: containment is still
derived from paths, but from `paths`, and identity is `portable` with `id` as
the native path of first sighting.

- [ ] **Step 11: Commit**

```bash
git add scripts/precedent.py references/schema.md
git commit -m "feat: resolve a project by portable id on reads and writes"
```

---

### Task 14: Backfill lazily, and stop calling absent projects dead

**Files:**
- Modify: `scripts/precedent.py:495-520` (`cmd_brief` head), `:838-845` (`cmd_maintain` liveness), `:482-493` (`worth_backfilling` docstring)
- Test: `_check_identity(s)`, `_check_maintain(s)`

**Interfaces:**
- Consumes: `paths_of`, `portable_id`, `upsert_project` from Task 13.
- Produces: `liveness(row: dict) -> str` returning `"live"`, `"elsewhere"` or `"gone"`.

- [ ] **Step 1: Write the failing test**

Append to `_check_maintain(s)`, inside its `try:`:

```python
    # "The directory is not here" was never evidence a project is dead. It is
    # equally consistent with an unmounted drive, another checkout, or a
    # machine you are not sitting at — and dead projects get discounted.
    assert liveness({"id": "/tmp", "paths": "/tmp"}) == "live"
    assert liveness({"id": "C:\\dev\\x", "paths": "C:\\dev\\x"}) == "elsewhere"
    assert liveness({"id": "/tmp", "paths": "/nope/x\n/tmp"}) == "live", \
        "live if ANY known path exists"
    assert liveness({"id": "/nope/x", "paths": "/nope/x"}) == "gone"
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: FAIL with `NameError: name 'liveness' is not defined`

- [ ] **Step 3: Implement the three-state check**

Above `cmd_maintain`:

```python
def liveness(row: dict) -> str:
    """live | elsewhere | gone.

    A path shaped for another platform is unknown, not dead: on Linux every
    Windows-recorded project fails an exists() test, and feeding that into a
    discount rule writes off the other machine's whole history.
    """
    known = paths_of(row) or [row["id"]]
    if any(pathlib.Path(p).exists() for p in known):
        return "live"
    foreign = os.sep == "/" and any(re.match(r"^[A-Za-z]:\\", p) for p in known)
    foreign = foreign or (os.sep == "\\" and all(p.startswith("/") for p in known))
    return "elsewhere" if foreign else "gone"
```

- [ ] **Step 4: Use it in `cmd_maintain`**

Replace the `gone = [...]` block and its print:

```python
    projects = s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name, p.paths AS paths")
    states = {"gone": [], "elsewhere": []}
    for r in projects:
        st = liveness(r)
        if st != "live":
            states[st].append(r)
    print(f"\n== projects whose path no longer exists ({len(states['gone'])}) ==")
    for r in states["gone"]:
        print(f"  {r['name']}  ({r['id']})")
    print(f"\n== projects recorded on another machine ({len(states['elsewhere'])}) ==")
    for r in states["elsewhere"]:
        print(f"  {r['name']}  ({r['id']})  — not gone, just not here")
```

- [ ] **Step 5: Backfill the portable id from `brief`**

In `cmd_brief`, directly after `info = project_info(s, a.project)`:

```python
    # brief runs on every session start, so this is the cheapest place to fill
    # in a portable id that did not exist when the project was first recorded —
    # a repo that gains a remote later would otherwise stay split forever.
    # This writes to an EXISTING node only; brief must still never create one.
    if info["portable"] and s.q("MATCH (p:Project {id:$id}) RETURN p.portable AS pp",
                                {"id": info["id"]}) == [{"pp": None}]:
        s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp",
            {"id": info["id"], "pp": info["portable"]})
```

Update the docstring comment at the top of `cmd_brief`, which currently claims
the command never writes:

```python
    # Writes to an existing Project node only, to backfill a portable id. Never
    # creates one: a SessionStart hook calls this in every directory the user
    # opens, and creating would litter the graph with empty projects.
```

- [ ] **Step 6: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-plan selftest`
Expected: PASS

- [ ] **Step 7: Verify end to end**

```bash
uv run scripts/precedent.py maintain
uv run scripts/precedent.py brief --project .
uv run scripts/precedent.py --home /tmp/precedent-plan selftest
```
Expected: `maintain` prints both liveness sections with zero entries each; the
brief is unchanged; selftest reports `unchanged`.

- [ ] **Step 8: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: backfill portable ids lazily and report absent projects as elsewhere"
```

---

## Done when

```bash
uv run scripts/precedent.py --home /tmp/precedent-plan selftest   # ok, unchanged
uv run scripts/precedent.py rebuild                                # every entry replayed
uv run scripts/precedent.py maintain                               # no contradictions, no spelling dupes
uv run scripts/precedent.py check --topic database --chose sqlite   # vocabulary, never "clear"
grep -nE "fcntl|symlink|close_matches|refuse_drift|CONTAINS" scripts/precedent.py   # no output
```

Then plan B (packaging: `adapters/`, `standing-orders`, dual manifest, CI
matrix) and plan C (measurement: capture evals, the Codex experiment).
