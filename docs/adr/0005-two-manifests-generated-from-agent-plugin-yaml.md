# Ship two manifests, generated from `agent-plugin.yaml`

The store is agent-neutral and should reach Codex and Cursor, not only Claude
Code. Two distribution channels cover that, and they are alternatives rather
than layers — ACR's Claude Code adapter materialises into `~/.claude/` itself:

- **Claude Code plugin** (`plugin.json`, marketplace) — already installed for
  every Claude Code user, so install is `/plugin install` and nothing else.
- **[ACR](https://github.com/jbaruch/agentic-context-registry)**
  (`agent-plugin.yaml`) — the only thing that reaches Codex and Cursor, at the
  cost of a bootstrap binary each user must install first.

We ship both, and **generate `plugin.json` from `agent-plugin.yaml`**. Two
hand-written manifests drift, and drift here means a Codex user silently getting
last month's commands.

## Why that direction

`agent-plugin.yaml` is the richer format — it already expresses rules, skills,
scripts and hooks across three agents — so `plugin.json` is a lossy projection
of it and the generator is a downhill map. Generating uphill ends with the
missing fields hardcoded in the generator, which is the drift being avoided. It
also fails safely: an ACR schema change breaks the build loudly in CI rather
than shipping a wrong `plugin.json` quietly.

## Consequences

Claude packaging becomes downstream of a third-party spec. Accepted: one correct
manifest beats two that disagree, and CI on three operating systems is where the
breakage surfaces.

## Superseded in part

The packaging work that followed this decision found the real ACR v1 schema
(`agentic-context-registry`'s published `schemas/agent-plugin.schema.json`) to
be narrower than assumed above, and found Claude Code's own plugin contract to
be a different shape than assumed too. The decision to generate `plugin.json`
from `agent-plugin.yaml` stands; the description of both formats does not:

- **`agent-plugin.yaml` has no per-agent section.** Real ACR v1 top level is
  exactly `{schemaVersion, name, version, description?, source, artifacts}`,
  and `artifacts` allows exactly `rules` / `skills` / `scripts` / `hooks` — each
  a flat list, with no notion of "Claude" vs. "Codex" vs. "Cursor" inside the
  manifest. "Already expresses ... across three agents" overstated it: it
  expresses the artifacts once, agent-neutrally; which agents can consume which
  artifact class is a fact about the agents, not something the manifest
  encodes. In particular, ACR has no `commands` artifact class, so the 9 Claude
  Code slash commands under `adapters/claude/commands/` are Claude-only no
  matter what the manifest says.
- **"Install is `/plugin install` and nothing else" is not true today.** That
  statement presumed a marketplace listing, and there was none. It is true now:
  ADR 0007 adds `.claude-plugin/marketplace.json`, moves the plugin root to the
  repository root so the install carries `scripts/precedent.py`, and makes
  `marketplace.json` the second generated projection of this manifest. The
  plugin manifest carries `name` / `description` / `version` / `author` /
  `homepage` plus the two adapter paths convention can no longer find; skills
  and the hook are still not enumerated one by one.
