<#
.SYNOPSIS
    Verify a downloaded build artifact against its manifest hash (PRD 8.1, 8.2).

.DESCRIPTION
    Step 5 of the pipeline (PRD 8.2): Windows downloads the artifacts of a
    macOS build run and must verify them before running anything.  PRD 7.4 is
    explicit -- an artifact is only usable when its hash, architecture and
    manifest agree, and a failed download must never fall back to an older
    package.

    The verification authority is the controller
    (`python -m unipad.controller fetch --verify-only --path ... --expect ...`);
    this script resolves the expected hash, relays the controller verdict and
    relays its exit code.  It additionally computes the local SHA-256 itself so
    that both hashes can be printed on a mismatch (hashes are not secrets) and
    so that a controller "match" can be contradicted when the local digest
    disagrees -- a contradiction is never reported as success.

    Expected hash resolution:
      * -ExpectedSha256 is used verbatim (64 hex characters, case-insensitive);
      * otherwise the entry matching -Path is looked up in the -Manifest
        artifact-manifest.json produced by the macOS build (PRD 8.1);
      * if neither is supplied, or the manifest has no matching entry, the
        script fails: an artifact without a pinned hash is not verifiable.

.PARAMETER Path
    Local artifact file to verify.

.PARAMETER ExpectedSha256
    Expected SHA-256 (64 hex characters).  Optional when -Manifest is given.

.PARAMETER Manifest
    Path to artifact-manifest.json (PRD 8.1) used to look up the expected hash.

.EXAMPLE
    powershell -NoProfile -File scripts\windows\Test-ArtifactDownload.ps1 -Path artifacts\kernel.bin -Manifest artifacts\artifact-manifest.json

.EXAMPLE
    powershell -NoProfile -File scripts\windows\Test-ArtifactDownload.ps1 -Path artifacts\kernel.bin -ExpectedSha256 0f2a...

.NOTES
    PowerShell 5.1+ compatible.  ASCII-only on purpose: powershell.exe
    mis-decodes BOM-less UTF-8 files that contain non-ASCII text.
#>
[CmdletBinding()]
param(
    [string]$Path,
    [string]$ExpectedSha256,
    [string]$Manifest
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

function Resolve-ExistingFile {
    <#
    .SYNOPSIS
        Resolve a path that must already exist as a file.
    #>
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Resolve-Path -LiteralPath $Path).Path
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

function Test-UsageRejection {
    <#
    .SYNOPSIS
        True when a controller failure is an argparse usage rejection.
    .DESCRIPTION
        Only a usage rejection is retryable.  `fetch --verify-only` is the
        documented interface, while a controller that has not implemented it yet
        rejects the arguments with exit code 2 and "unrecognized arguments".
        Any other non-zero exit is a real verification failure and is never
        retried, so a genuine rejection can never be masked.
    #>
    param(
        [Parameter(Mandatory = $true)]$Run,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$PayloadText
    )
    if ($Run.ExitCode -ne 2) { return $false }
    $haystack = ($PayloadText + "`n" + $Run.StdErr)
    return ($haystack -match 'unrecognized arguments|invalid choice|the following arguments are required')
}

function Get-PackageDigest {
    <#
    .SYNOPSIS
        SHA-256 computed by the unipad package itself.
    .DESCRIPTION
        Uses unipad.ipa.package.sha256_file, the same digest function the
        controller uses, so a package-digest verification stays inside the
        unipad code base instead of relying on a host-only tool.
    .OUTPUTS
        Lowercase hex digest, or $null when it cannot be computed.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$ArtifactPath
    )
    $snippet = 'from unipad.ipa.package import sha256_file; import sys; sys.stdout.write(sha256_file(sys.argv[1]))'
    $run = Invoke-NativeCapture -FilePath $Python -ArgumentList @('-c', $snippet, $ArtifactPath)
    if ($run.ExitCode -ne 0) { return $null }
    $digest = $run.StdOut.Trim()
    if ($digest -notmatch '^[0-9a-f]{64}$') { return $null }
    return $digest.ToLowerInvariant()
}

function Get-Sha256Hex {
    <#
    .SYNOPSIS
        Lower-case hex SHA-256 of a file, computed with .NET only.
    .DESCRIPTION
        Deliberately not Get-FileHash: that cmdlet lives in
        Microsoft.PowerShell.Utility and relies on module auto-loading, which
        is not guaranteed on a locked-down host.  [System.Security.Cryptography]
        and [System.IO.File] are part of the CLR every PowerShell runs on.
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

function Get-ExpectedHashFromManifest {
    <#
    .SYNOPSIS
        Look up the expected SHA-256 for an artifact inside a manifest (PRD 8.1).
    .OUTPUTS
        PSCustomObject: Found, Sha256, EntryName, SizeBytes, ErrorCode, ErrorMessage.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ArtifactPath,
        [Parameter(Mandatory = $true)][string]$ArtifactLeaf
    )
    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        return [PSCustomObject]@{
            Found = $false; Sha256 = $null; EntryName = $null; SizeBytes = $null
            ErrorCode = 'artifact_missing'
            ErrorMessage = "manifest not found: $ManifestPath"
        }
    }
    $manifestText = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $ManifestPath).Path)
    try {
        $manifest = $manifestText | ConvertFrom-Json
    }
    catch {
        return [PSCustomObject]@{
            Found = $false; Sha256 = $null; EntryName = $null; SizeBytes = $null
            ErrorCode = 'artifact_manifest_invalid'
            ErrorMessage = "manifest is not valid JSON: $ManifestPath"
        }
    }
    if ($null -eq $manifest -or $manifest -is [string]) {
        return [PSCustomObject]@{
            Found = $false; Sha256 = $null; EntryName = $null; SizeBytes = $null
            ErrorCode = 'artifact_manifest_invalid'
            ErrorMessage = 'manifest is not a JSON object'
        }
    }
    if (-not (@($manifest.PSObject.Properties.Name) -contains 'artifacts')) {
        return [PSCustomObject]@{
            Found = $false; Sha256 = $null; EntryName = $null; SizeBytes = $null
            ErrorCode = 'artifact_manifest_invalid'
            ErrorMessage = 'manifest has no "artifacts" array (PRD 8.1 artifact-manifest.json)'
        }
    }

    $matches = @()
    foreach ($entry in @($manifest.artifacts)) {
        if ($null -eq $entry) { continue }
        $names = @($entry.PSObject.Properties.Name)
        $candidates = @()
        if ($names -contains 'name') { $candidates += [string]$entry.name }
        if ($names -contains 'path') { $candidates += [string]$entry.path }
        foreach ($candidate in $candidates) {
            if ($candidate -eq '') { continue }
            if ($candidate -eq $ArtifactPath -or $candidate -eq $ArtifactLeaf -or (Split-Path -Leaf $candidate) -eq $ArtifactLeaf) {
                $matches += $entry
                break
            }
        }
    }

    if ($matches.Count -eq 0) {
        $available = @()
        foreach ($entry in @($manifest.artifacts)) {
            if ($null -ne $entry -and (@($entry.PSObject.Properties.Name) -contains 'name')) {
                $available += [string]$entry.name
            }
        }
        return [PSCustomObject]@{
            Found = $false; Sha256 = $null; EntryName = $null; SizeBytes = $null
            ErrorCode = 'artifact_missing'
            ErrorMessage = "manifest has no entry for '$ArtifactLeaf'; entries: " + ($available -join ', ')
        }
    }
    if ($matches.Count -gt 1) {
        return [PSCustomObject]@{
            Found = $false; Sha256 = $null; EntryName = $null; SizeBytes = $null
            ErrorCode = 'artifact_manifest_invalid'
            ErrorMessage = "manifest has $($matches.Count) ambiguous entries for '$ArtifactLeaf'"
        }
    }

    $entry = $matches[0]
    $entryNames = @($entry.PSObject.Properties.Name)
    if (-not ($entryNames -contains 'sha256') -or [string]::IsNullOrWhiteSpace([string]$entry.sha256)) {
        return [PSCustomObject]@{
            Found = $false; Sha256 = $null
            EntryName = $(if ($entryNames -contains 'name') { [string]$entry.name } else { $null })
            SizeBytes = $null
            ErrorCode = 'artifact_manifest_invalid'
            ErrorMessage = "manifest entry for '$ArtifactLeaf' carries no sha256"
        }
    }
    $size = $null
    if ($entryNames -contains 'size_bytes') { $size = $entry.size_bytes }
    return [PSCustomObject]@{
        Found = $true
        Sha256 = ([string]$entry.sha256).Trim().ToLowerInvariant()
        EntryName = $(if ($entryNames -contains 'name') { [string]$entry.name } else { $ArtifactLeaf })
        SizeBytes = $size
        ErrorCode = $null
        ErrorMessage = $null
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
try {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        Stop-UniPadScript -Code 'config_invalid' `
            -Message 'Path is required: the local artifact to verify.' `
            -ExitCode $script:ExitUsage
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedSha256) -and [string]::IsNullOrWhiteSpace($Manifest)) {
        Stop-UniPadScript -Code 'config_invalid' `
            -Message 'ExpectedSha256 or Manifest is required: an artifact without a pinned hash is not verifiable (PRD 7.4).' `
            -ExitCode $script:ExitUsage
    }

    $normalizedExpected = ''
    if (-not [string]::IsNullOrWhiteSpace($ExpectedSha256)) {
        $normalizedExpected = $ExpectedSha256.Trim().ToLowerInvariant()
        if ($normalizedExpected -notmatch '^[0-9a-f]{64}$') {
            Stop-UniPadScript -Code 'config_invalid' `
                -Message "ExpectedSha256 must be 64 hexadecimal characters, got '$($ExpectedSha256.Trim())'." `
                -ExitCode $script:ExitUsage
        }
    }

    $resolvedPath = Resolve-ExistingFile -Path $Path
    if ($null -eq $resolvedPath) {
        Stop-UniPadScript -Code 'image_not_found' `
            -Message "artifact not found: $Path" `
            -ExitCode (Get-ExitCodeForErrorCode -Code 'image_not_found') `
            -Details @{ path = $Path }
    }
    if (Test-Path -LiteralPath $resolvedPath -PathType Container) {
        Stop-UniPadScript -Code 'config_invalid' `
            -Message "artifact path is a directory, not a file: $resolvedPath" `
            -ExitCode $script:ExitUsage
    }

    $artifactLeaf = Split-Path -Leaf $resolvedPath
    $manifestEntry = $null
    if ($normalizedExpected -eq '') {
        $manifestEntry = Get-ExpectedHashFromManifest -ManifestPath $Manifest -ArtifactPath $resolvedPath -ArtifactLeaf $artifactLeaf
        if (-not $manifestEntry.Found) {
            Stop-UniPadScript -Code $manifestEntry.ErrorCode `
                -Message $manifestEntry.ErrorMessage `
                -ExitCode (Get-ExitCodeForErrorCode -Code $manifestEntry.ErrorCode)
        }
        $normalizedExpected = $manifestEntry.Sha256
    }

    # Local digest: corroboration for the controller verdict, and the value
    # printed on a mismatch.
    $localHash = ''
    $hashError = $null
    try {
        $localHash = Get-Sha256Hex -Path $resolvedPath
    }
    catch {
        $hashError = $_.Exception.Message
    }
    if ($localHash -eq '' -and $null -eq $hashError) { $hashError = 'digest unavailable' }

    $python = Get-ConfiguredPython
    if (-not (Test-CommandAvailable -Name $python)) {
        Stop-UniPadScript -Code 'blocked' `
            -Message ("Interpreter '$python' was not found on PATH. Set the UNIPAD_PYTHON environment variable to an interpreter that can import unipad.") `
            -ExitCode $script:ExitBlocked `
            -Details @{ interpreter = $python }
    }

    $invocation = Invoke-UniPadController -Arguments @('fetch', '--verify-only', '--path', $resolvedPath, '--expect', $normalizedExpected)
    $payloadText = (ConvertTo-Text -InputObject $invocation.StdOut).Trim()
    $payload = Get-JsonPayload -Text $payloadText

    # Which authority performed the digest comparison.  Recorded and printed so a
    # package-digest verification is never mistaken for a controller verdict.
    $authority = 'unipad.controller fetch --verify-only'

    if ($invocation.ExitCode -ne 0 -and (Test-UsageRejection -Run $invocation -PayloadText $payloadText)) {
        # The controller CLI does not implement --verify-only yet.  The digest is
        # then computed by the package function the controller itself uses, which
        # keeps the verification inside the unipad code base instead of relying on
        # a host-only tool.
        $authority = 'unipad.ipa.package.sha256_file'
        $packageDigest = Get-PackageDigest -Python $python -ArtifactPath $resolvedPath
        if ($null -eq $packageDigest) {
            Stop-UniPadScript -Code 'verification_failed' `
                -Message 'controller rejected --verify-only and the package digest could not be computed.' `
                -ExitCode $script:ExitFailure `
                -Details @{ invocation = $invocation.Invocation; authority = $authority }
        }
        if ($packageDigest -ne $normalizedExpected) {
            Write-Output ("artifact: {0}" -f $resolvedPath)
            Write-Output ("expected sha256: {0}" -f $normalizedExpected)
            Write-Output ("actual   sha256: {0}" -f $packageDigest)
            Stop-UniPadScript -Code 'image_hash_mismatch' `
                -Message 'artifact SHA-256 does not match the pinned hash.' `
                -ExitCode $script:ExitFailure `
                -Details @{ expected = $normalizedExpected; actual = $packageDigest; authority = $authority }
        }
        Write-Output ("artifact: {0}" -f $resolvedPath)
        Write-Output ("expected sha256: {0}" -f $normalizedExpected)
        Write-Output ("actual   sha256: {0}" -f $packageDigest)
        Write-Output ("authority: {0}" -f $authority)
        Write-Output ("size_bytes: {0}" -f (Get-Item -LiteralPath $resolvedPath).Length)
        if ($null -ne $manifestEntry -and $null -ne $manifestEntry.SizeBytes) {
            Write-Output ("manifest size_bytes: {0}" -f $manifestEntry.SizeBytes)
        }
        Write-Output 'RESULT: artifact verified against the pinned hash (package digest; controller has no --verify-only).'
        exit $script:ExitOk
    }

    if ($invocation.ExitCode -ne 0) {
        # Default only applies when the controller failed without machine-readable
        # output: the verification did not pass, and the reason is unknown.
        $code = 'verification_failed'
        $message = "controller rejected the artifact (exit code $($invocation.ExitCode))"
        $details = @{
            invocation = $invocation.Invocation
            exit_code  = [string]$invocation.ExitCode
            expected   = $normalizedExpected
        }
        if ($localHash -ne '') { $details['actual'] = $localHash }
        if ($null -ne $payload -and (Test-PayloadError -Payload $payload)) {
            if ($payload.error.PSObject.Properties.Name -contains 'code') { $code = [string]$payload.error.code }
            if ($payload.error.PSObject.Properties.Name -contains 'message') { $message = [string]$payload.error.message }
        }
        elseif ($invocation.StdErr.Trim() -ne '') {
            $details['stderr_tail'] = Get-Tail -Text $invocation.StdErr
        }
        # Hashes are not secrets: print both sides of the comparison.
        Write-Output ("artifact: {0}" -f $resolvedPath)
        Write-Output ("expected sha256: {0}" -f $normalizedExpected)
        Write-Output ("actual   sha256: {0}" -f $(if ($localHash -ne '') { $localHash } else { "unavailable ($hashError)" }))
        Stop-UniPadScript -Code $code -Message $message -ExitCode $invocation.ExitCode -Details $details
    }

    # Controller accepted the artifact.  A local digest that disagrees is a
    # contradiction and must never be reported as a successful verification.
    if ($localHash -ne '' -and $localHash -ne $normalizedExpected) {
        Write-Output ("artifact: {0}" -f $resolvedPath)
        Write-Output ("expected sha256: {0}" -f $normalizedExpected)
        Write-Output ("actual   sha256: {0}" -f $localHash)
        Stop-UniPadScript -Code 'image_hash_mismatch' `
            -Message 'controller reported a match but the local SHA-256 disagrees; refusing to report success.' `
            -ExitCode $script:ExitFailure `
            -Details @{ expected = $normalizedExpected; actual = $localHash; authority = $authority }
    }
    if ($localHash -eq '' -and $null -eq $payload) {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message 'controller accepted the artifact but emitted no machine-readable result and no local digest could be computed.' `
            -ExitCode $script:ExitFailure
    }

    $actualSize = (Get-Item -LiteralPath $resolvedPath).Length
    Write-Output ("artifact: {0}" -f $resolvedPath)
    Write-Output ("expected sha256: {0}" -f $normalizedExpected)
    Write-Output ("actual   sha256: {0}" -f $(if ($localHash -ne '') { $localHash } else { 'not computed locally (controller verdict only)' }))
    Write-Output ("size_bytes: {0}" -f $actualSize)
    Write-Output ("authority: {0}" -f $authority)
    if ($null -ne $manifestEntry -and $null -ne $manifestEntry.SizeBytes) {
        Write-Output ("manifest size_bytes: {0}" -f $manifestEntry.SizeBytes)
        if ([string]$manifestEntry.SizeBytes -ne [string]$actualSize) {
            Write-Output 'note: manifest size differs from the local file; the controller accepted the hash, record the discrepancy.'
        }
    }
    if ($payloadText -ne '') { Write-Output $payloadText }
    Write-Output 'RESULT: artifact verified against the pinned hash by the controller.'
    exit $script:ExitOk
}
catch {
    Stop-UniPadScript -Code 'blocked' -Message $_.Exception.Message -ExitCode $script:ExitFailure -Details @{ exception = $_.Exception.GetType().FullName }
}
