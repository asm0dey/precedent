#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""Generate Claude Code's two manifests from agent-plugin.yaml.

ADR 0005: agent-plugin.yaml is the richer format and plugin.json is a downhill
projection of it. The plugin root is the REPOSITORY root, not adapters/claude, because
`/plugin install` copies the plugin root and nothing above it: with the
adapter as root, the skills and the hook install without
`scripts/precedent.py`, the CLI every one of them drives, and the hook then
fails closed — silently. Rooting the plugin at the repository puts the CLI
inside the installed tree, at the same `../../../scripts/precedent.py` the
hook already resolves in a clone.

The cost is that skills and hooks are no longer where convention looks for
them (`<root>/skills`, `<root>/hooks/hooks.json`), so plugin.json names both
paths explicitly. They are plugin-root-relative with a leading `./`.

The marketplace catalog beside it is the second projection: one plugin, source
`./`, so `/plugin marketplace add asm0dey/precedent` then
`/plugin install precedent@precedent` is the whole install.
"""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "agent-plugin.yaml"
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_SOURCE = "./"


def owner(acr: dict) -> dict:
    """The GitHub account the ACR name is scoped to."""
    account = acr["name"].split("/")[0]
    return {"name": account, "url": f"https://github.com/{account}"}


def plugin(acr: dict) -> dict:
    """Project the ACR manifest onto Claude Code's plugin.json shape."""
    return {
        "name": acr["name"].split("/")[-1],
        "description": " ".join(acr["description"].split()),
        "version": acr["version"],
        "author": owner(acr),
        "homepage": acr["source"]["repository"],
        "skills": ["./adapters/claude/skills"],
        "hooks": "./adapters/claude/hooks/hooks.json",
    }


def marketplace(acr: dict) -> dict:
    """Project it onto the catalog that makes `/plugin install` possible."""
    entry = plugin(acr)
    return {
        "name": entry["name"],
        "owner": owner(acr),
        "description": entry["description"],
        "plugins": [{
            "name": entry["name"],
            "source": PLUGIN_SOURCE,
            "description": entry["description"],
            "version": entry["version"],
        }],
    }


def declared_paths_exist(manifest: dict) -> bool:
    """The two paths plugin.json names are the whole plugin channel.

    A rename under adapters/claude/ leaves the manifest still valid JSON and
    still schema-valid; the install just ships no skills and no hook. Checked
    here because this is the only place both are written down.
    """
    ok = True
    for declared in [*manifest["skills"], manifest["hooks"]]:
        if not (ROOT / declared.lstrip("./")).exists():
            print(f"plugin.json names a path that does not exist: {declared}",
                  file=sys.stderr)
            ok = False
    return ok


def main() -> int:
    acr = yaml.safe_load(SOURCE.read_text())
    if not declared_paths_exist(plugin(acr)):
        return 1
    outputs = [(PLUGIN, plugin(acr)), (MARKETPLACE, marketplace(acr))]
    stale = False
    for target, content in outputs:
        generated = json.dumps(content, indent=2) + "\n"
        if "--check" in sys.argv:
            current = target.read_text() if target.exists() else ""
            if current != generated:
                print(f"{target.name} is stale — regenerate with:\n"
                      f"  uv run scripts/gen-plugin-json.py", file=sys.stderr)
                stale = True
            else:
                print(f"{target.name} is current")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated)
        print(f"wrote {target.relative_to(ROOT)}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
