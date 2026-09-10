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

# Resolve the CLI relative to this script, not an assumed install path. Two
# channels lay it out differently — see the same block in session-start.sh:
#
#   repository / Claude Code plugin  adapters/claude/hooks/  -> ..\..\..\scripts\
#   acr realize                      .claude\hooks\<pkg>\    -> ..\..\scripts\<pkg>\
#
# The ACR directory carries the package name, so its sibling is discovered
# rather than spelled out. Get-ChildItem -Directory is PowerShell 3.0+, so it
# is available under the 5.1 fallback this script still has to run on.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(Join-Path $here '..\..\..\scripts\precedent.py')
$candidates += Get-ChildItem -Path (Join-Path $here '..\..\scripts') -Directory `
    -ErrorAction SilentlyContinue | ForEach-Object {
        Join-Path $_.FullName 'precedent.py'
    }
$dg = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $dg) { exit 0 }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { exit 0 }

$brief = (& uv run --quiet $dg brief --project $PWD --only-if-relevant 2>$null) -join "`n"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($brief)) { exit 0 }

$orders = (& uv run --quiet $dg standing-orders 2>$null) -join "`n"
if ($LASTEXITCODE -ne 0) { exit 0 }

# Same two layouts as the CLI lookup above: a sibling skills directory in the
# repository, or the agent's own skills root when acr realize placed it.
$skillCandidates = @(Join-Path $here '..\skills\precedent\SKILL.md')
$skillCandidates += Get-ChildItem -Path (Join-Path $here '..\..\skills') -Directory `
    -Filter '*precedent' -ErrorAction SilentlyContinue | ForEach-Object {
        Join-Path $_.FullName 'SKILL.md'
    }
$skill = $skillCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
@"
PRECEDENT — your recorded decisions, loaded for this session.

$brief

$orders
Full guidance: $skill
"@
