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
