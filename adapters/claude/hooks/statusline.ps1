# Statusline badge: renders [PRECEDENT] while the graph is speaking here.
# Twin of statusline.sh; both must stay behaviourally identical.
#
# Not a hook — Claude Code's `statusLine` is a settings.json field, so this is
# chained into the user's own statusline command (see README). It prints a
# badge only when session-start.ps1 left a marker for this directory, and the
# marker's NAME carries everything: its contents are never read, so a planted
# file cannot push terminal escapes through this script.
$ErrorActionPreference = 'SilentlyContinue'

# The statusline payload arrives as JSON on stdin. `workspace.current_dir` is
# the directory the badge is about; $PWD is the fallback for a caller that
# sends nothing. Matched with a regex rather than ConvertFrom-Json because
# this runs on every render.
$payload = [Console]::In.ReadToEnd()
$dir = $null
if ($payload -match '"current_dir"\s*:\s*"([^"]*)"') { $dir = $Matches[1] }
if (-not $dir) { $dir = $PWD.Path }
# JSON escapes a Windows path's separators; the hook keys the marker off the
# raw path, so undo that before deriving the same key.
$dir = $dir -replace '\\\\', '\'

$root = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$mark = Join-Path (Join-Path $root '.precedent-primed') ($dir -replace '[^A-Za-z0-9-]', '_')
if (-not (Test-Path -LiteralPath $mark -PathType Leaf)) { exit 0 }

# [char]27 rather than `e: the escape sequence is PowerShell 6+, and Claude
# Code falls back to Windows PowerShell 5.1 when pwsh 7 is missing.
$e = [char]27
[Console]::Write("$e[38;5;110m[PRECEDENT]$e[0m")
