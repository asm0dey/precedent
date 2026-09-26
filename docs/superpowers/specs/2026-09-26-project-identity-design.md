# Project identity — design

Settled 2026-09-26 in a brainstorming session. Fixes issue #13 (ghost
projects from a non-existent `--project`) and the identity problems it
exposed. Vocabulary is defined in `CONTEXT.md`.

## Global constraints

- One file, `scripts/precedent.py`, PEP 723, `uv run`. No new dependencies.
- Must run on Linux, macOS and Windows; CI runs `selftest` on all three.
- `precedent.py selftest` is the test entry point.
- `journal.jsonl` is append-only. Never edit or reorder lines.
- `SCHEMA` stays at `1`. Every change here is additive to the journal format
  (see §7).

## 1. The graph key is the remote

- `Project.id` is the **portable id** when one exists: the normalised git
  remote, plus `#/<sub>` for a directory below the repo root (as
  `portable_id` computes today).
- Otherwise `Project.id` is the native path of the **main checkout** (§3).
- `Project.paths` remains the set of local paths. Containment
  (`enclosing`/`contained`) and liveness keep reading `paths`, unchanged.
- `Project.portable` is kept on every portable-keyed node, permanently. It
  equals `id` there, and it is how 0.4.x binaries find the node (§7).
- Remote selection: `origin` if present; otherwise the remote, if there is
  exactly one; otherwise none. Several remotes and no `origin` is "no
  remote" — guessing risks a wrong identity.
- Supersedes the rejected option "migrating the primary key outright" in
  `docs/adr/0002`. Recorded in a new `docs/adr/0009`.

## 2. A missing `--project` is an error

- `project_info` checks `Path(path).is_dir()` first. On failure: exit 2,
  create nothing, print:

  ```
  --project 'calit' is not a directory (resolved to /home/u/work/calit/calit).
  Pass a path, not a name. You are in 'calit' — did you mean --project . ?
  ```

- The "did you mean" line appears only when the value equals the current
  directory's name or an existing project's name.
- Applies to every command taking `--project` (`record`, `brief`, `check`,
  `suggest`, `tag`), since all go through `project_info`.

## 3. Repos without a remote

| Situation | Identity |
|---|---|
| Not a git repo | native path |
| Git repo, no remote | main-checkout path (+ relative subpath) |
| Linked worktree, no remote | mapped onto the main checkout |
| Linked worktree, with remote | portable id (already the same as main) |
| Remote added later | automatic fold into the portable node (§4) |

- `main_checkout(root)`: `git rev-parse --path-format=absolute
  --git-common-dir`. Common dir `<X>/.git` → main checkout `<X>`. A bare
  common dir → the bare repo's path is the key root. Not a repo, git
  unavailable, or main checkout missing → no mapping.
- Mapping changes only `id`/`path`. `contents` is still read from the
  directory actually open.
- Rejected: root-commit sha (`cp -r old new` silently merges two projects)
  and a minted id in `.git/config` (writes into the user's repo; does not
  cross machines).

### Known limitation: path-keyed projects across machines

A project keyed by path matches only at that same path. On a second machine
with a different layout, a path-keyed **enclosing** project (e.g. a plain
`my-decisions/` folder around several repos) no longer encloses the clone:
its decisions drop out of "inherited from enclosing projects" and appear only
as tag-ranked kin under "closest projects". Verified 2026-09-26 by cloning
this repo elsewhere with a copied store: "decided here", tags and kin were
identical; the inherited section was missing.

Not addressed here — there is nothing portable to match on, and guessing
(say, by directory name) is the silent merge ADR 0002 rules out. Workarounds:
keep the same layout, or make the parent a git repo with a remote. A
journaled `alias-path` command (add a local path to a node's `paths` without
re-keying it) is the follow-up if this bites.

## 4. Folding and lazy re-key

`fold_project(s, from_id, into_id)` — the single graph operation behind
every merge:

1. Re-point every `(:Decision)-[:IN_PROJECT]->` edge from `from` to `into`.
2. Union `TAGGED` tags and `paths` onto `into`.
3. Keep `into`'s `name` and `portable`.
4. `DETACH DELETE` the `from` node.

Decision ids never change, so `--supersedes` and amendments still resolve.

It runs in three places:

- **Lazy re-key (required).** In `upsert_project`, and in `brief`'s portable
  backfill, when the resolved portable id `P` exists and a node is found
  whose `id != P` — by `portable = P`, or by `paths` containing the local
  path while it has no `portable` — fold it into `P` (creating `P` from it
  if `P` does not exist yet). This absorbs path-keyed nodes created by
  0.4.x writers and repos that acquired a remote. `brief` still never
  creates a node where none existed.
- **Replay.** `replay_entry` resolves every line to its key through the same
  `upsert_project`, so old path-keyed `record` lines land on the portable
  node. No journal rewrite.
- **`merge-project`** (§5).

Conservative rule: a node that already has a *different* `portable` is never
folded on a path match. A directory whose remote changed gets a new node;
`maintain` reports the pair. A wrong merge is worse than a visible split.

Automatic folds write no journal line: replay reproduces them from the
`portable` data already journaled.

## 5. `merge-project`

```
precedent.py merge-project <from-id> --into <id-or-path>
```

- `<from-id>` must match a node id exactly. No match → exit 2, list up to 5
  nodes whose name or id contains the value.
- `--into`: exact node id first, else a live path via `project_info`. A path
  with no node creates one.
- `from == into` → exit 2.
- Journals `project_merge {from, into}` before mutating, then calls
  `fold_project`.
- Afterwards prints any same-topic live-decision contradictions now in
  `into`, in `maintain`'s format. Never resolves them itself.
- `replay_entry` handles `project_merge`; a missing `from` is a no-op
  (an automatic fold may already have absorbed it).
- Built for agent callers: ids are copied from `maintain` output; `.` works
  for the current project.

`maintain` changes:

- "projects whose path no longer exists": when a live project shares the
  name, print `merge-project <id> --into <candidate>`.
- "two nodes, one repo": replace the hand-written `cypher` fix with a
  `merge-project` line.

## 6. Names

- `upsert_project` sets `name` only on create (`ON CREATE SET`).
- `brief`, `suggest`, `tag` print the node's stored name, so a worktree
  shows `project: calit`, not `project: calit-gdpr`.

## 7. Migration and mixed versions

### Migrating a store

- New marker node `(:Meta {graph_format: 2})`.
- On open, a writer that finds no marker or `graph_format < 2` runs
  `replay_journal` once and writes the marker — the same path
  `_migrate_grafeo_store` uses. Every node gets its new key in one pass.
- `replay_journal` pre-scans the whole journal for lines newer than it
  understands **before** wiping the graph, and refuses without touching it.

### Older binaries (0.4.x) on a migrated store

| 0.4.x does | Result |
|---|---|
| reads | works — resolves by `portable` property |
| writes to an existing project | works — resolves by `portable`; still overwrites `name` (cosmetic) |
| creates a project | path-keyed node with `portable`; the new binary lazily re-keys it (§4) |
| sees `(:Meta)` | ignores it |
| `rebuild` | complete graph; `project_merge` lines reported as "unknown journal op" and skipped, so merged ghosts reappear. The marker is wiped, so the next new-binary open migrates again. No data lost. |

This is why `SCHEMA` does not change: 0.4.x stops, after wiping, at any line
stamped with a higher `v`, and `s.log` stamps every line. An unknown op at
`v: 1` is skipped and reported instead.

## 8. Errors

All exit 2 with an actionable message:

- `--project` not a directory (with resolved path and hint).
- `merge-project` source not found (with near-matches).
- `merge-project` `from == into`, or `--into` resolves to nothing.
- git missing or timing out behaves as "no remote", as today.

## 9. Tests (inline `selftest`)

- Missing `--project` exits 2 and creates no node (issue #13 repro).
- Worktree without a remote resolves to the main checkout's id; worktree
  with a remote resolves to the portable id (real `git worktree add`).
- Single non-`origin` remote is used; several without `origin` → no remote.
- Replay: an old path-keyed `record` followed by a line carrying `portable`
  yields one portable-keyed node holding both decisions.
- Lazy re-key: a path-keyed node with `portable` (as 0.4.x creates) is folded
  on the next `record` and on the next `brief`; `brief` creates nothing when
  no node exists.
- A repo gaining a remote folds its path node on the next write.
- A node with a different `portable` is not folded on a path match.
- `merge-project`: edges moved, tags and paths unioned, source deleted,
  `project_merge` journaled, `rebuild` reproduces the same graph,
  contradictions printed, near-matches listed on a bad id, `from == into`
  refused.
- Replaying `project_merge` with a missing `from` is a no-op.
- `name` survives a write from a differently named worktree.
- Graph-format migration re-keys an old store exactly once.
- `replay_journal` refuses a too-new journal without wiping the graph.
- Mixed versions: against a migrated store, run the 0.4.x resolution and
  replay paths (portable lookup, path-keyed create, unknown-op skip), then
  reopen with the new code and assert one node per repo.

## 10. Docs

- `docs/adr/0009-the-remote-is-the-graph-key.md`, superseding the relevant
  option in ADR 0002.
- `CONTEXT.md`: update **Project** and **Portable id**.
- Precedent skill / `maintain` text: mention `merge-project`.
- After merge, offer to record the superseding decision in the graph.
