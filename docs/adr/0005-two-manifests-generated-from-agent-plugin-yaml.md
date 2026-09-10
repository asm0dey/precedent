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
