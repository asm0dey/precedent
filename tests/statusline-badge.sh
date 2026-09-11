#!/usr/bin/env bash
# The statusline badge is only as good as the marker the SessionStart hook
# leaves behind: both sides derive the marker's name from the project path
# independently, and a mismatch is silent — no badge, no error, ever. This
# runs the real hook and the real statusline against a seeded store and
# requires the badge to appear, then requires it to go away again for a
# directory the graph has nothing to say about.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
CLI="$ROOT/scripts/precedent.py"
HOOK="$ROOT/adapters/claude/hooks/session-start.sh"
LINE="$ROOT/adapters/claude/hooks/statusline.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

export PRECEDENT_HOME="$WORK/store"
export CLAUDE_CONFIG_DIR="$WORK/claude"
PROJECT="$WORK/proj"
QUIET="$WORK/quiet"
mkdir -p "$PROJECT" "$QUIET"

uv run --quiet "$CLI" tag --project "$PROJECT" --add bot,python >/dev/null
uv run --quiet "$CLI" record --project "$PROJECT" \
  --title "Postgres for state" --scope architecture --topic persistence \
  --chose postgres --rejected sqlite --rationale "concurrent writers" >/dev/null

fail=0
badge() {  # <dir> — statusline output for a session in <dir>
  printf '{"workspace":{"current_dir":"%s"}}' "$1" | (cd "$1" && bash "$LINE") || true
}

(cd "$PROJECT" && bash "$HOOK" >/dev/null)
out=$(badge "$PROJECT")
if printf '%s' "$out" | grep -q 'PRECEDENT'; then
  echo "ok: primed directory renders the badge"
else
  echo "FAIL: hook primed $PROJECT but the statusline rendered nothing" >&2
  fail=1
fi

# The payload is the authority, not the working directory: the statusline is
# invoked for a session whose directory need not be the renderer's own.
out=$(cd "$QUIET" && printf '{"workspace":{"current_dir":"%s"}}' "$PROJECT" | bash "$LINE")
if printf '%s' "$out" | grep -q 'PRECEDENT'; then
  echo "ok: the badge follows current_dir from the payload"
else
  echo "FAIL: statusline ignored current_dir and used its own cwd" >&2
  fail=1
fi

# A directory with nothing to say must not carry a badge, and a later quiet
# session must clear one an earlier session left.
(cd "$QUIET" && bash "$HOOK" >/dev/null)
if [ -z "$(badge "$QUIET")" ]; then
  echo "ok: unprimed directory renders nothing"
else
  echo "FAIL: $QUIET has no decisions but the statusline rendered a badge" >&2
  fail=1
fi

uv run --quiet "$CLI" --home "$PRECEDENT_HOME" cypher \
  "MATCH (d:Decision) DETACH DELETE d" >/dev/null 2>&1 || true
(cd "$PROJECT" && bash "$HOOK" >/dev/null)
if [ -z "$(badge "$PROJECT")" ]; then
  echo "ok: a session with nothing to say clears the previous badge"
else
  echo "FAIL: badge survived a session where the hook printed nothing" >&2
  fail=1
fi

exit "$fail"
