# precedent Packaging and Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make precedent installable by Codex and Cursor users as well as Claude Code users, and prove on real Windows and macOS runners that the portability work in plan A actually holds.

**Architecture:** `scripts/precedent.py` stays where it is and stays the only executable. Everything Claude-Code-specific moves under `adapters/claude/`, so the repository root stops asserting that this is a Claude Code skill. The standing-orders banner — today duplicated between `SKILL.md` and the session-start hook — becomes a CLI subcommand that every adapter shells out to, so there is one copy. `agent-plugin.yaml` becomes the source manifest and `plugin.json` is generated from it (ADR 0005). A GitHub Actions matrix over ubuntu, macos and windows runs `selftest`, which is the first time any of plan A's cross-platform work executes on the platforms it targets.

**Tech Stack:** Python 3.12+, PEP 723 inline metadata, `grafeo`, `filelock`, `uv run`, GitHub Actions, PowerShell (Windows hook twin), YAML (ACR manifest).

**Spec:** `docs/spec-2026-09-10-precedent-hardening.md` — this plan implements subsystem **B** only. A (CLI hardening) is complete; C (measurement) is a separate plan.

**Carried forward:** `docs/carried-forward-from-plan-a.md` lists what plan A parked. One item is folded in — Ruling 29's missing `try`/`finally` in `_check_decisions`, fixed in Task 1 because Task 5 is about to run that suite on three platforms and a leaked fixture there costs three times as much to diagnose. The rest stay parked. Ruling 28 (`selftest` shelling out to `git` in the user's real project directories) is sidestepped by Task 5's scratch `--home` rather than fixed; if the Windows runner makes it painful, that is the moment to reopen it.

## Global Constraints

- Python `>=3.12`. `scripts/precedent.py` stays one file with PEP 723 inline metadata and no durable venv.
- Dependencies are exactly `grafeo` and `filelock`. Add nothing to the script. CI may install `uv` and `git`; it installs no Python packages by hand.
- Must run on Linux, macOS and Windows. No `fcntl` (a selftest assert enforces the literal string's absence), no `os.symlink`, no new `os.sep` string surgery.
- `uv run scripts/precedent.py selftest` is the only test entry point, and must end with `selftest ok (N nodes, unchanged)`.
- `journal.jsonl` is append-only. Never edit or reorder existing lines.
- Grafeo dialect (`references/schema.md`): alias every returned expression; `NOT (n)-[:R]->()` does not parse — use `NOT EXISTS { MATCH ... }`; parameters are positional dicts; `max()` is not available.
- Each selftest check leaves the graph **and the filesystem** exactly as it found them. The node-count invariant catches a leak.
- **No task opens the real store at `~/.local/share/precedent`.** Use a scratch `--home` under a temp directory. Filesystem metadata reads (`ls`, `stat`, `wc -l`, copying `journal.jsonl`) are fine. This carried over from plan A after two accidental touches; `rebuild` deletes every node before replaying.
- The standing-orders banner has exactly one source after Task 1. A second copy anywhere is a defect.

## File Structure

| Path | Responsibility | Task |
|---|---|---|
| `scripts/precedent.py` | The only executable. Gains `standing-orders`. | 1 |
| `adapters/claude/SKILL.md` | moved from `SKILL.md` | 2 |
| `adapters/claude/commands/*.md` | moved from `commands/` | 2 |
| `adapters/claude/hooks/session-start.sh` | moved from `hooks/`, rewritten to resolve its own path and call `standing-orders` | 2 |
| `adapters/claude/hooks/session-start.ps1` | Windows twin | 3 |
| `adapters/claude/hooks/hooks.json` | per-platform shell pinning | 3 |
| `agent-plugin.yaml` | ACR manifest — the source of truth | 4 |
| `plugin.json` | generated from `agent-plugin.yaml`, committed | 4 |
| `scripts/gen-plugin-json.py` | the generator, and its `--check` mode | 4 |
| `.github/workflows/test.yml` | ubuntu/macos/windows matrix | 5 |
| `README.md`, `adapters/claude/SKILL.md` | vendor-neutral reframe; lock locality documented | 6 |

---

## Task 1: `standing-orders` becomes a subcommand

The banner exists twice today — in `hooks/session-start.sh` and in `SKILL.md`. Every adapter this plan adds would be a third and fourth copy. One command, and everything shells out to it.

**Files:**
- Modify: `scripts/precedent.py` — new `STANDING_ORDERS` constant, `cmd_standing_orders`, subparser entry, `_check_lock_modes` table, `_check_helpers`
- Modify: `scripts/precedent.py` — `_check_decisions` gains a `try`/`finally` (carried forward, see Step 6)

**Interfaces:**
- Produces: `STANDING_ORDERS: str` — the banner with a single `{cli}` placeholder.
- Produces: `cmd_standing_orders(a) -> None` — prints the banner with `{cli}` filled in. Runs **without** a `Store`, exactly as `cmd_init`'s no-argument branch does.
- Produces: subcommand `standing-orders`.

- [ ] **Step 1: Write the failing test**

Add above `cmd_selftest`, and add `_check_standing_orders()` to the runner list in `cmd_selftest`:

```python
def _check_standing_orders() -> None:
    """One source for the banner, and it must name a runnable CLI path.

    The banner lived in two places — the session-start hook and SKILL.md —
    and this plan adds two more adapters. Four copies drift, and a drifted
    standing order is a model told to run a command that no longer exists.
    """
    import io
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cmd_standing_orders(None)
    text = out.getvalue()

    assert "{cli}" not in text, "the placeholder must be substituted, not printed"
    assert str(pathlib.Path(__file__).resolve()) in text, \
        "the banner must name this script's real path, so a copied install still works"
    for verb in ("check", "record", "regret"):
        assert f" {verb}" in text, f"the banner must tell the model about `{verb}`"
    assert "not a veto" in text, "the banner must keep the 'precedent is information' line"
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run scripts/precedent.py --home /tmp/precedent-planb selftest`
Expected: FAIL with `NameError: name 'cmd_standing_orders' is not defined`

- [ ] **Step 3: Add the constant**

Below `SCOPES`, before `SCHEMA`. Copy the wording from `hooks/session-start.sh`'s heredoc verbatim — it is the version a model has actually been reading — replacing every `$DG` with `{cli}` and unescaping the shell-escaped backticks:

```python
STANDING_ORDERS = """Standing orders for the rest of this session:
- Before recommending a technology, framework, provider, or process choice, run
  `uv run {cli} check --topic <topic> --chose <option>` and lead with what it returns.
  Your opinion is worth less than what the user already chose and lived with.
- When a choice gets settled in conversation, offer to record it, then run
  `uv run {cli} record ...` with --rationale and --rejected. Ask first; a wrong
  entry is worse than a missing one because it gets quoted back as precedent.
- The user will not type a command. They say an ordinary sentence, and you notice:
  "let's go with X" -> `record`. "what did I use last time" -> `check`.
  "X was a mistake" / "X bit us" / "never again" -> `regret` (marks every project
  that chose it, so the graph stops arguing for it), NOT another `record`.
  "I usually do X, but here..." -> `record --despite`. Draft it, show one line,
  run it once they confirm.
- Precedent is information, not a veto. Say when consistency is wrong here."""
```

- [ ] **Step 4: Add the command**

Beside `cmd_init`:

```python
def cmd_standing_orders(a) -> None:
    """Print the banner every adapter appends after a brief.

    Runs without a Store: opening one would create the store directory as a
    side effect of printing text, and this command is called from hooks that
    may run in directories the user never records anything in.

    The path is resolved from __file__ rather than assumed, so a checkout
    installed anywhere — ~/.claude/skills, an ACR cache, a bare clone —
    prints a command line that actually runs.
    """
    print(STANDING_ORDERS.format(cli=pathlib.Path(__file__).resolve()))
```

- [ ] **Step 5: Register the subcommand**

In `build_parser`, beside the `init` entry. Read how `init` is registered and how `main` special-cases it **before** constructing a `Store`, and follow that pattern exactly — including its `writes=` value, which is unused for a no-Store command but must be present so `_check_lock_modes`'s table stays complete:

```python
    so = sub.add_parser("standing-orders",
                        help="print the standing orders every adapter appends after a brief")
    so.set_defaults(writes=False, fn=cmd_standing_orders)
```

Then add `"standing-orders": False` to `_check_lock_modes`'s literal expected mapping, and extend `main`'s no-Store special case to cover it alongside `init`.

- [ ] **Step 6: Give `_check_decisions` a `try`/`finally`**

Carried forward from plan A (`docs/carried-forward-from-plan-a.md`). `_check_decisions` is the one check without one: a failing assert leaks its `selftest-*` nodes, and the **next** run reports the node-count failure against the wrong check. Task 5 runs this suite on three platforms for the first time, so a confusing failure gets three times more expensive.

Wrap its body exactly as `_check_projects` does — hoist the fixture ids above the `try`, put every assertion inside it, and move the existing cleanup into the `finally`.

- [ ] **Step 7: Run the test and verify it passes**

Run: `uv run scripts/precedent.py --home /tmp/precedent-planb selftest`
Expected: PASS, ending `selftest ok (N nodes, unchanged)`

- [ ] **Step 8: Verify the command by hand**

```bash
uv run scripts/precedent.py standing-orders
```
Expected: the banner, with a real absolute path to `scripts/precedent.py` in the `uv run` lines. This command opens no store — confirm no directory was created at the default location if it did not already exist.

- [ ] **Step 9: Commit**

```bash
git add scripts/precedent.py
git commit -m "feat: one source for the standing orders, printed by the CLI"
```

---

## Task 2: Everything Claude-specific moves under `adapters/claude/`

**Files:**
- Move: `SKILL.md` → `adapters/claude/SKILL.md`
- Move: `commands/` → `adapters/claude/commands/`
- Move: `hooks/session-start.sh` → `adapters/claude/hooks/session-start.sh`
- Modify: `adapters/claude/hooks/session-start.sh` — resolve its own path, call `standing-orders`
- Modify: `adapters/claude/SKILL.md` — delete its copy of the banner
- Modify: `README.md` — install paths only; the prose reframe is Task 6

**Interfaces:**
- Consumes: `standing-orders` from Task 1.
- Produces: the `adapters/claude/` tree that Task 3's hooks and Task 4's manifests reference.

- [ ] **Step 1: Move the files with git**

```bash
mkdir -p adapters/claude/hooks
git mv SKILL.md adapters/claude/SKILL.md
git mv commands adapters/claude/commands
git mv hooks/session-start.sh adapters/claude/hooks/session-start.sh
rmdir hooks
```

Use `git mv`, not `cp` — the history of these files is worth keeping, and a copy-then-delete loses it.

- [ ] **Step 2: Rewrite the hook to resolve its own path**

The hook currently hardcodes `$HOME/.claude/skills/precedent/scripts/precedent.py`, which is false as soon as the file lives under `adapters/claude/`. Replace the whole file:

```bash
#!/usr/bin/env bash
# SessionStart hook: prime the session with the user's recorded decisions.
# Silent unless the graph has something relevant — an empty brief is noise.
set -uo pipefail

# Resolve the CLI relative to this script rather than assuming an install
# path: this file now ships under adapters/claude/ and is installed by at
# least two channels (Claude Code plugin, ACR) that put it in different
# places. ../../../scripts/precedent.py from here is the repository layout.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
DG="$HERE/../../../scripts/precedent.py"
[ -f "$DG" ] || exit 0
command -v uv >/dev/null 2>&1 || exit 0

# --only-if-relevant prints nothing when this directory has no decisions and no
# comparable projects, so the decision is made on the data rather than by
# grepping output whose wording can change.
BRIEF=$(timeout 20 uv run --quiet "$DG" brief --project "$PWD" --only-if-relevant 2>/dev/null) || exit 0
[ -n "$BRIEF" ] || exit 0

# The standing orders come from the CLI so this hook, SKILL.md and every
# other adapter cannot drift apart.
ORDERS=$(uv run --quiet "$DG" standing-orders 2>/dev/null) || exit 0

cat <<PRIME
PRECEDENT — your recorded decisions, loaded for this session.

$BRIEF

$ORDERS
Full guidance: $HERE/../SKILL.md
PRIME
```

Note the deleted `[ -d "$HOME/.local/share/precedent" ] || exit 0` guard: `brief --only-if-relevant` already prints nothing when there is nothing to say, and the store may legitimately live elsewhere behind a `location` pointer since plan A's Task 3.

- [ ] **Step 3: Delete SKILL.md's copy of the banner**

In `adapters/claude/SKILL.md`, find the standing-orders block and replace it with a pointer to the command:

```markdown
The session-start hook prints the standing orders automatically. To see them
without a hook, or from another agent:

    uv run scripts/precedent.py standing-orders
```

Do not paraphrase the orders here. One source is the point of Task 1.

- [ ] **Step 4: Update the README's install paths**

`README.md` lines that symlink `SKILL.md`, `commands/` and `hooks/` now point at the wrong place. Update only the paths — leave every sentence of prose alone; Task 6 rewrites those deliberately and doing it here would make that diff unreadable:

```bash
ln -s ~/src/precedent/adapters/claude ~/.claude/skills/precedent
ln -s ~/src/precedent/adapters/claude/commands/*.md ~/.claude/commands/
```

and the hook path becomes `$HOME/.claude/skills/precedent/hooks/session-start.sh`.

- [ ] **Step 5: Verify the hook runs from its new location**

```bash
bash adapters/claude/hooks/session-start.sh
```
Expected: either silence (if this directory has no relevant decisions) or the banner with a real brief. It must **not** print an error, and it must not print the standing orders twice.

Then confirm the path resolution is real rather than accidental:

```bash
cd /tmp && bash ~/src/precedent/adapters/claude/hooks/session-start.sh; cd -
```
Adjust the path to wherever this checkout lives. Expected: same behaviour — the hook must not depend on the working directory.

- [ ] **Step 6: Verify nothing still references the old paths**

```bash
grep -rn "skills/precedent/scripts\|^SKILL.md\|\"commands/\|'hooks/" --include="*.md" --include="*.sh" --include="*.py" . | grep -v adapters/
```
Expected: no hits outside `adapters/`. Investigate every hit rather than assuming it is stale.

- [ ] **Step 7: Run the selftest**

Run: `uv run scripts/precedent.py --home /tmp/precedent-planb selftest`
Expected: PASS. Nothing in this task touches the script, so a failure here means a move broke something unexpected.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor: move the Claude Code adapter under adapters/claude"
```

---

## Task 3: A PowerShell twin of the hook, and per-platform shell pinning

A `.sh` hook is unreachable on Windows. Plan A made the CLI run there; without this the priming that makes the tool useful does not.

**Files:**
- Create: `adapters/claude/hooks/session-start.ps1`
- Create: `adapters/claude/hooks/hooks.json`
- Modify: `README.md` — the hook registration snippet

**Interfaces:**
- Consumes: `standing-orders` from Task 1, the `adapters/claude/` layout from Task 2.
- Produces: `hooks.json` describing which hook file to run per platform.

- [ ] **Step 1: Write the PowerShell twin**

Create `adapters/claude/hooks/session-start.ps1`. It must behave identically to the `.sh`: silent when there is nothing relevant, never erroring:

```powershell
# SessionStart hook: prime the session with the user's recorded decisions.
# Silent unless the graph has something relevant — an empty brief is noise.
# Twin of session-start.sh; both must stay behaviourally identical.
$ErrorActionPreference = 'SilentlyContinue'

# Resolve the CLI relative to this script, not an assumed install path.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dg = Join-Path $here '..\..\..\scripts\precedent.py'
if (-not (Test-Path $dg)) { exit 0 }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { exit 0 }

$brief = & uv run --quiet $dg brief --project $PWD --only-if-relevant 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($brief)) { exit 0 }

$orders = & uv run --quiet $dg standing-orders 2>$null
if ($LASTEXITCODE -ne 0) { exit 0 }

$skill = Join-Path $here '..\SKILL.md'
@"
PRECEDENT — your recorded decisions, loaded for this session.

$brief

$orders
Full guidance: $skill
"@
```

- [ ] **Step 2: Write `hooks.json`**

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command",
            "command": "$CLAUDE_PLUGIN_ROOT/hooks/session-start.sh",
            "platforms": ["linux", "darwin"] },
          { "type": "command",
            "command": "powershell -NoProfile -ExecutionPolicy Bypass -File $CLAUDE_PLUGIN_ROOT/hooks/session-start.ps1",
            "platforms": ["win32"] }
        ]
      }
    ]
  }
}
```

- [ ] **Step 3: Verify the PowerShell parses**

If `pwsh` is available:

```bash
pwsh -NoProfile -Command '$null = [System.Management.Automation.Language.Parser]::ParseFile("adapters/claude/hooks/session-start.ps1", [ref]$null, [ref]$null); if ($?) { "parses" }'
```
Expected: `parses`.

If `pwsh` is not installed, say so in your report and note that Task 5's Windows runner is the first real execution. Do not skip silently — an unparsed hook that ships is a hook nobody runs.

- [ ] **Step 4: Verify the two hooks agree**

Read both files side by side and confirm every guard in the `.sh` has a counterpart in the `.ps1`: missing CLI, missing `uv`, empty brief, failed brief, failed standing-orders. A guard present in one and absent in the other is the drift this task exists to avoid — list each pair in your report.

The `.sh` has a `timeout 20` on the brief and the `.ps1` does not, because PowerShell has no direct equivalent. Note that asymmetry in your report rather than inventing a job-based workaround; a hung `brief` on Windows is a real gap, and naming it is better than a fragile fix.

- [ ] **Step 5: Update the README's hook registration**

Replace the `~/.claude/settings.json` snippet with one that reflects `hooks.json`, and add a sentence naming the Windows file. Paths only — no prose rewriting, that is Task 6.

- [ ] **Step 6: Commit**

```bash
git add adapters/claude/hooks/session-start.ps1 adapters/claude/hooks/hooks.json README.md
git commit -m "feat: a PowerShell hook twin so Windows gets primed too"
```

---

## Task 4: `agent-plugin.yaml` is the source; `plugin.json` is generated

Implements ADR 0005 — read `docs/adr/0005-two-manifests-generated-from-agent-plugin-yaml.md`. Two hand-written manifests drift, and drift means a Codex user silently getting last month's commands.

**Files:**
- Create: `agent-plugin.yaml`
- Create: `scripts/gen-plugin-json.py`
- Create: `plugin.json` (generated, committed)

**Interfaces:**
- Consumes: the `adapters/claude/` layout from Task 2, `hooks.json` from Task 3.
- Produces: `gen-plugin-json.py` with two modes — default writes `plugin.json`, `--check` exits non-zero if the committed file is stale.

- [ ] **Step 1: Write the ACR manifest**

Create `agent-plugin.yaml`. Enumerate every command file explicitly rather than globbing — a glob makes the generator's output depend on directory listing order, and `--check` then fails spuriously:

```yaml
name: precedent
description: >-
  Remember and reuse the user's architectural, business and tooling decisions
  across every project, in a queryable graph.
version: 0.1.0
license: MIT
agents:
  claude-code:
    skills:
      - source: adapters/claude/SKILL.md
    commands:
      - source: adapters/claude/commands/precedent-analyze.md
      - source: adapters/claude/commands/precedent-check.md
      - source: adapters/claude/commands/precedent-diverge.md
      - source: adapters/claude/commands/precedent-maintain.md
      - source: adapters/claude/commands/precedent-prime.md
      - source: adapters/claude/commands/precedent-record.md
      - source: adapters/claude/commands/precedent-regret.md
      - source: adapters/claude/commands/precedent-suggest.md
      - source: adapters/claude/commands/precedent-tag.md
    hooks:
      - source: adapters/claude/hooks/hooks.json
scripts:
  - source: scripts/precedent.py
    entrypoint: true
```

Verify this against ACR's current schema before writing it — the repository is linked from ADR 0005. If a field name here is wrong, use ACR's and say so in your report; this plan's guess is not authority.

- [ ] **Step 2: Write the failing check**

Create `scripts/gen-plugin-json.py` with only the `--check` path first, so you can watch it fail:

```python
#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""Generate plugin.json from agent-plugin.yaml.

ADR 0005: agent-plugin.yaml is the richer format and plugin.json is a lossy
projection of it, so the generator runs downhill. Generating uphill ends with
the missing fields hardcoded here, which is the drift being avoided.
"""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "agent-plugin.yaml"
TARGET = ROOT / "plugin.json"


def project(acr: dict) -> dict:
    """Project the ACR manifest onto Claude Code's plugin.json shape."""
    claude = acr["agents"]["claude-code"]
    return {
        "name": acr["name"],
        "description": " ".join(acr["description"].split()),
        "version": acr["version"],
        "license": acr["license"],
        "skills": [s["source"] for s in claude.get("skills", [])],
        "commands": [c["source"] for c in claude.get("commands", [])],
        "hooks": [h["source"] for h in claude.get("hooks", [])],
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
```

- [ ] **Step 3: Run the check and verify it fails**

Run: `uv run scripts/gen-plugin-json.py --check`
Expected: exit 1, `plugin.json is stale` — the file does not exist yet.

- [ ] **Step 4: Generate it**

```bash
uv run scripts/gen-plugin-json.py
uv run scripts/gen-plugin-json.py --check
```
Expected: `wrote plugin.json`, then `plugin.json is current`.

- [ ] **Step 5: Verify the generated manifest is valid**

```bash
python3 -c "import json,pathlib; d=json.loads(pathlib.Path('plugin.json').read_text()); print(len(d['commands']), 'commands'); assert all(pathlib.Path(p).exists() for p in d['commands']+d['skills']+d['hooks']), 'a manifest path does not exist'; print('every path resolves')"
```
Expected: `9 commands`, then `every path resolves`. A manifest naming a file that is not there is the failure mode that reaches users as a broken install.

- [ ] **Step 6: Verify the check actually catches drift**

```bash
printf '\n' >> plugin.json
uv run scripts/gen-plugin-json.py --check; echo "exit=$?"
uv run scripts/gen-plugin-json.py
```
Expected: exit 1, then regenerated. A `--check` that cannot fail is not a check — Task 5 depends on this one biting.

- [ ] **Step 7: Commit**

```bash
git add agent-plugin.yaml scripts/gen-plugin-json.py plugin.json
git commit -m "feat: generate plugin.json from the ACR manifest"
```

---

## Task 5: CI matrix on ubuntu, macOS and Windows

This is the task the whole plan is for. Plan A's Windows and macOS work — `filelock` replacing the POSIX lock module, the pointer file replacing the symlink, `as_posix()` in the portable id, and the macOS `/tmp` symlink fix — has never executed on either platform. Implements the recorded decision `#test-on-every-os-claimed-to-be-supported`.

**Files:**
- Create: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: `gen-plugin-json.py --check` from Task 4.

- [ ] **Step 1: Write the workflow**

```yaml
name: test

on:
  push:
    branches: [main]
  pull_request:

jobs:
  selftest:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - name: selftest
        shell: bash
        run: uv run scripts/precedent.py --home "${RUNNER_TEMP}/precedent-ci" selftest

  manifest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - name: plugin.json is current
        run: uv run scripts/gen-plugin-json.py --check
```

`fail-fast: false` is deliberate: when Windows fails, the macOS result is the next thing you want, and a cancelled job tells you nothing.

The scratch `--home` under `RUNNER_TEMP` is not optional. `selftest` against a default home would create a store on the runner, and one check enumerates every Project node and shells out to `git` in their directories.

- [ ] **Step 2: Push and watch all three**

```bash
git add .github/workflows/test.yml
git commit -m "ci: run the selftest on ubuntu, macos and windows"
git push
gh run watch
```

- [ ] **Step 3: Expect Windows and macOS to fail, and report what actually broke**

This is the first execution on those platforms. **Do not assume the workflow is wrong.** Read each failure and classify it:

- a real portability defect in `scripts/precedent.py` (the valuable outcome — plan A's reasoning was wrong somewhere)
- a selftest fixture that is still platform-specific despite plan A's final fix wave
- a genuine CI configuration problem

Report the classification with the failing output before changing anything. A defect found here is worth more than a green badge: it is the thing three plans of reasoning could not establish.

- [ ] **Step 4: Fix what the runners found**

Fix defects in the order they block: a failure in `_check_helpers` hides everything after it. Keep each fix minimal and commit it separately with a message naming the platform that found it, so the history shows what reasoning missed.

If a failure needs a design decision rather than a fix — for example if `filelock`'s Windows behaviour differs from what plan A assumed — **stop and report it** rather than choosing. Plan A recorded `#read-write-lock-plus-a-bounded-timeout-gated-on-` on the basis of a Linux measurement; a Windows contradiction is a decision to revisit, not a bug to patch.

- [ ] **Step 5: Verify all three are green**

```bash
gh run list --limit 1
```
Expected: all four jobs pass. Paste the run URL into your report.

- [ ] **Step 6: Commit**

Already committed per fix in Step 4. Confirm the working tree is clean.

---

## Task 6: Reframe README and SKILL as a vendor-neutral decision store

Last deliberately: every path this changes was still moving until Task 5 went green. Reframing before the layout settled would have meant writing it twice.

**Files:**
- Modify: `README.md`
- Modify: `adapters/claude/SKILL.md`

**Interfaces:**
- Consumes: the final layout from Tasks 2-4, and whatever Task 5 learned about the platforms.

- [ ] **Step 1: Rewrite the README's opening**

Line 3 currently reads *"A Claude Code skill that remembers…"*. The tool is a CLI over a graph; Claude Code is one of three ways to reach it. Replace with something that leads with what it is and lists the agents afterwards. Keep the existing second and third sentences — the problem statement is unchanged and still good.

- [ ] **Step 2: Restructure Installation by channel**

Today there is one install path. Give it three headings — Claude Code plugin (`/plugin install`), ACR (for Codex and Cursor), and manual symlink (what the README has now, corrected by Task 2) — each two or three lines. The plugin and ACR paths exist because of Task 4's manifests; say which manifest drives which.

- [ ] **Step 3: Fix the Requirements section**

Line 207 currently reads `- Claude Code (skill, commands and hook)`. Replace with a line naming Claude Code, Codex and Cursor as supported agents, and keep the `uv` and Python 3.12+ lines. The OS line was already corrected in plan A.

- [ ] **Step 4: Document that the lock is local**

Spec B requires this and nothing says it yet. Add a short subsection under Storage, in the README:

```markdown
### A synced store is not a shared store

The lock is a local file. Two machines writing to one store over Dropbox,
iCloud or a network mount are not serialised by it — each sees its own lock
file, and `grafeo` silently drops concurrent writes (GrafeoDB/grafeo#405:
120 writes across 6 processes, 60 stored, nothing raised).

Sync the journal, not the graph. `journal.jsonl` is append-only and merges
in git; `precedent.py rebuild` reconstructs the graph from it on each
machine. That is also why a project's identity is its git remote rather
than its path — see `docs/adr/0002`.
```

- [ ] **Step 5: Update SKILL.md's framing**

`adapters/claude/SKILL.md`'s prose assumes it is the only entry point. Adjust the framing sentences so it reads as one adapter's guidance. Leave the YAML frontmatter `description` alone — it is what triggers the skill, it is tuned, and rewriting it changes behaviour rather than framing.

- [ ] **Step 6: Verify no stale Claude-centric framing remains**

```bash
grep -n "Claude" README.md
```
Expected: hits only where Claude Code is named as one supported agent among several, or in an install path under the Claude Code heading. No hit should assert that precedent *is* a Claude Code skill. Read every remaining hit aloud and confirm it is one of those two cases.

- [ ] **Step 7: Run the selftest one last time**

Run: `uv run scripts/precedent.py --home /tmp/precedent-planb selftest`
Expected: PASS. Documentation-only changes should not move it; if they do, something in Step 5 touched more than prose.

- [ ] **Step 8: Commit**

```bash
git add README.md adapters/claude/SKILL.md
git commit -m "docs: a decision store that three agents can reach"
```

---

## Done when

```bash
uv run scripts/precedent.py --home /tmp/precedent-planb selftest   # ok, unchanged
uv run scripts/precedent.py standing-orders                        # the banner, one source
uv run scripts/gen-plugin-json.py --check                          # plugin.json is current
gh run list --limit 1                                              # ubuntu, macos, windows all green
grep -rn "Standing orders for the rest" --include="*.md" --include="*.sh" --include="*.ps1" .   # no output
```

The last grep is the one worth keeping: it proves the banner still has exactly one source. Plan C (measurement: capture evals, the negative case, the Codex priming experiment) follows.
