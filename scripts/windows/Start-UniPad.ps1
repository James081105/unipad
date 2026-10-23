<#
.SYNOPSIS
    Start the UniPad virtual device on this Intel Windows host (PRD 4.1, 9.1).

.DESCRIPTION
    Windows entry point for `unipad start` (PRD 9.1).  The host is an Intel
    machine: WHPX/HVF must never be selected implicitly, so this script refuses
    to run for any profile other than 'tcg' before the controller is even
    invoked (unipad.errors.ACCELERATOR_NOT_TCG).

    Success is never asserted by this script.  The controller's process exit
    code is relayed unchanged, and the machine-readable JSON payload decides
    what the script reports:

      * payload carries an "error" member, or the exit code is non-zero
        -> failure, exit code relayed (or 1 when the controller claims success
           while its own payload reports an error: the contradiction is never
           resolved in favour of success);
      * payload carries a recognisable success indicator (ok/success/status/
        state/result/stage) -> success, exit 0;
      * payload is valid JSON but carries no recognisable indicator
        -> "indeterminate": the JSON is relayed verbatim and the script says so
           explicitly instead of claiming success.

    With -Json the controller payload is the only thing written to stdout (all
    human-readable notes go to stderr) so a caller can pipe it straight into a
    JSON parser.  Without -Json a compact summary is printed on stdout.

.PARAMETER Profile
    Machine profile.  Only 'tcg' is permitted on this host (case-insensitive
    comparison, value forwarded verbatim so the controller validates it).

.PARAMETER Artifact
    Path (or run reference) of the artifact manifest produced by the macOS
    build, i.e. artifact-manifest.json (PRD 8.1).  Required: starting without
    a verified manifest would run an unknown build.

.PARAMETER ExperimentId
    Experiment identifier recorded in the journal (PRD 7.3).  Optional; when
    omitted the controller decides the experiment id.

.PARAMETER Json
    Emit the controller payload verbatim on stdout.

.EXAMPLE
    powershell -NoProfile -File scripts\windows\Start-UniPad.ps1 -Profile tcg -Artifact artifacts\known-good.json

.EXAMPLE
    powershell -NoProfile -File scripts\windows\Start-UniPad.ps1 -Profile hvf -Artifact artifacts\manifest.json
    # -> refuses: "Only TCG is permitted on this Intel host"

.EXAMPLE
    powershell -NoProfile -File scripts\windows\Start-UniPad.ps1 -Artifact artifacts\manifest.json -ExperimentId exp-001 -Json

.NOTES
    PowerShell 5.1+ compatible.  ASCII-only on purpose: powershell.exe
    mis-decodes BOM-less UTF-8 files that contain non-ASCII text.
#>
[CmdletBinding()]
param(
    [string]$Profile = 'tcg',
    [string]$Artifact,
    [string]$ExperimentId,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Exit codes mirror unipad.errors.ExitCode (see Invoke-HostInventory.ps1).
# ---------------------------------------------------------------------------
$script:ExitOk = 0
$script:ExitFailure = 1
$script:ExitUsage = 2
$script:ExitBlocked = 3

# ---------------------------------------------------------------------------
# Helpers (duplicated per entry script; see Invoke-HostInventory.ps1).
# ---------------------------------------------------------------------------

function Get-ConfiguredPython {
    <#
    .SYNOPSIS
        Interpreter used to reach the unipad package (env override, no secret).
    #>
    $override = $env:UNIPAD_PYTHON
    if ($override -and $override.Trim() -ne '') {
        return $override.Trim()
    }
    return 'python'
}

function Test-CommandAvailable {
    <#
    .SYNOPSIS
        True when a command name resolves on PATH (or is a full path).
    #>
    param([Parameter(Mandatory = $true)][string]$Name)
    return ($null -ne (Get-Command -Name $Name -ErrorAction SilentlyContinue))
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
    #>
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Code)
    switch ($Code) {
        'blocked' { return $script:ExitBlocked }
        'budget_exceeded' { return $script:ExitBlocked }
        'config_invalid' { return $script:ExitUsage }
        'unsupported_command' { return $script:ExitUsage }
        'experiment_state_invalid' { return $script:ExitUsage }
        default { return $script:ExitFailure }
    }
}

function ConvertTo-NativeArgumentString {
    <#
    .SYNOPSIS
        Build a Windows command line from an argument vector (no shell involved).
    .DESCRIPTION
        Native commands are started through System.Diagnostics.Process, which
        takes one command-line string, so arguments are quoted here following the
        CommandLineToArgvW rules (spaces, embedded quotes, trailing backslashes).
    #>
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments
    )
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($arg in $Arguments) {
        $text = [string]$arg
        if ($text.Length -eq 0) {
            $parts.Add('""')
            continue
        }
        if ($text -notmatch '[\s"]') {
            $parts.Add($text)
            continue
        }
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
        System.Diagnostics.Process is used instead of the call operator on
        purpose: a native command that writes to stderr raises NativeCommandError
        under $ErrorActionPreference='Stop' (a `2>` redirect does not contain it),
        which would abort the script before the exit code could be relayed.
        Process also yields the raw streams without error-record formatting.
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
    # The controller emits UTF-8 JSON (ensure_ascii=False).  Console output on a
    # zh-CN Windows host is GBK/GB2312, which silently corrupts non-ASCII text
    # and makes ConvertFrom-Json fail, so the streams are decoded as UTF-8.
    $startInfo.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $startInfo.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $startInfo.WorkingDirectory = (Get-Location).Path
    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return [PSCustomObject]@{
            ExitCode = $process.ExitCode
            StdOut   = $stdout
            StdErr   = $stderr
        }
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
                ExitCode     = $fallback.ExitCode
                StdOut       = $fallback.StdOut
                StdErr       = $fallback.StdErr
                Invocation   = $invocation
                UsedFallback = $true
            }
        }
    }

    return [PSCustomObject]@{
        ExitCode     = $primary.ExitCode
        StdOut       = $primary.StdOut
        StdErr       = $primary.StdErr
        Invocation   = $invocation
        UsedFallback = $false
    }
}

function Get-JsonPayload {
    <#
    .SYNOPSIS
        Extract and parse the JSON object emitted by the controller.
    .DESCRIPTION
        Tolerates leading progress text by retrying on the first '{' .. last '}'
        span.  Returns $null when no JSON object can be recovered.
    #>
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    $trimmed = $Text.Trim()
    if ($trimmed -eq '') { return $null }
    try {
        $candidate = $trimmed | ConvertFrom-Json
        if ($null -ne $candidate -and -not ($candidate -is [string]) -and -not ($candidate -is [System.Array])) {
            return $candidate
        }
    }
    catch {
        # fall through to the span retry below
    }
    $first = $trimmed.IndexOf('{')
    $last = $trimmed.LastIndexOf('}')
    if ($first -ge 0 -and $last -gt $first) {
        try {
            $candidate = $trimmed.Substring($first, $last - $first + 1) | ConvertFrom-Json
            if ($null -ne $candidate -and -not ($candidate -is [string]) -and -not ($candidate -is [System.Array])) {
                return $candidate
            }
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
        True when a controller payload actually reports an error.
    .DESCRIPTION
        The controller envelope always carries an "error" member (null on
        success), so presence alone is not a failure: only a non-null value is.
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

    # The property names this payload actually carries.  Without this line
    # $names is $null here -- PowerShell has no block scope for it, and the
    # assignment at :381 belongs to Test-PayloadError -- so both loops below
    # are always False and this function returned 'indeterminate' for every
    # payload, success included.  UniPad-Launcher.ps1:436 has always had it;
    # the two scripts disagreed about the same JSON because this copy lacked
    # one line.  Failing closed meant the success branch at :562 was dead
    # code rather than a false claim, which is why this survived review.
    $names = @($Payload.PSObject.Properties.Name)

    foreach ($key in @('ok', 'success')) {
        if ($names -contains $key) {
            $value = $Payload.$key
            if ($value -is [bool]) {
                if ($value) { return 'success' } else { return 'failure' }
            }
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
        Human-readable note: stdout in normal mode, stderr in -Json mode.
    #>
    param([Parameter(Mandatory = $true)][string]$Text)
    if ($Json) {
        [Console]::Error.WriteLine($Text)
    }
    else {
        Write-Output $Text
    }
}

function Get-Tail {
    <#
    .SYNOPSIS
        Last N characters of a text block, for compact diagnostics.
    #>
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text,
        [int]$MaxChars = 400
    )
    if ($Text.Length -le $MaxChars) { return $Text.Trim() }
    return ('...' + $Text.Substring($Text.Length - $MaxChars)).Trim()
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
try {
    # Guard first: an Intel host must never reach WHPX/HVF by accident
    # (unipad.errors.ACCELERATOR_NOT_TCG, PRD 2.2 / 8.3).
    if ($Profile -ne 'tcg') {
        # Write-Error is required by the contract, but $ErrorActionPreference
        # is 'Stop', so it is emitted with a temporarily relaxed preference and
        # followed by the machine-readable error object and exit code.
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            Write-Error 'Only TCG is permitted on this Intel host'
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        Stop-UniPadScript -Code 'accelerator_not_tcg' `
            -Message 'Only TCG is permitted on this Intel host' `
            -ExitCode $script:ExitUsage `
            -Details @{ requested_profile = $Profile; permitted_profile = 'tcg' }
    }

    if ([string]::IsNullOrWhiteSpace($Artifact)) {
        Stop-UniPadScript -Code 'config_invalid' `
            -Message 'Artifact is required: start must reference the artifact-manifest.json produced by the macOS build (PRD 8.1).' `
            -ExitCode $script:ExitUsage
    }

    $python = Get-ConfiguredPython
    if (-not (Test-CommandAvailable -Name $python)) {
        Stop-UniPadScript -Code 'blocked' `
            -Message ("Interpreter '$python' was not found on PATH. Set the UNIPAD_PYTHON environment variable to an interpreter that can import unipad.") `
            -ExitCode $script:ExitBlocked `
            -Details @{ interpreter = $python }
    }

    $controllerArgs = @('start', '--profile', $Profile, '--artifact', $Artifact)
    if (-not [string]::IsNullOrWhiteSpace($ExperimentId)) {
        $controllerArgs += @('--experiment', $ExperimentId)
    }

    $invocation = Invoke-UniPadController -Arguments $controllerArgs
    $payloadText = (ConvertTo-Text -InputObject $invocation.StdOut).Trim()

    if ($invocation.ExitCode -ne 0) {
        $payload = Get-JsonPayload -Text $payloadText
        $code = 'blocked'
        $message = 'controller start failed'
        $details = @{
            invocation = $invocation.Invocation
            exit_code  = [string]$invocation.ExitCode
            profile    = $Profile
        }
        if ($null -ne $payload -and (Test-PayloadError -Payload $payload)) {
            if ($payload.error.PSObject.Properties.Name -contains 'code') { $code = [string]$payload.error.code }
            if ($payload.error.PSObject.Properties.Name -contains 'message') { $message = [string]$payload.error.message }
        }
        elseif ($invocation.StdErr.Trim() -ne '') {
            $details['stderr_tail'] = Get-Tail -Text $invocation.StdErr
        }
        if ($Json -and $payloadText -ne '') { Write-Output $payloadText }
        else { Write-Note -Text ("controller exit code: {0}" -f $invocation.ExitCode) }
        Stop-UniPadScript -Code $code -Message $message -ExitCode $invocation.ExitCode -Details $details
    }

    if ($payloadText -eq '') {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message 'controller start exited 0 without machine-readable output; success is unconfirmed.' `
            -ExitCode $script:ExitFailure `
            -Details @{ invocation = $invocation.Invocation }
    }

    $payload = Get-JsonPayload -Text $payloadText
    if ($null -eq $payload) {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message 'controller start output is not valid JSON; success is unconfirmed.' `
            -ExitCode $script:ExitFailure `
            -Details @{ invocation = $invocation.Invocation }
    }

    $verdict = Get-ControllerVerdict -Payload $payload

    # The controller payload is always relayed verbatim on stdout; in -Json mode
    # it is the only thing on stdout (notes go to stderr) so it can be piped
    # straight into a JSON parser.
    Write-Output $payloadText

    Write-Note -Text ("controller exit code: {0}" -f $invocation.ExitCode)
    Write-Note -Text ("verdict from controller JSON: {0}" -f $verdict)
    if ($invocation.UsedFallback) {
        Write-Note -Text 'note: controller was reached through the direct-module fallback (unipad.controller.cli:main).'
    }

    if ($verdict -eq 'failure') {
        # The controller exited 0 but its own payload reports an error: never
        # resolve that contradiction in favour of success.
        $code = 'verification_failed'
        $message = 'controller reported an error in its JSON payload'
        if ($payload.error.PSObject.Properties.Name -contains 'code') { $code = [string]$payload.error.code }
        if ($payload.error.PSObject.Properties.Name -contains 'message') { $message = [string]$payload.error.message }
        Stop-UniPadScript -Code $code -Message $message -ExitCode $script:ExitFailure
    }

    if ($verdict -eq 'indeterminate') {
        Write-Note -Text 'WARNING: controller payload carries no success indicator; this script does NOT report success (PRD 7.4: missing evidence is not success).'
        exit $script:ExitOk
    }

    $names = @($payload.PSObject.Properties.Name)
    foreach ($key in @('run_id', 'experiment_id', 'pid', 'qemu_pid', 'accel', 'qmp_endpoint', 'serial_log', 'artifact_manifest', 'status')) {
        if ($names -contains $key) {
            Write-Note -Text ("{0}: {1}" -f $key, $payload.$key)
        }
    }
    Write-Note -Text 'RESULT: start succeeded according to the controller JSON payload.'
    exit $script:ExitOk
}
catch {
    Stop-UniPadScript -Code 'blocked' -Message $_.Exception.Message -ExitCode $script:ExitFailure -Details @{ exception = $_.Exception.GetType().FullName }
}
