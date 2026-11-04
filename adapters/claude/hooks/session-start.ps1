# SessionStart hook: prime the session with the user's recorded decisions.
# The brief only when the graph has something relevant — an empty brief is
# noise. The standing orders, which carry every principle, always.
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

# The statusline badge (hooks/statusline.ps1) renders off a marker named after
# the directory this hook primed. Dropping it first and writing it back only
# on a successful, non-empty brief means every early exit below — no CLI, no
# uv, nothing to say — also clears a badge left by an earlier session.
$markRoot = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$markDir = Join-Path $markRoot '.precedent-primed'
$mark = Join-Path $markDir ($PWD.Path -replace '[^A-Za-z0-9-]', '_')
# A subagent (the SubagentStart entry passes --subagent) is not a session:
# its run must leave the badge as the session left it.
$subagent = $args -contains '--subagent'
if (-not $subagent) { Remove-Item -LiteralPath $mark -Force -ErrorAction SilentlyContinue }

# Resolve the CLI relative to this script, not an assumed install path. Two
# channels lay it out differently — see the same block in session-start.sh:
#
#   repository / Claude Code plugin  adapters/claude/hooks/  -> ..\skills\precedent\scripts\
#   acr realize                      .claude\hooks\<pkg>\    -> ..\..\skills\acr__<owner>__<repo>__precedent\scripts\
#
# The ACR directory carries the package name, so its sibling is discovered
# rather than spelled out. Get-ChildItem -Directory is PowerShell 3.0+, so it
# is available under the 5.1 fallback this script still has to run on.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(Join-Path $here '..\skills\precedent\scripts\precedent.py')
$candidates += Get-ChildItem -Path (Join-Path $here '..\..\skills') -Directory `
    -Filter '*precedent' -ErrorAction SilentlyContinue | ForEach-Object {
        Join-Path $_.FullName 'scripts\precedent.py'
    }
$dg = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $dg) { exit 0 }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { exit 0 }

# A failure still exits 0 but says so in one line, as in session-start.sh:
# printing nothing made a broken store look like an empty one (issue #33).
# This twin has no timeout guard, so there is no timed-out case to report.
$errFile = [System.IO.Path]::GetTempFileName()
function Unavailable {
    $reason = Get-Content -LiteralPath $errFile -ErrorAction SilentlyContinue |
        Where-Object { $_.Trim() } | Select-Object -First 1
    if (-not $reason) { $reason = "exit status $LASTEXITCODE" }
    "PRECEDENT unavailable: $reason — rerun: uv run `"$dg`" brief --project ."
}

# SubagentStart: the CLI reads the hook's JSON from stdin and answers in the
# hook JSON that event requires. A failure before Python answers gets the
# same notice, as JSON, exactly as session-start.sh --subagent does.
if ($subagent) {
    $hookInput = [Console]::In.ReadToEnd()
    $out = ($hookInput | & uv run --quiet $dg subagent-start 2>$errFile) -join "`n"
    if ($LASTEXITCODE -eq 0) {
        if ($out) { $out }
    } else {
        @{ hookSpecificOutput = @{ hookEventName = 'SubagentStart'
                                   additionalContext = (Unavailable) } } | ConvertTo-Json -Compress
    }
    Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
    exit 0
}
$notice = $false
$brief = (& uv run --quiet $dg brief --project $PWD --only-if-relevant 2>$errFile) -join "`n"
if ($LASTEXITCODE -ne 0) { $brief = Unavailable; $notice = $true }

# An empty file: the statusline reads only the name, never the contents.
# Only a real brief lights the badge; orders alone are not a primed project.
if (-not [string]::IsNullOrWhiteSpace($brief)) {
    if (-not $notice) {
        New-Item -ItemType Directory -Path $markDir -Force -ErrorAction SilentlyContinue | Out-Null
        New-Item -ItemType File -Path $mark -Force -ErrorAction SilentlyContinue | Out-Null
    }
    $brief += "`n`n"
} else {
    $brief = ''
}

$orders = (& uv run --quiet $dg standing-orders 2>$errFile) -join "`n"
if ($LASTEXITCODE -ne 0) { $orders = Unavailable }
Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue

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

$brief$orders
Full guidance: $skill
"@
