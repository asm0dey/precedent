#!/usr/bin/env bash
# SessionStart hook: prime the session with the user's recorded decisions.
# Silent unless the graph has something relevant — an empty brief is noise.
set -uo pipefail
DG="$HOME/.claude/skills/precedent/scripts/precedent.py"
[ -f "$DG" ] || exit 0
[ -d "$HOME/.local/share/precedent" ] || exit 0
command -v uv >/dev/null 2>&1 || exit 0

# --only-if-relevant prints nothing when this directory has no decisions and no
# comparable projects, so the decision is made on the data rather than by
# grepping output whose wording can change.
BRIEF=$(timeout 20 uv run --quiet "$DG" brief --project "$PWD" --only-if-relevant 2>/dev/null) || exit 0
[ -n "$BRIEF" ] || exit 0

cat <<PRIME
PRECEDENT — your recorded decisions, loaded for this session.

$BRIEF

Standing orders for the rest of this session:
- Before recommending a technology, framework, provider, or process choice, run
  \`uv run $DG check --topic <topic> --chose <option>\` and lead with what it returns.
  Your opinion is worth less than what the user already chose and lived with.
- When a choice gets settled in conversation, offer to record it, then run
  \`uv run $DG record ...\` with --rationale and --rejected. Ask first; a wrong
  entry is worse than a missing one because it gets quoted back as precedent.
- Precedent is information, not a veto. Say when consistency is wrong here.
Full guidance: $HOME/.claude/skills/precedent/SKILL.md
PRIME
