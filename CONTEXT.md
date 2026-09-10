# precedent

Durable, cross-project memory of the decisions a developer has made, and the
reasoning they had at the time. The store is agent-neutral; each coding agent
reaches it through an adapter.

## Language

### Decisions

**Decision**:
A choice that was settled, with the reasoning that settled it and the options
turned down on the way. The unit of memory.
_Avoid_: record, entry, note

**Rationale**:
Why a decision was made, in the words of the person who made it. Never inferred
by an agent without saying so — an invented rationale is replayed years later as
the user's own reasoning and believed.
_Avoid_: reason, justification, why

**Scope**:
Which part of life a decision belongs to: architecture, business, process,
tooling, product. Orthogonal to Topic.
_Avoid_: category, kind, area

**Supersession**:
Replacing a decision with a later one on the same question. The old decision is
kept with its rationale intact and stops counting as current.
_Avoid_: update, overwrite, revision

**Regret**:
A judgment that a choice was wrong *everywhere it was made*. It inverts
precedent: the repetitions become evidence that the lesson was expensive rather
than evidence of a norm.
_Avoid_: mistake, rollback, retraction

**Lesson**:
What a Regret teaches, and what to choose instead. The durable half of a regret;
the decisions it regrets are the evidence half.

**Divergence**:
A departure from precedent. *Unacknowledged*, it is a warning `check` emits.
*Acknowledged* — recorded with a rationale — it is a deliberate exception, and
the warning is answered in that project while still firing everywhere else.
_Avoid_: exception, override, deviation

### What `check` reports

**Precedent**:
What comparable projects decided about a topic. Information, never a veto.
_Avoid_: rule, policy, convention

**Norm**:
The option chosen for a topic across two or more projects. What a Divergence
diverges from.
_Avoid_: default, standard, house style

**Conflict**:
The proposed option was explicitly rejected by an earlier live decision.
Distinct from a Divergence, which is only a departure from the majority.

**Principle**:
A standing rule promoted from a choice repeated often enough to stop being a
per-project decision.

### Projects and how they are compared

**Project**:
One tagged unit of work. Not necessarily one repository — a monorepo module is
its own Project.

**Portable id**:
The identity that survives a machine, a clone location, and an operating system:
the git remote, plus a subpath for a module inside it. Absent for a repo with no
remote, which is a fine answer — a *wrong* portable id silently merges unrelated
histories.

**Native path**:
Where a Project lives on one machine. A display and liveness fact, never the
identity. Several may point at one Project.

**Kin**:
Another Project ranked comparable by how many tags it shares. Excludes anything
in the same filesystem tree, which is one codebase rather than comparable work.
_Avoid_: neighbour, sibling, closest project, comparable project

**Containment**:
A Project physically inside another. Derived from paths, never stored, so it is
always accurate. An enclosing Project lends its tags and its decisions; it is
never Kin.

**Backfill**:
Reading a project that predates the graph and recording the decisions already
visible in its manifests, CI and docs.

### Vocabulary

**Tag**:
A free-form word describing what kind of thing a Project is. A Project carries a
*set* of them, never one. Supplied by an agent that has read the project, never
inferred from dependencies.
_Avoid_: type, kind, classification, category

**Topic**:
The question a decision answers — `persistence`, `auth`, `ingest`.
_Avoid_: subject, area, domain

**Option**:
A concrete answer to a Topic, chosen or rejected — `postgres`, `sqlite`.
_Avoid_: choice, technology, alternative

**Matching**:
Resolving a string to what is stored. Exact equality, always, so precedent is
reproducible and the tool never asserts that a decision applies when it does not.

**Detecting**:
Judging whether two different strings mean the same thing. Judgment, so it
belongs to the model, which has the vocabulary and the project in front of it —
not to a similarity metric with nine characters to go on.

### Storage

**Journal**:
The append-only record of every write, and the source of truth. Plain text, so
it diffs and survives.

**Graph**:
The queryable index built from the Journal. Rebuildable, and therefore
disposable.

### Packaging

**Core**:
The parts that know nothing about any coding agent: the CLI, the schema, the
standing-orders text.

**Adapter**:
The per-agent surface — how a session gets primed, how commands are invoked, how
the standing orders are worded for that harness.

**Standing orders**:
The instructions an agent is given at session start about when to consult and
when to record. Authored once in Core; each Adapter renders it.
