<#
.SYNOPSIS
    Collect the UniPad WP00 host inventory (PRD sections 6.1 and 9.1).

.DESCRIPTION
    WP00 ("baseline freeze", PRD 6.1) is the first work package of the project:
    before any experiment runs, the Windows host has to be inventoried (CPU,
    RAM, free disk space, accelerator facts, toolchain versions).  Guest memory
    sizing, the TCG-only guard and the placement of artifacts are all derived
    from that inventory, so the measurement belongs to the controller
    (`python -m unipad.controller doctor --json`, PRD 9.1 "unipad doctor") and
    the result is stored as JSON so an experiment can cite it (PRD 7.3 runtime
    block, PRD 9.2 evidence set).

    This script is only the Windows entry point (PRD 4.1: PowerShell is the
    Windows launcher / local-controller entry).  It invents nothing:

      * the controller is executed with stdout and stderr captured separately;
      * $OutPath is written only when stdout is a JSON object that carries no
        "error" member -- a partial or failed inventory is never stored as if
        it were evidence;
      * the written file is read back and re-parsed before the script claims
        anything, and the absolute path, byte length and SHA-256 are printed so
        the artifact can be cited as evidence (hashes are not secrets);
      * every failure path prints a machine-readable error object
        {"error":{"code":...,"message":...,"details":{...}}} and exits non-zero.

    Fallback: when `python -m unipad.controller` is not importable (the editable
    install is a plain .pth file, see README), the script retries through the
    console entry point declared in pyproject.toml
    (unipad.controller.cli:main).

.PARAMETER OutPath
    Destination for the inventory JSON.  Relative paths resolve against the
    caller's current directory.  Default 'host-inventory.json' is the WP00
    deliverable named in PRD 6.1; run the script from the repository root to
    refresh the committed baseline file.

.EXAMPLE
    powershell -NoProfile -File scripts\windows\Invoke-HostInventory.ps1

.EXAMPLE
    powershell -NoProfile -File scripts\windows\Invoke-HostInventory.ps1 -OutPath runs\exp-001\host-inventory.json

.NOTES
    PowerShell 5.1+ compatible.  ASCII-only on purpose: powershell.exe
    mis-decodes BOM-less UTF-8 files that contain non-ASCII text.
#>
[CmdletBinding()]
param(
    [string]$OutPath = 'host-inventory.json'
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Exit codes mirror unipad.errors.ExitCode so that a script-level failure and
# a controller-level failure branch the same way for a caller.
# ---------------------------------------------------------------------------
$script:ExitOk = 0
$script:ExitFailure = 1
$script:ExitUsage = 2
$script:ExitBlocked = 3

# ---------------------------------------------------------------------------
# Helpers.  They are intentionally duplicated in every entry script: each .ps1
# must run and be auditable on its own, with no dot-sourced module that could
# go missing when a single entry point is copied to a host.
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
    .DESCRIPTION
        Mirrors the console entry point declared in pyproject.toml
        (unipad = unipad.controller.cli:main).  The argument vector is passed
        through the UNIPAD_ARGV environment variable as a JSON array so that no
        shell quoting is involved.
    #>
    return 'import json, os, sys; sys.argv = ["unipad"] + json.loads(os.environ["UNIPAD_ARGV"]); from unipad.controller.cli import main; raise SystemExit(main())'
}

function Test-UsageRejection {
    <#
    .SYNOPSIS
        True when a controller failure is an argparse usage rejection.
    .DESCRIPTION
        Only a usage rejection is retryable with a reduced argument vector.
        `doctor --json` is the documented interface (PRD 9.1), while a
        controller that emits JSON unconditionally rejects the extra flag with
        exit code 2 and "unrecognized arguments".  Any other non-zero exit is a
        real failure and is never retried, so a genuine doctor failure can never
        be masked by a second attempt.
    #>
    param(
        [Parameter(Mandatory = $true)]$Run,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$PayloadText
    )
    if ($Run.ExitCode -ne 2) { return $false }
    $haystack = ($PayloadText + "`n" + $Run.StdErr)
    return ($haystack -match 'unrecognized arguments|invalid choice|the following arguments are required')
}

function New-ControllerResult {
    <#
    .SYNOPSIS
        Normalise a controller run into one result object.
    #>
    param(
        [Parameter(Mandatory = $true)]$Run,
        [Parameter(Mandatory = $true)][string]$Invocation,
        [Parameter(Mandatory = $true)][bool]$UsedFallback
    )
    return [PSCustomObject]@{
        ExitCode     = $Run.ExitCode
        StdOut       = (ConvertTo-Text -InputObject $Run.StdOut)
        StdErr       = $Run.StdErr
        Invocation   = $Invocation
        UsedFallback = $UsedFallback
    }
}

function Invoke-UniPadController {
    <#
    .SYNOPSIS
        Run the unipad controller CLI, with safe fallbacks.
    .DESCRIPTION
        Primary form: `python -m unipad.controller <args>`.  Two and only two
        conditions are retried, never anything else:

          1. the package is not importable through -m (the editable install is a
             plain .pth file, see README) -> retry through the console entry
             point declared in pyproject.toml (unipad.controller.cli:main);
          2. the controller rejected the argument vector as a usage error and the
             caller supplied a reduced vector -> retry with that vector.

        A real controller failure (non-zero exit for any other reason) is
        returned as-is so the caller relays it.
    .OUTPUTS
        PSCustomObject: ExitCode, StdOut, StdErr, Invocation, UsedFallback.
    #>
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [AllowNull()][string[]]$ReducedArguments = $null
    )
    $python = Get-ConfiguredPython
    $primary = Invoke-NativeCapture -FilePath $python -ArgumentList (@('-m', 'unipad.controller') + $Arguments)
    $invocation = ('{0} -m unipad.controller {1}' -f $python, ($Arguments -join ' '))

    if ($primary.ExitCode -eq 0) {
        return (New-ControllerResult -Run $primary -Invocation $invocation -UsedFallback $false)
    }

    $primaryText = (ConvertTo-Text -InputObject $primary.StdOut)
    $moduleMissing = (($primaryText + "`n" + $primary.StdErr) -match 'No module named')
    $usageRejected = ($null -ne $ReducedArguments -and $ReducedArguments.Count -gt 0 -and (Test-UsageRejection -Run $primary -PayloadText $primaryText))

    if ($moduleMissing) {
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
        return (New-ControllerResult -Run $fallback `
            -Invocation ('{0} -c <unipad.controller.cli:main> {1}' -f $python, ($Arguments -join ' ')) `
            -UsedFallback $true)
    }

    if ($usageRejected) {
        $reduced = Invoke-NativeCapture -FilePath $python -ArgumentList (@('-m', 'unipad.controller') + $ReducedArguments)
        return (New-ControllerResult -Run $reduced `
            -Invocation ('{0} -m unipad.controller {1}' -f $python, ($ReducedArguments -join ' ')) `
            -UsedFallback $true)
    }

    return (New-ControllerResult -Run $primary -Invocation $invocation -UsedFallback $false)
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

function Resolve-OutputPath {
    <#
    .SYNOPSIS
        Resolve a possibly non-existent output path to an absolute path.
    .DESCRIPTION
        The destination normally does not exist yet (the caller creates the
        directory afterwards), so Resolve-Path cannot be used.  Rooted paths are
        normalised by [System.IO.Path]::GetFullPath; relative paths are joined
        onto the caller's PowerShell location first, because
        [Environment]::CurrentDirectory is not updated by Set-Location across
        drives.
    .OUTPUTS
        String: absolute path, or '' when the path is malformed.
    #>
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    $combined = $Path
    if (-not [System.IO.Path]::IsPathRooted($Path)) {
        $combined = Join-Path (Get-Location).Path $Path
    }
    try {
        return [System.IO.Path]::GetFullPath($combined)
    }
    catch {
        return ''
    }
}

function Get-Sha256Hex {
    <#
    .SYNOPSIS
        Lower-case hex SHA-256 of a file, computed with .NET only.
    .DESCRIPTION
        Deliberately not Get-FileHash: that cmdlet lives in
        Microsoft.PowerShell.Utility and relies on module auto-loading, which
        is not guaranteed on a locked-down host.  [System.Security.Cryptography]
        and [System.IO.File] are part of the CLR every PowerShell runs on, so
        the digest is computed the same way everywhere.
    .OUTPUTS
        String: 64 lower-case hex characters, or '' when the file is unreadable.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )
    $fullPath = $Path
    try { $fullPath = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path } catch { return '' }
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) { return '' }
    $algorithm = $null
    $stream = $null
    try {
        $algorithm = [System.Security.Cryptography.SHA256]::Create()
        $stream = [System.IO.File]::OpenRead($fullPath)
        $digest = $algorithm.ComputeHash($stream)
    }
    catch {
        return ''
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $algorithm) { $algorithm.Dispose() }
    }
    $builder = New-Object System.Text.StringBuilder
    foreach ($byte in $digest) { [void]$builder.Append($byte.ToString('x2')) }
    return $builder.ToString()
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
    if ([string]::IsNullOrWhiteSpace($OutPath)) {
        Stop-UniPadScript -Code 'config_invalid' -Message 'OutPath must not be empty.' -ExitCode $script:ExitUsage
    }

    $python = Get-ConfiguredPython
    if (-not (Test-CommandAvailable -Name $python)) {
        Stop-UniPadScript -Code 'blocked' `
            -Message ("Interpreter '$python' was not found on PATH. Set the UNIPAD_PYTHON environment variable to an interpreter that can import unipad.") `
            -ExitCode $script:ExitBlocked `
            -Details @{ interpreter = $python }
    }

    $invocation = Invoke-UniPadController -Arguments @('doctor', '--json') -ReducedArguments @('doctor')

    if ($invocation.ExitCode -ne 0) {
        $details = @{
            invocation = $invocation.Invocation
            exit_code  = [string]$invocation.ExitCode
        }
        $payload = $null
        $payloadText = (ConvertTo-Text -InputObject $invocation.StdOut).Trim()
        if ($payloadText -ne '') {
            try { $payload = $payloadText | ConvertFrom-Json } catch { $payload = $null }
        }
        $code = 'blocked'
        $message = 'controller doctor failed'
        if ($null -ne $payload -and ($payload.PSObject.Properties.Name -contains 'error')) {
            $err = $payload.error
            if ($err.PSObject.Properties.Name -contains 'code') { $code = [string]$err.code }
            if ($err.PSObject.Properties.Name -contains 'message') { $message = [string]$err.message }
        }
        elseif ($invocation.StdErr.Trim() -ne '') {
            $details['stderr_tail'] = Get-Tail -Text $invocation.StdErr
        }
        Stop-UniPadScript -Code $code -Message $message -ExitCode $invocation.ExitCode -Details $details
    }

    $payloadText = (ConvertTo-Text -InputObject $invocation.StdOut).Trim()
    if ($payloadText -eq '') {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message 'controller exited 0 but produced no output; no host inventory was written.' `
            -ExitCode $script:ExitFailure `
            -Details @{ invocation = $invocation.Invocation }
    }

    $payload = $null
    try {
        $payload = $payloadText | ConvertFrom-Json
    }
    catch {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message 'controller output is not valid JSON; no host inventory was written.' `
            -ExitCode $script:ExitFailure `
            -Details @{ invocation = $invocation.Invocation; reason = $_.Exception.Message }
    }

    if ($null -eq $payload -or $payload -is [string] -or $payload -is [System.Array]) {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message 'controller did not emit a JSON object; no host inventory was written.' `
            -ExitCode $script:ExitFailure `
            -Details @{ invocation = $invocation.Invocation }
    }

    $names = @($payload.PSObject.Properties.Name)
    if ($names.Count -lt 1) {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message 'controller emitted an empty JSON object; no host inventory was written.' `
            -ExitCode $script:ExitFailure `
            -Details @{ invocation = $invocation.Invocation }
    }

    if (Test-PayloadError -Payload $payload) {
        $code = 'verification_failed'
        $message = 'controller reported an error instead of a host inventory.'
        if ($payload.error.PSObject.Properties.Name -contains 'code') { $code = [string]$payload.error.code }
        if ($payload.error.PSObject.Properties.Name -contains 'message') { $message = [string]$payload.error.message }
        Stop-UniPadScript -Code $code -Message $message -ExitCode (Get-ExitCodeForErrorCode -Code $code)
    }

    $fullPath = Resolve-OutputPath -Path $OutPath
    if ($fullPath -eq '') {
        Stop-UniPadScript -Code 'config_invalid' `
            -Message "OutPath is not a usable file path: $OutPath" `
            -ExitCode $script:ExitUsage
    }
    $parentDir = Split-Path -Parent $fullPath
    if ($parentDir -ne '' -and -not (Test-Path -LiteralPath $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }

    $fileText = $payloadText
    if (-not $fileText.EndsWith("`n")) { $fileText += "`r`n" }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($fullPath, $fileText, $utf8NoBom)

    # Read back before reporting anything: the file on disk must still be the
    # machine-readable object the controller produced.
    $readBack = [System.IO.File]::ReadAllText($fullPath)
    try {
        $null = $readBack | ConvertFrom-Json
    }
    catch {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message "host inventory was written to $fullPath but does not re-read as JSON." `
            -ExitCode $script:ExitFailure
    }

    $hash = Get-Sha256Hex -Path $fullPath
    if ($hash -eq '') {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message "host inventory was written to $fullPath but its SHA-256 could not be computed." `
            -ExitCode $script:ExitFailure
    }
    $length = (Get-Item -LiteralPath $fullPath).Length

    Write-Output ("host_inventory: {0}" -f $fullPath)
    Write-Output ("bytes: {0}" -f $length)
    Write-Output ("sha256: {0}" -f $hash)
    Write-Output ("invocation: {0}" -f $invocation.Invocation)
    Write-Output ("read_back: valid JSON ({0} top-level keys)" -f $names.Count)
    Write-Output ("keys: {0}" -f ($names -join ', '))
    foreach ($key in @('schema_version', 'collected_at', 'collector')) {
        if ($names -contains $key) {
            Write-Output ("{0}: {1}" -f $key, $payload.$key)
        }
    }
    if ($names -contains 'data') {
        $dataNames = @($payload.data.PSObject.Properties.Name)
        if ($dataNames -contains 'ready_for_run') {
            # A false value is an expected WP00 fact (no qemu-system-aarch64 on
            # PATH yet), not a failure of this collection: the inventory itself
            # is still valid evidence.
            Write-Output ("ready_for_run: {0}" -f $payload.data.ready_for_run)
        }
        if ($dataNames -contains 'findings') {
            foreach ($finding in @($payload.data.findings)) {
                if ($null -ne $finding -and (@($finding.PSObject.Properties.Name) -contains 'code')) {
                    Write-Output ("finding: {0}" -f $finding.code)
                }
            }
        }
    }
    if ($invocation.UsedFallback) {
        Write-Output 'note: controller was reached through the direct-module fallback (unipad.controller.cli:main).'
    }
    exit $script:ExitOk
}
catch {
    $details = @{ exception = $_.Exception.GetType().FullName }
    Stop-UniPadScript -Code 'blocked' -Message $_.Exception.Message -ExitCode $script:ExitFailure -Details $details
}
