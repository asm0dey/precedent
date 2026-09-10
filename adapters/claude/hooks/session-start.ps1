# SessionStart hook: prime the session with the user's recorded decisions.
# Silent unless the graph has something relevant — an empty brief is noise.
# Twin of session-start.sh; both must stay behaviourally identical.
#
# hooks.json cannot gate a hook entry by platform (there is no such field in
# Claude Code's hook schema), so this entry is invoked on every OS via the
# cross-platform `pwsh` binary — including POSIX boxes that happen to have
# pwsh installed. $IsWindows is a PowerShell 6+ automatic variable; it does
# not exist under Windows PowerShell 5.1 (Claude Code's own fallback when
# pwsh 7 is missing), where an undefined variable reads as $null. `-not $null`
# is $true, so `if (-not $IsWindows)` would exit on 5.1 too, disabling the
# hook on the default Windows configuration. `$IsWindows -eq $false` avoids
# that: `$null -eq $false` is $false, so 5.1 falls through and primes, while
# PS 6+ on POSIX (where $IsWindows is actually $false) still exits. Do not
# "simplify" this back to `-not $IsWindows`.
if ($IsWindows -eq $false) { exit 0 }

$ErrorActionPreference = 'SilentlyContinue'

# Resolve the CLI relative to this script, not an assumed install path.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dg = Join-Path $here '..\..\..\scripts\precedent.py'
if (-not (Test-Path $dg)) { exit 0 }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { exit 0 }

$brief = (& uv run --quiet $dg brief --project $PWD --only-if-relevant 2>$null) -join "`n"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($brief)) { exit 0 }

$orders = (& uv run --quiet $dg standing-orders 2>$null) -join "`n"
if ($LASTEXITCODE -ne 0) { exit 0 }

$skill = Join-Path $here '..\SKILL.md'
@"
PRECEDENT — your recorded decisions, loaded for this session.

$brief

$orders
Full guidance: $skill
"@
