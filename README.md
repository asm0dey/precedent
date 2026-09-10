# precedent

A CLI over a decision graph that remembers the architectural, business and
tooling decisions you make — across every project — and surfaces them the
next time a similar choice comes up. Claude Code, Codex and Cursor can all
reach it; see Install below for how each one gets there.

You decided something two years ago, in a repo you have not opened since, for a
reason that made sense at the time. The code still shows *what* you chose. The
*why* is gone, and so is the list of things you rejected on the way. `precedent`
keeps all three, in a graph, and puts them back in front of you at the moment
they matter.

```
project: gym-bot   tags: bot, telegram, python

== closest projects (3) ==
  bot-alpha            3 shared: bot, python, telegram
  bot-beta             3 shared: bot, python, telegram
  api-one              1 shared: python

== precedent from those projects ==
  persistence: postgres   [3x: bot-alpha,bot-beta,bot-gamma]
  logging: structlog      [3x: bot-alpha,bot-beta,bot-gamma]
  ingest: webhook         [2x: bot-alpha,bot-beta]
```

And when you are about to contradict yourself:

```
$ precedent.py check --topic persistence --chose sqlite

== verdict for choosing 'sqlite' ==
  CONFLICT: rejected in bot-alpha (Postgres for bot state) — Two bots already
     share one Postgres; SQLite locks up once webhook handlers run concurrently
  DIVERGENCE: you chose 'postgres' for this in 3 other projects
```

Neither is a veto. Every rationale carries a condition, and the agent's job is to
ask whether it still holds: *that* rationale is about concurrent writers, so it
says nothing about a single-user tool. Consistency is usually right and sometimes
exactly wrong.

## Install

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12+. There is no install
step for the CLI itself — it is a PEP 723 single file and `uv` fetches what it
needs on first run, then reuses a cached environment. (A persistent virtualenv
was measured and rejected: it saves 11 ms per call, both paths being dominated
by the ~50 ms `grafeo` import, and costs an install step plus an environment to
keep in sync.) What differs by agent is how the skill, commands and hook get in
front of it.

### Claude Code plugin

`adapters/claude/` is the plugin root — `SKILL.md` at its top, `commands/*.md`
and `hooks/hooks.json` are discovered by Claude Code's own convention, nothing
is enumerated by hand. The manifest that drives this channel is
`adapters/claude/.claude-plugin/plugin.json`, generated from `agent-plugin.yaml`
by `scripts/gen-plugin-json.py` (`--check` fails CI if it drifts).

No marketplace catalog for this plugin is published anywhere yet — this repo
ships no `marketplace.json`, so `/plugin install precedent` is not something a
reader can run today. A marketplace entry that lists this plugin would point at
the subdirectory — `{"name": "precedent", "source": "./adapters/claude"}` for a
marketplace hosted in this repo, or a `git-subdir` source with
`path: "adapters/claude"` from elsewhere — but a `git-subdir` source sparse-
checks out only that named subdirectory, so it would ship the adapter
*without* `scripts/precedent.py`, the CLI the adapter drives. A plugin install
of this project needs the full repository checkout, not a subdirectory-only
one; that file, and which install shape avoids the gap above, is a decision
for whoever hosts the catalog, not something this repo settles for them.
`claude plugin validate adapters/claude` is Anthropic's own checker and
currently passes, with one warning (the optional `author` field is absent).

### ACR (Codex and Cursor)

[ACR](https://github.com/jbaruch/agentic-context-registry) is the only channel
that reaches Codex and Cursor. It is driven by `agent-plugin.yaml`, which ships
the skill, the CLI script and the session-start hook. ACR's v1 schema has no
`commands` artifact class, so the 9 slash commands under Commands below cannot
be expressed this way — Codex and Cursor users get the skill and the script,
not the commands. Point ACR at this repo's `agent-plugin.yaml`; see ACR's own
docs for the current install invocation.

ACR's `hookArtifact` takes exactly one path per hook entry, and this manifest
declares only `adapters/claude/hooks/session-start.sh` — a POSIX shell script.
A second entry pointing at `session-start.ps1` was considered and rejected:
ACR has no platform gate either, so both would fire on every OS, which is the
exact double-hook failure mode fixed elsewhere in this README's Claude Code
hooks section. Windows Codex/Cursor users installed via ACR get the skill and
the script but no session-priming hook until ACR itself grows a platform
condition.

### Manual symlink

What every channel above ends up doing to `~/.claude/`, done by hand — also the
option if you would rather not add a marketplace or install ACR:

```bash
git clone https://github.com/asm0dey/precedent ~/src/precedent
ln -s ~/src/precedent/adapters/claude ~/.claude/skills/precedent
ln -s ~/src/precedent/adapters/claude/commands/*.md ~/.claude/commands/
```

Optionally, prime every session automatically — add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command",
            "command": "\"$HOME/.claude/skills/precedent/hooks/session-start.sh\"",
            "shell": "bash",
            "timeout": 20 }
        ]
      }
    ]
  }
}
```

`hooks.json` (`adapters/claude/hooks/hooks.json`, the copy of this block the
plugin channel ships) carries the same single bash entry, for the same
reason: Claude Code's hook schema has no per-platform field, so `shell` only
picks which interpreter runs a given entry's `command` — it does not gate by
OS. A second entry pointing at `session-start.ps1` would therefore be
dispatched on every OS too, not just Windows. On macOS or Linux with `pwsh`
installed it would run and rely on the script's own `$IsWindows` guard to
exit quietly; on macOS or Linux *without* `pwsh` installed, there is no
interpreter to run it at all, and the entry throws — a user-visible hook
error every session, which defeats the entire point of a silent-by-design
hook. Shipping only the bash entry is silent by construction on POSIX, and
on Windows with Git Bash it still works; a Windows user without Git Bash
simply gets no priming hook rather than an error.

Windows users without Git Bash can opt in to the PowerShell hook by pasting
a second entry into their own `~/.claude/settings.json`, pointing at
`session-start.ps1` (`adapters/claude/hooks/session-start.ps1` in the repo —
`$HOME/.claude/skills/precedent/hooks/session-start.ps1` if symlinked per
this section):

```json
{ "type": "command",
  "command": "& \"$HOME/.claude/skills/precedent/hooks/session-start.ps1\"",
  "shell": "powershell",
  "timeout": 20 }
```

Add it as a second element of the `"hooks"` array above. The script still
guards itself — `if ($IsWindows -eq $false) { exit 0 }` — so it only does
anything on real Windows, including the PowerShell 5.1 fallback Claude Code
uses when `pwsh` 7 is not installed.

The hook runs a brief for the working directory and injects it, so a session
opens already knowing what you decided here and in comparable projects. It stays
silent where the graph has nothing to say, so quiet directories cost nothing.

## Commands

| Command | Does |
|---|---|
| `/precedent-prime` | Load this project's decisions and precedent into context |
| `/precedent-record` | Record a decision that was just settled |
| `/precedent-check <topic> [option]` | Prior decisions on a topic, plus a conflict verdict |
| `/precedent-tag [tags]` | Show or set this project's tags |
| `/precedent-analyze` | Read an existing project and record the decisions visible in it |
| `/precedent-suggest` | Decisions this project still owes |
| `/precedent-diverge` | Record that this project departs from precedent, and why |
| `/precedent-regret` | Mark a repeated choice as a mistake, inverting its precedent |
| `/precedent-maintain` | Contradictions, tag drift, dead projects |

These 9 commands are Claude Code only — see Requirements.

The skill also triggers on its own — when you settle a choice, when you weigh
options, when you start work somewhere it has not briefed you on.

## When precedent does not simply apply

Three situations get confused with each other and want different records:

| The situation | What to run |
|---|---|
| The precedent is sound, but **this project is genuinely different** | `record --despite "<why>"` |
| The choice was right then and is **wrong now, here** | `record --supersedes <id>` |
| The choice was **wrong everywhere**, and you repeated it | `regret --topic … --chose … --because "<lesson>"` |

The first matters more than it looks. Diverge without recording why, and the
graph reports `DIVERGENCE` in that project forever — a warning that is correct,
unanswerable and permanent, which is how a useful signal becomes noise. Record
the exception and `check` reports it as *acknowledged here*, while the plain
warning still fires everywhere else.

The third exists because repetition is what gives precedent its weight, and that
is exactly the problem when the repeated thing was a mistake. Without `regret`,
*"you chose mongo in eight projects"* reads as a strong norm and argues for a
ninth. Afterwards:

```
REGRET: you chose this in 8 project(s) and later concluded it was a mistake —
   "schema drift made every migration a manual rewrite ..."
   you now prefer: postgres
```

Nothing is deleted. The eight keep their original rationales, because a reason
that looked good eight separate times is what tells you the lesson was expensive
and the trap convincing. They simply stop counting as a norm.

## How it finds comparable work

A project carries a **set of tags**, not one type. A service is not
`backend` *or* `java` *or* `distributed` — it is all three, and which of them a
past decision rhymes with depends on the decision. Persistence choices travel
along `backend`; build choices along `java`; retry semantics along `distributed`.

Precedent is ranked by **tag overlap**, so a sibling sharing four tags outranks
one sharing a single generic tag, and you can see which tags did the matching.

Tags are supplied by the agent, never inferred. Keyword matching was tried and
removed: a Rust CLI named `nextgen` was classified `web-frontend` because the
word "next" appeared in its description, and `react-native` matched `react`
before it could match itself. A wrong classification is worse than none — the
project then silently inherits decisions from unrelated work.

There is no list of known build files either. The tool shows the directory's
top-level contents and lets the agent read them, so `build.zig`, `shard.yml`,
`BUILD.bazel` or whatever appears next year is as visible as `package.json`.

**Monorepos** need no special handling. `Project.id` is an absolute path, so
containment is a string-prefix fact: tag `/repo/backend` and `/repo/slackbot`
separately and they never lend each other precedent, while decisions recorded at
`/repo` (CI, branching, licensing) are inherited by both.

## Storage

```
~/.local/share/precedent/
├── journal.jsonl   append-only, fsync'd, one line per decision — source of truth
├── graph.db        Grafeo graph — a queryable index
└── .lock           exclusive lock, held for the length of one command
```

Keep it elsewhere — a synced folder, an encrypted volume, a private git
checkout — with `init`. It moves an existing store to the new directory and
leaves a pointer file at the default path naming it, so the hook and the slash
commands keep working with no configuration:

```bash
uv run ~/src/precedent/scripts/precedent.py init ~/Sync/precedent
uv run ~/src/precedent/scripts/precedent.py init   # where is it now?
```

Better than `PRECEDENT_HOME`, which the SessionStart hook never sees. Two
populated stores are not merged silently: `init` stops and tells you to
concatenate the journals and `rebuild`.

The journal exists because the graph engine is young. If the graph is ever
corrupted, `precedent.py rebuild` replays the journal into a fresh one and
nothing is lost. It is plain text, so it also diffs and belongs in a private git
repo if you want history.

The lock is not decoration. Two processes on one embedded graph silently lose
writes — measured at 120 writes across 6 processes leaving 60 stored, with every
writer reporting success ([GrafeoDB/grafeo#405](https://github.com/GrafeoDB/grafeo/issues/405),
filed from this work). Every command takes the lock, so concurrent sessions —
Claude Code, Codex, Cursor, or several of them at once — queue instead of
clobbering.

### A synced store is not a shared store

The lock is a local file. Two machines writing to one store over Dropbox,
iCloud or a network mount are not serialised by it — each sees its own lock
file, and `grafeo` silently drops concurrent writes (GrafeoDB/grafeo#405:
120 writes across 6 processes, 60 stored, nothing raised).

Sync the journal, not the graph. `journal.jsonl` is append-only and merges
in git; `precedent.py rebuild` reconstructs the graph from it on each
machine. That is also why a project's identity is its git remote rather
than its path — see `docs/adr/0002`.

## Measured

Four realistic prompts, run by subagents with and without the skill, graded
against 29 assertions by an independent grader.

| | With skill | Baseline | Delta |
|---|---|---|---|
| Pass rate | **93%** ± 8% | 35% ± 15% | +0.58 |
| Time | 117s | 82s | +36s |
| Tokens | 84.5k | 77.6k | +6.9k |

The sharpest result is a baseline failure. Asked what to be consistent with when
starting a new Telegram bot, the skill-less run read the manifests of two
unrelated FastAPI services and recommended async SQLAlchemy — precedent
transferred across the wrong kind of project, stated confidently. Another
baseline searched honestly, found nothing, and then asserted *"No decision record
covers it"*, which was false.

Honest limits: the two remaining failures are word-count assertions — answers
carrying real reasoning run longer than the limits allow, and the limits have not
been relaxed to make the number look better. `evals/` holds the prompts,
assertions and a fixture seeder if you want to re-run or extend them.

## Requirements

- `uv` and Python 3.12+
- Claude Code, Codex or Cursor — the graph, skill and CLI reach all three; the
  9 slash commands are Claude Code only (see Install)
- Linux, macOS or Windows

## License

MIT
