#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""Generate .claude-plugin/plugin.json from agent-plugin.yaml.

ADR 0005: agent-plugin.yaml is the richer format and plugin.json is a downhill
projection of it. Claude Code's plugin.json only needs name/description/version
here — commands, hooks and skills are all discovered by convention from
adapters/claude/'s layout (commands/*.md, hooks/hooks.json, a root SKILL.md as
the single-skill form), so nothing else has to be enumerated, glob'd, or
derived. Enumerating them would be both unnecessary and, in Claude Code's
actual schema, illegal (paths there are plugin-root-relative with a leading
./, not repo-relative).
"""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "agent-plugin.yaml"
TARGET = ROOT / "adapters" / "claude" / ".claude-plugin" / "plugin.json"


def project(acr: dict) -> dict:
    """Project the ACR manifest onto Claude Code's plugin.json shape."""
    return {
        "name": acr["name"].split("/")[-1],
        "description": " ".join(acr["description"].split()),
        "version": acr["version"],
    }


def main() -> int:
    generated = json.dumps(project(yaml.safe_load(SOURCE.read_text())),
                           indent=2) + "\n"
    if "--check" in sys.argv:
        current = TARGET.read_text() if TARGET.exists() else ""
        if current != generated:
            print(f"{TARGET.name} is stale — regenerate with:\n"
                  f"  uv run scripts/gen-plugin-json.py", file=sys.stderr)
            return 1
        print(f"{TARGET.name} is current")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(generated)
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
