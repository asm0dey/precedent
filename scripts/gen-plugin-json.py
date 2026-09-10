#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""Generate plugin.json from agent-plugin.yaml.

ADR 0005: agent-plugin.yaml is meant to be the richer format so plugin.json is
a downhill projection of it. Against ACR's real v1 schema that premise only
holds partially — ACR has no `commands` artifact class and no per-agent
section, so the 9 Claude Code slash commands cannot be expressed there at all.
Rather than hardcode a second command list here (the exact drift ADR 0005
exists to avoid), `commands` is derived from a sorted glob of
adapters/claude/commands/*.md — sorted so the result is deterministic and
--check cannot fail on directory-listing order alone. `hooks` is derived from
the ACR session-start hook entry's own path: its containing directory is where
Claude Code's hooks.json manifest lives, so that path is reconstructed rather
than written out a second time. `license` is dropped entirely — it is not a
field ACR v1 carries, and hardcoding it uphill is exactly the drift this
generator exists to prevent.
"""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "agent-plugin.yaml"
TARGET = ROOT / "plugin.json"
COMMANDS_DIR = ROOT / "adapters" / "claude" / "commands"


def project(acr: dict) -> dict:
    """Project the ACR manifest onto Claude Code's plugin.json shape."""
    artifacts = acr["artifacts"]

    session_start = next(
        h for h in artifacts.get("hooks", []) if h["event"] == "session-start"
    )
    hooks_dir = pathlib.PurePosixPath(session_start["path"]).parent
    hooks_manifest = (hooks_dir / "hooks.json").as_posix()

    commands = sorted(
        p.relative_to(ROOT).as_posix() for p in COMMANDS_DIR.glob("*.md")
    )

    return {
        "name": acr["name"],
        "description": " ".join(acr["description"].split()),
        "version": acr["version"],
        "skills": [s["path"] for s in artifacts.get("skills", [])],
        "commands": commands,
        "hooks": [hooks_manifest],
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
    TARGET.write_text(generated)
    print(f"wrote {TARGET.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
