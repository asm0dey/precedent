# precedent

A Claude Code skill that remembers the architectural, business and tooling
decisions you make — across every project — and surfaces them the next time a
similar choice comes up.

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

Neither is a veto. Every rationale carries a condition, and the skill's job is to
ask whether it still holds: *that* rationale is about concurrent writers, so it
says nothing about a single-user tool. Consistency is usually right and sometimes
exactly wrong.

## Install

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12+. There is no install
step — the CLI is a PEP 723 single file and `uv` fetches what it needs on first
run, then reuses a cached environment. (A persistent virtualenv was measured and
rejected: it saves 11 ms per call, both paths being dominated by the ~50 ms
`grafeo` import, and costs an install step plus an environment to keep in sync.)

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
      { "hooks": [ { "type": "command",
                     "command": "$HOME/.claude/skills/precedent/hooks/session-start.sh" } ] }
    ]
  }
}
```

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
symlinks the default path at it, so the hook and the slash commands keep working
with no configuration:

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
filed from this work). Every command takes the lock, so concurrent Claude
sessions queue instead of clobbering.

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
- Claude Code (skill, commands and hook)
- Linux, macOS or Windows

## License

MIT
