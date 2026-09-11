# The repository is the plugin root

`/plugin install` copies the plugin root and nothing above it. The adapter
directory was the obvious root — `adapters/claude/` already has `skills/` and
`hooks/hooks.json` exactly where Claude Code's convention looks for them — and
an install from it is broken in the one way that is hardest to see: the skills
and the hook arrive, `scripts/precedent.py` does not, and the hook fails closed.
No error, no brief, a session indistinguishable from one where the graph had
nothing to say. Every skill fails the same way, one command later.

So the plugin root is the repository root, and `.claude-plugin/plugin.json`
names the two paths convention can no longer find:

```json
"skills": ["./adapters/claude/skills"],
"hooks": "./adapters/claude/hooks/hooks.json"
```

The CLI then sits at `../../../scripts/precedent.py` from the installed hook —
the same relative path it has in a clone, which is why the hook's existing
resolution needed no change.

## The marketplace

`/plugin install` needs a catalog to install from, so `.claude-plugin/marketplace.json`
lists this one plugin with source `./`. It is generated alongside `plugin.json`
from `agent-plugin.yaml`, per ADR 0005.

A `git-subdir` source pointing at `adapters/claude` was the alternative that
kept the adapter as root. It sparse-checks out the named subdirectory, which
is the same missing-CLI install described above, arrived at from the other
direction.

## Consequences

The install carries the whole repository — tests, docs, evals, the ACR
manifest — where an adapter-rooted one carried 11 files. That is the price of
shipping the CLI, which is not optional, and it is a few hundred kilobytes.

Two paths now live in a manifest instead of in a convention, so renaming
anything under `adapters/claude/` can ship an install with no skills and no
hook while every other check still passes. `gen-plugin-json.py --check`
asserts both paths exist, and CI runs it.
