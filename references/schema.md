# Decision graph schema

Read this before writing ad-hoc Cypher via `precedent.py cypher`. The engine is Grafeo
(openCypher 9.0 dialect).

## Nodes

| Label | Key | Properties |
|---|---|---|
| `Decision` | `id` | `title`, `statement`, `rationale`, `scope`, `status`, `created`, `despite` |
| `Project` | `id` (native path of first sighting) | `name`, `seen`, `portable`, `paths` |
| `Tag` | `name` | — |
| `Topic` | `name` (lowercased) | — |
| `Option` | `name` | — |
| `Principle` | `id` | `statement`, `created` (see `check`'s use of `ABOUT` below) |
| `Lesson` | `id` | `statement`, `topic`, `option`, `instead`, `created` |

`Decision.scope` ∈ `architecture` `business` `process` `tooling` `product`.
`Decision.status` ∈ `active` `superseded` `regretted`. A `regretted` decision was
a choice the user later judged a mistake; it keeps its rationale as evidence but
stops counting as a norm, and the `Lesson` linked to it carries the reason.

`Decision.despite` records a knowing exception: this project departed from the
precedent on purpose, for the stated reason. `check` reports the divergence as
acknowledged in that project and unchanged everywhere else. Filter on `status='active'` in any
query that answers "what is true now" — superseded decisions are kept
deliberately and will otherwise pollute the result.

A project's kind is a SET of `Tag` nodes, not a property. Tags are free-form
strings supplied by the agent, never inferred from dependencies, and are written
only by `precedent.py tag --add`. A project with no tags never appears as precedent.

Precedent is ranked by **tag overlap**: other projects are scored by how many
tags they share, and `--min-shared` sets the floor. Matching is exact string
equality, so the vocabulary has to stay tight — `precedent.py tag` with no flags lists
what is in use, and `--add` refuses near-duplicates.

## Edges

```
(Decision)-[:IN_PROJECT]->(Project)
(Decision)-[:ABOUT]->(Topic)
(Decision)-[:CHOSE]->(Option)
(Decision)-[:REJECTED]->(Option)
(Decision)-[:SUPERSEDES]->(Decision)
(Principle)-[:DERIVED_FROM]->(Decision)
(Principle)-[:ABOUT]->(Topic)
(Project)-[:TAGGED]->(Tag)
(Lesson)-[:REGRETS]->(Decision)
(Decision)-[:DIVERGES_FROM]->(Decision)
```

`check` finds principles in play by matching `(Principle)-[:ABOUT]->(Topic {name:$topic})`,
never by scanning `Principle.statement` — matching stays exact and structural, the same
rule Topic and Tag already follow (see `docs/adr/0001`). A `Principle` created with no
`--topic` gets no `ABOUT` edge and is therefore invisible to `check`; `principle` warns
about this at creation time.

Cross-project precedent is tag overlap, not a stored edge:

```cypher
MATCH (p:Project)-[:TAGGED]->(t:Tag)
WHERE t.name IN $tags AND p.id <> $pid
RETURN p.name AS project, collect(DISTINCT t.name) AS shared,
       count(DISTINCT t) AS n
ORDER BY n DESC
```

then decisions are read from the projects that scored above `--min-shared`.

## Identity

`Project.portable` is the identity: the git remote reduced to `host/owner/repo`,
plus a `#/subpath` for a module inside it. `Project.id` is only the native path
this project was first seen at, and stays the graph key — every query that
matches a node matches on `id`.

`Project.paths` is every native path the project has been seen at, one per line
(a string, not a list property). Both reads and writes resolve `portable` to an
existing `id` before touching a node, so one repository checked out on two
machines is one Project rather than two with no shared precedent. A repo with no
remote has no `portable` and behaves exactly as it did before. See
`docs/adr/0002`.

## Containment

Containment needs no stored edge: a monorepo root is a string prefix of its
modules, so it is derived — always accurate, never maintained. It is derived
from `paths`, not from `id`: `id` may be a path recorded on another
machine, and comparing this machine's directory against a Windows key would
silently report no containment. Comparison is path-boundary aware:
`/repo-elsewhere` is not inside `/repo`.

The three helpers take the info dict from `project_info`, whose `id` is the
graph key and whose `path` is this machine's directory. Results are ordered by
the local path that matched, never by `id` — a foreign `id` is not a depth:

- `enclosing(s, info)` — projects containing this one, outermost first.
- `contained(s, info)` — modules inside this one.
- `effective_tags(s, info)` — own tags plus every enclosing project's tags.
- `same_tree(s, info)` — self, ancestors and descendants; excluded from precedent
  because they are one codebase rather than comparable work.

Decisions recorded on an enclosing project surface in a module's `brief` as
inherited. Kin matching uses effective tags but scores candidates on their own
tags, so two sibling modules do not become kin purely by sharing the root's.

## Grafeo dialect notes

Learned by testing 0.5.42; these bite when you write Cypher from habit.

- **Alias every returned expression.** Rows come back as dicts keyed by the
  column name. `RETURN count(d)` yields the key `countnonnull(...)`; always
  write `RETURN count(d) AS n`.
- **`NOT (n)-[:R]->()` does not parse.** Use `NOT EXISTS { MATCH (n)-[:R]->() }`.
  `EXISTS { ... }` itself works fine.
- **Parameters are positional:** `db.execute(query, {"k": v})`, and referenced
  as `$k`. There is no `parameters=` keyword.
- Confirmed working: `MERGE` with `SET`, `OPTIONAL MATCH`, `collect()`,
  `collect(DISTINCT …)`, `count(DISTINCT …)`, `WITH … WHERE`, `ORDER BY`,
  `LIMIT`, `CONTAINS`, `STARTS WITH`, `IN $list`, `coalesce()`, `size()`,
  multi-hop patterns, `DETACH DELETE`, and `n:A OR n:B` label tests.

## Journal format

`journal.jsonl` is the source of truth; the graph is a rebuildable index. One
JSON object per line, appended and fsync'd before the graph is touched.

```json
{"ts":"2026-09-10T14:02:11","op":"record","id":"postgres-for-state-1789...",
 "title":"...","statement":"...","rationale":"...","scope":"architecture",
 "created":"2026-09-10","project_id":"/home/u/proj","project_name":"proj",
 "project_path":"/home/u/proj","portable":"github.com/u/proj",
 "project_type":"telegram-bot","stack":"aiogram","topics":["persistence"],
 "chose":["postgres"],"rejected":["sqlite"],"supersedes":[]}
```

`op` is `record`, `project_tags`, `tag_merge`, `regret` or `principle`; the
pre-tag `project_type` and the retired `tags_distinct` are still replayed so old
journals keep working. `precedent.py rebuild` replays the file in order, so
entries must stay append-only — editing or reordering lines rewrites history.

`project_path` and `portable` are how a replay on a second machine resolves onto
the project that is already there instead of inventing another. **Both `record`
and `project_tags` lines carry them**, and `replay_entry` resolves through them
on either — a tag line that arrived without them would create a second node for
the same repo. A line written before they existed carries neither, and falls
back to `project_id` — the native path — exactly as it did then.
