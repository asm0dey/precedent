# precedent

A CLI over a decision graph. It remembers the architectural, business and tooling
choices you make across every project, and puts them back in front of you the next
time a similar choice comes up. Claude Code, Codex and Cursor can all reach it; see
Install for how each one gets there.

You decided something two years ago, in a repo you have not opened since, for a reason
that made sense at the time. The code still shows *what* you chose. The *why* is gone,
and so is the list of things you rejected on the way. `precedent` keeps all three, in a
graph, and hands them back at the moment they matter.

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

Neither verdict is a veto. Every rationale carries a condition, and the agent's job is to
ask whether that condition still holds. The rationale above is about concurrent writers,
so it says nothing about a single-user tool. Consistency is usually right, and sometimes
exactly wrong.

## Is this a memory layer?

It is memory, in the sense that something survives the end of the session. What goes in
is a lot narrower than what most tools in that space keep.

The unit here is a **settled decision**: a topic, the option you chose, the options you
rejected, and the reason. That is the entire record. Transcripts, session summaries and
inferred preferences about your coding style all stay out of it.

Three things follow from that shape.

**Rejected options get a home.** `sqlite` sits in the graph as the thing three projects
turned down, with the reason attached. A store that keeps what is true has nowhere to put
what you decided against, which is precisely what you need at the moment you are about to
choose it again.

**Retrieval runs on project kinship.** Precedent is ranked by how many tags two projects
share, and the output names the tags that did the matching. Your Telegram bot inherits
from your other Telegram bots. Your Next.js app stays out of it, however close the
sentences describing them embed.

**A norm can be inverted.** `regret` marks a repeated choice as a mistake, and every
project that made it stops counting toward the pattern. Without that, "you chose mongo in
eight projects" argues for a ninth, and repetition turns a scar into a recommendation.

### How it compares

| | Stores | Retrieval | Scope | Answers with |
|---|---|---|---|---|
| **precedent** | settled decisions with rejected options | tag overlap between projects | every project you work in | a verdict (`CONFLICT` / `DIVERGENCE` / `REGRET`) |
| [Mem0](https://mem0.ai), [Zep](https://www.getzep.com) / [Graphiti](https://github.com/getzep/graphiti), [Letta](https://www.letta.com) | facts and preferences extracted from conversation | embedding similarity, plus a graph of entities; Zep adds validity windows over time | a user or an assistant | relevant memories |
| [OpenMemory MCP](https://mem0.ai/openmemory), [basic-memory](https://github.com/basicmachines-co/basic-memory) | notes and captured preferences, as markdown or a local store | search over the note index | one user across MCP clients | matching notes |
| `CLAUDE.md`, `AGENTS.md`, editor rules | instructions you wrote by hand | loaded whole, every session | the repo the file sits in | the file's contents |
| [adr-tools](https://github.com/npryce/adr-tools), [Log4brains](https://github.com/thomvaill/log4brains) | ADRs as markdown, with supersession links | grep, or a generated site | the repo, or a site built from several | the document you searched for |

The row that comes closest is the ADR one, and the gap is when the answer arrives. An ADR
is a document you go and read, filed in the repo where the decision was made. Precedent is
queried by the agent while a choice is on the table, in whatever repo you happen to be in,
and it answers with a verdict about the option you named. The two coexist happily. This
repo keeps ADRs under `docs/adr/` and records the same decisions in the graph.

The other rows are answering a different question. They are built to remember *you* across
conversations. Precedent remembers what you *settled*, which is a much smaller set, and
worth carrying between projects for years.

### What it does not do

- No semantic search. Ask it about "the database thing" and you get nothing; ask it about
  the `persistence` topic and you get everything.
- No conversation history. It never sees your chats, only the records the agent writes.
- Tags come from the agent, never from a keyword rule (see How it finds comparable work).
- Single user, local files. Syncing across machines has a sharp edge, covered under
  Storage.

## Install

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12+. The CLI itself has no install
step: it is a PEP 723 single file, and `uv` fetches what it needs on first run, then reuses
a cached environment. (A persistent virtualenv was measured and rejected. It saves 11 ms
per call, both paths being dominated by the engine import, and costs an install
step plus an environment to keep in sync.) What differs by agent is how the skills and the
hook get in front of it.

### Claude Code plugin

`adapters/claude/` is the plugin root. `skills/*/SKILL.md` and `hooks/hooks.json` are
discovered by Claude Code's own convention, so nothing is enumerated by hand. The
manifest that drives this channel is
`adapters/claude/.claude-plugin/plugin.json`, generated from `agent-plugin.yaml` by
`scripts/gen-plugin-json.py` (`--check` fails CI if it drifts).

No marketplace catalog for this plugin is published anywhere yet. This repo ships no
`marketplace.json`, so `/plugin install precedent` is not something a reader can run today.
A marketplace entry that lists this plugin would point at the subdirectory,
`{"name": "precedent", "source": "./adapters/claude"}` for a marketplace hosted in this
repo, or a `git-subdir` source with `path: "adapters/claude"` from elsewhere. A `git-subdir`
source sparse-checks out only that named subdirectory, so it would ship the adapter without
`scripts/precedent.py`, the CLI the adapter drives. A plugin install of this project needs
the full repository checkout. That file, and which install shape avoids the gap above, is a
decision for whoever hosts the catalog. `claude plugin validate adapters/claude` is
Anthropic's own checker and currently passes, with one warning (the optional `author` field
is absent).

### ACR (Codex and Cursor)

[ACR](https://github.com/jbaruch/agentic-context-registry) is the only channel that reaches
Codex and Cursor. It is driven by `agent-plugin.yaml`, which ships the ten skills, the CLI
script and the session-start hook:

```bash
acr install github:asm0dey/precedent --agent claude-code   # or codex, or cursor
acr realize
```

`install` resolves the latest GitHub release, so a release has to exist. Each of the ten
skills is its own artifact, and `realize` writes them into the agent's own skills directory:
`.claude/skills/`, `.codex/skills/` or `.cursor/skills/` as appropriate.

All three agents read those paths, so every skill works on every agent. The realized
directories carry an ACR prefix (`acr__asm0dey__precedent__precedent-check`), which matters
only if you type the name: skills fire from their descriptions, and that is how they are
meant to fire. Codex registers them under their frontmatter name regardless, so `$precedent-check`
works there.

### Manual symlink

What every channel above ends up doing to `~/.claude/`, done by hand. Also the option if you
would rather not add a marketplace or install ACR:

```bash
git clone https://github.com/asm0dey/precedent ~/src/precedent
ln -s ~/src/precedent/adapters/claude/skills/* ~/.claude/skills/
```

The hook lives beside the skills rather than inside one, so it is wired by path rather
than through a symlink. Optionally, prime every session automatically by adding this to
`~/.claude/settings.json`:

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

`adapters/claude/hooks/hooks.json`, the copy of this block the plugin channel ships, carries
the same single bash entry, for the same reason. Claude Code's hook schema has no
per-platform field, so `shell` only picks which interpreter runs a given entry's `command`;
it does not gate by OS. A second entry pointing at `session-start.ps1` would therefore be
dispatched on every OS. On macOS or Linux with `pwsh` installed it would run and rely on the
script's own `$IsWindows` guard to exit quietly. On macOS or Linux *without* `pwsh`, there is
no interpreter to run it at all, and the entry throws: a user-visible hook error every
session, which defeats the point of a silent-by-design hook. Shipping only the bash entry is
silent by construction on POSIX, and on Windows with Git Bash it still works. A Windows user
without Git Bash gets no priming hook, and no error either.

Windows users without Git Bash can opt in to the PowerShell hook by pasting a second entry
into their own `~/.claude/settings.json`, pointing at `session-start.ps1`
(`adapters/claude/hooks/session-start.ps1` in the repo, or
`$HOME/src/precedent/adapters/claude/hooks/session-start.ps1` for the clone above):

```json
{ "type": "command",
  "command": "& \"$HOME/src/precedent/adapters/claude/hooks/session-start.ps1\"",
  "shell": "powershell",
  "timeout": 20 }
```

Add it as a second element of the `"hooks"` array above. The script still guards itself with
`if ($IsWindows -eq $false) { exit 0 }`, so it only does anything on real Windows, including
the PowerShell 5.1 fallback Claude Code uses when `pwsh` 7 is missing.

The hook runs a brief for the working directory and injects it, so a session opens already
knowing what you decided here and in comparable projects. It stays quiet where the graph has
nothing to say, so empty directories cost nothing.

### The statusline badge

A session primed by the hook is otherwise indistinguishable from one that was not, and the
difference matters: it decides whether the standing orders are in force. `statusline.sh`
renders `[PRECEDENT]` while the graph is speaking in the current directory.

Claude Code has no plugin-provided statusline — `statusLine` is a field in
`settings.json`, one command for the whole line — so this is chained into whatever
statusline you already run rather than installed:

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
`.claude/scripts/acr__asm0dey__precedent__statusline/statusline.sh`. Windows without Git
Bash has the twin, `statusline.ps1`, wired the same way.

The badge is not a decoration on the hook — it is the hook's own state. The SessionStart
hook drops an empty marker named after the directory it primed, and clears it whenever it
has nothing to say, so the badge appears exactly when a brief was injected. The statusline
reads only the marker's name, never its contents, and forks nothing: it runs on every
render.

## The skills

`precedent` carries the judgment: what is worth recording, how to read a verdict, when
consistency is wrong. Nine task skills cover the daily paths.

| Skill | Does |
|---|---|
| `precedent-prime` | Load this project's decisions and precedent into context |
| `precedent-record` | Record a decision that was just settled |
| `precedent-check` | Prior decisions on a topic, plus a conflict verdict |
| `precedent-tag` | Show or set this project's tags |
| `precedent-analyze` | Read an existing project and record the decisions visible in it |
| `precedent-suggest` | Decisions this project still owes |
| `precedent-diverge` | Record that this project departs from precedent, and why |
| `precedent-regret` | Mark a repeated choice as a mistake, inverting its precedent |
| `precedent-maintain` | Contradictions, tag drift, dead projects |

Each fires on its own description, so nothing has to be typed. Claude Code also takes them
as slash commands, Codex with a `$` prefix, Cursor from the slash menu.

The main skill triggers on its own too — when you settle a choice, when you weigh options,
when you start work somewhere it has not briefed you on.

## When precedent does not simply apply

Three situations get confused with each other, and each wants a different record:

| The situation | What to run |
|---|---|
| The precedent is sound, but **this project is genuinely different** | `record --despite "<why>"` |
| The choice was right then and is **wrong now, here** | `record --supersedes <id>` |
| The choice was **wrong everywhere**, and you repeated it | `regret --topic ... --chose ... --because "<lesson>"` |

The first matters more than it looks. Diverge without recording why, and the graph reports
`DIVERGENCE` in that project forever: a warning that is correct, unanswerable and permanent,
which is how a useful signal becomes noise. Record the exception and `check` reports it as
*acknowledged here*, while the plain warning still fires everywhere else.

The third exists because repetition is what gives precedent its weight, which is a problem
when the repeated thing was a mistake. Without `regret`, *"you chose mongo in eight
projects"* reads as a strong norm. Afterwards:

```
REGRET: you chose this in 8 project(s) and later concluded it was a mistake —
   "schema drift made every migration a manual rewrite ..."
   you now prefer: postgres
```

Nothing is deleted. The eight keep their original rationales, because a reason that looked
good eight separate times is what tells you the lesson was expensive and the trap
convincing. They simply stop counting as a norm.

## How it finds comparable work

A project carries a **set of tags**, never one type. A service is not `backend` *or* `java`
*or* `distributed`. It is all three, and which of them a past decision rhymes with depends
on the decision. Persistence choices travel along `backend`, build choices along `java`,
retry semantics along `distributed`.

Precedent is ranked by **tag overlap**, so a sibling sharing four tags outranks one sharing
a single generic tag, and you can see which tags did the matching.

Tags are supplied by the agent, never inferred. Keyword matching was tried and removed: a
Rust CLI named `nextgen` was classified `web-frontend` because the word "next" appeared in
its description, and `react-native` matched `react` before it could match itself. A wrong
classification is worse than none, because the project then silently inherits decisions from
unrelated work.

There is no list of known build files either. The tool shows the directory's top-level
contents and lets the agent read them, so `build.zig`, `shard.yml`, `BUILD.bazel` or
whatever appears next year is as visible as `package.json`.

**Monorepos** need no special handling. Containment is a string-prefix fact over the path
key, so tagging `/repo/backend` and `/repo/slackbot` separately keeps them from lending each
other precedent, while decisions recorded at `/repo` (CI, branching, licensing) are inherited
by both. A project's durable identity is its git remote, so a clone at a different path still
resolves to the same node; the local path stays as an alias. See `docs/adr/0002`.

## Storage

```
~/.local/share/precedent/
├── journal.jsonl   append-only, fsync'd, one line per decision — source of truth
└── graph.db        graphdblite graph — a queryable index
```

Keep it elsewhere (a synced folder, an encrypted volume, a private git checkout) with
`init`. It moves an existing store to the new directory and leaves a pointer file at the
default path naming it, so the hook and every skill keep working with no configuration:

```bash
uv run ~/src/precedent/scripts/precedent.py init ~/Sync/precedent
uv run ~/src/precedent/scripts/precedent.py init   # where is it now?
```

The pointer beats `PRECEDENT_HOME`, which the SessionStart hook never sees. Two populated
stores are never merged silently: `init` stops and tells you to concatenate the journals and
`rebuild`.

The journal exists because the graph engine is young, and because it makes the engine
replaceable. If the graph is ever corrupted, `precedent.py rebuild` replays the journal into
a fresh one and nothing is lost. That escape hatch has been used for real: the store was a
Grafeo graph until the journal carried it across to graphdblite, which cost a rebuild rather
than a migration. The journal is plain text, so it also diffs, and belongs in a private git
repo if you want history.

Concurrent sessions are not coordinated by this tool. Several commands run at once by
design — a SessionStart hook running `brief` while the model runs `check` and you run
`record`, times however many sessions are open — and there is no file lock. graphdblite
serialises writers through SQLite and waits 5 seconds before failing loudly. Measured, that
bound is never approached: 8 processes writing back-to-back never waited past 850 ms.

That is a guarantee this project does not take on trust, because the previous engine did not
hold it: unlocked, 120 writes across 6 processes stored 60 on macOS and 40 on Linux, every
writer exiting 0 ([GrafeoDB/grafeo#405](https://github.com/GrafeoDB/grafeo/issues/405), filed
from this work). So `selftest` measures both properties — that concurrent writers all land,
and that a reader never sees a half-applied write — on Linux, macOS and Windows in CI, on
every push.

### A synced store is not a shared store

SQLite serialises writers through the filesystem, and a synced folder is not one. Dropbox,
iCloud, and network mounts do not carry the locking SQLite relies on; they copy files after
the fact. Two machines writing to one synced store are not serialised, and the loser is
whichever copy the sync service resolves away — silently.

Sync the journal, and leave the graph alone. `journal.jsonl` is append-only and merges in
git, and `precedent.py rebuild` reconstructs the graph from it on each machine. That is the
second reason a project's identity is its git remote instead of its path.

## Measured

Four realistic prompts, run by subagents with the skill and without it, graded against 29
assertions by an independent grader.

| | With skill | Baseline | Delta |
|---|---|---|---|
| Pass rate | **26/29** (90%) | 35% ± 15% | +0.55 |
| Time | 117s | 82s | +36s |
| Tokens | 84.5k | 77.6k | +6.9k |

The with-skill number is a single graded run against the current tree (per eval: 6/7, 6/7,
8/8, 6/7). An earlier run of the same suite, before a rewrite of the skill text and a large
change to the CLI, scored 93% ± 8% over repeated samples. The baseline column is from that
earlier round and has not been re-run, because nothing about the skill-less condition
changed. Time and token figures are also carried over from it.

The sharpest result is still a baseline failure. Asked what to be consistent with when
starting a new Telegram bot, the skill-less run read the manifests of two unrelated FastAPI
services and recommended async SQLAlchemy: precedent transferred across the wrong kind of
project, stated confidently. Another baseline searched honestly, found nothing, then asserted
*"No decision record covers it"*, which was false.

Honest limits. All three failures in the graded run are word-count assertions, over by 103,
12 and 24 words. Answers carrying real reasoning run longer than the limits allow, and the
limits have not been relaxed to make the number look better. One assertion turns on a
judgment call the grader made explicit: the run that recorded two decisions also wrote a
classification tag for the untagged project, and the grader counted that tag as metadata
instead of a third journal entry. Under a literal line count the score is 25/29. `evals/`
holds the prompts, assertions and a fixture seeder if you want to re-run or extend them.

## Requirements

- `uv` and Python 3.12+
- Claude Code, Codex or Cursor. The graph, the ten skills and the CLI reach all three
- Linux, macOS or Windows

## License

MIT
