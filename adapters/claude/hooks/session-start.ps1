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
