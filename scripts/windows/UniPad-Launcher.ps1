<#
.SYNOPSIS
    Concise operator launcher for UniPad on this Intel Windows host
    (PRD 3.2 P1 bullet 6): start, stop, import IPA, view logs, restore.

.DESCRIPTION
    A single surface over the five day-to-day operations.  It is deliberately
    thin: it does NOT reimplement what the dedicated entry scripts already own.

      * Start delegates to Start-UniPad.ps1 (same directory), which owns the
        TCG-only guard and the controller-verdict classification; this script
        only adds the non-ASCII pre-flight guard and forwards the interpreter /
        experiment id.
      * Stop / Install / Restore invoke the controller directly
        (`python -m unipad.controller ...`, with the direct-module fallback the
        other entry scripts use) and relay its JSON payload and exit code.
      * Logs reads the run's serial log from disk (no guest involved) and, with
        -Follow, tails it like `tail -f`, stopping cleanly on Ctrl-C.  Both forms
        show the same bounded tail from ONE constant ($script:LogTailLines) and
        declare the truncation on stderr, so an unbounded guest log never reaches
        stdout unannounced.

    The repository root is resolved from THIS script's location, never from the
    caller's working directory, so a double-click from anywhere lands in the
    right runs/, artifacts/ and config/ tree.

    Non-ASCII pre-flight guard (the single most important robustness feature):
    the Windows QEMU build resolves paths with the ANSI code page and fails
    ~30s later with an opaque "open <path> failed" when a path contains a
    non-ASCII character (this host's user directory is non-ASCII).  Every path
    this launcher would hand to QEMU-derived tooling -- the repository root,
    the runs/artifacts roots and the operator-supplied artifact / IPA /
    checkpoint / serial log -- is checked up front and refused with an
    actionable message (config_invalid, exit 2) before anything is launched.

    Success is never asserted by this script: the controller's process exit
    code is relayed unchanged, and a payload that reports an error is never
    resolved in favour of success (mirrors Start-UniPad.ps1).

    Exit codes mirror unipad.errors.ExitCode:
      0 ok | 1 failure | 2 usage (bad -Action, missing/misplaced arg,
      non-ASCII path) | 3 blocked (no interpreter / no run to stop).

.PARAMETER Action
    One of: start, stop, install, logs, restore (case-insensitive).

.PARAMETER Artifact
    start: artifact manifest produced by the macOS build (PRD 8.1).

.PARAMETER Ipa
    install: path to the .ipa to import (FR06).

.PARAMETER Checkpoint
    restore: checkpoint id under the artifacts root (the controller joins it).

.PARAMETER Run
    stop / restore / logs: run id (stop/restore default to latest; logs too).

.PARAMETER Follow
    logs: follow the serial log like `tail -f`.

.PARAMETER WhatIf
    Print the resolved plan as one JSON object and launch nothing.

.EXAMPLE
    powershell -NoProfile -File scripts\windows\UniPad-Launcher.ps1 -Action start -Artifact artifacts\manifest.json

.EXAMPLE
    scripts\launcher.cmd logs -Run exp-001 -Follow

.NOTES
    PowerShell 5.1+ compatible.  ASCII-only on purpose: powershell.exe
    mis-decodes BOM-less UTF-8 files that contain non-ASCII text.
#>
[CmdletBinding()]
param(
    [string]$Action,
    # start (delegated to Start-UniPad.ps1)
    [string]$Artifact,
    [string]$Profile = 'tcg',
    [string]$ExperimentId,
    # install
    [string]$Ipa,
    # stop
    [switch]$Graceful,
    [switch]$Force,
    # logs / stop / restore
    [string]$Run,
    [switch]$Follow,
    # restore
    [string]$Checkpoint,
    # roots (default to the repository's own layout)
    [string]$RunsRoot,
    [string]$ArtifactsRoot,
    # dry run
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Exit codes mirror unipad.errors.ExitCode (see Invoke-HostInventory.ps1).
# ---------------------------------------------------------------------------
$script:ExitOk = 0
$script:ExitFailure = 1
$script:ExitUsage = 2
$script:ExitBlocked = 3

# How much of the serial log `logs` shows.  ONE constant, used by both the plain
# read and -Follow: when the bound was spelled literally at each Get-Content the
# two drifted (plain dumped the whole file, -Follow tailed 100) and the operator
# could not tell which verb they were looking at.
$script:LogTailLines = 100

# Why a non-ASCII path cannot be used (mirrors unipad.host.qemu).
$NON_ASCII_REASON = 'The Windows QEMU build resolves file paths with the ANSI code page. Put the run directory on an ASCII-only path; on this host %TEMP% is under a non-ASCII user directory.'

# ---------------------------------------------------------------------------
# Helpers (duplicated per entry script; see Invoke-HostInventory.ps1).
# ---------------------------------------------------------------------------

function Test-UniPadInterpreter {
    <#
    .SYNOPSIS
        True when the interpreter can actually `import unipad`.
    .DESCRIPTION
        Resolving on PATH is not enough: the unipad import shim is installed
        per interpreter, and on this host `py` is 3.14 with no shim while
        `python` is 3.12.9 with one.  Probing the import here is what turns a
        wrong pick into either a working interpreter or a clear `blocked`,
        instead of an opaque "No module named unipad" thirty seconds later.
    #>
    param([Parameter(Mandatory = $true)][string]$Exe)
    $probe = Invoke-NativeCapture -FilePath $Exe -ArgumentList @('-c', 'import unipad')
    return ($probe.ExitCode -eq 0)
}

function Get-ConfiguredPython {
    <#
    .SYNOPSIS
        Interpreter used to reach the unipad package.
    .DESCRIPTION
        $env:UNIPAD_PYTHON wins when set; otherwise `python` is preferred and
        `py` is the fallback (there is no `python3` on this host), and each
        candidate must be able to `import unipad`.  Returns '' when none
        resolves, so the caller reports `blocked` rather than guessing.
    #>
    $override = $env:UNIPAD_PYTHON
    if ($override -and $override.Trim() -ne '') { return $override.Trim() }
    foreach ($candidate in @('python', 'py')) {
        if ((Test-CommandAvailable -Name $candidate) -and (Test-UniPadInterpreter -Exe $candidate)) {
            return $candidate
        }
    }
    return ''
}

function Test-CommandAvailable {
    <#
    .SYNOPSIS
        True when a command name resolves on PATH (or is a full path).
    #>
    param([Parameter(Mandatory = $true)][string]$Name)
    return ($null -ne (Get-Command -Name $Name -ErrorAction SilentlyContinue))
}

function Test-UniPadAsciiPath {
    <#
    .SYNOPSIS
        True when a path contains only ASCII characters (QEMU-openable).
    #>
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Path)
    if ([string]::IsNullOrEmpty($Path)) { return $true }
    foreach ($ch in $Path.ToCharArray()) {
        if ([int]$ch -gt 127) { return $false }
    }
    return $true
}

function ConvertTo-Text {
    <#
    .SYNOPSIS
        Flatten native-command output (string, string[] or $null) into text.
    #>
    param([Parameter(ValueFromPipeline = $true)][AllowNull()]$InputObject)
    if ($null -eq $InputObject) { return '' }
    if ($InputObject -is [System.Array]) {
        $parts = @()
        foreach ($item in $InputObject) { $parts += [string]$item }
        return ($parts -join "`n")
    }
    return [string]$InputObject
}

function ConvertTo-JsonString {
    <#
    .SYNOPSIS
        Escape a string for embedding in a JSON document (RFC 8259).
    #>
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $Text.ToCharArray()) {
        $code = [int]$ch
        if ($ch -eq '"') { [void]$sb.Append('\"') }
        elseif ($ch -eq '\') { [void]$sb.Append('\\') }
        elseif ($ch -eq "`n") { [void]$sb.Append('\n') }
        elseif ($ch -eq "`r") { [void]$sb.Append('\r') }
        elseif ($ch -eq "`t") { [void]$sb.Append('\t') }
        elseif ($code -lt 32) { [void]$sb.Append(('\u{0:x4}' -f $code)) }
        else { [void]$sb.Append($ch) }
    }
    return $sb.ToString()
}

function Write-StructuredError {
    <#
    .SYNOPSIS
        Print one machine-readable error object on stderr (never a bare string).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Message,
        [hashtable]$Details = @{}
    )
    $pairs = @()
    foreach ($key in ($Details.Keys | Sort-Object)) {
        $pairs += ('"{0}":"{1}"' -f (ConvertTo-JsonString -Text ([string]$key)), (ConvertTo-JsonString -Text ([string]$Details[$key])))
    }
    $detailJson = '{' + ($pairs -join ',') + '}'
    $line = '{"error":{"code":"' + (ConvertTo-JsonString -Text $Code) +
            '","message":"' + (ConvertTo-JsonString -Text $Message) +
            '","details":' + $detailJson + '}}'
    [Console]::Error.WriteLine($line)
}

function Stop-UniPadScript {
    <#
    .SYNOPSIS
        Emit a structured error and terminate this script with a fixed code.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Message,
        [Parameter(Mandatory = $true)][int]$ExitCode,
        [hashtable]$Details = @{}
    )
    Write-StructuredError -Code $Code -Message $Message -Details $Details
    exit $ExitCode
}

function Get-ExitCodeForErrorCode {
    <#
    .SYNOPSIS
        Mirror of unipad.errors._CODE_TO_EXIT, so scripts and CLI agree.
    .DESCRIPTION
        Every code this launcher emits must be resolvable here, and a caller
        must be able to derive the process exit code from the code alone.  The
        codes are therefore never spelled next to a hand-picked exit code:
        callers ask this function, which is what keeps the two from drifting.
    #>
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Code)
    switch ($Code) {
        'blocked' { return $script:ExitBlocked }
        'budget_exceeded' { return $script:ExitBlocked }
        'config_invalid' { return $script:ExitUsage }
        'unsupported_command' { return $script:ExitUsage }
        'experiment_state_invalid' { return $script:ExitUsage }
        # The controller's own catch-all code for an exception no handler
        # claimed (unipad/controller/cli.py); a launcher-side unexpected fault
        # is the same class of thing and must not be called `blocked`, which
        # means the operator is missing material and exits 3.
        'internal_error' { return $script:ExitFailure }
        default { return $script:ExitFailure }
    }
}

function ConvertTo-NativeArgumentString {
    <#
    .SYNOPSIS
        Build a Windows command line from an argument vector (no shell involved).
    #>
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments
    )
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($arg in $Arguments) {
        $text = [string]$arg
        if ($text.Length -eq 0) { $parts.Add('""'); continue }
        if ($text -notmatch '[\s"]') { $parts.Add($text); continue }
        $sb = New-Object System.Text.StringBuilder
        [void]$sb.Append('"')
        $backslashes = 0
        foreach ($ch in $text.ToCharArray()) {
            if ($ch -eq '\') { $backslashes++; continue }
            if ($ch -eq '"') {
                for ($i = 0; $i -lt ($backslashes * 2); $i++) { [void]$sb.Append('\') }
                [void]$sb.Append('\"')
                $backslashes = 0
                continue
            }
            for ($i = 0; $i -lt $backslashes; $i++) { [void]$sb.Append('\') }
            $backslashes = 0
            [void]$sb.Append($ch)
        }
        for ($i = 0; $i -lt ($backslashes * 2); $i++) { [void]$sb.Append('\') }
        [void]$sb.Append('"')
        $parts.Add($sb.ToString())
    }
    return ($parts -join ' ')
}

function Invoke-NativeCapture {
    <#
    .SYNOPSIS
        Run a native command, capturing stdout, stderr and the exit code.
    .DESCRIPTION
        System.Diagnostics.Process is used instead of the call operator so a
        native command writing to stderr does not abort the script under
        $ErrorActionPreference='Stop'.  Streams are decoded as UTF-8 because the
        controller emits UTF-8 JSON (ensure_ascii=False) and the zh-CN console
        code page would otherwise corrupt it.
    .OUTPUTS
        PSCustomObject: ExitCode, StdOut, StdErr.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$ArgumentList
    )
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (ConvertTo-NativeArgumentString -Arguments $ArgumentList)
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $startInfo.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $startInfo.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $startInfo.WorkingDirectory = (Get-Location).Path
    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return [PSCustomObject]@{ ExitCode = $process.ExitCode; StdOut = $stdout; StdErr = $stderr }
    }
    finally {
        $process.Dispose()
    }
}

function Get-ControllerFallbackSnippet {
    <#
    .SYNOPSIS
        Direct-module fallback used when `-m unipad.controller` is not importable.
    #>
    return 'import json, os, sys; sys.argv = ["unipad"] + json.loads(os.environ["UNIPAD_ARGV"]); from unipad.controller.cli import main; raise SystemExit(main())'
}

function Invoke-UniPadController {
    <#
    .SYNOPSIS
        Run the unipad controller CLI, with a direct-module fallback.
    .OUTPUTS
        PSCustomObject: ExitCode, StdOut, StdErr, Invocation, UsedFallback.
    #>
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $python = Get-ConfiguredPython
    $primary = Invoke-NativeCapture -FilePath $python -ArgumentList (@('-m', 'unipad.controller') + $Arguments)
    $invocation = ('{0} -m unipad.controller {1}' -f $python, ($Arguments -join ' '))

    if ($primary.ExitCode -ne 0) {
        $combined = ($primary.StdOut + "`n" + $primary.StdErr)
        if ($combined -match 'No module named') {
            $argvParts = @()
            foreach ($arg in $Arguments) { $argvParts += (ConvertTo-JsonString -Text ([string]$arg)) }
            $previousArgv = $env:UNIPAD_ARGV
            $env:UNIPAD_ARGV = ('[' + ($argvParts -join ',') + ']')
            try {
                $fallback = Invoke-NativeCapture -FilePath $python -ArgumentList @('-c', (Get-ControllerFallbackSnippet))
            }
            finally {
                if ($null -eq $previousArgv) { Remove-Item Env:UNIPAD_ARGV -ErrorAction SilentlyContinue }
                else { $env:UNIPAD_ARGV = $previousArgv }
            }
            $invocation = ('{0} -c <unipad.controller.cli:main> {1}' -f $python, ($Arguments -join ' '))
            return [PSCustomObject]@{
                ExitCode = $fallback.ExitCode; StdOut = $fallback.StdOut; StdErr = $fallback.StdErr
                Invocation = $invocation; UsedFallback = $true
            }
        }
    }

    return [PSCustomObject]@{
        ExitCode = $primary.ExitCode; StdOut = $primary.StdOut; StdErr = $primary.StdErr
        Invocation = $invocation; UsedFallback = $false
    }
}

function Get-JsonPayload {
    <#
    .SYNOPSIS
        Extract and parse the JSON object emitted by the controller.
    #>
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    $trimmed = $Text.Trim()
    if ($trimmed -eq '') { return $null }
    try {
        $candidate = $trimmed | ConvertFrom-Json
        if ($null -ne $candidate -and -not ($candidate -is [string]) -and -not ($candidate -is [System.Array])) { return $candidate }
    }
    catch {
        # fall through to the span retry below
    }
    $first = $trimmed.IndexOf('{')
    $last = $trimmed.LastIndexOf('}')
    if ($first -ge 0 -and $last -gt $first) {
        try {
            $candidate = $trimmed.Substring($first, $last - $first + 1) | ConvertFrom-Json
            if ($null -ne $candidate -and -not ($candidate -is [string]) -and -not ($candidate -is [System.Array])) { return $candidate }
        }
        catch {
            return $null
        }
    }
    return $null
}

function Test-PayloadError {
    <#
    .SYNOPSIS
        True when a controller payload actually reports an error (non-null).
    #>
    param([Parameter(Mandatory = $true)]$Payload)
    $names = @($Payload.PSObject.Properties.Name)
    if (-not ($names -contains 'error')) { return $false }
    $error = $Payload.error
    if ($null -eq $error) { return $false }
    if ($error -is [string]) { return ($error.Trim() -ne '') }
    return $true
}

function Get-ControllerVerdict {
    <#
    .SYNOPSIS
        Classify a controller payload as success / failure / indeterminate.
    .DESCRIPTION
        Only a payload that actually says so counts as success: no key, or an
        unrecognised value, yields 'indeterminate' and the caller must not
        report success.
    #>
    param([Parameter(Mandatory = $true)]$Payload)
    if (Test-PayloadError -Payload $Payload) { return 'failure' }
    $names = @($Payload.PSObject.Properties.Name)

    foreach ($key in @('ok', 'success')) {
        if ($names -contains $key) {
            $value = $Payload.$key
            if ($value -is [bool]) { if ($value) { return 'success' } else { return 'failure' } }
        }
    }
    $positive = @('ok', 'passed', 'pass', 'success', 'succeeded', 'started', 'running', 'accepted', 'complete', 'completed', 'installed', 'registered')
    $negative = @('failed', 'failure', 'error', 'blocked', 'rejected', 'cancelled', 'canceled')
    foreach ($key in @('status', 'result', 'state', 'stage', 'phase')) {
        if ($names -contains $key) {
            $value = [string]$Payload.$key
            if ($value -ne '') {
                $lower = $value.ToLowerInvariant()
                if ($positive -contains $lower) { return 'success' }
                if ($negative -contains $lower) { return 'failure' }
            }
        }
    }
    return 'indeterminate'
}

function Write-Note {
    <#
    .SYNOPSIS
        Human-readable note.  Always stderr, so stdout stays the verbatim CLI
        JSON a caller may pipe straight into a parser.
    .DESCRIPTION
        Start-UniPad.ps1's Write-Note switches on -Json; this launcher has no
        such switch, and every action writes either the controller's JSON
        envelope or the verbatim serial log to stdout, so a note there would
        corrupt both.  Stderr unconditionally is the deliberate difference.
    #>
    param([Parameter(Mandatory = $true)][string]$Text)
    [Console]::Error.WriteLine($Text)
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
try {
    # -- repository root from THIS script's location, never from $PWD --------
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'pyproject.toml'))) {
        Stop-UniPadScript -Code 'config_invalid' `
            -Message 'repository root could not be resolved from the script location' `
            -ExitCode $script:ExitUsage `
            -Details @{ script_root = $PSScriptRoot }
    }

    # -- validate the action and its parameter combination ------------------
    $allowed = [ordered]@{
        start   = @('Artifact', 'Profile', 'ExperimentId')
        stop    = @('Graceful', 'Force', 'Run')
        install = @('Ipa', 'ExperimentId')
        logs    = @('Run', 'Follow')
        restore = @('Checkpoint', 'Run')
    }
    $globalParams = @('Action', 'WhatIf', 'RunsRoot', 'ArtifactsRoot')
    $required = @{ start = 'Artifact'; install = 'Ipa'; restore = 'Checkpoint' }
    $act = ("$Action").Trim().ToLowerInvariant()

    if ([string]::IsNullOrWhiteSpace($act)) {
        Stop-UniPadScript -Code 'config_invalid' -Message ('missing -Action; valid: ' + ($allowed.Keys -join ', ')) -ExitCode $script:ExitUsage
    }
    if (-not $allowed.Contains($act)) {
        Stop-UniPadScript -Code 'unsupported_command' -Message ('unknown -Action ''' + $Action + '''; valid: ' + ($allowed.Keys -join ', ')) -ExitCode $script:ExitUsage
    }
    $permitted = $allowed[$act] + $globalParams
    foreach ($key in $PSBoundParameters.Keys) {
        if ($permitted -notcontains $key) {
            Stop-UniPadScript -Code 'config_invalid' -Message ('-' + $key + ' is not valid for -Action ' + $act) -ExitCode $script:ExitUsage
        }
    }
    if ($required.Contains($act) -and -not $PSBoundParameters.ContainsKey($required[$act])) {
        Stop-UniPadScript -Code 'config_invalid' -Message ('-Action ' + $act + ' requires -' + $required[$act]) -ExitCode $script:ExitUsage
    }
    if ($act -eq 'stop' -and $PSBoundParameters.ContainsKey('Graceful') -and $PSBoundParameters.ContainsKey('Force')) {
        Stop-UniPadScript -Code 'config_invalid' -Message '-Action stop takes -Graceful or -Force, not both' -ExitCode $script:ExitUsage
    }

    # -- resolve paths against the caller's cwd (before we change directory) -
    $callerCwd = (Get-Location).Path
    $runsRoot      = if ($RunsRoot) { [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($callerCwd, $RunsRoot)) } else { Join-Path $repoRoot 'runs' }
    $artifactsRoot = if ($ArtifactsRoot) { [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($callerCwd, $ArtifactsRoot)) } else { Join-Path $repoRoot 'artifacts' }
    $artifactAbs   = if ($Artifact) { [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($callerCwd, $Artifact)) } else { '' }
    $ipaAbs        = if ($Ipa) { [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($callerCwd, $Ipa)) } else { '' }
    # --checkpoint is an *id* the controller joins to --artifacts-root itself.
    $checkpointDir = if ($act -eq 'restore') { Join-Path $artifactsRoot $Checkpoint } else { '' }

    # logs: pick the run now so its serial log can be guarded and shown.
    $runId = ''
    $serialLog = ''
    if ($act -eq 'logs') {
        if ($Run) {
            $runId = $Run
        }
        else {
            # Mirror the controller's own rule for "the latest run"
            # (unipad/controller/cli.py::_latest_run): only directories that
            # carry an experiment.json journal are runs at all, and they are
            # ranked by the journal's mtime with a deterministic tie-break on
            # the id.  Ranking *every* directory here would let a book-keeping
            # directory (runs/lifecycle, which holds no journal) win, and logs
            # would then fail with artifact_missing while the controller this
            # launcher fronts happily acted on a different, real run.
            $latest = $null
            if (Test-Path -LiteralPath $runsRoot) {
                $latest = Get-ChildItem -LiteralPath $runsRoot -Directory | ForEach-Object {
                    $journal = Get-Item -LiteralPath (Join-Path $_.FullName 'experiment.json') -ErrorAction SilentlyContinue
                    if ($null -ne $journal -and -not $journal.PSIsContainer) {
                        [PSCustomObject]@{
                            Name      = $_.Name
                            JournalAt = $journal.LastWriteTime
                        }
                    }
                } | Sort-Object -Property @{ Expression = 'JournalAt' }, @{ Expression = 'Name' } -Descending | Select-Object -First 1
            }
            $runId = if ($latest) { $latest.Name } else { '' }
        }
        $serialLog = if ($runId) { Join-Path (Join-Path $runsRoot $runId) 'serial.log' } else { '' }
    }

    # -- non-ASCII pre-flight guard (before the interpreter, before QEMU) ----
    $guarded = New-Object System.Collections.Generic.List[string]
    $guardTargets = @(
        @{ path = $repoRoot;      label = 'repository root' }
        @{ path = $runsRoot;      label = 'runs root' }
        @{ path = $artifactsRoot; label = 'artifacts root' }
    )
    if ($act -eq 'start')   { $guardTargets += @{ path = $artifactAbs;   label = 'artifact manifest' } }
    if ($act -eq 'install') { $guardTargets += @{ path = $ipaAbs;        label = 'IPA' } }
    if ($act -eq 'restore') { $guardTargets += @{ path = $checkpointDir; label = 'checkpoint' } }
    # --run is an id the controller joins to --runs-root itself, so a non-ASCII
    # run id would put QEMU on a path it cannot open just the same.
    if ($Run -and ($act -eq 'stop' -or $act -eq 'restore')) { $guardTargets += @{ path = (Join-Path $runsRoot $Run); label = 'run directory' } }
    # -ExperimentId is the same kind of value for the two actions that go on to
    # launch a guest: the controller derives the run directory -- and therefore
    # the serial.log it hands QEMU -- from it (cli.py:986/989 and 1161/1168).
    if ($ExperimentId -and ($act -eq 'start' -or $act -eq 'install')) { $guardTargets += @{ path = (Join-Path $runsRoot $ExperimentId); label = 'experiment id' } }
    if ($act -eq 'logs')    { $guardTargets += @{ path = $serialLog;     label = 'serial log' } }
    foreach ($t in $guardTargets) {
        if (-not [string]::IsNullOrEmpty($t.path)) {
            $guarded.Add($t.path)
            if (-not (Test-UniPadAsciiPath -Path $t.path)) {
                Stop-UniPadScript -Code 'config_invalid' `
                    -Message ($t.label + ' ''' + $t.path + ''' contains a non-ASCII character; QEMU cannot open it.') `
                    -ExitCode $script:ExitUsage `
                    -Details @{ path = $t.path; reason = $NON_ASCII_REASON }
            }
        }
    }

    # -- interpreter probe (reported; required only for controller actions) --
    $python = Get-ConfiguredPython

    # -- build the command vector (empty for logs, which reads the file) -----
    # $command is the FULL argv shown in the -WhatIf plan; $controllerArgs is
    # just the subcommand + options, because Invoke-UniPadController prepends
    # `-m unipad.controller` itself (passing $command would double it).
    $command = New-Object System.Collections.Generic.List[string]
    $controllerArgs = New-Object System.Collections.Generic.List[string]
    $delegatesTo = $null
    if ($act -eq 'start') {
        $delegatesTo = 'Start-UniPad.ps1'
        $command.AddRange([string[]]@('-File', (Join-Path $PSScriptRoot 'Start-UniPad.ps1'), '-Profile', $Profile, '-Artifact', $artifactAbs))
        if ($ExperimentId) { $command.AddRange([string[]]@('-ExperimentId', $ExperimentId)) }
        $command.Add('-Json')
    }
    elseif ($act -ne 'logs') {
        $controllerArgs.Add($act)
        switch ($act) {
            'install' {
                $controllerArgs.Add($ipaAbs)
                if ($ExperimentId) { $controllerArgs.AddRange([string[]]@('--experiment', $ExperimentId)) }
            }
            'stop' {
                if ($Graceful) { $controllerArgs.Add('--graceful') }
                if ($Force) { $controllerArgs.Add('--force') }
                if ($Run) { $controllerArgs.AddRange([string[]]@('--run', $Run)) }
            }
            'restore' {
                $controllerArgs.AddRange([string[]]@('--checkpoint', $Checkpoint))
                if ($Run) { $controllerArgs.AddRange([string[]]@('--run', $Run)) }
            }
        }
        $controllerArgs.AddRange([string[]]@('--runs-root', $runsRoot, '--artifacts-root', $artifactsRoot))
        $command.AddRange([string[]]@('-m', 'unipad.controller'))
        $command.AddRange($controllerArgs)
    }

    # -- -WhatIf: emit the plan as one JSON object and launch nothing --------
    if ($WhatIf) {
        $plan = [ordered]@{
            schema              = 'unipad.launcher.plan/v1'
            what_if             = $true
            action              = $act
            delegates_to        = $delegatesTo
            interpreter         = $python
            repo_root           = $repoRoot
            working_directory   = $repoRoot
            command             = $command.ToArray()
            serial_log          = if ($serialLog) { $serialLog } else { $null }
            follow              = [bool]$Follow
            guarded_paths       = $guarded.ToArray()
        }
        [Console]::Out.WriteLine(($plan | ConvertTo-Json -Depth 5))
        exit $script:ExitOk
    }

    # -- logs: read the serial log from disk; no guest is involved -----------
    if ($act -eq 'logs') {
        if (-not $runId) {
            Stop-UniPadScript -Code 'blocked' -Message ('no runs found under ''' + $runsRoot + '''; nothing to show') -ExitCode $script:ExitBlocked -Details @{ runs_root = $runsRoot }
        }
        # A serial-log path that EXISTS but is not a readable file -- a directory
        # left behind by a crashed run, a permissions change, a half-created path
        # -- used to reach Get-Content and blow up, and the failure was then lost
        # two different ways: on the -Follow path the bare catch that exists for
        # Ctrl-C swallowed it and the operator got exit 0 with no message at all;
        # on the plain path it reached the outer catch, which reports `blocked`
        # (a code Get-ExitCodeForErrorCode maps to exit 3) while exiting 1.  Both
        # are settled here, once, before either path can run.
        if (Test-Path -LiteralPath $serialLog) {
            $serialItem = Get-Item -LiteralPath $serialLog -ErrorAction SilentlyContinue
            if ($null -eq $serialItem -or $serialItem.PSIsContainer) {
                Stop-UniPadScript -Code 'artifact_missing' `
                    -Message ('serial log is not a readable file at ' + $serialLog) `
                    -ExitCode $script:ExitFailure `
                    -Details @{ run = $runId; path = $serialLog }
            }
        }
        if ($Follow) {
            try {
                $announced = $false
                while (-not (Test-Path -LiteralPath $serialLog)) {
                    if (-not $announced) { Write-Note ('waiting for serial log ''' + $serialLog + ''' (Ctrl-C to stop)'); $announced = $true }
                    Start-Sleep -Milliseconds 500
                }
                Get-Content -LiteralPath $serialLog -Wait -Tail $script:LogTailLines
            }
            catch {
                # Ctrl-C stops the follow cleanly; that is not a failure, so the
                # exit code stays 0.  It is no longer *silent*, though: a swallowed
                # error here used to make a stopped follow and a broken follow
                # indistinguishable to the operator.
                #
                # Only the exception *type* is reported, never the raw message --
                # the same line the outer catch takes, for the same two reasons.
                # A .NET message quotes the offending value, which on a host whose
                # working directory is non-ASCII would put the Windows username on
                # stderr (PRD 9.2); and on this host it is a localized Chinese
                # string written in the ANSI code page, so relaying it handed the
                # operator mojibake -- measured, a serial log whose bytes are
                # locked by another process yields
                #   follow ended: System.IO.IOException -- <undecodable GBK bytes>
                # The type alone still tells a stopped follow from a broken one.
                # The exit code cannot branch on it: Ctrl-C is the one case that
                # must stay 0, and this host cannot deliver a console Ctrl-C to a
                # child with redirected stdio, so the Ctrl-C type is unobserved
                # (docs/launcher.md).  Guessing at it would be worse than useless.
                Write-Note ('follow ended: ' + $_.Exception.GetType().FullName +
                            ' (the raw exception message is deliberately not relayed; PRD 9.2)')
            }
            exit $script:ExitOk
        }
        if (-not (Test-Path -LiteralPath $serialLog)) {
            Stop-UniPadScript -Code 'artifact_missing' -Message ('serial log not found for run ''' + $runId + ''' at ''' + $serialLog + '''') -ExitCode $script:ExitFailure -Details @{ run = $runId; path = $serialLog }
        }
        # Same bound as -Follow, from the same constant: the two invocations of
        # one verb must never disagree about how much log the operator sees, and
        # a guest serial log grows for as long as the guest runs.
        Get-Content -LiteralPath $serialLog -Tail $script:LogTailLines
        # Declare the truncation rather than let the operator assume this is all
        # there is (PRD 9.2: no unbounded, unannounced log on stdout).  The total
        # is counted with a streaming reader, so announcing it does not undo the
        # bound the tail just applied.
        $logItem = Get-Item -LiteralPath $serialLog
        # Counting is best-effort on purpose: the log itself has already been
        # printed, and losing the operator's view of it to a failed count would
        # be a far worse outcome than a '?' in an informational note.
        $logLines = '?'
        try { $logLines = [System.Linq.Enumerable]::Count([System.IO.File]::ReadLines($serialLog)) }
        catch { Write-Note ('could not count the lines of ' + $serialLog + ': ' + $_.Exception.GetType().FullName) }
        Write-Note ('showing the last {0} of {1} line(s), {2} byte(s); full log: {3}' -f $script:LogTailLines, $logLines, $logItem.Length, $serialLog)
        $evidenceJson = Join-Path (Join-Path $runsRoot $runId) 'evidence.json'
        if (Test-Path -LiteralPath $evidenceJson) { Write-Note ('evidence bundle: ' + $evidenceJson) }
        exit $script:ExitOk
    }

    # -- start / stop / install / restore: run from the repo root so the
    #    controller's relative defaults (runs/, artifacts/, config/*) resolve.
    Push-Location -LiteralPath $repoRoot
    try {
        if ($act -eq 'start') {
            # Delegate: Start-UniPad.ps1 owns the TCG guard and the verdict.
            $startScript = Join-Path $PSScriptRoot 'Start-UniPad.ps1'
            if (-not (Test-Path -LiteralPath $startScript)) {
                Stop-UniPadScript -Code 'config_invalid' -Message 'Start-UniPad.ps1 was not found next to the launcher' -ExitCode $script:ExitUsage -Details @{ expected = $startScript }
            }
            if (-not $python) {
                Stop-UniPadScript -Code 'blocked' -Message 'no interpreter found (tried UNIPAD_PYTHON, python, py)' -ExitCode $script:ExitBlocked -Details @{ probed = 'python, py' }
            }
            $delegateArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $startScript, '-Profile', $Profile, '-Artifact', $artifactAbs)
            if ($ExperimentId) { $delegateArgs += @('-ExperimentId', $ExperimentId) }
            $delegateArgs += @('-Json')
            $delegated = Invoke-NativeCapture -FilePath 'powershell' -ArgumentList $delegateArgs
            if ($delegated.StdOut -and $delegated.StdOut.Trim() -ne '') { Write-Output (ConvertTo-Text -InputObject $delegated.StdOut) }
            if ($delegated.StdErr -and $delegated.StdErr.Trim() -ne '') { [Console]::Error.WriteLine((ConvertTo-Text -InputObject $delegated.StdErr).Trim()) }
            exit $delegated.ExitCode
        }

        # stop / install / restore
        if (-not $python) {
            Stop-UniPadScript -Code 'blocked' -Message 'no interpreter found (tried UNIPAD_PYTHON, python, py)' -ExitCode $script:ExitBlocked -Details @{ probed = 'python, py' }
        }
        $invocation = Invoke-UniPadController -Arguments $controllerArgs.ToArray()
        $payloadText = (ConvertTo-Text -InputObject $invocation.StdOut).Trim()
        if ($payloadText -ne '') {
            Write-Output $payloadText
        }
        elseif ($invocation.StdErr.Trim() -ne '') {
            [Console]::Error.WriteLine((ConvertTo-Text -InputObject $invocation.StdErr).Trim())
        }
        $payload = Get-JsonPayload -Text $payloadText
        $verdict = if ($null -ne $payload) { Get-ControllerVerdict -Payload $payload } else { 'indeterminate' }
        Write-Note ('controller exit code: {0}; verdict: {1}' -f $invocation.ExitCode, $verdict)
        if ($invocation.UsedFallback) { Write-Note 'note: controller reached through the direct-module fallback (unipad.controller.cli:main).' }
        # Never resolve a contradictory payload in favour of success.
        if ($invocation.ExitCode -eq 0 -and $verdict -eq 'failure') { exit $script:ExitFailure }
        exit $invocation.ExitCode
    }
    finally {
        Pop-Location | Out-Null
    }
}
catch {
    # An exception no inner handler claimed.  This used to report `blocked` --
    # a code Get-ExitCodeForErrorCode maps to exit 3, and which PRD 7.5
    # reserves for a missing material, credential or budget -- while exiting
    # 1, so the code and the exit code contradicted each other and a caller
    # branching on either reached the opposite conclusion.  The exit code is
    # now DERIVED from the code, so the two cannot drift apart again.
    #
    # Only the exception *type* is reported, never the raw message: a .NET
    # message quotes the offending value, which on a host whose working
    # directory is non-ASCII puts the Windows username on stderr (PRD 9.2), and
    # on this host it is a localized Chinese string written in the ANSI code
    # page, so the operator received mojibake.  httpsvc's handle_error takes
    # the same line for the same reason.
    $code = 'internal_error'
    Stop-UniPadScript -Code $code `
        -Message 'the launcher stopped on an unexpected error; see details.exception' `
        -ExitCode (Get-ExitCodeForErrorCode -Code $code) `
        -Details @{ exception = $_.Exception.GetType().FullName }
}
