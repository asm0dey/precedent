# The CLI lives in the `precedent` skill's directory

The CLI outgrew one file, and the split puts a `precedent_cli` package beside a
thin PEP 723 entry script; Python adds the script's directory to `sys.path`, so
nothing is installed. That only works where the package travels with the script.

ACR ships a `scripts` artifact as exactly one regular file (ACR
`docs/package-manifest.md`, "Artifact Model"), so a package beside
`scripts/precedent.py` would be missing from every ACR install — Codex and
Cursor included — and the hook would fail closed, silently. A skill artifact is
its whole directory. So the entry script and the package live in
`adapters/claude/skills/precedent/scripts/`, and the `precedent-cli` script
artifact is gone from `agent-plugin.yaml`.

The plugin channel is unaffected in kind: it copies the repository (ADR 0007),
and the hook resolves the CLI relative to itself, now at
`../skills/precedent/scripts/precedent.py`.

## Rejected

- **Keep `scripts/`, drop ACR.** Least path churn; loses the only channel that
  reaches Codex and Cursor.
- **Keep one file.** The file was 4,266 lines.

## Consequences

Anyone who invoked `scripts/precedent.py` from a clone by hand must use the new
path. ADR 0007's statement of the CLI's location is historical from here on.
