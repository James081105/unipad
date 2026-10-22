<#
.SYNOPSIS
    Collect a UniPad host diagnostics snapshot (PRD 4.1, 6.1, 9.2).

.DESCRIPTION
    Triage entry point for the Windows host: one text file that answers the
    questions asked every time an experiment is blocked -- which Python, whether
    qemu-system-aarch64 is on PATH, whether Hyper-V is active (it decides
    whether WHPX could ever be picked up implicitly, which must never happen:
    unipad.errors.ACCELERATOR_NOT_TCG), how much free space every drive has,
    which run evidence exists, and which commit the checkout is on.

    The snapshot is written to runs/diagnostics-<timestamp>.txt (the directory
    is created when missing) and printed to stdout in the same pass.

    What this file is NOT: it is not milestone evidence and it never proves a
    gate.  Per PRD 7.4 a gate needs the evidence set of PRD 9.2 plus the
    verifier verdict; a diagnostics snapshot only supports triage.  No secrets
    are collected -- no tokens, no private keys, no user documents.

.NOTES
    PowerShell 5.1+ compatible.  ASCII-only on purpose: powershell.exe
    mis-decodes BOM-less UTF-8 files that contain non-ASCII text.
#>
[CmdletBinding()]
param()

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
        which would abort a diagnostics sweep half-way through.  Process also
        yields the raw streams without error-record formatting.
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
    # Tool output may contain non-ASCII text; the console code page on a zh-CN
    # Windows host is GBK/GB2312 and would corrupt it, so decode as UTF-8.
    $startInfo.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $startInfo.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $startInfo.WorkingDirectory = (Get-Location).Path
    try {
        $process = [System.Diagnostics.Process]::Start($startInfo)
    }
    catch {
        return [PSCustomObject]@{ ExitCode = -1; StdOut = ''; StdErr = $_.Exception.Message }
    }
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

function Get-RepoRoot {
    <#
    .SYNOPSIS
        Repository root implied by this script's location, with a CWD fallback.
    #>
    $candidate = $PSScriptRoot
    if ($candidate) {
        $parent = Split-Path -Parent $candidate
        if ($parent) {
            $grand = Split-Path -Parent $parent
            if ($grand -and (Test-Path -LiteralPath (Join-Path $grand 'src\unipad'))) {
                return $grand
            }
        }
    }
    return (Get-Location).Path
}

function Get-FirstLine {
    <#
    .SYNOPSIS
        First non-empty line of a text block, trimmed.
    #>
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line.Trim() -ne '') { return $line.Trim() }
    }
    return ''
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
try {
    $repoRoot = Get-RepoRoot
    $runsDir = Join-Path $repoRoot 'runs'
    if (-not (Test-Path -LiteralPath $runsDir)) {
        New-Item -ItemType Directory -Path $runsDir -Force | Out-Null
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $outPath = Join-Path $runsDir ('diagnostics-{0}.txt' -f $stamp)

    $lines = New-Object System.Collections.Generic.List[string]
    $null = $lines.Add('=== UniPad host diagnostics ===')
    $null = $lines.Add(('output: {0}' -f $outPath))
    $null = $lines.Add(('generated_at: {0}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')))
    $null = $lines.Add(('repo_root: {0}' -f $repoRoot))
    $null = $lines.Add(('computer: {0}' -f $env:COMPUTERNAME))
    $null = $lines.Add('note: triage snapshot only. This is not milestone evidence and never proves a gate (PRD 7.4 / 9.2).')
    $null = $lines.Add('note: no tokens, private keys or user documents are collected.')

    # --- python ------------------------------------------------------------
    $null = $lines.Add('')
    $null = $lines.Add('--- python ---')
    $pythonCmd = $env:UNIPAD_PYTHON
    if ([string]::IsNullOrWhiteSpace($pythonCmd)) { $pythonCmd = 'python' }
    $null = $lines.Add(('command: {0}' -f $pythonCmd))
    $pythonCommand = Get-Command -Name $pythonCmd -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        $null = $lines.Add('found: false')
        $null = $lines.Add('version: unavailable (interpreter not on PATH)')
    }
    else {
        $null = $lines.Add('found: true')
        $null = $lines.Add(('source: {0}' -f $pythonCommand.Source))
        $pythonRun = Invoke-NativeCapture -FilePath $pythonCmd -ArgumentList @('--version')
        $versionLine = Get-FirstLine -Text (($pythonRun.StdOut + ' ' + $pythonRun.StdErr).Trim())
        if ($versionLine -eq '') {
            $versionLine = ('unavailable (exit code {0})' -f $pythonRun.ExitCode)
        }
        $null = $lines.Add(('version: {0}' -f $versionLine))
        $importRun = Invoke-NativeCapture -FilePath $pythonCmd -ArgumentList @('-c', 'import unipad; print(unipad.__version__)')
        $importLine = Get-FirstLine -Text $importRun.StdOut
        if ($importRun.ExitCode -eq 0 -and $importLine -ne '') {
            $null = $lines.Add(('unipad_importable: true (version {0})' -f $importLine))
        }
        else {
            $null = $lines.Add('unipad_importable: false (editable .pth install missing? see README)')
        }
    }

    # --- qemu --------------------------------------------------------------
    $null = $lines.Add('')
    $null = $lines.Add('--- qemu ---')
    $qemuCommand = Get-Command -Name 'qemu-system-aarch64' -ErrorAction SilentlyContinue
    if ($null -eq $qemuCommand) {
        $null = $lines.Add('qemu-system-aarch64: not found on PATH')
    }
    else {
        $null = $lines.Add(('qemu-system-aarch64: {0}' -f $qemuCommand.Source))
        $qemuRun = Invoke-NativeCapture -FilePath 'qemu-system-aarch64' -ArgumentList @('--version')
        $qemuLine = Get-FirstLine -Text $qemuRun.StdOut
        if ($qemuLine -eq '') { $qemuLine = ('unavailable (exit code {0})' -f $qemuRun.ExitCode) }
        $null = $lines.Add(('version: {0}' -f $qemuLine))
    }

    # --- hyper-v -----------------------------------------------------------
    $null = $lines.Add('')
    $null = $lines.Add('--- hyper-v ---')
    try {
        $computerSystem = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
        if ($computerSystem.HypervisorPresent) {
            $null = $lines.Add('hypervisor_present: true')
        }
        else {
            $null = $lines.Add('hypervisor_present: false')
        }
    }
    catch {
        $null = $lines.Add(('hypervisor_present: unavailable ({0})' -f $_.Exception.Message))
    }
    try {
        $vmms = Get-CimInstance -ClassName Win32_Service -Filter "Name='vmms'" -ErrorAction Stop
        if ($null -eq $vmms) {
            $null = $lines.Add('vmms_service: not installed')
        }
        else {
            $null = $lines.Add(('vmms_service: {0}' -f $vmms.State))
        }
    }
    catch {
        $null = $lines.Add(('vmms_service: unavailable ({0})' -f $_.Exception.Message))
    }
    $null = $lines.Add('note: Hyper-V presence does not authorise WHPX. QEMU must run with explicit -accel tcg (errors.ACCELERATOR_NOT_TCG).')

    # --- disks -------------------------------------------------------------
    $null = $lines.Add('')
    $null = $lines.Add('--- disks (fixed drives) ---')
    try {
        $disks = @(Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=3' -ErrorAction Stop | Sort-Object DeviceID)
        if ($disks.Count -eq 0) {
            $null = $lines.Add('(no fixed drives reported)')
        }
        foreach ($disk in $disks) {
            $sizeGb = 'unknown'
            $freeGb = 'unknown'
            if ($null -ne $disk.Size -and $disk.Size -gt 0) {
                $sizeGb = [math]::Round([double]$disk.Size / 1GB, 1).ToString('0.0')
            }
            if ($null -ne $disk.FreeSpace) {
                $freeGb = [math]::Round([double]$disk.FreeSpace / 1GB, 1).ToString('0.0')
            }
            # Win32_LogicalDisk.DeviceID already carries the colon ('C:').
            $null = $lines.Add(('{0} total_gb={1} free_gb={2}' -f $disk.DeviceID, $sizeGb, $freeGb))
        }
    }
    catch {
        $null = $lines.Add(('unavailable ({0})' -f $_.Exception.Message))
    }

    # --- recent run evidence ----------------------------------------------
    $null = $lines.Add('')
    $null = $lines.Add('--- recent runs/*/evidence.json ---')
    $evidenceFiles = @()
    if (Test-Path -LiteralPath $runsDir) {
        $evidenceFiles = @(Get-ChildItem -LiteralPath $runsDir -Filter 'evidence.json' -Recurse -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 5)
    }
    if ($evidenceFiles.Count -eq 0) {
        $null = $lines.Add('(none found under runs/)')
    }
    else {
        foreach ($file in $evidenceFiles) {
            $relative = $file.FullName
            if ($relative.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
                $relative = $relative.Substring($repoRoot.Length).TrimStart('\', '/')
            }
            $null = $lines.Add(('{0}  last_write={1}  bytes={2}' -f $relative, $file.LastWriteTime.ToString('yyyy-MM-ddTHH:mm:ss'), $file.Length))
        }
    }

    # --- git ---------------------------------------------------------------
    $null = $lines.Add('')
    $null = $lines.Add('--- git ---')
    $gitCommand = Get-Command -Name 'git' -ErrorAction SilentlyContinue
    if ($null -eq $gitCommand) {
        $null = $lines.Add('git: not found on PATH')
    }
    else {
        $gitRun = Invoke-NativeCapture -FilePath 'git' -ArgumentList @('-C', $repoRoot, 'rev-parse', 'HEAD')
        $commit = Get-FirstLine -Text $gitRun.StdOut
        if ($gitRun.ExitCode -eq 0 -and $commit -match '^[0-9a-fA-F]{7,64}$') {
            $null = $lines.Add(('commit: {0}' -f $commit))
        }
        else {
            $gitError = Get-FirstLine -Text $gitRun.StdErr
            if ($gitError -eq '') { $gitError = ('exit code {0}' -f $gitRun.ExitCode) }
            $null = $lines.Add(('commit: unavailable ({0})' -f $gitError))
        }
    }

    $content = ($lines -join "`r`n") + "`r`n"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($outPath, $content, $utf8NoBom)

    # Read back before reporting, then print exactly what is on disk.
    $onDisk = [System.IO.File]::ReadAllText($outPath)
    if ($onDisk -ne $content) {
        Stop-UniPadScript -Code 'evidence_incomplete' `
            -Message "diagnostics file $outPath does not read back as written." `
            -ExitCode $script:ExitFailure
    }

    Write-Output $onDisk
    exit $script:ExitOk
}
catch {
    Stop-UniPadScript -Code 'blocked' -Message $_.Exception.Message -ExitCode $script:ExitFailure -Details @{ exception = $_.Exception.GetType().FullName }
}
