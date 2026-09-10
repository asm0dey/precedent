# Portable id is identity, the native path is an alias

`Project.id` was a native absolute path, which made containment a string-prefix
fact needing no stored edge — a genuinely good property. It also meant one
repository checked out on two machines was two Projects sharing no precedent,
and `init`'s headline use case is putting the store in a synced folder, so that
is the intended configuration rather than an edge case. Worse, `maintain`'s
liveness check is `Path(id).exists()`, so on Linux every Windows-recorded
project reported as gone — and dead projects are discounted, so the other
machine's history would have been silently written off.

A Project is now identified by a **portable id**: the git remote, plus a `#`
subpath for a module inside it. `Project.id` remains the native path of first
sighting, `Project.paths` collects the rest, and both reads and writes resolve
through the portable id.

## Considered options

- **`~`-relative ids** — rejected. `/home/u/src/proj` and `C:\dev\proj` are the
  same repository at different relative paths, which is the normal case, so it
  solves almost nothing; and it will occasionally match two genuinely different
  projects sitting at the same relative path on two machines, silently merging
  their histories. An absent portable id is a fine answer; a wrong one is the
  same failure class as a wrong tag.
- **Migrating the primary key outright** — rejected as too large. The chosen
  design is additive: no journal rewrite, no key migration.
- **Read-only fallback** — rejected. Reads would resolve to the existing node
  while writes created a second one, fragmenting the graph a little more with
  every machine and every session, invisibly.

## Consequences

- `#` keeps containment derivable: `repo#/backend` is still a string prefix.
- Repos with no remote have no portable id and behave exactly as before. Because
  one may acquire a remote later, `portable` is computed lazily on every `brief`
  and `record` and backfilled onto existing nodes — `brief` is therefore no
  longer strictly read-only, but it still never *creates* a node, which was the
  actual reason for that rule.
- Liveness checks every path in `Project.paths` and reports a third state, "not
  on this machine", distinct from gone. A missing directory was never evidence a
  project is dead — an unmounted drive and another checkout look identical.
