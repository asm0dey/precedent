# Setup, storage and the CLI

Everything under the agent-facing surface described in the [README](../README.md): wiring the
session-start hook and the statusline badge, where the store lives, and the CLI the skills drive.
Installing the skills themselves is in the README.

## The session-start hook

The hook lives beside the skills rather than inside one, so it is wired by path rather than
through a symlink. ACR and the plugin channel do that for you. For the manual symlink route, add
this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command",
            "command": "\"$HOME/src/precedent/adapters/claude/hooks/session-start.sh\"",
            "shell": "bash",
            "timeout": 20 }
        ]
      }
    ]
  }
}
```

`adapters/claude/hooks/hooks.json`, the copy of this block the plugin channel ships, carries the
same single bash entry, for the same reason. Claude Code's hook schema has no per-platform field,
so `shell` only picks which interpreter runs a given entry's `command`; it does not gate by OS. A
second entry pointing at `session-start.ps1` would therefore be dispatched on every OS. On macOS
or Linux with `pwsh` installed it would run and rely on the script's own `$IsWindows` guard to
exit quietly. On macOS or Linux *without* `pwsh`, there is no interpreter to run it at all, and
the entry throws: a user-visible hook error every session, which defeats the point of a
silent-by-design hook. Shipping only the bash entry is silent by construction on POSIX, and on
Windows with Git Bash it still works. A Windows user without Git Bash gets no priming hook, and
no error either.

Windows users without Git Bash can opt in to the PowerShell hook by pasting a second entry into
their own `~/.claude/settings.json`, pointing at `session-start.ps1`
(`adapters/claude/hooks/session-start.ps1` in the repo, or
`$HOME/src/precedent/adapters/claude/hooks/session-start.ps1` for the clone above):

```json
{ "type": "command",
  "command": "& \"$HOME/src/precedent/adapters/claude/hooks/session-start.ps1\"",
  "shell": "powershell",
  "timeout": 20 }
```

Add it as a second element of the `"hooks"` array above. The script still guards itself with
`if ($IsWindows -eq $false) { exit 0 }`, so it only does anything on real Windows, including the
PowerShell 5.1 fallback Claude Code uses when `pwsh` 7 is missing.

## The statusline badge

A session primed by the hook is otherwise indistinguishable from one that was not, and the
difference matters: it decides whether the standing orders are in force. `statusline.sh` renders
`[PRECEDENT]` while the graph is speaking in the current directory.

Claude Code has no plugin-provided statusline — `statusLine` is a field in `settings.json`, one
command for the whole line — so this is chained into whatever statusline you already run rather
than installed:

```json
{
  "statusLine": {
    "type": "command",
    "command": "$HOME/src/precedent/adapters/claude/hooks/statusline.sh"
  }
}
```

With an existing statusline, put it at either end and separate the two:

```json
{ "type": "command",
  "command": "your-statusline; printf ' '; $HOME/src/precedent/adapters/claude/hooks/statusline.sh" }
```

ACR installs it as a script artifact, at
`.claude/scripts/acr__asm0dey__precedent__statusline/statusline.sh`. Windows without Git Bash has
the twin, `statusline.ps1`, wired the same way.

The badge is not a decoration on the hook — it is the hook's own state. The SessionStart hook
drops an empty marker named after the directory it primed, and clears it whenever it has nothing
to say, so the badge appears exactly when a brief was injected. The statusline reads only the
marker's name, never its contents, and forks nothing: it runs on every render.

## Storage

```
~/.local/share/precedent/
├── journal.jsonl   append-only, fsync'd, one line per decision — source of truth
└── graph.db        graphdblite graph — a queryable index
```

Keep it elsewhere (a synced folder, an encrypted volume, a private git checkout) with `init`. It
moves an existing store to the new directory and leaves a pointer file at the default path naming
it, so the hook and every skill keep working with no configuration:

```bash
uv run ~/src/precedent/scripts/precedent.py init ~/Sync/precedent
uv run ~/src/precedent/scripts/precedent.py init   # where is it now?
```

The pointer beats `PRECEDENT_HOME`, which the SessionStart hook never sees. Two populated stores
are never merged silently: `init` stops and tells you to concatenate the journals and `rebuild`.

The journal exists because the graph engine is young, and because it makes the engine
replaceable. If the graph is ever corrupted, `precedent.py rebuild` replays the journal into a
fresh one and nothing is lost. That escape hatch has been used for real: the store was a Grafeo
graph until the journal carried it across to graphdblite, which cost a rebuild rather than a
migration. The journal is plain text, so it also diffs, and belongs in a private git repo if you
want history.

Upgrading from a store written before the engine changed needs nothing from you. That store keeps
`graph.db` as a directory, which the current engine cannot read, so the first command that
records replays the journal into a new graph and then deletes the old one. It is deleted rather
than kept because an install that predates the change would otherwise go on reading and writing
it unseen — two stores that quietly disagree is worse than one that had to be rebuilt — and it is
safe to delete because the journal it was rebuilt from is untouched. A replay that cannot read
every entry keeps the old store instead, and says so.

### Concurrency

Concurrent sessions are not coordinated by this tool. Several commands run at once by design — a
SessionStart hook running `brief` while the model runs `check` and you run `record`, times however
many sessions are open — and there is no file lock. graphdblite serialises writers through SQLite
and waits 5 seconds before failing loudly. Measured, that bound is never approached: 8 processes
writing back-to-back never waited past 850 ms.

That is a guarantee this project does not take on trust, because the previous engine did not hold
it: unlocked, 120 writes across 6 processes stored 60 on macOS and 40 on Linux, every writer
exiting 0 ([GrafeoDB/grafeo#405](https://github.com/GrafeoDB/grafeo/issues/405), filed from this
work). So `selftest` measures both properties — that concurrent writers all land, and that a
reader never sees a half-applied write — on Linux, macOS and Windows in CI, on every push.

### A synced store is not a shared store

SQLite serialises writers through the filesystem, and a synced folder is not one. Dropbox, iCloud,
and network mounts do not carry the locking SQLite relies on; they copy files after the fact. Two
machines writing to one synced store are not serialised, and the loser is whichever copy the sync
service resolves away — silently.

Sync the journal, and leave the graph alone. `journal.jsonl` is append-only and merges in git, and
`precedent.py rebuild` reconstructs the graph from it on each machine. That is the second reason a
project's identity is its git remote instead of its path.

## The CLI

`scripts/precedent.py`, run with `uv run`. The skills drive it; you do not have to. The verdicts
it prints are facts about the graph, and whether they apply in the case at hand is the agent's
call (see `docs/adr/0001`).

| Command | Does |
|---|---|
| `brief` | project type, decisions here, precedent from similar projects — what the hook injects |
| `record` | write a decision (`--topic --chose --rationale --rejected --despite --supersedes`) |
| `check` | precedent for a topic, plus a conflict verdict (`--topic --chose`) |
| `suggest` | decisions not yet made here, and principle candidates |
| `tag` | list the tag vocabulary, or change this project's tags |
| `regret` | mark a repeated choice as a mistake, inverting its precedent |
| `principle` | promote a repeated choice to a standing principle |
| `maintain` | contradictions, dead projects, orphans, counts |
| `init` | show where the store lives, or move it elsewhere |
| `rebuild` | replay `journal.jsonl` into a fresh graph |
| `export` | snapshot the store (graph + journal) to a directory |
| `cypher` | escape hatch |
| `standing-orders` | the session rules the hook injects alongside the brief |
| `selftest` | concurrency and crash-safety checks |

`--help` on any of them for the flags.

```
$ uv run scripts/precedent.py check --topic persistence --chose sqlite

== verdict for choosing 'sqlite' ==
  CONFLICT: rejected in bot-alpha (Postgres for bot state) — Two bots already
     share one Postgres; SQLite locks up once webhook handlers run concurrently
  DIVERGENCE: you chose 'postgres' for this in 3 other projects
```
