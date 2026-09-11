#!/usr/bin/env bash
# SessionStart hook: prime the session with the user's recorded decisions.
# Silent unless the graph has something relevant — an empty brief is noise.
set -uo pipefail

# Resolve the CLI relative to this script rather than assuming an install
# path: this file ships under adapters/claude/ and is installed by two
# channels that lay it out differently.
#
#   repository / Claude Code plugin  adapters/claude/hooks/  -> ../../../scripts/
#   acr realize                      .claude/hooks/<pkg>/    -> ../../scripts/<pkg>/
#
# The ACR directory names carry the package name, so the sibling cannot be
# spelled out; the glob finds it whatever the package is called. A glob that
# matches nothing stays literal, and the `-f` test then rejects it.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

# The statusline badge (hooks/statusline.sh) renders off a marker named after
# the directory this hook primed. Dropping it first and writing it back only
# on a successful, non-empty brief means every early exit below — no CLI, no
# uv, nothing to say — also clears a badge left by an earlier session.
MARK="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.precedent-primed/${PWD//[^A-Za-z0-9-]/_}"
rm -f "$MARK" 2>/dev/null
DG=""
for candidate in "$HERE/../../../scripts/precedent.py" \
                 "$HERE"/../../scripts/*/precedent.py; do
  [ -f "$candidate" ] && { DG="$candidate"; break; }
done
[ -n "$DG" ] || exit 0
command -v uv >/dev/null 2>&1 || exit 0

# --only-if-relevant prints nothing when this directory has no decisions and no
# comparable projects, so the decision is made on the data rather than by
# grepping output whose wording can change.
# `timeout` is GNU coreutils and is not on a stock macOS, where Homebrew
# spells it `gtimeout`. Hardcoding it meant the command failed to start at
# all and the `|| exit 0` below read that as "nothing to say" — no brief on
# any Mac, silently. The bound is best-effort: without either binary the
# brief still runs, unguarded, rather than not running.
TIMEOUT=""
for t in timeout gtimeout; do
  if command -v "$t" >/dev/null 2>&1; then TIMEOUT="$t"; break; fi
done
BRIEF=$(${TIMEOUT:+$TIMEOUT 20} uv run --quiet "$DG" brief --project "$PWD" --only-if-relevant 2>/dev/null) || exit 0
[ -n "$BRIEF" ] || exit 0

# An empty file: the statusline reads only the name, never the contents.
mkdir -p "${MARK%/*}" 2>/dev/null && : > "$MARK" 2>/dev/null

# The standing orders come from the CLI so this hook, SKILL.md and every
# other adapter cannot drift apart.
ORDERS=$(uv run --quiet "$DG" standing-orders 2>/dev/null) || exit 0

# The guidance pointer needs the same two-layout treatment as the CLI above:
# in the repository the main skill is a sibling directory, while acr realize
# writes it under the agent's skills root beside this hook's own directory.
SKILL=""
for candidate in "$HERE/../skills/precedent/SKILL.md" \
                 "$HERE"/../../skills/*precedent/SKILL.md; do
  [ -f "$candidate" ] && { SKILL="$candidate"; break; }
done

cat <<PRIME
PRECEDENT — your recorded decisions, loaded for this session.

$BRIEF

$ORDERS
${SKILL:+Full guidance: $SKILL}
PRIME
