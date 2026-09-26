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

Without a remote, a path is not an identity of its own to fold anything
into, so a write there resolves onto whichever node already holds it
(`path_holder`) instead of the other way round: its own id first, then a
portable-less node listing it, then a remote-keyed one — so a notes folder
merged into a remote-keyed project stays there. Resolving onto a remote-keyed
node is not folding it; a node with a different portable is still never
folded on a path match. Only a node keyed
on one of its own paths — a worktree keyed on itself, say — is what folds
in. A node that merely lists the path already owns it, and matching on that
membership too would drag it back under whichever path last wrote — undoing
a `merge-project` the moment someone writes at the path the ghost used to
live at. This is what keeps an id put once a merge has settled it.

## How an existing store gets there

No journal rewrite. `upsert_project` folds any node holding the project
under an older key — the same portable id under a path, or a portable-less
node on one of its paths — into the new key, on every write and every
replay. A `(:Meta {graph_format: 2})` marker makes the first writer replay
the journal once. Folds the journal cannot reproduce on its own are
journalled as `project_merge`, the same op `merge-project` writes. Each such
line carries the target's `portable`, `project_path` and `name` as well as
`from` and `into`, and replay upserts the target from them before folding:
a target the fold itself created (a merge into a directory with no node, a
key that did not exist yet) would otherwise come back as a rename of the
source — its dead paths only, no portable id. `settle()` also adds the live
path to the key node, so a fold from a worktree leaves the main checkout's
path on it.

The first writer is decided by a claim, not a lock: one statement sets
`claim`/`claimed` on `(:Meta {id:'meta'})`, conditioned on nobody already
holding a fresh one — `MERGE` finds-or-creates the node and the guard sits
in the same query, so two writers racing this cannot both see their own
token come back. A claim older than 300 seconds is stale and gets retaken,
so a migrator that crashed mid-replay does not wedge every session after
it. The wipe a replay starts with spares `:Meta`, so the claim survives its
own replay; writing the new `graph_format` clears it. A writer that loses
the race, or opens a store already at the current format, proceeds without
replaying — `project_info` resolves older keys for it either way, migrated
or not.

That claim window is also a visibility window: a reader can open the store
while another writer is mid-replay and see a graph short whatever has not
gone back in yet. This is the same window a manual `rebuild` has always
had — the graph is a disposable index and the journal is the source of
truth — reached automatically now instead of by a command the user typed.

## Considered options

- **Root-commit sha for remote-less repos** — rejected: `cp -r old new`
  gives two projects one identity, the silent merge ADR 0002 rules out.
- **A minted id in `.git/config`** — rejected: writes into the user's repo
  and does not cross machines.
- **Bumping `SCHEMA` for `project_merge`** — rejected: 0.4.x stops a rebuild
  at any newer line after wiping the graph, and every new line would be
  newer. At v1 it skips the unknown op and says so.
- **A positional `from` on `merge-project`** — rejected: `tag --merge/--into`
  and `amend --id` are already flags, an id here is copied out of `maintain`
  rather than typed, and a flag costs nothing while reading like its
  siblings. The command is `merge-project --from <id> --into <id-or-path>`.

## Consequences

- A path-keyed project (no remote) matches only at the same path on another
  machine; an enclosing plain folder stops enclosing a clone elsewhere.
  Known limitation; an `alias-path` command is the follow-up if it bites.
- A directory whose remote changes to a different one gets a new node;
  `maintain` reports the pair under "one checkout, two remotes" when the
  shared checkout is on this machine, with a `merge-project` line from the
  other node into the one the checkout now reports. It never merges: a fork
  and its upstream are two projects.
- `--project` must name a directory.
- Every `merge-project` line this prints — in `maintain`'s output and in the
  skill docs that show the command — quotes both ids in plain double quotes
  with no escaping. That is the one syntax that pastes unmodified into bash,
  zsh, PowerShell and cmd; an id containing `"` would break it, and Windows
  already forbids that character in a path.
