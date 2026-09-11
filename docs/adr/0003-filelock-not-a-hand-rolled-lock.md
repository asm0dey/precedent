# `filelock` instead of a hand-rolled cross-platform lock

Concurrent processes on one embedded Grafeo database silently drop writes —
measured at 120 writes across 6 processes leaving 60 stored, with every writer
reporting success ([GrafeoDB/grafeo#405](https://github.com/GrafeoDB/grafeo/issues/405)).
Every command therefore takes an exclusive lock. That lock was `fcntl.flock`,
imported at module level, so on Windows `precedent.py --help` raised
`ModuleNotFoundError` — not a degraded path, the tool did not run at all. We
added `filelock` to the PEP 723 dependency header rather than writing an
`fcntl`/`msvcrt` shim.

A dependency is already being fetched for `grafeo`, so a second one costs a word
in a comment header and no install step. The alternative is owning a concurrency
primitive on an operating system we do not run, with no way to test it — and
grafeo#405 is this project's own evidence for what a silently-wrong lock costs.

## Consequences

The lock is local and a synced store is not. Two machines against one Dropbox
folder is grafeo#405 with extra steps; this is documented rather than prevented.

## Superseded by ADR 0006

The lock is gone, along with the `filelock` dependency. It existed to work
around an engine that silently dropped concurrent writes; the engine was
replaced with one that does not. See
[0006](0006-graphdblite-instead-of-grafeo-and-a-lock.md). The reasoning above
still stands for the situation it was written about, and the 30-second bound it
chose survives as `Store.BUSY_TIMEOUT_MS`.
