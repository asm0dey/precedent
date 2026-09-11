# precedent

`precedent` gives your coding agent a memory of the architectural, business and tooling
choices you have settled — across every project, for years — and puts them back in front of
you when a similar choice comes up again.

You decided something two years ago, in a repo you have not opened since, for a reason that
made sense at the time. The code still shows *what* you chose. The *why* is gone, and so is
the list of things you rejected on the way. `precedent` keeps all three and hands them to the
agent while the choice is still on the table.

Works with Claude Code, Codex and Cursor.

## What it looks like

A session opens. Before you type anything, the agent is handed this:

```
project: gym-bot   tags: bot, telegram, python

== decided here (2) ==
  [architecture] webhook, not polling  (ingest)
  [tooling] uv, no durable venv  (packaging)

== precedent from comparable projects ==
  persistence: postgres   [3x: bot-alpha,bot-beta,bot-gamma]
  logging: structlog      [3x: bot-alpha,bot-beta,bot-gamma]
```

Then you work normally. You never type a command:

```
you    Let's just use SQLite for bot state, keeps deploy simple.

agent  Worth knowing first: you rejected SQLite for bot state in bot-alpha —
       "two bots already share one Postgres; SQLite locks up once webhook
       handlers run concurrently." You chose Postgres for this in 3 other
       projects.

       That reason is about concurrent writers. This bot has one handler and
       one writer, so it does not apply here. SQLite is fine.

       Want me to record it as a deliberate departure, so the warning stops
       firing in this project but still fires elsewhere?

you    yes

agent  Recorded: persistence -> sqlite, despite the Postgres precedent.
```

That is the whole loop. The agent reads the graph when you weigh something, writes to it when
you settle something, and asks before writing — a wrong entry is worse than a missing one,
because it gets quoted back as precedent.

A warning is not a veto. Every rationale carries a condition, and the agent's job is to ask
whether that condition still holds. Consistency is usually right, and sometimes exactly wrong.

## What the agent does with it

| You say | It does |
|---|---|
| "let's go with X" | records the decision, with your reason and what you rejected |
| "what did I use last time" / "which should I use here" | prior decisions on that topic, plus a verdict on the option you named |
| "X was a mistake" / "X bit us" / "never again" | inverts the precedent — every project that chose X stops counting toward the pattern |
| "I usually do X, but not here" | records the departure so the warning is answered, not repeated forever |
| starting work somewhere it has not briefed you on | reads the project and records the decisions already visible in its manifests, CI and docs |

Ten skills carry this, one per path (`record`, `check`, `regret`, `diverge`, `prime`, `tag`,
`analyze`, `suggest`, `maintain`, plus the judgment skill that decides which applies). Each
fires from its own description, so nothing has to be typed. If you *want* to type, they are
slash commands in Claude Code and Cursor, `$`-prefixed in Codex.

## Is this a memory layer?

It is memory, in the sense that something survives the end of the session. What goes in is a
lot narrower than what most tools in that space keep.

The unit here is a **settled decision**: a topic, the option you chose, the options you
rejected, and the reason. That is the entire record. Transcripts, session summaries and
inferred preferences about your coding style all stay out of it.

Three things follow from that shape.

**Rejected options get a home.** `sqlite` sits in the graph as the thing three projects turned
down, with the reason attached. A store that keeps what is true has nowhere to put what you
decided against, which is precisely what you need at the moment you are about to choose it
again.

**Retrieval runs on project kinship.** Precedent is ranked by how many tags two projects share,
and the output names the tags that did the matching. Your Telegram bot inherits from your other
Telegram bots. Your Next.js app stays out of it, however close the sentences describing them
embed.

**A norm can be inverted.** Marking a repeated choice as a regret makes every project that made
it stop counting toward the pattern. Without that, "you chose mongo in eight projects" argues
for a ninth, and repetition turns a scar into a recommendation.

### How it compares

| | Stores | Retrieval | Scope | Answers with |
|---|---|---|---|---|
| **precedent** | settled decisions with rejected options | tag overlap between projects | every project you work in | a verdict (`CONFLICT` / `DIVERGENCE` / `REGRET`) |
| [Mem0](https://mem0.ai), [Zep](https://www.getzep.com) / [Graphiti](https://github.com/getzep/graphiti), [Letta](https://www.letta.com) | facts and preferences extracted from conversation | embedding similarity, plus a graph of entities; Zep adds validity windows over time | a user or an assistant | relevant memories |
| [OpenMemory MCP](https://mem0.ai/openmemory), [basic-memory](https://github.com/basicmachines-co/basic-memory) | notes and captured preferences, as markdown or a local store | search over the note index | one user across MCP clients | matching notes |
| `CLAUDE.md`, `AGENTS.md`, editor rules | instructions you wrote by hand | loaded whole, every session | the repo the file sits in | the file's contents |
| [adr-tools](https://github.com/npryce/adr-tools), [Log4brains](https://github.com/thomvaill/log4brains) | ADRs as markdown, with supersession links | grep, or a generated site | the repo, or a site built from several | the document you searched for |

The row that comes closest is the ADR one, and the gap is when the answer arrives. An ADR is a
document you go and read, filed in the repo where the decision was made. Precedent is queried by
the agent while a choice is on the table, in whatever repo you happen to be in, and it answers
with a verdict about the option you named. The two coexist happily. This repo keeps ADRs under
`docs/adr/` and records the same decisions in the graph.

The other rows are answering a different question. They are built to remember *you* across
conversations. Precedent remembers what you *settled*, which is a much smaller set, and worth
carrying between projects for years.

### What it does not do

- No semantic search. Ask it about "the database thing" and you get nothing; ask about the
  `persistence` topic and you get everything.
- No conversation history. It never sees your chats, only the records the agent writes.
- Tags come from the agent, never from a keyword rule (see How it finds comparable work).
- Single user, local files. Syncing across machines has a sharp edge, covered in
  [docs/setup.md](docs/setup.md#storage).

## When precedent does not simply apply

Three situations get confused with each other, and each wants a different record. You say the
ordinary sentence; the agent picks the right one.

| The situation | What gets recorded |
|---|---|
| The precedent is sound, but **this project is genuinely different** | a decision *despite* the precedent |
| The choice was right then and is **wrong now, here** | a decision that *supersedes* the old one |
| The choice was **wrong everywhere**, and you repeated it | a *regret*, which inverts the norm |

The first matters more than it looks. Diverge without recording why, and the graph reports
`DIVERGENCE` in that project forever: a warning that is correct, unanswerable and permanent,
which is how a useful signal becomes noise. Record the exception and it reads as *acknowledged
here*, while the plain warning still fires everywhere else.

The third exists because repetition is what gives precedent its weight, which is a problem when
the repeated thing was a mistake. Without it, *"you chose mongo in eight projects"* reads as a
strong norm. Afterwards the agent is told:

```
REGRET: you chose this in 8 project(s) and later concluded it was a mistake —
   "schema drift made every migration a manual rewrite ..."
   you now prefer: postgres
```

Nothing is deleted. The eight keep their original rationales, because a reason that looked good
eight separate times is what tells you the lesson was expensive and the trap convincing. They
simply stop counting as a norm.

## How it finds comparable work

A project carries a **set of tags**, never one type. A service is not `backend` *or* `java` *or*
`distributed`. It is all three, and which of them a past decision rhymes with depends on the
decision. Persistence choices travel along `backend`, build choices along `java`, retry
semantics along `distributed`.

Precedent is ranked by **tag overlap**, so a sibling sharing four tags outranks one sharing a
single generic tag, and you can see which tags did the matching.

Tags are supplied by the agent, never inferred. Keyword matching was tried and removed: a Rust
CLI named `nextgen` was classified `web-frontend` because the word "next" appeared in its
description, and `react-native` matched `react` before it could match itself. A wrong
classification is worse than none, because the project then silently inherits decisions from
unrelated work.

There is no list of known build files either. The tool shows the directory's top-level contents
and lets the agent read them, so `build.zig`, `shard.yml`, `BUILD.bazel` or whatever appears
next year is as visible as `package.json`.

**Monorepos** need no special handling. Containment is a string-prefix fact over the path key,
so tagging `/repo/backend` and `/repo/slackbot` separately keeps them from lending each other
precedent, while decisions recorded at `/repo` (CI, branching, licensing) are inherited by both.
A project's durable identity is its git remote, so a clone at a different path still resolves to
the same node; the local path stays as an alias. See `docs/adr/0002`.

## Measured

Four realistic prompts, run by subagents with the skill and without it, graded against 29
assertions by an independent grader.

| | With skill | Baseline | Delta |
|---|---|---|---|
| Pass rate | **26/29** (90%) | 35% ± 15% | +0.55 |
| Time | 117s | 82s | +36s |
| Tokens | 84.5k | 77.6k | +6.9k |

The with-skill number is a single graded run against the current tree (per eval: 6/7, 6/7, 8/8,
6/7). An earlier run of the same suite, before a rewrite of the skill text and a large change to
the CLI, scored 93% ± 8% over repeated samples. The baseline column is from that earlier round
and has not been re-run, because nothing about the skill-less condition changed. Time and token
figures are also carried over from it.

The sharpest result is still a baseline failure. Asked what to be consistent with when starting a
new Telegram bot, the skill-less run read the manifests of two unrelated FastAPI services and
recommended async SQLAlchemy: precedent transferred across the wrong kind of project, stated
confidently. Another baseline searched honestly, found nothing, then asserted *"No decision
record covers it"*, which was false.

Honest limits. All three failures in the graded run are word-count assertions, over by 103, 12
and 24 words. Answers carrying real reasoning run longer than the limits allow, and the limits
have not been relaxed to make the number look better. One assertion turns on a judgment call the
grader made explicit: the run that recorded two decisions also wrote a classification tag for the
untagged project, and the grader counted that tag as metadata instead of a third journal entry.
Under a literal line count the score is 25/29. `evals/` holds the prompts, assertions and a
fixture seeder if you want to re-run or extend them.

## Install

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12+, and Claude Code, Codex or Cursor.
Linux, macOS or Windows. The CLI itself has no install step: it is a PEP 723 single file, and
`uv` fetches what it needs on first run, then reuses a cached environment. (A persistent
virtualenv was measured and rejected. It saves 11 ms per call, both paths being dominated by the
engine import, and costs an install step plus an environment to keep in sync.) What differs by
agent is how the skills and the hook get in front of it.

### ACR (Codex, Cursor, and Claude Code)

[ACR](https://github.com/jbaruch/agentic-context-registry) is the only channel that reaches Codex
and Cursor. It is driven by `agent-plugin.yaml`, which ships the ten skills, the CLI script and
the session-start hook:

```bash
acr install github:asm0dey/precedent --agent claude-code   # or codex, or cursor
acr realize
```

`install` resolves the latest GitHub release, so a release has to exist. Each of the ten skills
is its own artifact, and `realize` writes them into the agent's own skills directory:
`.claude/skills/`, `.codex/skills/` or `.cursor/skills/` as appropriate.

All three agents read those paths, so every skill works on every agent. The realized directories
carry an ACR prefix (`acr__asm0dey__precedent__precedent-check`), which matters only if you type
the name: skills fire from their descriptions, and that is how they are meant to fire. Codex
registers them under their frontmatter name regardless, so `$precedent-check` works there.

### Claude Code plugin

`adapters/claude/` is the plugin root. `skills/*/SKILL.md` and `hooks/hooks.json` are discovered
by Claude Code's own convention, so nothing is enumerated by hand. The manifest that drives this
channel is `adapters/claude/.claude-plugin/plugin.json`, generated from `agent-plugin.yaml` by
`scripts/gen-plugin-json.py` (`--check` fails CI if it drifts).

No marketplace catalog for this plugin is published anywhere yet. This repo ships no
`marketplace.json`, so `/plugin install precedent` is not something a reader can run today. A
marketplace entry that lists this plugin would point at the subdirectory,
`{"name": "precedent", "source": "./adapters/claude"}` for a marketplace hosted in this repo, or
a `git-subdir` source with `path: "adapters/claude"` from elsewhere. A `git-subdir` source sparse-
checks out only that named subdirectory, so it would ship the adapter without
`scripts/precedent.py`, the CLI the adapter drives. A plugin install of this project needs the
full repository checkout. That file, and which install shape avoids the gap above, is a decision
for whoever hosts the catalog. `claude plugin validate adapters/claude` is Anthropic's own
checker and currently passes, with one warning (the optional `author` field is absent).

### Manual symlink

What every channel above ends up doing to `~/.claude/`, done by hand. Also the option if you
would rather not add a marketplace or install ACR:

```bash
git clone https://github.com/asm0dey/precedent ~/src/precedent
ln -s ~/src/precedent/adapters/claude/skills/* ~/.claude/skills/
```

### Then wire the session-start hook

The hook runs a brief for the working directory and injects it, so a session opens already
knowing what you decided here and in comparable projects. It stays quiet where the graph has
nothing to say, so empty directories cost nothing. ACR wires it for you; the plugin ships it in
`hooks.json`; the symlink route needs a few lines in `~/.claude/settings.json`.

That wiring, the `[PRECEDENT]` statusline badge, the Windows PowerShell variants, where the store
lives and how to move or sync it, and the CLI underneath all of it:
[docs/setup.md](docs/setup.md).

## License

MIT
