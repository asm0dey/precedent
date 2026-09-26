# Remote-keyed project identity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Key every Project node by its git remote (main-checkout path when there is none), refuse a `--project` that is not a directory, fold older keys into the new one lazily, and add a journaled `merge-project` command — fixing issue #13.

**Architecture:** Identity is computed in `project_info` (git remote → portable id; else the worktree-aware main-checkout path). One graph primitive, `fold_project(from, into)`, moves decisions/tags/paths between nodes; `upsert_project` uses it to absorb older keys on every write and every replay, `settle()` uses it (journalled) from the writing commands, and `merge-project` exposes it. A `(:Meta {graph_format: 2})` marker triggers a one-time journal replay that re-keys an existing store. `SCHEMA` stays `1` so 0.4.x keeps replaying.

**Tech Stack:** Python ≥3.12, `graphdblite`, `uv run` + PEP 723, `argparse`, `git` CLI via `subprocess`. Tests are `_check_*` functions inside `scripts/precedent.py`, run by `precedent.py selftest` — there is no pytest.

**Spec:** `docs/superpowers/specs/2026-09-26-project-identity-design.md` (read it before starting).

## Global Constraints

- One file: `scripts/precedent.py`. No new dependencies (`graphdblite` only).
- Must run on Linux, macOS, Windows. No literal `/tmp` for anything touched on disk — use `tempfile`. Graph-key-only strings like `"/tmp/precedent-selftest-x"` are fine (existing checks do this).
- **`SCHEMA` stays `1`.** 0.4.x stops a rebuild (after wiping) at any line with a higher `v`, and `Store.log` stamps every line. An unknown op at `v: 1` is skipped and reported instead.
- `journal.jsonl` is append-only. Never edit or reorder lines.
- **Always run selftest against a throwaway home**, never the real store — Task 6 makes any writer migrate the store it opens:
  `uv run scripts/precedent.py --home "$(mktemp -d)" selftest` → expected last line `selftest ok (N nodes, unchanged)`.
- Every check cleans up after itself (node-count invariant in `cmd_selftest`). Checks that journal use a throwaway `Store(pathlib.Path(tempfile.mkdtemp-dir), write=True)`.
- Errors a caller must act on: message to **stderr**, `raise SystemExit(2)`.
- Matching is exact string equality (`docs/adr/0001`). Never guess an identity: several remotes and no `origin` = no remote.
- Cypher values always go through parameters.
- **Refinements of the spec, deliberate:**
  - Folds performed at runtime by `settle()` / `merge-project` are journalled as `project_merge` (spec §4 said automatic folds need none). A remote-less worktree keyed on its own path cannot be re-folded from journal data alone, so the fold itself must be recorded. `upsert_project`'s own folds (by portable id / by path) stay unjournalled: replay reproduces them from `portable`/`project_path` already in each line.
  - The command is `merge-project --from <id> --into <id-or-path>` — flags, matching `tag --merge/--into` and `amend --id`. Do not change it to a positional.
- Commit messages end with: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## Review Focus

Inputs the spec implies but does not spell out; each has a test in the owning task.

1. **A git submodule** — its common dir is `<super>/.git/modules/<name>`, which must not be mistaken for a linked worktree; it keeps its own path. (Task 1)
2. **`--project` naming a file, not a directory** — same exit-2 error as a missing path. (Task 2)
3. **A subdirectory of a worktree** (`wt/mod`) — maps to `main/mod`, and with a remote to `…#/mod`. (Task 1)
4. **`merge-project --into .` whose `--from` is this project's own older key** — reported as folded, not an error. (Task 5)
5. **A worktree whose main checkout was moved away** — keyed by its own path, no crash. (Task 1)

---

### Task 1: Remote selection and worktree-aware identity path

Pure helpers, no graph. `portable_id` learns "the only remote counts"; `identity_path` maps a linked worktree onto its main checkout.

**Files:**
- Modify: `scripts/precedent.py` — `portable_id` (~line 398); add `git_out`, `remote_url`, `identity_path` directly above `portable_id`; add `_git`, `_git_repo`, `_check_worktrees` after `_check_detect_project` (~line 1877); register in `cmd_selftest` after `_check_detect_project()`.

**Interfaces:**
- Consumes: `normalise_remote(url: str) -> str | None` (existing).
- Produces:
  - `git_out(root: pathlib.Path, *args: str) -> str | None`
  - `remote_url(root: pathlib.Path) -> str` (`""` when none)
  - `portable_id(root: pathlib.Path) -> str | None` (same signature, new remote rule)
  - `identity_path(root: pathlib.Path) -> pathlib.Path` (always resolved)
  - selftest helpers `_git(root, *args) -> None`, `_git_repo(root: pathlib.Path, remote: str | None = None) -> None` — Task 4 and Task 7 reuse them.

- [ ] **Step 1: Write the failing test**

Add after `_check_detect_project`:

```python
def _git(root: pathlib.Path, *args: str) -> None:
    import subprocess
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


def _git_repo(root: pathlib.Path, remote: str | None = None) -> None:
    """A repo at `root` with one commit — `git worktree add` needs a HEAD.

    Identity and signing come from `-c` so the check runs on any machine,
    including one that signs every commit.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "README").write_text("selftest")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=selftest", "-c", "user.email=selftest@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    if remote:
        _git(root, "remote", "add", "origin", remote)


def _check_worktrees() -> None:
    """A worktree is its main checkout; a submodule is not.

    Only a repo with a remote got this for free, through the portable id.
    Without one, the worktree's own path keyed a second project — issue #13.
    """
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve()
        main, wt = base / "main", base / "wt"
        (main / "mod").mkdir(parents=True)
        (main / "mod" / "f").write_text("x")
        _git_repo(main)
        _git(main, "worktree", "add", "-q", str(wt))

        assert identity_path(main) == main
        assert identity_path(main / "mod") == main / "mod"
        assert identity_path(wt) == main, "a remote-less worktree is its main checkout"
        assert identity_path(wt / "mod") == main / "mod", "its subdirectories map with it"
        assert portable_id(wt) is None

        _git(main, "remote", "add", "upstream", "git@github.com:asm0dey/selftest-wt.git")
        assert portable_id(main) == "github.com/asm0dey/selftest-wt", \
            "the only remote counts, whatever it is called"
        assert portable_id(wt / "mod") == "github.com/asm0dey/selftest-wt#/mod"
        _git(main, "remote", "add", "fork", "git@github.com:someone/selftest-wt.git")
        assert portable_id(main) is None, "two remotes and no origin is not guessed at"
        _git(main, "remote", "add", "origin", "git@github.com:asm0dey/selftest-wt.git")
        assert portable_id(main) == "github.com/asm0dey/selftest-wt"

        # A submodule's common dir is <super>/.git/modules/<name>, and it is
        # not a linked worktree: it keeps its own path.
        src = base / "subsrc"
        _git_repo(src)
        # as_uri(): a file:// URL is accepted by git on every OS; a bare
        # Windows path with backslashes is not reliably.
        _git(main, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
             src.as_uri(), "vendored")
        assert identity_path(main / "vendored") == main / "vendored", \
            "a submodule must not be mapped onto .git/modules"

        # The main checkout moved away: git cannot answer, so no mapping.
        shutil.move(str(main), str(base / "moved"))
        assert identity_path(wt) == wt, "a worktree whose main checkout is gone keys itself"
```

Register in `cmd_selftest`, right after `_check_detect_project()`:

```python
    _check_worktrees()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: FAIL with `NameError: name 'identity_path' is not defined`

- [ ] **Step 3: Implement**

Replace the body of `portable_id` and add the helpers above it. Keep `portable_id`'s docstring; replace its inner `git()` closure:

```python
def git_out(root: pathlib.Path, *args: str) -> str | None:
    """stdout of one git command run in `root`; None if git is missing, slow or fails.

    None is a fine answer everywhere this is used: no remote, no worktree
    mapping. A guessed answer is not — it merges two projects' histories.
    """
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def remote_url(root: pathlib.Path) -> str:
    """origin, else the only remote there is, else nothing.

    Several remotes and no origin is not guessed at: picking one is how a
    fork and its upstream would end up sharing one identity.
    """
    url = git_out(root, "remote", "get-url", "origin")
    if url:
        return url
    names = (git_out(root, "remote") or "").split()
    return (git_out(root, "remote", "get-url", names[0]) or "") if len(names) == 1 else ""


def identity_path(root: pathlib.Path) -> pathlib.Path:
    """This directory as its main checkout sees it.

    A linked worktree is the same project as the checkout it was added from.
    A repo with a remote gets that through the portable id; one without would
    key a second project on the worktree's own path. Only a linked worktree
    is mapped — its git dir differs from the common dir — so a submodule,
    whose common dir is `.git/modules/<name>`, keeps its own path.

    ponytail: a worktree of a submodule maps onto .git/modules/<name>. Still
    one stable key per project, just not a pretty one.
    """
    root = root.resolve()
    top = git_out(root, "rev-parse", "--show-toplevel")
    git_dir = git_out(root, "rev-parse", "--path-format=absolute", "--git-dir")
    common = git_out(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not (top and git_dir and common):
        return root
    common_p = pathlib.Path(common).resolve()
    if pathlib.Path(git_dir).resolve() == common_p:
        return root
    main = common_p.parent if common_p.name == ".git" else common_p
    if not main.is_dir():
        return root
    return main / root.relative_to(pathlib.Path(top).resolve())
```

In `portable_id`, delete the nested `import subprocess` / `def git(...)` block and change the two call sites:

```python
    remote = normalise_remote(remote_url(root))
    if not remote:
        return None
    top = git_out(root, "rev-parse", "--show-toplevel")
```

(the rest of `portable_id` is unchanged).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: `selftest ok (…)`

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: worktree-aware identity path; the only remote counts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Refuse a `--project` that is not a directory

The issue #13 fix proper. Every `--project` goes through `project_info`.

**Files:**
- Modify: `scripts/precedent.py` — add `refuse_missing_project` above `project_info` (~line 471); call it first in `project_info`; `_check_verdicts`' `here` (~line 2534) becomes a real temp dir; add `_check_missing_project` after `_check_worktrees`; register it after `_check_worktrees()`.

**Interfaces:**
- Consumes: `Store.q`.
- Produces: `refuse_missing_project(s: Store, value: str) -> None` (returns, or prints to stderr and `raise SystemExit(2)`). Task 4 keeps calling it as the first line of the rewritten `project_info`.

- [ ] **Step 1: Write the failing test**

```python
def _check_missing_project(s: Store) -> None:
    """`--project calit` resolved to <cwd>/calit, a directory that does not
    exist, and keyed a fresh untagged project every time — issue #13. It must
    be refused, create nothing, and say what was probably meant.
    """
    import contextlib
    import io
    import tempfile

    count = lambda: s.q("MATCH (p:Project) RETURN count(p) AS n")[0]["n"]
    before = count()
    cwd = os.getcwd()
    named = "selftest-named-project"
    try:
        s.q("CREATE (:Project {id:'selftest-named-id', name:$n})", {"n": named})
        with tempfile.TemporaryDirectory() as tmp:
            here = pathlib.Path(tmp) / "calit"
            here.mkdir()
            (here / "notes.txt").write_text("x")
            os.chdir(here)
            try:
                for value, hint in (("calit", "did you mean --project . ?"),
                                    ("notes.txt", None),   # a file is not a project
                                    (named, f"A project named {named!r} exists"),
                                    ("nope-selftest", None)):
                    err = io.StringIO()
                    try:
                        with contextlib.redirect_stderr(err):
                            project_info(s, value)
                        raise AssertionError(f"--project {value!r} must be refused")
                    except SystemExit as exc:
                        assert exc.code == 2, (value, exc.code)
                    assert "is not a directory" in err.getvalue(), err.getvalue()
                    if hint:
                        assert hint in err.getvalue(), err.getvalue()
            finally:
                os.chdir(cwd)   # before the directory is removed: Windows cannot delete a cwd
    finally:
        s.q("MATCH (p:Project {id:'selftest-named-id'}) DETACH DELETE p")
    assert count() == before, "a refused --project must create no node"
```

Register after `_check_worktrees()`:

```python
    _check_missing_project(s)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: FAIL with `AssertionError: --project 'calit' must be refused`

- [ ] **Step 3: Implement**

Add above `project_info`:

```python
def refuse_missing_project(s: "Store", value: str) -> None:
    """Exit 2 unless `--project` names a directory.

    A name passed here resolves against the cwd into a path nothing lives at;
    that path has no remote, so it keyed a fresh, untagged, invisible project
    on every call (issue #13). Refusing is cheaper than any repair.
    """
    target = pathlib.Path(value)
    if target.is_dir():
        return
    here = pathlib.Path.cwd().resolve()
    msg = [f"--project {value!r} is not a directory (resolved to {target.resolve()}).",
           "Pass a path, not a name."]
    if value == here.name:
        msg.append(f"You are in {here.name!r} — did you mean --project . ?")
    elif s.q("MATCH (p:Project {name:$n}) RETURN p.id AS id LIMIT 1", {"n": value}):
        msg.append(f"A project named {value!r} exists — pass its directory,"
                   " or run from inside it with --project .")
    print("\n".join(msg), file=sys.stderr)
    raise SystemExit(2)
```

First line of `project_info`'s body:

```python
    refuse_missing_project(s, path)
```

`_check_verdicts` passes `here` as `--project`, and `here` is currently
`str(pathlib.Path("/tmp").resolve())` — `C:\tmp` on Windows, which need not
exist and is now refused. Replace that assignment (~line 2534) with:

```python
    import tempfile
    here = str(pathlib.Path(tempfile.gettempdir()).resolve())
```

and replace the comment above it with: `# A real directory (every --project must be one), resolved so the stored id and cmd_check's lookup agree — on macOS /tmp is a symlink to /private/tmp.`

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: `selftest ok (…)`

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "fix: refuse a --project that is not a directory (#13)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `fold_project`, remote-keyed `upsert_project`, fixed names, replay re-keys

The graph core. After this task a replay (and every write through `upsert_project`) keys projects by remote and absorbs older keys.

**Files:**
- Modify: `scripts/precedent.py`
  - add `legacy_keys` and `fold_project` directly above `upsert_project` (~line 644)
  - replace `upsert_project`
  - `replay_entry`'s `project_portable` branch (~line 1662)
  - `_check_projects` cleanup (~line 1955), `_check_identity` (~line 2843–2875), `_check_identity_gaps` (~line 2891–2933)
  - replace `_check_backfill_replay` with `_check_portable_replay`; add `_check_rekey`; update `cmd_selftest` registrations

**Interfaces:**
- Consumes: `paths_of`, `today`, `Store.q`, `write_decision`, `replay_entry`.
- Produces:
  - `legacy_keys(s: Store, key: str, portable: str | None, local_paths: list[str]) -> list[str]` — sorted node ids holding this project under another key. Task 4 calls it from `project_info`.
  - `fold_project(s: Store, frm: str, into: str) -> int` — decisions moved; no-op (0) when `frm == into` or `frm` is missing; creates `into` from `frm` when absent. Tasks 4, 5 call it.
  - `upsert_project(s, info) -> dict` — same signature; `info["id"]` is now `portable or path`.

- [ ] **Step 1: Write the failing test**

Add `_check_rekey` after `_check_identity_gaps`:

```python
def _check_rekey(s: Store) -> None:
    """One node per project, keyed by its remote — and every older key folds in.

    Older keys are history, not choice: every node before this change was
    path-keyed, and 0.4.x still creates path-keyed nodes carrying `portable`.
    The one direction that must never happen is a path match folding a node
    whose remote says it is a different project.
    """
    P = "/tmp/precedent-selftest-rk"
    pp, other = "github.com/asm0dey/selftest-rk", "github.com/asm0dey/selftest-rk-other"
    dec = lambda did, pid, path, portable: {
        "id": did, "title": "t", "statement": "t", "rationale": "", "scope": "tooling",
        "created": today(), "project_id": pid, "project_name": "rk",
        "project_path": path, "portable": portable,
        "tags": [], "topics": [], "chose": [], "rejected": [], "supersedes": []}
    in_project = lambda did: s.q(
        "MATCH (:Decision {id:$d})-[:IN_PROJECT]->(p:Project) RETURN p.id AS id", {"d": did})
    node = lambda pid: s.q("MATCH (p:Project {id:$id}) RETURN p.name AS name, p.paths AS paths,"
                           " p.portable AS pp", {"id": pid})
    wipe = lambda: (s.q("MATCH (n) WHERE n.id STARTS WITH 'selftest-rk' DETACH DELETE n"),
                    s.q("MATCH (p:Project) WHERE p.id IN $ids DETACH DELETE p",
                        {"ids": [P, P + "-b", pp, other]}),
                    s.q("""MATCH (t:Tag {name:'selftest-rk-tag'})
                           WHERE NOT EXISTS { MATCH (t)<--() } DETACH DELETE t"""))
    try:
        # fold_project: decisions, tags and paths move; the source goes; the target keeps its name.
        upsert_project(s, {"id": P, "path": P, "name": "src"})
        upsert_project(s, {"id": P + "-b", "path": P + "-b", "name": "dst"})
        attach_tags(s, P, ["selftest-rk-tag"])
        write_decision(s, dec("selftest-rk1", P, P, None))
        assert fold_project(s, P, P + "-b") == 1
        assert node(P) == [], "the source node must be gone"
        assert in_project("selftest-rk1") == [{"id": P + "-b"}]
        assert "selftest-rk-tag" in tags_of(s, P + "-b")
        assert set(paths_of(node(P + "-b")[0])) == {P, P + "-b"}
        assert node(P + "-b")[0]["name"] == "dst", "the target keeps its name"
        assert fold_project(s, "selftest-rk-missing", P + "-b") == 0, "a missing source is a no-op"
        wipe()

        # 0.4.x shape: path-keyed node carrying the portable id. The next write re-keys it.
        s.q("CREATE (:Project {id:$id, name:'rk', portable:$pp, paths:$id})", {"id": P, "pp": pp})
        s.q("CREATE (:Decision {id:'selftest-rk2', status:'active'})")
        s.q("MATCH (d:Decision {id:'selftest-rk2'}), (p:Project {id:$id}) MERGE (d)-[:IN_PROJECT]->(p)",
            {"id": P})
        got = upsert_project(s, {"id": P, "path": P, "name": "rk", "portable": pp})
        assert got["id"] == pp, got
        assert node(P) == [] and in_project("selftest-rk2") == [{"id": pp}]
        wipe()

        # A repo that gained a remote: its portable-less path node folds in.
        write_decision(s, dec("selftest-rk3", P, P, None))
        upsert_project(s, {"id": P, "path": P, "name": "rk", "portable": pp})
        assert in_project("selftest-rk3") == [{"id": pp}] and node(P) == []
        wipe()

        # A DIFFERENT remote at the same path is another project: never folded.
        upsert_project(s, {"id": P, "path": P, "name": "rk", "portable": other})
        upsert_project(s, {"id": P, "path": P, "name": "rk", "portable": pp})
        assert node(other) and node(pp), "a different portable must not fold on a path match"
        wipe()

        # The name is fixed when the node is created.
        upsert_project(s, {"id": P, "path": P, "name": "first", "portable": pp})
        upsert_project(s, {"id": P, "path": P + "-b", "name": "second", "portable": pp})
        assert node(pp)[0]["name"] == "first", node(pp)
        wipe()

        # Replay: an old path-keyed record line, then one carrying the portable id.
        replay_entry(s, {"op": "record", **dec("selftest-rk4", P, P, None)})
        replay_entry(s, {"op": "record", **dec("selftest-rk5", pp, P, pp)})
        assert in_project("selftest-rk4") == [{"id": pp}] == in_project("selftest-rk5")
        assert node(P) == [], "replay must converge on one node per repo"
    finally:
        wipe()
```

Replace `_check_backfill_replay` entirely with:

```python
def _check_portable_replay() -> None:
    """0.4.x `brief` journalled `project_portable` when a project gained a
    remote. Replayed now, it folds the node it names into the portable key —
    and, as before, never creates a node.

    Throwaway Store: `Store.log` appends to a real append-only journal.
    """
    import tempfile

    pid = "/tmp/precedent-selftest-bp"
    portable = "github.com/asm0dey/selftest-bp"
    entry = {"op": "project_portable", "project_id": pid, "portable": portable, "v": 1}
    with tempfile.TemporaryDirectory() as tmp_home:
        with Store(pathlib.Path(tmp_home), write=True) as s:
            upsert_project(s, {"id": pid, "path": pid, "name": "bp", "portable": None})
            replay_entry(s, entry)
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": pid}) == []
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.portable AS pp, p.paths AS paths",
                       {"id": portable}) == [{"pp": portable, "paths": pid}]

            s.q("MATCH (p:Project) DETACH DELETE p")
            replay_entry(s, entry)
            assert s.q("MATCH (p:Project) RETURN count(p) AS n") == [{"n": 0}], \
                "replaying a project_portable line must not create a node"

            # Spec A4: the schema stamp is written after the payload, so a
            # payload key named "v" cannot shadow it.
            s.log("project_portable", {"project_id": pid, "portable": portable, "v": 99})
            last = json.loads(s.journal.read_text(encoding="utf-8").splitlines()[-1])
            assert last["v"] == SCHEMA, last
```

In `cmd_selftest`: replace `_check_backfill_replay()` with `_check_portable_replay()`, and add `_check_rekey(s)` after `_check_identity_gaps(s)`.

Update existing checks for the new key:

- `_check_projects` `finally`: the tuple becomes
  `for pid in (proj, other, root, mod, sibling, win, "github.com/asm0dey/selftest-c"):`
- `_check_identity`:
  - `assert a_info["id"] == pp, a_info`
  - `assert b_info["id"] == pp, "the second sighting must resolve onto the same node"`
  - `row = s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths", {"id": pp})[0]`
  - `assert c_info["id"] == pp2, "a different remote must key a different node"`
  - the paths assertion's lookup uses `{"id": pp2}`
  - `finally`: `for pid in (linux, windows, unrelated, pp, pp2):`
- `_check_identity_gaps`:
  - `assert pp not in gaps, "a project that HAS an identity is not a gap"`
  - `assert twin and twin["id"] == pp, …` (message unchanged)
  - `ids = [holder, pp]`

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: FAIL with `NameError: name 'fold_project' is not defined` (or an `_check_identity` assertion on `a_info["id"]`)

- [ ] **Step 3: Implement**

Add above `upsert_project`:

```python
def legacy_keys(s: Store, key: str, portable: str | None,
                local_paths: list[str]) -> list[str]:
    """Nodes holding this project under a key other than `key`.

    Two shapes, both left by history: a node carrying this exact portable id
    under a path key (every node before docs/adr/0009, and what 0.4.x still
    creates), and a node with no portable id whose paths include one of ours
    (a repo that has since gained a remote, or a worktree keyed on its own
    path). A node with a DIFFERENT portable is never matched by path: its
    remote says it is another project, and a wrong merge is worse than a
    visible split.
    """
    rows = s.q("""MATCH (p:Project) WHERE p.id <> $key
                  RETURN p.id AS id, p.portable AS pp, p.paths AS paths""", {"key": key})
    mine = set(local_paths)
    return sorted(r["id"] for r in rows
                  if (portable and r["pp"] == portable)
                  or (r["pp"] is None and mine & set(paths_of(r) or [r["id"]])))


def fold_project(s: Store, frm: str, into: str) -> int:
    """Move one project node into another: its decisions, tags and paths.

    The single operation behind every merge — `merge-project`, the re-key of
    a node an older key left behind, and the replay of both. Decision ids do
    not change, so every reference to one still resolves. The target keeps
    its own name and portable id; a missing target is created from the
    source, which makes the fold a rename. Returns the decisions moved.
    """
    if frm == into:
        return 0
    src = s.q("""MATCH (p:Project {id:$id})
                 RETURN p.name AS name, p.seen AS seen, p.paths AS paths""", {"id": frm})
    if not src:
        return 0
    src = src[0]
    s.q("MERGE (p:Project {id:$id}) ON CREATE SET p.name=$name, p.seen=$seen",
        {"id": into, "name": src["name"], "seen": src["seen"] or today()})
    dst = s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths", {"id": into})[0]
    paths = sorted(set(paths_of(dst)) | set(paths_of(src)))
    s.q("MATCH (p:Project {id:$id}) SET p.paths=$paths",
        {"id": into, "paths": "\n".join(paths)})
    moved = s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$f})
                   RETURN count(d) AS n""", {"f": frm})[0]["n"]
    s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$f}), (p:Project {id:$t})
           MERGE (d)-[:IN_PROJECT]->(p)""", {"f": frm, "t": into})
    s.q("""MATCH (:Project {id:$f})-[:TAGGED]->(t:Tag), (p:Project {id:$t})
           MERGE (p)-[:TAGGED]->(t)""", {"f": frm, "t": into})
    s.q("MATCH (p:Project {id:$id}) DETACH DELETE p", {"id": frm})
    return moved
```

Replace `upsert_project`:

```python
def upsert_project(s: Store, info: dict) -> dict:
    """Key the node by the remote when there is one, by the local path when not.

    `id` is the graph key. `path` stays this machine's, because containment
    is derived from paths and must be derived from local ones. Anything the
    project was keyed by before folds in here, on writes and on replay alike,
    so a store converges on one node per project with no migration step to
    remember. These folds need no journal line: replay reproduces them from
    the `portable` and `project_path` every line already carries.

    The name is set once, at creation: a write from a worktree or a clone
    under another directory name must not rename the project for everyone.
    """
    local, portable = info["path"], info.get("portable")
    key = portable or local
    for old in legacy_keys(s, key, portable, [local]):
        fold_project(s, old, key)
    s.q("MERGE (p:Project {id:$id}) ON CREATE SET p.name=$name",
        {"id": key, "name": info["name"]})
    s.q("MATCH (p:Project {id:$id}) SET p.seen=$seen", {"id": key, "seen": today()})
    if portable:
        # Kept although it equals `id`: 0.4.x finds nodes by this property.
        s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp", {"id": key, "pp": portable})
    row = s.q("MATCH (p:Project {id:$id}) RETURN p.paths AS paths", {"id": key})[0]
    known = paths_of(row)
    if local not in known:
        known = sorted(known + [local])
        s.q("MATCH (p:Project {id:$id}) SET p.paths=$paths",
            {"id": key, "paths": "\n".join(known)})
    info["id"], info["paths"] = key, known
    return info
```

Replace the `project_portable` branch of `replay_entry`:

```python
    elif e["op"] == "project_portable":
        # Written by 0.4.x `brief` when a project gained a remote. The node it
        # names folds into its portable key — MATCH first, because `brief`
        # never creates a node and its journal line must not either.
        if s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": e["project_id"]}):
            fold_project(s, e["project_id"], e["portable"])
            s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp",
                {"id": e["portable"], "pp": e["portable"]})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: `selftest ok (…)`

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: key projects by git remote; fold older keys on write and replay

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `project_info` identity, `settle()` (lazy re-key), wired into the writers

Readers see a not-yet-re-keyed node; writers fold it, journalled. Replaces `backfill_portable`. Brief headers show the stored name.

**Files:**
- Modify: `scripts/precedent.py`
  - replace `project_info` (~line 471)
  - add `settle` after `project_info`
  - delete `backfill_portable` (~line 960)
  - `cmd_record` (~line 750), `cmd_brief` (~line 999), `cmd_tag` (~line 1323: before `upsert_project(s, info)`)
  - `replay_entry`: add the `project_merge` branch (beside `project_portable`)
  - add `_check_settle` after `_check_rekey`; register after `_check_rekey(s)`
  - `_check_lock_modes`: change the `brief` comment to `# settle() folds older keys`

**Interfaces:**
- Consumes: `refuse_missing_project` (Task 2), `identity_path`, `portable_id` (Task 1), `legacy_keys`, `fold_project`, `upsert_project` (Task 3), `_git`, `_git_repo` (Task 1).
- Produces:
  - `project_info(s, path, extra_tags="") -> dict` with keys `id` (node to read), `key` (where the project belongs), `legacy: list[str]`, `path`, `portable`, `name`, `paths`, `tags`, `contents`.
  - `settle(s: Store, info: dict) -> None` — journals `{"op": "project_merge", "from", "into", "portable"}` per legacy node, folds it, sets `info["id"] = info["key"]`, `info["legacy"] = []`. Task 5 calls it.
  - Journal op `project_merge {from: str, into: str, portable?: str|null}`.

- [ ] **Step 1: Write the failing test**

```python
def _check_settle() -> None:
    """Lazy re-key: readers still find a node no writer has re-keyed; the
    next writer folds it, journalled, so `rebuild` lands in the same place.

    Throwaway Store: settle() journals.
    """
    import argparse
    import contextlib
    import io
    import tempfile

    pp = "github.com/asm0dey/selftest-st"
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        base = pathlib.Path(tmp).resolve()
        with Store(pathlib.Path(tmp_home), write=True) as s:
            in_project = lambda did: s.q(
                "MATCH (:Decision {id:$d})-[:IN_PROJECT]->(p:Project) RETURN p.id AS id",
                {"d": did})

            # 1. The 0.4.x shape in a repo with a remote.
            R = base / "remote-repo"
            _git_repo(R, "git@github.com:asm0dey/selftest-st.git")
            s.q("CREATE (:Project {id:$id, name:'remote-repo', portable:$pp, paths:$id})",
                {"id": str(R), "pp": pp})
            s.q("CREATE (:Decision {id:'selftest-st1', status:'active'})")
            s.q("""MATCH (d:Decision {id:'selftest-st1'}), (p:Project {id:$id})
                   MERGE (d)-[:IN_PROJECT]->(p)""", {"id": str(R)})
            info = project_info(s, str(R))
            assert info["key"] == pp and info["id"] == str(R), \
                f"a reader must see the node no writer has re-keyed yet: {info}"
            settle(s, info)
            assert info["id"] == pp and info["legacy"] == []
            assert in_project("selftest-st1") == [{"id": pp}]
            last = json.loads(s.journal.read_text(encoding="utf-8").splitlines()[-1])
            assert (last["op"], last["from"], last["into"], last["v"]) \
                == ("project_merge", str(R), pp, 1), last

            # 2. brief never creates a node from nothing.
            E = base / "empty-repo"
            _git_repo(E, "git@github.com:asm0dey/selftest-st-empty.git")
            n = s.q("MATCH (p:Project) RETURN count(p) AS n")[0]["n"]
            settle(s, project_info(s, str(E)))
            assert s.q("MATCH (p:Project) RETURN count(p) AS n")[0]["n"] == n

            # 3. A remote-less worktree that 0.4.x keyed on its own path.
            M, W = base / "main", base / "wt"
            _git_repo(M)
            _git(M, "worktree", "add", "-q", str(W))
            d = {"id": "selftest-st3", "title": "t", "statement": "t", "rationale": "",
                 "scope": "tooling", "created": today(), "project_id": str(W),
                 "project_name": "wt", "project_path": str(W), "portable": None,
                 "tags": [], "topics": [], "chose": [], "rejected": [], "supersedes": []}
            s.log("record", d)
            write_decision(s, d)
            info = project_info(s, str(W))
            assert info["key"] == str(M) and info["legacy"] == [str(W)], info
            settle(s, info)
            assert in_project("selftest-st3") == [{"id": str(M)}]
            # Only the journalled fold can reproduce this on rebuild.
            replay_journal(s)
            assert in_project("selftest-st3") == [{"id": str(M)}], \
                "rebuild must land the worktree's decisions on the main checkout"
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": str(W)}) == []

            # 4. The header names the project, not the worktree directory.
            s.q("MATCH (p:Project {id:$id}) SET p.name='main'", {"id": str(M)})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_brief(argparse.Namespace(project=str(W), min_shared=1,
                                             only_if_relevant=False), s)
            assert "project: main" in out.getvalue(), out.getvalue()

            # 5. A worktree of a repo WITH a remote: portable key, main-checkout path.
            M2, W2 = base / "main2", base / "wt2"
            _git_repo(M2, "git@github.com:asm0dey/selftest-st2.git")
            _git(M2, "worktree", "add", "-q", str(W2))
            info = project_info(s, str(W2))
            assert info["key"] == "github.com/asm0dey/selftest-st2", info
            assert info["path"] == str(M2) and info["name"] == "main2", info
```

Register after `_check_rekey(s)`:

```python
    _check_settle()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: FAIL with `KeyError: 'key'`

- [ ] **Step 3: Implement**

Replace `project_info`:

```python
def project_info(s: "Store", path: str, extra_tags: str = "") -> dict:
    """Identify this directory: where it belongs, and which node to read.

    `key` is where the project belongs: its portable id, else its
    main-checkout path (docs/adr/0009). `id` is the node to READ — the key's
    node when it exists, else a node an older key left behind, so `check`
    and `suggest`, which must not write, still see a project no writer has
    re-keyed yet. `legacy` lists those older nodes; `settle` folds them.

    `contents` comes from the directory actually open, not the main
    checkout, so the listing matches what is on disk here. The name is the
    stored one when the project exists: a worktree reads as its project.

    Reads only.
    """
    refuse_missing_project(s, path)
    here = pathlib.Path(path)
    info = detect_project(here)
    ident = identity_path(here)
    info["path"], info["name"] = str(ident), ident.name
    info["portable"] = portable_id(here)
    info["key"] = info["portable"] or info["path"]
    info["legacy"] = legacy_keys(s, info["key"], info["portable"],
                                 sorted({info["path"], str(here.resolve())}))
    read = lambda pid: s.q("""MATCH (p:Project {id:$id})
                              RETURN p.id AS id, p.name AS name, p.paths AS paths""",
                           {"id": pid})
    row = read(info["key"]) or (read(info["legacy"][0]) if info["legacy"] else [])
    if row:
        info["id"], info["paths"] = row[0]["id"], paths_of(row[0])
        info["name"] = row[0]["name"] or info["name"]
    else:
        info["id"], info["paths"] = info["key"], [info["path"]]
    info["tags"] = sorted(set(tags_of(s, info["id"])) | set(csv(extra_tags)))
    return info
```

Add after it:

```python
def settle(s: Store, info: dict) -> None:
    """Fold every older node this project left behind into its key.

    The lazy re-key: called by the commands that write — `record`, `tag`,
    `brief`, `merge-project` — never by readers. Each fold is journalled
    first, because a worktree keyed on its own path cannot be re-folded from
    journal data alone. `brief` still never creates a node from nothing:
    with no older node there is nothing to fold, and folding one is a rename.
    Replaces `backfill_portable`: a project that gained a remote is exactly a
    portable-less node on one of our paths.
    """
    if not info["legacy"]:
        return
    for old in info["legacy"]:
        s.log("project_merge", {"from": old, "into": info["key"],
                                "portable": info["portable"]})
        fold_project(s, old, info["key"])
    if info["portable"]:
        s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp",
            {"id": info["key"], "pp": info["portable"]})
    row = s.q("MATCH (p:Project {id:$id}) RETURN p.name AS name, p.paths AS paths",
              {"id": info["key"]})[0]
    info["id"], info["legacy"] = info["key"], []
    info["name"], info["paths"] = row["name"] or info["name"], paths_of(row)
    info["tags"] = sorted(set(info["tags"]) | set(tags_of(s, info["id"])))
```

Delete `backfill_portable` entirely. In `identity_gaps`'s docstring, replace
"so `backfill_portable` reads back a non-None value and returns without
writing" with "so nothing stamps it" — the function it names is gone.

`cmd_brief` — replace the first two lines of the body:

```python
    info = project_info(s, a.project)
    settle(s, info)
```

and update the comment above them to: `# Writes only to fold an older key into this project's (settle). Never creates a node: …` (keep the rest of the existing sentence about the SessionStart hook).

`cmd_record` — after `info = project_info(s, a.project)`:

```python
    settle(s, info)
```

`cmd_tag` — immediately before the existing `upsert_project(s, info)` (after the listing branch's `return`):

```python
    settle(s, info)
```

`replay_entry` — add beside the `project_portable` branch:

```python
    elif e["op"] == "project_merge":
        # A missing source is a no-op: upsert_project's own fold on replay may
        # already have absorbed it.
        fold_project(s, e["from"], e["into"])
        if e.get("portable"):
            s.q("MATCH (p:Project {id:$id}) SET p.portable=$pp",
                {"id": e["into"], "pp": e["portable"]})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: `selftest ok (…)`. Also `grep -n backfill_portable scripts/precedent.py` → no matches.

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: lazy re-key — writers fold older project keys, journalled

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `merge-project` command

**Files:**
- Modify: `scripts/precedent.py`
  - `contradictions_in` (~line 1375): optional `project_id` filter
  - add `cmd_merge_project` after `cmd_tag`'s helpers (after `apply_tag_merge`)
  - `build_parser`: new subparser after `tag`
  - `_check_lock_modes`: add `"merge-project": True,  # folds one project into another`
  - add `_check_merge_project` after `_check_settle`; register after `_check_settle()`

**Interfaces:**
- Consumes: `project_info`, `settle` (Task 4), `upsert_project`, `fold_project` (Task 3), `Store.log`.
- Produces:
  - `contradictions_in(s, project_id: str | None = None) -> list[dict]`
  - `cmd_merge_project(a, s)` — `a.frm: str`, `a.into: str`
  - CLI: `precedent.py merge-project --from <id> --into <id-or-path>`

- [ ] **Step 1: Write the failing test**

```python
def _check_merge_project() -> None:
    """The #13 repair, as one journalled command an agent can run.

    Throwaway Store: merge-project journals.
    """
    import argparse
    import contextlib
    import io
    import tempfile

    ghost = os.path.join(os.sep + "nonexistent-precedent-selftest", "calit", "calit")
    run = lambda s, frm, into: cmd_merge_project(argparse.Namespace(frm=frm, into=into), s)

    def refused(s, frm, into) -> str:
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                run(s, frm, into)
            raise AssertionError(f"merge-project {frm!r} -> {into!r} must be refused")
        except SystemExit as exc:
            assert exc.code == 2, exc.code
        return err.getvalue()

    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        real = pathlib.Path(tmp).resolve() / "calit"
        real.mkdir()
        with Store(pathlib.Path(tmp_home), write=True) as s:
            dec = lambda did, pid, chose: {
                "id": did, "title": did, "statement": did, "rationale": "", "scope": "tooling",
                "created": today(), "project_id": pid, "project_name": "calit",
                "project_path": pid, "portable": None, "tags": [], "topics": ["cache"],
                "chose": [chose], "rejected": [], "supersedes": []}
            for d in (dec("selftest-mp1", ghost, "redis"), dec("selftest-mp2", str(real), "memcached")):
                s.log("record", d)
                write_decision(s, d)
            attach_tags(s, ghost, ["selftest-ghost"])

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                run(s, ghost, str(real))
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": ghost}) == []
            assert {r["d"] for r in s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$id})
                                            RETURN d.id AS d""", {"id": str(real)})} \
                == {"selftest-mp1", "selftest-mp2"}
            assert "selftest-ghost" in tags_of(s, str(real))
            assert "cache" in out.getvalue(), "contradictions it creates must be shown"
            last = json.loads(s.journal.read_text(encoding="utf-8").splitlines()[-1])
            assert (last["op"], last["from"], last["into"], last["v"]) \
                == ("project_merge", ghost, str(real), 1), last

            replay_journal(s)
            assert {r["d"] for r in s.q("""MATCH (d:Decision)-[:IN_PROJECT]->(:Project {id:$id})
                                            RETURN d.id AS d""", {"id": str(real)})} \
                == {"selftest-mp1", "selftest-mp2"}, "rebuild must reproduce the merge"

            assert "calit" in refused(s, "calit", str(real)), "near-matches must be listed"
            assert "same project" in refused(s, str(real), str(real))
            refused(s, str(real), os.path.join(tmp, "missing-dir"))

            # --from is this project's own older key: folded, not an error.
            # `--into` is a path spelled unlike the node id ("<real>/."), so it
            # resolves through project_info — the way `--into .` does.
            older = os.path.join(os.sep + "nonexistent-precedent-selftest", "older")
            s.q("CREATE (:Project {id:$id, name:'calit', paths:$p})",
                {"id": older, "p": str(real)})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                run(s, older, os.path.join(str(real), "."))
            assert "older key" in out.getvalue(), out.getvalue()
            assert s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": older}) == []

            n = s.q("MATCH (p:Project) RETURN count(p) AS n")[0]["n"]
            replay_entry(s, {"op": "project_merge", "from": "selftest-nope", "into": str(real)})
            assert s.q("MATCH (p:Project) RETURN count(p) AS n")[0]["n"] == n, \
                "replaying a merge whose source is gone is a no-op"
```

Register after `_check_settle()`:

```python
    _check_merge_project()
```

Add to `_check_lock_modes`'s `expected`:

```python
        "merge-project": True,  # folds one project into another
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: FAIL with `NameError: name 'cmd_merge_project' is not defined` (or the lock-modes table mismatch)

- [ ] **Step 3: Implement**

`contradictions_in` — new signature and filter; add `p.id AS pid` to the RETURN:

```python
def contradictions_in(s: "Store", project_id: str | None = None) -> list[dict]:
```

```python
                  RETURN p.id AS pid, p.name AS pname, t.name AS topic, d.id AS did,
                         collect(DISTINCT o.name) AS opts""")
    grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
    for r in rows:
        if project_id and r["pid"] != project_id:
            continue
        grouped.setdefault((r["pname"], r["topic"]), {})[r["did"]] = sorted(r["opts"])
```

Add after `apply_tag_merge`:

```python
def cmd_merge_project(a, s: Store) -> None:
    """Fold one project node into another — the repair for a ghost (#13).

    Built for an agent: `--from` is the exact id `maintain` prints (a ghost
    has no directory to name it by), `--into` is an id or a live path, so `.`
    works from inside the real project. A wrong id lists near-misses instead
    of guessing, so the next call can be right. Whether two histories are one
    project is the caller's judgment; this carries it out, journalled.
    """
    exists = lambda pid: bool(s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id",
                                  {"id": pid}))
    if not exists(a.frm):
        v = a.frm.lower()
        near = [r for r in s.q("MATCH (p:Project) RETURN p.id AS id, p.name AS name ORDER BY p.id")
                if v in r["id"].lower() or v in (r["name"] or "").lower()][:5]
        print(f"no project has the id {a.frm!r}.", file=sys.stderr)
        for r in near:
            print(f"  {r['name']}  {r['id']}", file=sys.stderr)
        if not near:
            print("  `precedent.py maintain` lists project ids", file=sys.stderr)
        raise SystemExit(2)
    if exists(a.into):
        into = a.into
    else:
        info = project_info(s, a.into)            # exits 2 unless a directory
        if a.frm in info["legacy"]:
            settle(s, info)
            print(f"{a.frm} was an older key of {info['key']} — folded into it")
            return
        if info["key"] == a.frm:
            into = a.frm
        else:
            settle(s, info)
            into = upsert_project(s, info)["id"]
    if into == a.frm:
        print(f"{a.frm!r} and {a.into!r} are the same project.", file=sys.stderr)
        raise SystemExit(2)
    s.log("project_merge", {"from": a.frm, "into": into})
    moved = fold_project(s, a.frm, into)
    print(f"merged {a.frm} into {into} — {moved} decision(s) moved")
    for r in contradictions_in(s, into):
        print(f"  now contradicting: {r['project']}/{r['topic']} — supersede one:")
        for did, opts in sorted(r["decisions"].items()):
            print(f"     {','.join(opts)}  #{did}")
```

`build_parser`, after the `tag` subparser:

```python
    mp = sub.add_parser("merge-project",
                        help="fold one project node into another (repair a ghost or a split)")
    mp.add_argument("--from", dest="frm", required=True,
                    help="exact project id to fold away, as `maintain` prints it")
    mp.add_argument("--into", required=True,
                    help="project id, or a directory such as . for the project you are in")
    mp.set_defaults(writes=True, fn=cmd_merge_project)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: `selftest ok (…)`

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: merge-project — journalled fold of one project into another

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Graph-format marker, one-time migration, safe replay

**Files:**
- Modify: `scripts/precedent.py`
  - add `GRAPH_FORMAT = 2` under `SCHEMA` (~line 79)
  - add `mark_graph_format` above `replay_journal`
  - `replay_journal`: pre-scan before the wipe; mark at the end
  - `Store.__enter__`: call `self._ensure_graph_format()` after the `_finish_migration` block; add the method after `_finish_migration`
  - add `_check_graph_format` after `_check_merge_project`; register after `_check_merge_project()`

**Interfaces:**
- Consumes: `replay_journal`, `upsert_project` (Task 3 — the re-key happens inside replay).
- Produces: `GRAPH_FORMAT: int = 2`; `mark_graph_format(s: Store) -> None`; `Store._ensure_graph_format() -> None`; node `(:Meta {id: 'meta', graph_format: int})`.

- [ ] **Step 1: Write the failing test**

```python
def _check_graph_format() -> None:
    """An older store is re-keyed once, by a writer, and never again; a
    too-new journal is refused before anything is wiped.
    """
    import contextlib
    import io
    import tempfile

    pp, old = "github.com/asm0dey/selftest-gf", "/tmp/precedent-selftest-gf"
    meta = lambda s: s.q("MATCH (m:Meta) RETURN m.graph_format AS f")
    with tempfile.TemporaryDirectory() as tmp_home:
        home = pathlib.Path(tmp_home)
        with Store(home, write=True) as s:
            assert meta(s) == [{"f": GRAPH_FORMAT}], "a new store is marked at once"
            # What 0.4.x leaves: path-keyed node with portable, its journal line, no marker.
            s.log("record", {"id": "selftest-gf1", "title": "t", "statement": "t",
                             "rationale": "", "scope": "tooling", "created": today(),
                             "project_id": old, "project_name": "gf", "project_path": old,
                             "portable": pp, "tags": [], "topics": [], "chose": [],
                             "rejected": [], "supersedes": []})
            s.q("CREATE (:Project {id:$id, name:'gf', portable:$pp, paths:$id})",
                {"id": old, "pp": pp})
            s.q("CREATE (:Decision {id:'selftest-gf1', status:'active'})")
            s.q("""MATCH (d:Decision {id:'selftest-gf1'}), (p:Project {id:$id})
                   MERGE (d)-[:IN_PROJECT]->(p)""", {"id": old})
            s.q("MATCH (m:Meta) DETACH DELETE m")

        with Store(home, write=False) as s:
            assert meta(s) == [], "a reader must not migrate"

        with contextlib.redirect_stderr(io.StringIO()):
            with Store(home, write=True) as s:
                assert s.q("MATCH (p:Project {id:$id}) RETURN p.id AS id", {"id": old}) == []
                assert s.q("""MATCH (:Decision {id:'selftest-gf1'})-[:IN_PROJECT]->(p:Project)
                              RETURN p.id AS id""") == [{"id": pp}]
                assert meta(s) == [{"f": GRAPH_FORMAT}]
                s.q("CREATE (:Project {id:'selftest-gf-sentinel'})")

        with Store(home, write=True) as s:
            assert s.q("MATCH (p:Project {id:'selftest-gf-sentinel'}) RETURN p.id AS id"), \
                "a marked graph must not be replayed again"
            # 0.4.x finds nodes by the portable property: it must still be there.
            assert s.q("MATCH (p:Project {portable:$pp}) RETURN p.id AS id", {"pp": pp}) \
                == [{"id": pp}]

            with open(s.journal, "a", encoding="utf-8") as f:
                f.write(json.dumps({"op": "record", "id": "selftest-gf-future",
                                    "v": SCHEMA + 1}) + "\n")
            n = s.q("MATCH (n) RETURN count(n) AS n")[0]["n"]
            try:
                replay_journal(s)
                raise AssertionError("a too-new journal must be refused")
            except SystemExit as exc:
                assert "upgrade" in str(exc), exc
            assert s.q("MATCH (n) RETURN count(n) AS n")[0]["n"] == n, \
                "a refused replay must not have wiped the graph"
```

Register after `_check_merge_project()`:

```python
    _check_graph_format()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: FAIL with `NameError: name 'GRAPH_FORMAT' is not defined`

- [ ] **Step 3: Implement**

Under `SCHEMA`:

```python
GRAPH_FORMAT = 2    # graph layout; 2 = projects keyed by git remote (docs/adr/0009)
```

Above `replay_journal`:

```python
def mark_graph_format(s: "Store") -> None:
    s.q("MERGE (m:Meta {id:'meta'}) SET m.graph_format=$f", {"f": GRAPH_FORMAT})
```

In `replay_journal`, insert before `s.q("MATCH (n) DETACH DELETE n")`:

```python
    # Refuse a journal from a newer precedent BEFORE wiping. Stopping at the
    # line itself, as replay_entry does, leaves a half-rebuilt graph.
    for lineno, line in enumerate(open(s.journal), 1):
        try:
            e = json.loads(line) if line.strip() else {}
        except json.JSONDecodeError:
            continue
        v = e.get("v", 1) if isinstance(e, dict) else 1
        if isinstance(v, int) and v > SCHEMA:
            raise SystemExit(f"stopping before touching the graph: journal line {lineno}"
                             f" is schema v{v}, this precedent understands v{SCHEMA}"
                             " — upgrade before replaying")
```

and before its `return n, skipped`:

```python
    mark_graph_format(s)
```

In `Store.__enter__`, after the `if self._pending_migration is not None:` block and before `return self`:

```python
        self._ensure_graph_format()
```

Add after `_finish_migration`:

```python
    def _ensure_graph_format(self) -> None:
        """Re-key a graph laid out by an older precedent, once.

        Replaying the journal is the whole migration: upsert_project keys each
        project by its remote as the lines go in. Readers skip it — they must
        not write, and project_info resolves older keys for them. A 0.4.x
        `rebuild` wipes the marker along with everything else, so the next
        writer here simply migrates again; the journal is the same, so
        nothing is lost either way.
        """
        if not self.write:
            return
        row = self.q("MATCH (m:Meta {id:'meta'}) RETURN m.graph_format AS f")
        if row and (row[0]["f"] or 0) >= GRAPH_FORMAT:
            return
        if not self.journal.exists():
            mark_graph_format(self)
            return
        n, skipped = replay_journal(self)
        # stderr: brief's stdout is injected into a model's context.
        print(f"precedent: graph re-keyed by git remote ({n} journal entries)",
              file=sys.stderr)
        for msg in skipped:
            print(f"  skipped {msg}", file=sys.stderr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: `selftest ok (…)`

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: graph-format marker re-keys an older store once; replay refuses a too-new journal before wiping

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `maintain` points at `merge-project`

**Files:**
- Modify: `scripts/precedent.py` — `cmd_maintain` "projects whose path no longer exists" block (~line 1524) and the identity-gap `twin` block (~line 1565); add `_check_maintain_hints` after `_check_graph_format`; register after `_check_graph_format()`.

**Interfaces:**
- Consumes: `liveness`, `identity_gaps`, `_git_repo` (Task 1), `upsert_project`.
- Produces: output lines `    merge with: precedent.py merge-project --from "<id>" --into "<id>"`.

- [ ] **Step 1: Write the failing test**

```python
def _check_maintain_hints() -> None:
    """maintain names the repair, not a hand-written query."""
    import argparse
    import contextlib
    import io
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as tmp_home:
        base = pathlib.Path(tmp).resolve()
        with Store(pathlib.Path(tmp_home), write=True) as s:
            live = base / "calit"
            live.mkdir()
            ghost = os.path.join(os.sep + "nonexistent-precedent-selftest", "calit")
            upsert_project(s, {"id": ghost, "path": ghost, "name": "calit"})
            upsert_project(s, {"id": str(live), "path": str(live), "name": "calit"})

            pp = "github.com/asm0dey/selftest-mh"
            split = base / "split"
            _git_repo(split, "git@github.com:asm0dey/selftest-mh.git")
            upsert_project(s, {"id": "/tmp/precedent-selftest-mh", "name": "mh",
                               "path": "/tmp/precedent-selftest-mh", "portable": pp})
            upsert_project(s, {"id": str(split), "path": str(split), "name": "mh"})

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_maintain(argparse.Namespace(apply=False), s)
            text = out.getvalue()
            assert (f"merge-project --from {json.dumps(ghost)}"
                    f" --into {json.dumps(str(live))}") in text, text
            assert (f"merge-project --from {json.dumps(str(split))}"
                    f" --into {json.dumps(pp)}") in text, text
            assert "cypher --params" not in text, "the hand-written query is replaced"
```

Register after `_check_graph_format()`:

```python
    _check_maintain_hints()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: FAIL with an `AssertionError` showing the maintain output without `merge-project`

- [ ] **Step 3: Implement**

Gone block — replace:

```python
    print(f"\n== projects whose path no longer exists ({len(states['gone'])}) ==")
    for r in states["gone"]:
        print(f"  {r['name']}  ({r['id']})")
```

with:

```python
    print(f"\n== projects whose path no longer exists ({len(states['gone'])}) ==")
    gone = {r["id"] for r in states["gone"]}
    for r in states["gone"]:
        print(f"  {r['name']}  ({r['id']})")
        # A ghost (#13) usually shares its name with the project it belongs to.
        # Offered, never run: which one it is remains a judgment.
        for c in projects:
            if c["name"] == r["name"] and c["id"] not in gone and c["id"] != r["id"]:
                print(f"    merge with: precedent.py merge-project --from {json.dumps(r['id'])}"
                      f" --into {json.dumps(c['id'])}")
```

Twin block — replace the four lines from `print("     precedent will not merge them for you …` through the `WHERE p.id IN $ids …` print with:

```python
            print("     one repo, so this is almost certainly one project — but check"
                  " both sides' decisions first, then:")
            print(f"       precedent.py merge-project --from {json.dumps(g['id'])}"
                  f" --into {json.dumps(g['twin']['id'])}")
```

Also update the `identity_gaps` docstring sentence "Reports, never merges." to "Reports, never merges: `merge-project` does, when the caller decides."

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: `selftest ok (…)`

- [ ] **Step 5: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: maintain suggests merge-project for ghosts and split repos

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Docs, and a real mixed-version check against 0.4.1

**Files:**
- Create: `docs/adr/0009-the-remote-is-the-graph-key.md`
- Modify: `CONTEXT.md` (**Project**, **Portable id** entries), `references/schema.md` (Identity section ~line 75; op list ~line 149; `project_portable` paragraph ~line 157; `Project` row ~line 11), `adapters/claude/skills/precedent-maintain/SKILL.md` (~lines 18–25), `adapters/claude/skills/precedent/SKILL.md` (after the `tag --merge` example ~line 420)

**Interfaces:** none (docs + manual verification).

- [ ] **Step 1: Write ADR 0009**

```markdown
# The remote is the graph key

Supersedes the rejected option "migrating the primary key outright" in
`docs/adr/0002`. That ADR made the portable id the identity but kept the
native path of first sighting as `Project.id`. The split kept producing
nodes nobody could see: a `--project` name resolved to a path that did not
exist, keyed a fresh project, and five decisions vanished from precedent
(issue #13); a worktree without a remote became its own project.

`Project.id` is now the portable id when a git remote exists — `origin`,
else the only remote there is; several and no `origin` is no remote. Without
one it is the path of the main checkout, with linked worktrees mapped onto
it. `Project.portable` is kept equal to `id` so 0.4.x, which looks nodes up
by it, keeps working. `Project.paths` still holds every local path, and
containment is still derived from it.

## How an existing store gets there

No journal rewrite. `upsert_project` folds any node holding the project
under an older key — the same portable id under a path, or a portable-less
node on one of its paths — into the new key, on every write and every
replay. A `(:Meta {graph_format: 2})` marker makes the first writer replay
the journal once. Folds the journal cannot reproduce on its own are
journalled as `project_merge`, the same op `merge-project` writes.

## Considered options

- **Root-commit sha for remote-less repos** — rejected: `cp -r old new`
  gives two projects one identity, the silent merge ADR 0002 rules out.
- **A minted id in `.git/config`** — rejected: writes into the user's repo
  and does not cross machines.
- **Bumping `SCHEMA` for `project_merge`** — rejected: 0.4.x stops a rebuild
  at any newer line after wiping the graph, and every new line would be
  newer. At v1 it skips the unknown op and says so.

## Consequences

- A path-keyed project (no remote) matches only at the same path on another
  machine; an enclosing plain folder stops enclosing a clone elsewhere.
  Known limitation; an `alias-path` command is the follow-up if it bites.
- A directory whose remote changes to a different one gets a new node;
  `maintain` reports the pair and `merge-project` joins them if they are one.
- `--project` must name a directory.
```

- [ ] **Step 2: Update the reference docs**

`CONTEXT.md` — replace the **Portable id** definition body with:

```markdown
The graph key of a Project with a git remote: the remote reduced to
`host/owner/repo`, plus `#/<subpath>` for a module inside it. `origin` wins;
otherwise the only remote there is; several and no `origin` means none.
A Project without one is keyed by its main checkout's path. See `docs/adr/0009`.
```

and add to **Project**: `A linked git worktree is the same Project as its main checkout.`

`references/schema.md`:
- `Project` row: `` `id` (portable id, else main-checkout path) ``
- Identity section: replace the first two paragraphs with the ADR's second paragraph (verbatim), and change "See `docs/adr/0002`" to "See `docs/adr/0002` and `docs/adr/0009`."
- Op list: add `project_merge` to the enumerated ops.
- Replace the `project_portable` paragraph with:

```markdown
`project_portable` carries `project_id` and `portable`. 0.4.x `brief` wrote it
when a project gained a remote; it is no longer written. Replayed, it folds the
named node into its portable key — and only if that node exists, so it never
creates one.

`project_merge` carries `from`, `into` and optionally `portable`. It is written
by `merge-project` and by `settle()` when a writer folds an older key into the
project's key. Replay calls `fold_project(from, into)`; a missing `from` is a
no-op. It stays at `v: 1` so 0.4.x skips it as an unknown op instead of
stopping the rebuild.

`(:Meta {id:'meta', graph_format})` marks the graph layout. A writer that finds
it missing or below the current value replays the journal once.
```

`adapters/claude/skills/precedent-maintain/SKILL.md` — replace the sentence starting "Never merge them yourself: precedent has no merge" (through the end of that paragraph) with:

```markdown
`maintain` prints a `merge-project --from <id> --into <id>` line for each. Show
the user both sides' decisions and let them decide; run it only on a yes. The
same line appears under "projects whose path no longer exists" when a live
project shares the ghost's name.
```

`adapters/claude/skills/precedent/SKILL.md` — after the `tag --merge` paragraph, add:

````markdown
A project that split in two — a ghost from a mistyped `--project`, or an old
node `maintain` reports — is joined the same way, once the user agrees they
are one project:

```bash
uv run <skill>/../../../../scripts/precedent.py merge-project --from '<id from maintain>' --into .
```

Decisions, tags and paths move; decision ids do not change; the merge is
journalled. Any contradiction it creates is printed — drive it to a supersede
or a `--despite`. `--project` must be a directory: pass `.`, never a name.
````

- [ ] **Step 3: Run the full selftest once more**

Run: `uv run scripts/precedent.py --home "$(mktemp -d)" selftest`
Expected: `selftest ok (…)`

- [ ] **Step 4: Manual mixed-version check against the installed 0.4.1**

The 0.4.x layer cannot run inside selftest. Run it against a COPY of the real journal — never the real store:

```bash
OLD=~/.claude/plugins/cache/precedent/precedent/0.4.1/scripts/precedent.py
NEW=$PWD/scripts/precedent.py
H=$(mktemp -d); cp ~/.local/share/precedent/journal.jsonl "$H/"
uv run "$NEW" --home "$H" brief --project .          # expect: stderr "graph re-keyed…", 11 "decided here"
uv run "$OLD" --home "$H" brief --project .          # expect: same 11 decisions (0.4.x resolves by portable)
R=$(mktemp -d)/mixed && mkdir -p "$R" && git -C "$R" init -q && git -C "$R" remote add origin git@github.com:asm0dey/selftest-mixed.git
uv run "$OLD" --home "$H" record --project "$R" --title t --topic selftest-mixed --chose x
uv run "$NEW" --home "$H" brief --project "$R"       # expect: the 0.4.x decision listed under "decided here"
uv run "$NEW" --home "$H" cypher "MATCH (p:Project) WHERE p.portable='github.com/asm0dey/selftest-mixed' RETURN p.id AS id"
                                                     # expect: exactly one row, id = github.com/asm0dey/selftest-mixed
uv run "$OLD" --home "$H" rebuild                    # expect: completes; any project_merge lines reported as skipped
uv run "$NEW" --home "$H" brief --project .          # expect: re-migrates (stderr), same 11 decisions
```

All expectations must hold. If any fails, stop and report — do not adjust the expectations.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0009-the-remote-is-the-graph-key.md CONTEXT.md references/schema.md \
        adapters/claude/skills/precedent-maintain/SKILL.md adapters/claude/skills/precedent/SKILL.md
git commit -m "docs: ADR 0009 — the remote is the graph key; merge-project in skills and schema

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
