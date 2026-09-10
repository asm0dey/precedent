#!/usr/bin/env bash
# The session-start hook must locate scripts/precedent.py in every layout an
# install channel produces. It fails closed (`exit 0`, no output) when it
# cannot, so a hook wired to the wrong path is indistinguishable from a quiet
# session unless a test asserts the brief actually appears. That is what this
# checks: seed a store with a decision, then run the hook from each layout and
# require the banner.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
CLI="$ROOT/scripts/precedent.py"
HOOK="$ROOT/adapters/claude/hooks/session-start.sh"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

export PRECEDENT_HOME="$WORK/store"
PROJECT="$WORK/proj"
mkdir -p "$PROJECT"

uv run --quiet "$CLI" tag --project "$PROJECT" --add bot,python >/dev/null
uv run --quiet "$CLI" record --project "$PROJECT" \
  --title "Postgres for state" --scope architecture --topic persistence \
  --chose postgres --rejected sqlite --rationale "concurrent writers" >/dev/null

fail=0
assert_primes() {  # <layout name> <path to hook>
  local name=$1 hook=$2 out
  out=$(cd "$PROJECT" && bash "$hook") || true
  if [ -z "$out" ]; then
    echo "FAIL: $name layout — hook printed nothing; it did not find the CLI" >&2
    fail=1
  elif ! printf '%s' "$out" | grep -q "PRECEDENT"; then
    echo "FAIL: $name layout — hook output carries no banner:" >&2
    printf '%s\n' "$out" >&2
    fail=1
  else
    echo "ok: $name layout primes"
  fi
}

# Layout 1: the repository, which the Claude Code plugin channel ships as-is.
assert_primes repository "$HOOK"

# Layout 2: what `acr realize` writes. The package name is part of both
# directory names, so the hook cannot assume a fixed sibling — this uses a
# different package name than the real one on purpose.
ACR="$WORK/acr-project/.claude"
mkdir -p "$ACR/hooks/acr__someone__pkg__session-start" \
         "$ACR/scripts/acr__someone__pkg__precedent-cli"
cp "$HOOK" "$ACR/hooks/acr__someone__pkg__session-start/"
cp "$CLI" "$ACR/scripts/acr__someone__pkg__precedent-cli/"
assert_primes acr-realized \
  "$ACR/hooks/acr__someone__pkg__session-start/session-start.sh"

exit "$fail"
