#!/usr/bin/env bash
# Statusline badge: renders [PRECEDENT] while the graph is speaking here.
#
# Not a hook. Claude Code has no plugin-provided statusline — `statusLine` is
# a settings.json field — so this is chained into the user's own statusline
# command (see README). It reads a marker the SessionStart hook drops for the
# directory it primed, and prints nothing when there is no marker: a session
# where the graph had nothing to say gets no badge.
#
# It runs on every render, so it forks nothing and shells out to nothing. The
# marker's NAME carries all the information; its contents are never read, so
# a planted file cannot push terminal escapes through this script.
set -uo pipefail

# The statusline payload arrives as JSON on stdin. `workspace.current_dir` is
# the directory the badge is about; $PWD is the fallback for a caller that
# sends nothing. Matched with bash pattern operators rather than sed/jq to
# keep this fork-free.
payload=$(cat 2>/dev/null || true)
case $payload in
  *'"current_dir":"'*) dir=${payload##*'"current_dir":"'}; dir=${dir%%'"'*} ;;
  *) dir=$PWD ;;
esac
# JSON escapes a Windows path's separators; the hook keys the marker off the
# raw path, so undo that before deriving the same key.
dir=${dir//\\\\/\\}

MARK="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.precedent-primed/${dir//[^A-Za-z0-9-]/_}"
[ -L "$MARK" ] && exit 0
[ -f "$MARK" ] || exit 0

printf '\033[38;5;110m[PRECEDENT]\033[0m'
