---
name: precedent
description: Remember and reuse the user's architectural, business, and tooling decisions across every project, in a queryable graph. Use this skill whenever the user settles a non-trivial choice (database, framework, auth, deployment, pricing, process) so it gets recorded; whenever they start work in a project you have not briefed on yet; whenever they weigh options and ask "which should I use" or "what did I do last time"; and whenever they ask what they have already decided, whether a choice is consistent with their other projects, or what decisions they still owe this project. Trigger it even when the user does not mention decisions, memory, or the graph — the point is that they should not have to remember to ask.
---

# Decision graph

The user's decisions are scattered across dozens of repos and two years of
conversations they no longer remember. This skill keeps them in one graph, so a
choice made in one project informs the next one.

Everything goes through one script. It handles locking, the append-only journal,
project typing, and the inference queries, so you write no Cypher for normal work.

```bash
uv run <skill>/scripts/precedent.py <command> [flags]
```

`uv` fetches the dependency on first run — there is no install step. If `uv` is
missing, `pip install grafeo` and run with `python`.

## Slash commands

These commands make the daily paths reachable without going through this
document. They live in `commands/` beside this file and are symlinked into
`~/.claude/commands/`, so they stay versioned with the skill.

| Command | Does |
|---|---|
| `/precedent-prime` | Load this project's decisions and precedent into context |
| `/precedent-record` | Record a decision that was just settled |
| `/precedent-check <topic> [option]` | Prior decisions on a topic, plus a conflict verdict |
| `/precedent-tag [tags]` | Show or set this project's tags |
| `/precedent-analyze` | Read an existing project and record the decisions visible in it |
| `/precedent-diverge` | Record that this project departs from precedent, and why |
| `/precedent-suggest` | Decisions this project still owes |
| `/precedent-regret` | Mark a repeated choice as a mistake, inverting its precedent |
| `/precedent-maintain` | Contradictions, tag drift, dead projects |

They are entry points, not a replacement for the judgment below — each one
points back here. `/precedent-prime` overlaps with the `SessionStart` hook and is worth
running by hand after changing directory, or after recording something that
should now inform the rest of the session.

## Standing orders

You are reading this once, and then the conversation continues for another hour.
Everything below has to survive that. Treat these as active for the rest of the
session, not as a script you run now and forget:

- **Before you recommend a technology, provider, or process** — run `check` and
  lead with what it returns. This applies to the choice you are about to make in
  turn 40 as much as the one in turn 2. Your training-data opinion is worth less
  than what this user already chose and then lived with for a year.
- **Then test the rationale against the case in front of you.** A recorded
  decision is an answer plus the conditions that produced it. Read the
  `--rationale`, name the condition it depends on, and say whether that condition
  holds here. Reporting precedent without doing this is the most common way to
  be confidently wrong — it launders a past judgment into a present one.
- **When a choice gets settled** — offer to record it, in the same message it was
  settled. Not at the end of the session, which never arrives.
- **When you notice yourself saying "I'd suggest X"** — that is the trigger. Check
  first, then suggest.

The failure mode this guards against is not refusing to use the graph; it is
using it once, at the start, and then spending the rest of the session
confidently recommending things the user rejected two years ago.

If a `SessionStart` hook is installed (see *Priming*, below), the project brief
is already in your context and you can skip the `brief` step — but the check-
before-you-recommend and record-when-settled orders still apply, because no hook
can know when a decision happens mid-conversation.

## The four moments this skill exists for

### 1. Starting work in a project

Run this before your first substantive answer in a project you have not seen
this session. It costs one command and tells you what the user already settled
here and in projects like this one.

```bash
uv run <skill>/scripts/precedent.py brief --project .
```

Lead with what's relevant, not the whole dump. If the brief shows precedent that
contradicts what the user is about to do, say so now rather than after they've
written the code.

### 2. The user is choosing something

When they weigh options — a database, a queue, an auth provider, a pricing
model — check before you recommend. Your own opinion is worth less than what
they already decided and lived with.

```bash
uv run <skill>/scripts/precedent.py check --topic persistence --chose postgres
```

Two verdicts are worth interrupting for: `CONFLICT` (they rejected this exact
option before — quote the recorded reason) and `DIVERGENCE` (they chose
differently in two or more comparable projects).

Neither is a verdict on *this* case. Every rationale carries a condition, and
your job is to check whether it still holds:

> Recorded: *"SQLite locks up once webhook handlers run concurrently"* — chose
> Postgres, rejected SQLite, in three bots.
>
> This project: a single-user gym tracker.
>
> The condition is concurrent writers. A single-user bot does not have them, so
> the reason that produced the precedent does not reach this case. The
> precedent is still worth knowing — it says you will want Postgres the moment
> this stops being single-user — but it does not decide today.

That is the whole job. An answer that surfaces the precedent, quotes the
rationale, and then endorses it without asking whether the condition applies has
done the lookup and skipped the thinking. Consistency is usually right and
sometimes exactly wrong; say which you think it is, and why, then leave the
decision with them.

Where a precedent is weak — one project rather than three — say so. Three bots
agreeing is a pattern; one bot is a data point, and presenting them with equal
weight overstates what the graph actually knows.

### 3. A decision just got made

When the conversation settles something that will still matter in six months,
record it. Draft the command, show the user the one-line summary, and write it
once they confirm. Do not narrate the graph mechanics.

```bash
uv run <skill>/scripts/precedent.py record \
  --title "Postgres for the bot's state" \
  --rationale "Already run it for two other services; SQLite loses on concurrent writers" \
  --scope architecture --topic persistence \
  --chose postgres --rejected sqlite,mongo --project .
```

`--rationale` is the field that matters. In two years the *what* is recoverable
from the code and the *why* is not. `--rejected` is what powers the CONFLICT
verdict later, so record the alternatives they actually turned down.

Ask before writing, not after. A wrong entry is worse than a missing one,
because it will be quoted back as precedent.

**Recording is a side effect, never the reply.** People settle a decision and
move on in the same breath — *"ok, SQLite with litestream, now let's talk about
the handler structure"*. The handler structure is the deliverable. Recording is
a line of admin that happens on the way past:

> Recorded: SQLite + Litestream for state, systemd for deploy. Worth knowing
> that SQLite was rejected in the other three bots over concurrent handlers —
> single-instance here, so it likely does not bite.
>
> On handler structure: *[the actual answer]*

One or two lines of bookkeeping, then the answer. If your reply is mostly graph
mechanics, or if it ends by asking a question back instead of answering theirs,
you have inverted the job. Where a real ambiguity blocks recording, record what
is unambiguous, answer the question, and raise the ambiguity at the end.

**What is worth recording:** anything they would have to re-derive later — a
technology choice, an architectural boundary, a business or pricing rule, a
convention they intend to keep, a deliberate rejection. Include the ones they
argued themselves out of; a rejection with a reason is a first-class decision.

**What is not:** anything the code already states plainly, one-off fixes,
choices with no alternative, and preferences that will not outlive the session.

### 3a. When precedent does not simply apply

Three different things get confused with each other, and they want different
records. Pick by what is actually true:

| The situation | What to run | What it changes |
|---|---|---|
| The precedent is sound, but **this project is genuinely different** | `record --despite "<why this project differs>"` | Warning is answered here, and only here |
| The choice was right then and is **wrong now, in this project** | `record --supersedes <id>` | Old decision marked superseded, reasoning kept |
| The choice was **wrong everywhere**, and you repeated it | `regret --topic … --chose … --because "<lesson>"` | Every instance marked regretted; the verdict inverts |

The first is the one people skip, and skipping it is expensive. Diverge without
recording why and the graph reports `DIVERGENCE` in that project forever — a
warning that is correct, unanswerable and permanent, which is exactly how a
useful signal becomes noise the user learns to scroll past.

```bash
uv run <skill>/scripts/precedent.py record --project . \
  --title "SQLite for the CLI" --topic persistence --chose sqlite \
  --rationale "embedded in the binary; there is no server to run one against" \
  --despite "this ships as a single-file CLI, so the operational argument for Postgres does not exist here"
```

Afterwards `check` in that project reports *"DIVERGENCE from 'postgres'
(3 projects) — acknowledged here: …"* and leaves it at that. Everywhere else the
plain warning still fires, because the exception was about this project, not
about the precedent.

### 3b. A choice you repeated turns out to have been wrong

Repetition is what gives precedent its weight, which is exactly the problem when
the repeated thing was a mistake. Without saying so, `check` reports *"you chose
mongo in eight projects"* as a strong norm — and argues for a ninth.

```bash
uv run <skill>/scripts/precedent.py regret --topic persistence --chose mongo \
  --because "schema drift made every migration a manual rewrite; we spent more on backfills than the schema-free start ever saved" \
  --instead postgres
```

Every active decision that made that choice is marked `regretted` in one command,
and the verdict inverts:

```
REGRET: you chose this in 8 project(s) and later concluded it was a mistake —
   "schema drift made every migration a manual rewrite ..."
   you now prefer: postgres
```

Nothing is deleted. The eight decisions keep their original rationales, because
the fact that a reason looked good eight separate times is the most valuable
thing in the record — it is what tells you the lesson was expensive and the trap
convincing. They simply stop counting as a norm, so `brief` and `suggest` no
longer offer that option, and the lesson shows up in every project's brief.

Reach for this when the user says a past choice was a mistake, or when they are
undoing one in the current project and the same reasoning applies elsewhere. Ask
first — the scope is every project at once, and a regret recorded in irritation
is hard to distinguish later from one recorded in judgment.

### 4. Which decisions are still missing

```bash
uv run <skill>/scripts/precedent.py suggest --project .
```

Two inferences, both grounded in what the user actually did:

- **Coverage gaps** — topics settled in two or more projects of this type but
  open here. These are decisions they owe this project and have not made yet,
  which is exactly the kind of thing nobody notices until it bites.
- **Principle candidates** — the same answer reached in three or more projects.
  That is no longer a per-project decision; offer to promote it.

```bash
uv run <skill>/scripts/precedent.py principle --id persistence-postgres \
  --statement "For persistence, use Postgres unless the workload is single-writer."
```

A principle is a claim about the future, so ask before creating one. Repetition
is evidence, not consent.

## Tagging a project — your job, not a lookup table

Precedent is found by **tag overlap**, so tags are what make "what did I do in
my other backend services" work at all. A project carries a set of them:

```bash
uv run <skill>/scripts/precedent.py tag --project .                       # see the vocabulary
uv run <skill>/scripts/precedent.py tag --project . --add backend,java,distributed
```

A single type would force a false choice. A service is not `backend-api` OR
`java` OR `distributed` — it is all three, and which of them a past decision
rhymes with depends on the decision. Persistence choices travel along `backend`;
build and dependency choices travel along `java`; consistency and retry choices
travel along `distributed`. Overlap also gives you a gradient instead of a
binary: `brief` lists the closest projects with the count of shared tags, so a
sibling sharing four tags outranks one sharing a single generic tag, and you can
see which tags did the matching.

Nothing is inferred from the project's contents. An earlier version tried, and
keyword matching cannot be made correct: a Rust CLI named `nextgen` was typed
`web-frontend` because "next" appeared in its description, and `react-native`
matched `react` before it could match itself.

There is no list of known build files either — `brief` and `tag` just show the
directory's top-level contents, so `build.zig`, `shard.yml`, `BUILD.bazel` or
whatever appears next year is as visible as `package.json`. You are already
reading the project; classify it from what is actually there.

### Reuse tags; the graph will stop you if you don't

Matching is exact string equality, so `tgbot` beside an existing `telegram-bot`
hides half the history from the other half — silently, at the moment it happens.
Different agents in different sessions reach for different words for the same
thing, so this is the normal case, not an edge case. `tag --add` therefore
**refuses** a tag resembling one already in use, exits non-zero, writes nothing,
and prints the vocabulary so you can choose:

```
refusing to create the tag 'tgbot': it looks like 'telegram-bot', already in use.
  tags in use: backend, java, telegram-bot, distributed
  reuse one:   precedent.py tag --project . --add telegram-bot
  or if it really means something different:
               precedent.py tag --project . --add tgbot --new-tag
```

It catches both shapes drift takes: spelling variants (`data_pipeline` /
`data-pipeline`) and abbreviations (`tgbot` / `telegram-bot`), which share no
word at all. `--new-tag` records the pair as deliberately distinct so the
maintenance report stops proposing a merge that was already considered.

Prefer several small orthogonal tags to one compound name. `backend` + `java` +
`distributed` is better than `distributed-java-backend`: each part is reused by
other projects, which is what makes overlap meaningful, and short common words
are ones every agent spells the same way.

### Monorepos: tag the module, not the repo

A repository holding a Java backend and a Python Slack bot is two projects, and
they should not lend each other precedent. They already are two projects: the
key is the absolute path, so `/repo/backend` and `/repo/slackbot` are distinct
entries with their own tags. Containment falls out of the path — no extra
schema, nothing to keep in sync.

```bash
uv run <skill>/scripts/precedent.py tag --project ./backend  --add backend,java
uv run <skill>/scripts/precedent.py tag --project ./slackbot --add bot,slack,python
uv run <skill>/scripts/precedent.py tag --project .          --add monorepo,internal
```

Three consequences worth knowing:

- **Record against the narrowest directory the decision is about.** A choice
  about the bot's storage belongs to `./slackbot`, not the repo root. `brief` at
  the root lists the modules and reminds you.
- **Repo-level decisions reach every module.** CI, branching, release process and
  licensing are recorded once at the root and show up in each module's brief
  under *inherited from enclosing projects*. This is the reason to tag the root
  at all.
- **Modules inherit the root's tags, marked with `*`,** and those tags help a
  module find kin in other repos of the same shape. They do not make sibling
  modules kin with each other: a Java backend and a Python bot share only
  `monorepo` and `internal`, which say nothing about the work, so neither
  appears in the other's precedent. Two genuinely similar modules still match,
  because they share real tags.

Enclosing and contained projects are shown as structure rather than precedent —
they are the same codebase, and counting them as "closest projects" would crowd
out comparable work elsewhere.

### Repairing drift that already happened

`maintain` reports suspected pairs, and one command repairs them:

```bash
uv run <skill>/scripts/precedent.py tag --merge tg-bot --into telegram-bot
```

Every project carrying the old tag is retagged, the merge is journalled, and
`rebuild` replays it.

An untagged project still records decisions perfectly well; it just never
surfaces as precedent anywhere, and `brief` says so and lists the tags on offer.
Tag it when you know what it is — guessing early is worse than waiting.

## Keeping it healthy

```bash
uv run <skill>/scripts/precedent.py maintain          # report only
uv run <skill>/scripts/precedent.py maintain --apply  # also delete orphan nodes
```

Reports contradictions (one project, one topic, two live answers), projects
whose path no longer exists, and orphan nodes. Contradictions are for the user
to resolve — record a new decision with `--supersedes <old-id>` rather than
editing history. Superseded decisions stay in the graph; they are the record of
what the user used to think, which is the part that explains the current design.

Run this when the user asks for a cleanup, or when a `check` result looks
self-contradictory.

## Priming

Prose can be forgotten by turn 40; a hook cannot. `hooks/session-start.sh` is a
`SessionStart` hook that runs `brief` for the working directory and injects the
result, so every session opens already knowing what the user decided here and in
comparable projects. It stays silent when the graph has nothing to say about the
directory, so it costs nothing in projects with no history.

Wire it in `~/.claude/settings.json`:

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

The hook covers recall. It cannot cover capture — nothing outside the
conversation can tell when a decision was made — so the standing orders above
remain the only mechanism for recording.

## Where it lives, and why it is safe

```
~/.local/share/precedent/
├── journal.jsonl   append-only, fsync'd, one line per decision — source of truth
├── graph.db        Grafeo graph — a queryable index
└── .lock           exclusive lock, held for the length of one command
```

To keep it somewhere else — a synced folder, an encrypted volume, a private git
checkout — point `init` at that directory. It moves an existing store there and
symlinks the default path at it, so the hook and every slash command keep
working unchanged:

```bash
uv run <skill>/scripts/precedent.py init ~/Sync/precedent
uv run <skill>/scripts/precedent.py init          # where does it live right now?
```

Prefer this over `PRECEDENT_HOME`: the env var is not set in the SessionStart
hook's environment, so exporting it hides the store from priming. It refuses to
merge two populated stores — concatenating the journals and running `rebuild` is
a deliberate act, not something a path flag should do behind your back.

The journal exists because the graph engine is young (v0.5.x). If the graph is
ever corrupted or the engine is abandoned, `precedent.py rebuild` replays the journal
into a fresh graph and nothing is lost. The journal is plain text, so it also
diffs and belongs in a private git repo if the user wants history.

The lock is not decoration. Two processes on one embedded graph silently lose
writes — measured at 120 writes across 6 processes leaving 60 stored, with every
writer reporting success. Every command takes the lock, so concurrent Claude
sessions queue instead of clobbering. Never bypass the script to open the graph
directly.

```bash
uv run <skill>/scripts/precedent.py rebuild    # replay journal into a fresh graph
uv run <skill>/scripts/precedent.py selftest   # verify the install still works
```

## Escape hatch

For a question the subcommands do not answer, query the graph directly. The
schema is in `references/schema.md` — read it before writing Cypher.

```bash
uv run <skill>/scripts/precedent.py cypher "MATCH (d:Decision)-[:CHOSE]->(o:Option {name:'postgres'}) RETURN d.title AS t"
```

Prefer a subcommand where one fits. Ad-hoc Cypher is easy to get subtly wrong,
and a wrong answer here is quoted back to the user as their own history.
