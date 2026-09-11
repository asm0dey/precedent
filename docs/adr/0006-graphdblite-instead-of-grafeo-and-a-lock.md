# graphdblite instead of Grafeo, and no lock at all

Every command took an exclusive file lock (ADR 0003) because Grafeo lost
concurrent writes in silence: 120 writes across 6 processes stored 60, every
writer exiting 0. Readers could also observe a torn write — the new value of one
property beside the stale value of the other — because Grafeo's WAL records one
property per record, so `SET n.a=$i, n.b=$i` crossed no commit boundary. A
writer-side transaction was tried and did not fix it. The lock was the only
thing holding either property, which made it load-bearing and made every change
near it dangerous.

`graphdblite` uses SQLite as a crash-safe store under a graph-native layer, so
both properties come from the engine. Measured, unlocked, against Grafeo as the
control on the same machines:

| | Grafeo | graphdblite |
|---|---|---|
| 6 processes x 20 writes, Linux | 40/120 | 120/120 |
| 6 processes x 20 writes, macOS | 60/120 | 120/120 |
| 8 processes x 200 writes | 569/1600 | 1600/1600 |
| readers vs a live writer, macOS | 15 torn / 873 reads | 0 torn / 11,956 reads |
| SIGKILL mid-write | survives | survives |

macOS is the row that decided it. The Grafeo tear never reproduced on Linux, so
a clean Linux run proves nothing; the control tearing on macOS in the same
harness, on the same machine, minutes apart, is what makes the zero meaningful.

`BUSY_TIMEOUT_MS` is 5s, not the lock's inherited 30s. The lock was held for a
whole command, so 30s was a real bound on a real wait; nothing holds the
database across statements now. Measured at 8 processes writing back-to-back,
nothing waits past ~850 ms even when allowed thirty seconds, and everything
from 1s up records zero errors. 5s keeps a margin over the measurement and
stays under the SessionStart hook's own 20s timeout, so a genuinely stuck
command reports rather than being killed mid-write. Loud contention is a
supported outcome; silent loss is not, which is the distinction
`_check_concurrent_writers` asserts.

Migration cost was one rebuild. The journal is the source of truth, so
`rebuild` replayed it into the new engine and reproduced every node and edge
count exactly. A store written by the old engine keeps `graph.db` as a
directory; the first command that records renames it aside, replays the
journal, and then deletes it, so an upgrade does not need the user to know any
of this happened.

Deleting it is not tidiness. An install that predates the swap still opens
`graph.db` — another agent's copy of the plugin, an older ACR realisation, a
checkout nobody pulled — and a readable old store is a live store for those:
they would read and write decisions this engine never sees, with neither side
reporting anything wrong. Two stores that disagree is worse than one that had
to be rebuilt. It is safe to delete because the old graph was never the source
of truth: `journal.jsonl` is, it is untouched, and `rebuild` reconstructs the
graph from it at any time. The exception is a replay that could not read every
entry — the one case where the old store may hold something the journal does
not — and there it is kept and named.

## Consequences

The engine is young — 0.1.x, one author, few users — where Grafeo has a
community. This is the trade that was accepted, and the journal is the hedge:
the shim that proved the swap was twenty lines, so a third engine is a rebuild
away too. What makes it survivable is that it is measured on every OS in CI on
every push, not assumed.

openCypher conformance is stricter than Grafeo's, which surfaced four queries
here that put `WHERE` after `OPTIONAL MATCH`. Per the spec that filters the
optional pattern and not the rows, so `brief` listed superseded decisions.
Grafeo applied it to the whole query and hid the bug. They are fixed; the shape
is worth watching for in new queries.

`export` no longer produces a grafeo-server data directory, because there is no
server to read it. It writes a complete store — graph plus journal — that
`--home` can be pointed at directly. The read-only web UI on :7474 is gone.

A synced store is still not a shared store, for a new reason: SQLite's
serialisation needs a real filesystem, and Dropbox or iCloud is not one.
