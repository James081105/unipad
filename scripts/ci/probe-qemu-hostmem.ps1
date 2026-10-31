<#
.SYNOPSIS
  Measure which hostmem backends a QEMU w64 build registers.

.DESCRIPTION
  The gate question for PRD item 2 of the current goal: does a QEMU build exist
  that registers `memory-backend-file`?  This host's pinned 11.1.0 does not
  (fact 189: `-object help` prints exactly one hostmem backend,
  `memory-backend-ram`, and `-object memory-backend-file,...` exits 1 with
  `invalid object type`), and this host's route to qemu.weilnetz.de will not
  carry the 206 MB installer to find out for a newer one.

  For each requested build this script:

    1. downloads the installer;
    2. extracts it with 7-Zip (the installer is NSIS, a flat layout);
    3. asks `qemu-system-aarch64.exe -object help` for every hostmem backend;
    4. asks `-object memory-backend-file,id=probe,size=16M,mem-path=<tmp>` and
       records accept or refuse, with the exact stderr;
    5. asks the NEGATIVE CONTROL, `-object memory-backend-ram,...`, which is
       known-present on this host.

  Step 5 is the reason a negative answer can be trusted.  If the control is
  refused while `memory-backend-file` is also refused, the instrument is broken
  -- the same shape as the QMP client that never sent `qmp_capabilities` and
  made `pmemsave` look absent for days (fact: validate the instrument before
  trusting a negative).

  Three verdicts, kept distinct on purpose:

    present    -- listed in `-object help` AND accepted.
    absent     -- not listed, or listed and refused, WITH the control accepted.
    unmeasured -- not downloaded, not extracted, or would not run.  This is the
                  only verdict that fails the step.

  An absent verdict is a legitimate answer and does not fail the job.  Only
  "we could not measure" does, because that must never be recorded as absent.

.OUTPUTS
  qemu-hostmem-probe.json in the workspace root, plus a step summary.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Versions,

    [string]$Output = "qemu-hostmem-probe.json",

    #: Probe a binary already on disk instead of downloading one, as
    #: ``label|path|exe``.  This exists so the instrument can be validated on a
    #: build whose answer is already known -- the pinned 11.1.0, measured by hand
    #: as fact 189 -- rather than first being trusted against a 206 MB download.
    #: An instrument that has never produced a known answer is not an instrument.
    [string]$LocalBinary
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is minutes slower without this

#: Used when the caller supplies nothing.  A `push`-triggered run has no
#: workflow_dispatch inputs at all, so `inputs.versions` is the empty string --
#: and an empty list would make the job "succeed" having measured nothing, which
#: is the one outcome this script exists to prevent.  So the default lives here,
#: where it applies to every trigger.
$DEFAULT_VERSIONS = "2026.8.11|https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811.exe"
if ([string]::IsNullOrWhiteSpace($Versions)) {
    Write-Host "::notice::no versions supplied; using the default: $DEFAULT_VERSIONS"
    $Versions = $DEFAULT_VERSIONS
}

# 7-Zip is only needed for the download path.  ``-LocalBinary`` probes a binary
# already on disk, and requiring an extractor it will never use would make the
# instrument unrunnable on a host that has the binary but not 7-Zip -- which is
# this host, and the only place the instrument can be validated against a
# known answer before it is trusted with a 206 MB download.
$SevenZip = $null
if (-not $LocalBinary) {
    $SevenZip = @(
        "C:\Program Files\7-Zip\7z.exe",
        "C:\Program Files (x86)\7-Zip\7z.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $SevenZip) {
        Write-Error "::error::7-Zip is not on this runner image; the NSIS installer cannot be extracted"
        exit 1
    }
}

$workspace = (Get-Location).Path
$scratch = Join-Path $workspace "qemu-hostmem-scratch"
New-Item -ItemType Directory -Force -Path $scratch | Out-Null

function Invoke-Qemu([string]$exe, [string[]]$qemuArgs, [int]$TimeoutSec = 8) {
    # Run QEMU briefly and return its exit code, stdout and stderr.
    #
    # Three bugs this shape avoids, every one of them found by RUNNING it on
    # this host rather than by reasoning about it:
    #
    # 1. `& $exe ... 2>&1` on Windows PowerShell 5.1 wraps every stderr line in
    #    an ErrorRecord and, with $ErrorActionPreference='Stop' at the top of
    #    this script, makes it terminating -- so the FIRST probe, whose whole
    #    point is to record QEMU's refusal, aborted the measurement and
    #    reported "unmeasured" for a binary that had answered plainly.
    # 2. `-S` tells QEMU not to start the CPU, so a process that parsed its
    #    arguments successfully NEVER EXITS.  `Start-Process -Wait` on that
    #    blocks forever.
    # 3. `Start-Process -PassThru` hands back a Process object whose ExitCode
    #    reads as EMPTY for a process that exited 0, so the verdict came back
    #    "exited (blank)".  System.Diagnostics.Process with WaitForExit()
    #    returns the real code, and async reads avoid the pipe deadlock that
    #    synchronous RedirectStandardOutput would hit on a long-lived child.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = ($qemuArgs | ForEach-Object {
        if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
    }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdout = New-Object System.Text.StringBuilder
    $stderr = New-Object System.Text.StringBuilder
    $outEvent = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action {
        if ($EventArgs.Data) { $Event.MessageData.AppendLine($EventArgs.Data) }
    } -MessageData $stdout
    $errEvent = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action {
        if ($EventArgs.Data) { $Event.MessageData.AppendLine($EventArgs.Data) }
    } -MessageData $stderr
    $proc.BeginOutputReadLine()
    $proc.BeginErrorReadLine()

    if ($proc.WaitForExit($TimeoutSec * 1000)) {
        # Exited inside the window: the real code, then drain the events.
        $code = $proc.ExitCode
        Wait-Event -SourceIdentifier $outEvent.Name -Timeout 2 | Out-Null
        Wait-Event -SourceIdentifier $errEvent.Name -Timeout 2 | Out-Null
    } else {
        # Still alive: the arguments parsed, which IS the answer.  Kill it so a
        # stopped VM does not sit in the process table looking like a leak.
        try { $proc.Kill() } catch { }
        try { $null = $proc.WaitForExit(5000) } catch { }
        $code = 0
    }
    Unregister-Event -SubscriptionId $outEvent.Id -ErrorAction SilentlyContinue
    Unregister-Event -SubscriptionId $errEvent.Id -ErrorAction SilentlyContinue

    return [pscustomobject]@{
        exit_code   = $code
        still_alive = (-not $proc.HasExited)
        stdout      = $stdout.ToString()
        stderr      = $stderr.ToString()
    }
}

function Get-HostmemBackends([string]$exe) {
    # `-object help` prints ONE flat, alphabetical list under the heading
    # "List of user creatable objects:" -- there is no "hostmem backends:"
    # sub-heading.  An earlier draft of this parser looked for that heading,
    # found nothing, and reported zero backends for a binary that lists one:
    # the same mistake as grepping for a constant instead of reading the format.
    # So the list is taken as it is and filtered by prefix afterwards, and the
    # TOTAL is recorded too -- a parse that silently returns nothing must be
    # distinguishable from a binary that really has none.
    $run = Invoke-Qemu $exe @("-object", "help")
    if ($run.exit_code -ne 0) {
        throw "`-object help exited $($run.exit_code) : $(($run.stderr + $run.stdout).Substring(0, [Math]::Min(300, ($run.stderr + $run.stdout).Length)))"
    }
    $names = New-Object System.Collections.Generic.List[string]
    foreach ($line in ($run.stdout -split "`r?`n")) {
        $name = $line.Trim()
        if (-not $name) { continue }
        if ($name -match "^[a-z0-9][a-z0-9-]*$") { $names.Add($name) }
    }
    $script:ObjectTypeCount = $names.Count
    $backends = $names | Where-Object { $_ -like "memory-backend*" }
    return ,@($backends)
}

function Test-ObjectBackend([string]$exe, [string]$spec, [string]$tag) {
    # A minimal, valid `-object` line for this backend.  `memory-backend-ram`
    # needs only a size; `-file` needs a mem-path, so a scratch file is used.
    $memPath = Join-Path $scratch "$tag.bin"
    $object = if ($spec -eq "memory-backend-file") {
        "memory-backend-file,id=probe_$tag,size=16M,mem-path=$memPath,share=on"
    } elseif ($spec -eq "memory-backend-memfd") {
        "memory-backend-memfd,id=probe_$tag,size=16M"
    } else {
        "$spec,id=probe_$tag,size=16M"
    }
    # A machine that does nothing still parses `-object`, which is all this asks:
    # is the TYPE accepted.  `-machine none -S` keeps it from running anything.
    #
    # A non-zero exit is THE ANSWER, not an error: QEMU writes
    # "invalid object type: memory-backend-file" to stderr and exits 1.
    $run = Invoke-Qemu $exe @("-machine", "none", "-S", "-display", "none", "-object", $object)
    return [pscustomobject]@{
        spec      = $spec
        accepted  = ($run.exit_code -eq 0)
        exit_code = $run.exit_code
        stderr    = ($run.stderr + $run.stdout).Trim()
    }
}

$results = New-Object System.Collections.Generic.List[object]
$anyUnmeasured = $false

foreach ($entry in ($Versions -split ";" | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
    $label, $url = $entry -split "\|", 2
    $label = $label.Trim()
    $url = $url.Trim()
    $record = [pscustomobject]@{
        label        = $label
        url          = $url
        verdict      = "unmeasured"
        reason       = ""
        hostmem      = @()
        probes       = @()
        control_ok   = $false
        version_line = ""
        seconds      = 0
    }
    $started = Get-Date
    $exe = $null
    try {
        if ($LocalBinary) {
            # Already on disk: no download, no extraction.  The url in the entry
            # is overwritten so the record cannot be mistaken for something a
            # runner fetched.
            $exe = Get-Item $LocalBinary -ErrorAction Stop
            $record.url = "local:$($exe.FullName)"
        } else {
            $installer = Join-Path $scratch "$label-setup.exe"
            Write-Host "::group::downloading $label"
            try {
                Invoke-WebRequest -Uri $url -OutFile $installer -MaximumRetryCount 5 -RetryIntervalSec 5
            } catch {
                throw "download failed: $($_.Exception.Message)"
            } finally {
                Write-Host "::endgroup::"
            }
            $size = (Get-Item $installer).Length
            Write-Host "$label installer is $size bytes"

            $target = Join-Path $scratch $label
            New-Item -ItemType Directory -Force -Path $target | Out-Null
            Write-Host "::group::extracting $label"
            & $SevenZip x $installer "-o$target" -y | Out-Host
            if ($LASTEXITCODE -gt 1) { throw "7z exited $LASTEXITCODE" }
            Write-Host "::endgroup::"

            $exe = Get-ChildItem -Path $target -Recurse -Filter "qemu-system-aarch64.exe" |
                Select-Object -First 1
            if (-not $exe) { throw "qemu-system-aarch64.exe was not in the installer" }
        }
        Write-Host "binary: $($exe.FullName)"

        $version = & $exe --version 2>&1 | Out-String
        $record.version_line = ($version -split "`r?`n" | Where-Object { $_ -match "QEMU emulator version" } | Select-Object -First 1)

        $record.hostmem = Get-HostmemBackends $exe.FullName
        Write-Host "object types listed: $script:ObjectTypeCount; hostmem backends: $($record.hostmem -join ', ')"
        if ($script:ObjectTypeCount -lt 5) {
            # A parse that finds almost nothing is a broken parse, not a binary
            # with almost no objects.  Fail loudly rather than reporting "absent"
            # for a backend that was never looked for.
            throw "`-object help listed only $script:ObjectTypeCount object types; the parser is not reading the output"
        }

        foreach ($spec in @("memory-backend-file", "memory-backend-memfd", "memory-backend-ram")) {
            $record.probes += Test-ObjectBackend $exe.FullName $spec $label
        }

        # The negative control.  Without it an "absent" verdict is unfalsifiable.
        $control = $record.probes | Where-Object { $_.spec -eq "memory-backend-ram" } | Select-Object -First 1
        $record.control_ok = [bool]$control.accepted

        $wanted = $record.probes | Where-Object { $_.spec -eq "memory-backend-file" } | Select-Object -First 1
        if (-not $record.control_ok) {
            $record.verdict = "unmeasured"
            $record.reason = "the negative control (memory-backend-ram) was refused, so this binary cannot answer the question at all"
        } elseif ($wanted.accepted -and ($record.hostmem -contains "memory-backend-file")) {
            $record.verdict = "present"
            $record.reason = "listed in -object help and accepted by -object"
        } elseif ($record.hostmem -contains "memory-backend-file") {
            $record.verdict = "absent"
            $record.reason = "listed in -object help but refused by -object (exit $($wanted.exit_code))"
        } else {
            $record.verdict = "absent"
            $record.reason = "not among the hostmem backends this build registers"
        }
    } catch {
        $record.verdict = "unmeasured"
        $record.reason = $_.Exception.Message
        Write-Warning "::warning::$label could not be measured: $($_.Exception.Message)"
    }
    $record.seconds = [int]((Get-Date) - $started).TotalSeconds
    Write-Host "$label -> $($record.verdict): $($record.reason)"
    $results.Add($record)
    if ($record.verdict -eq "unmeasured") { $anyUnmeasured = $true }
}

$payload = [pscustomobject]@{
    generated_by = "scripts/ci/probe-qemu-hostmem.ps1"
    runner       = $env:RUNNER_OS
    note         = "A negative answer is a result. Only 'unmeasured' fails the step, because that must never be recorded as absent."
    builds       = $results
}
$payload | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $workspace $Output) -Encoding utf8

$summary = @()
$summary += "## QEMU hostmem probe"
$summary += ""
$summary += "| build | verdict | hostmem backends | control | seconds |"
$summary += "| --- | --- | --- | --- | --- |"
foreach ($r in $results) {
    $summary += "| $($r.label) | $($r.verdict) | $($r.hostmem -join ', ') | $($r.control_ok) | $($r.seconds) |"
}
$summary += ""
foreach ($r in $results) {
    $summary += "### $($r.label) -- $($r.verdict)"
    $summary += ""
    $summary += $r.reason
    $summary += ""
    if ($r.version_line) { $summary += "``$($r.version_line)``"; $summary += "" }
    foreach ($p in $r.probes) {
        $state = if ($p.accepted) { "accepted" } else { "refused (exit $($p.exit_code))" }
        $summary += "- ``$($p.spec)``: $state"
        if (-not $p.accepted -and $p.stderr) {
            $first = ($p.stderr -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
            if ($first) { $summary += "  - $first" }
        }
    }
    $summary += ""
}
$summary += "**What this does NOT settle:** that a boot using ``memory-backend-file``"
$summary += "works.  A backend that parses is not a window the guest and host agree on;"
$summary += "the shared-window handshake is a separate measurement."
$summaryText = ($summary -join "`n")
Write-Output $summaryText
# Outside Actions there is no step-summary file; writing to $null would fail the
# run after the measurement had already succeeded.
if ($env:GITHUB_STEP_SUMMARY) {
    Set-Content -Path $env:GITHUB_STEP_SUMMARY -Value $summaryText -Encoding utf8
} else {
    Set-Content -Path (Join-Path $workspace "$Output.summary.md") -Value $summaryText -Encoding utf8
}

if ($anyUnmeasured) {
    Write-Error "::error::at least one build could not be measured; see $Output. An unmeasured build is not an absent build."
    exit 1
}
$present = $results | Where-Object { $_.verdict -eq "present" }
if ($present) {
    Write-Host "::notice::$($present.Count) build(s) register memory-backend-file: $(($present.label) -join ', ')"
    Write-Host "::notice::Next: verify a real boot uses it, then build the host backend that answers QM_OPEN."
} else {
    Write-Host "::notice::No requested build registers memory-backend-file. PRD item 2's host side needs a different transport, not a newer QEMU."
}
exit 0
