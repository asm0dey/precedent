"""precedent: durable, cross-project memory of the decisions you have made.

Two stores, on purpose:

  journal.jsonl  append-only, one line per decision, the source of truth
  graph.db       graphdblite graph, a queryable index rebuilt from the journal

The journal exists because the graph engine is young (0.1.x) and because it
makes the engine replaceable: `rebuild` replays the journal into whatever
engine is current, so swapping one costs a rebuild, not the history. That is
not hypothetical — this store was grafeo until the journal carried it across.
The journal is also plain text, so it diffs and survives in git.

Every subcommand is a short-lived process, and several run at once: a
SessionStart hook runs `brief` while the model runs `check` and the user runs
`record`, times however many sessions are open. Nothing coordinates them.
There is no file lock — graphdblite serialises writers itself through SQLite,
waiting up to BUSY_TIMEOUT_MS and then failing loudly rather than quietly
dropping a write. That is a claim about someone else's engine, so `selftest`
measures it on every OS this ships to instead of trusting it: see
`_check_concurrent_writers` and `_check_reader_isolation`. The engine this
replaced could not hold either property — unlocked, 120 writes across 6
processes stored 60 and raised nothing.
"""
