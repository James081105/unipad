"""Windows host inventory collection (PRD 4.1, 6 WP00, 9.2).

``collect_host_inventory`` produces the machine-readable host inventory that
every experiment journal must reference (PRD 7.3 ``runtime.host_inventory``)
and that PRD 9.2 lists as part of the minimum runtime evidence set.

Design rules implemented here:

- **stdlib only.**  ``ctypes`` is used for ``GlobalMemoryStatusEx``; a
  PowerShell ``Get-CimInstance`` probe is used only as an optional enrichment
  and every probe degrades to ``None`` plus a note instead of raising.
- **Never fabricate.**  A value that could not be measured is ``None`` (or an
  empty list) with a note explaining why.  The function never invents a CPU
  name, a GPU model, or a hypervisor state.
- **Deterministic ordering** so repeated runs diff cleanly: drives are sorted
  by letter, CIM lists keep the order the OS reported.

The output schema (``schema_version`` 1) matches ``host-inventory.json`` at the
repository root::

    {"schema_version": 1, "collected_at": ..., "collector": ...,
     "host": {"os": ..., "kernel": ..., "arch": ...},
     "cpu": {"model": ..., "vendor": ..., "cores_physical": ...,
             "threads_logical": ..., "vt_x": ..., "hypervisor_present": ...,
             "hypervisor_note": ...},
     "memory": {"total_gb": ..., "free_gb_at_inventory": ..., "note": ...},
     "gpu": {"model": ..., "vram_gb": ..., "virtual_displays": [...], "note": ...},
     "disks": [{"device": "C:", "size_gb": ..., "free_gb": ...}, ...],
     "physical_disks": [{"model": ..., "size_gb": ...}, ...],
     "toolchain": {...}, "notes": [...]}
"""

from __future__ import annotations

import ctypes
import json
import locale
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from unipad.host.paths import safe_location

__all__ = [
    "INVENTORY_SCHEMA_VERSION",
    "collect_host_inventory",
    "write_host_inventory",
]

#: Schema version of the emitted dictionary (PRD 9.2 / host-inventory.json).
INVENTORY_SCHEMA_VERSION = 1

_COLLECTOR = "unipad.host.inventory"

#: PowerShell-only creation flag that keeps probe windows invisible.
_CREATE_NO_WINDOW = 0x08000000

_GIB = 1024 ** 3

#: Free-space thresholds (GiB) used to annotate drives.
_CRITICAL_FREE_GIB = 2.0
_LOW_FREE_GIB = 10.0


# ---------------------------------------------------------------------------
# low-level Windows probes
# ---------------------------------------------------------------------------


class _MemoryStatusEx(ctypes.Structure):
    """``MEMORYSTATUSEX`` as required by ``GlobalMemoryStatusEx``."""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_status() -> Optional[dict[str, int]]:
    """Return ``GlobalMemoryStatusEx`` counters, or ``None`` off Windows."""
    if sys.platform != "win32":
        return None
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
    except (AttributeError, OSError):  # pragma: no cover - platform guard
        return None
    return {
        "memory_load_percent": int(status.dwMemoryLoad),
        "total_bytes": int(status.ullTotalPhys),
        "available_bytes": int(status.ullAvailPhys),
        "total_page_file_bytes": int(status.ullTotalPageFile),
        "available_page_file_bytes": int(status.ullAvailPageFile),
    }


def _windows_locale_tag() -> Optional[str]:
    """Return the host UI locale as ``zh-CN`` style, or ``None``."""
    if sys.platform != "win32":
        return None
    try:
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        name = locale.windows_locale.get(lcid)
    except (AttributeError, OSError):
        return None
    if not name:
        return None
    return name.replace("_", "-")


def _normalize_arch(machine: str) -> str:
    """Map a ``platform.machine()`` value onto the PRD vocabulary."""
    table = {"AMD64": "x86_64", "X86": "x86", "ARM64": "arm64", "EM64T": "x86_64"}
    key = (machine or "").strip().upper()
    return table.get(key, (machine or "unknown").lower())


def _host_identity() -> dict[str, Any]:
    """Return the ``host`` block: OS caption, kernel build, architecture."""
    arch = _normalize_arch(platform.machine())
    if sys.platform != "win32":
        return {
            "os": f"{platform.system()} {platform.release()}",
            "kernel": platform.release(),
            "arch": arch,
            "note": "non-Windows host: Windows-only fields are unavailable",
        }
    try:
        version = sys.getwindowsversion()
        build = int(version.build)
        major, minor = int(version.major), int(version.minor)
    except (AttributeError, ValueError, TypeError):  # pragma: no cover
        build, major, minor = 0, 0, 0
    caption = "Microsoft Windows 11" if build >= 22000 else "Microsoft Windows 10"
    try:
        edition = platform.win32_edition()
    except (AttributeError, OSError):
        edition = ""
    if edition:
        caption = f"{caption} {edition}"
    tag = _windows_locale_tag()
    if tag:
        caption = f"{caption} ({tag})"
    return {"os": caption, "kernel": f"{major}.{minor}.{build}", "arch": arch}


# ---------------------------------------------------------------------------
# optional CIM probe (single PowerShell invocation)
# ---------------------------------------------------------------------------

_CIM_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$cs  = Get-CimInstance Win32_ComputerSystem
$gpu = @(Get-CimInstance Win32_VideoController |
         Select-Object Name, AdapterRAM, DriverVersion)
$disks = @(Get-CimInstance Win32_DiskDrive | Select-Object Model, Size)
[pscustomobject]@{
    hypervisor_present      = $cs.HypervisorPresent
    system_manufacturer     = $cs.Manufacturer
    system_model            = $cs.Model
    total_physical_memory   = $cs.TotalPhysicalMemory
    cpu_name                = $cpu.Name
    cpu_vendor              = $cpu.Manufacturer
    cpu_cores               = $cpu.NumberOfCores
    cpu_threads             = $cpu.NumberOfLogicalProcessors
    cpu_virtualization      = $cpu.VirtualizationFirmwareEnabled
    gpu                     = $gpu
    physical_disks          = $disks
} | ConvertTo-Json -Depth 5 -Compress
"""


def _powershell_executable() -> Optional[str]:
    """Locate ``powershell`` (or ``pwsh``) without touching the filesystem."""
    return shutil.which("powershell") or shutil.which("pwsh")


def _run_powershell(script: str, timeout: float) -> tuple[bool, str, str]:
    """Run a PowerShell snippet.

    Returns ``(ok, stdout, note)``.  ``ok`` is ``False`` whenever the shell is
    missing, times out, exits non-zero, or cannot be started; the reason is
    always carried in ``note`` so callers degrade instead of guessing.
    """
    executable = _powershell_executable()
    if executable is None:
        return False, "", "powershell/pwsh not found on PATH"
    command = [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired:
        return False, "", f"powershell timed out after {timeout:g}s"
    except OSError as exc:
        return False, "", f"powershell could not be started ({type(exc).__name__})"
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit status {completed.returncode}"
        return False, completed.stdout or "", f"powershell exited {completed.returncode}: {reason}"
    return True, completed.stdout or "", ""


def _probe_cim(timeout: float) -> tuple[dict[str, Any], Optional[str]]:
    """Query CIM for CPU/GPU/hypervisor/physical-disk facts.

    Returns ``(facts, note)``; ``facts`` is empty and ``note`` set when the
    probe is unavailable.
    """
    ok, stdout, note = _run_powershell(_CIM_SCRIPT, timeout)
    if not ok:
        return {}, note or "cim probe failed"
    text = stdout.strip()
    if not text:
        return {}, "cim probe returned no output"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"cim probe output was not JSON ({exc.msg})"
    if not isinstance(parsed, dict):
        return {}, "cim probe output was not a JSON object"
    return parsed, None


def _as_int(value: Any) -> Optional[int]:
    """Best-effort integer coercion for CIM values (``None`` when unusable)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------


def _cpu_block(notes: list[str], cim: dict[str, Any]) -> dict[str, Any]:
    """Build the ``cpu`` block, preferring measured values over fallbacks."""
    model = platform.processor() or ""
    vendor = None
    cores: Optional[int] = None
    threads: Optional[int] = None
    virtualized: Optional[bool] = None

    cim_name = cim.get("cpu_name")
    if isinstance(cim_name, str) and cim_name.strip():
        model = cim_name.strip()
        notes.append("cpu.model came from Win32_Processor.Name (platform.processor() fallback)")
    cim_vendor = cim.get("cpu_vendor")
    if isinstance(cim_vendor, str) and cim_vendor.strip():
        vendor = cim_vendor.strip()
    cores = _as_int(cim.get("cpu_cores"))
    threads = _as_int(cim.get("cpu_threads"))
    virtualization = cim.get("cpu_virtualization")
    if isinstance(virtualization, bool):
        virtualized = virtualization

    if threads is None:
        threads = os.cpu_count()
        notes.append("cpu.threads_logical fell back to os.cpu_count() (Win32_Processor unavailable)")
    if cores is None:
        # os.cpu_count() reports logical processors on Windows, so it can only
        # serve as an upper bound; record it rather than inventing a value.
        cores = os.cpu_count()
        notes.append(
            "cpu.cores_physical is an upper bound: os.cpu_count() reports logical "
            "processors on Windows and Win32_Processor.NumberOfCores was unavailable"
        )

    if virtualized is True:
        vt_x = "enabled_in_firmware"
    elif virtualized is False:
        vt_x = "present_disabled_in_firmware"
    else:
        vt_x = "unknown"

    vt_note = (
        "VT-x/AMD-V enablement is firmware state. Note that Win32_Processor."
        "VirtualizationFirmwareEnabled reports False while Hyper-V owns the "
        "CPU, so this value is not proof that VT-x is disabled in firmware. "
        "UniPad always runs explicit TCG (PRD 2.2) regardless."
    )

    return {
        "model": model or "unknown",
        "vendor": vendor,
        "cores_physical": cores if cores and cores > 0 else 1,
        "threads_logical": threads if threads and threads > 0 else 1,
        "vt_x": vt_x,
        "vt_x_note": vt_note,
        "platform_processor": platform.processor() or None,
    }


def _hypervisor_block(cim: dict[str, Any], cim_note: Optional[str]) -> tuple[Optional[bool], str]:
    """Return ``(hypervisor_present, hypervisor_note)``."""
    present = cim.get("hypervisor_present")
    if isinstance(present, bool):
        if present:
            return True, (
                "Hyper-V (or another hypervisor) is active on this host. WHPX availability "
                "must be probed at M0; QEMU must run with explicit TCG (-accel tcg) and "
                "must not silently fall back to WHPX/HVF. Record the actual accelerator "
                "at runtime."
            )
        return False, "No hypervisor was reported by Win32_ComputerSystem.HypervisorPresent."
    return None, (
        "hypervisor_present could not be determined"
        + (f": {cim_note}" if cim_note else " (Win32_ComputerSystem unavailable)")
        + ". Treat as unknown; do not assume WHPX is either available or absent."
    )


def _memory_block(notes: list[str]) -> dict[str, Any]:
    """Build the ``memory`` block from ``GlobalMemoryStatusEx``."""
    status = _memory_status()
    if status is None:
        notes.append("memory could not be read (GlobalMemoryStatusEx unavailable)")
        return {
            "total_gb": None,
            "free_gb_at_inventory": None,
            "memory_load_percent": None,
            "note": "GlobalMemoryStatusEx was unavailable; guest memory budget must not be sized from this value.",
        }
    total = round(status["total_bytes"] / _GIB, 1)
    free = round(status["available_bytes"] / _GIB, 1)
    note = (
        "Guest memory budget must be fixed in config/machine.json; host free memory "
        "at inventory time reflects other running processes."
    )
    if status["available_bytes"] / _GIB < 4.0:
        note += " Free memory is low: keep guest RAM conservative and record peak RSS."
    return {
        "total_gb": total,
        "free_gb_at_inventory": free,
        "memory_load_percent": status["memory_load_percent"],
        "note": note,
    }


def _gpu_block(cim: dict[str, Any], cim_note: Optional[str], notes: list[str]) -> dict[str, Any]:
    """Build the ``gpu`` block; an unavailable probe yields ``[]`` plus a note."""
    adapters: list[dict[str, Any]] = []
    raw = cim.get("gpu")
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("Name")
            if not isinstance(name, str) or not name.strip():
                continue
            vram_bytes = _as_int(item.get("AdapterRAM"))
            adapters.append(
                {
                    "name": name.strip(),
                    "driver_version": item.get("DriverVersion") if isinstance(item.get("DriverVersion"), str) else None,
                    "vram_gb": round(vram_bytes / _GIB, 1) if vram_bytes else None,
                }
            )
    if not adapters:
        reason = cim_note or "Win32_VideoController returned no adapters"
        notes.append(f"gpu list unavailable: {reason}")
        return {
            "model": None,
            "vram_gb": None,
            "virtual_displays": [],
            "adapters": [],
            "note": (
                f"GPU inventory unavailable ({reason}). P2 scope only; the launcher must "
                "target the QEMU window by title/pid, never by adapter."
            ),
        }
    primary = next((a for a in adapters if not _looks_virtual_display(a["name"])), adapters[0])
    virtual = [a["name"] for a in adapters if _looks_virtual_display(a["name"])]
    return {
        "model": primary["name"],
        "vram_gb": primary["vram_gb"],
        "virtual_displays": virtual,
        "adapters": adapters,
        "note": (
            "P2 scope only. Virtual display adapters may interfere with window enumeration; "
            "launcher must target the QEMU window by title/pid, not by adapter."
        ),
    }


def _looks_virtual_display(name: str) -> bool:
    """Heuristic: does an adapter name look like a virtual display driver?"""
    lowered = name.lower()
    return "virtual" in lowered or "idd" in lowered or "mirror" in lowered


def _disk_block(notes: list[str]) -> list[dict[str, Any]]:
    """Enumerate fixed drives A-Z via ``shutil.disk_usage`` (skip missing)."""
    entries: list[dict[str, Any]] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            usage = shutil.disk_usage(root)
        except (OSError, PermissionError):
            continue
        size_gb = round(usage.total / _GIB, 1)
        free_gb = round(usage.free / _GIB, 1)
        entry: dict[str, Any] = {
            "device": f"{letter}:",
            "size_gb": size_gb,
            "free_gb": free_gb,
        }
        if free_gb < _CRITICAL_FREE_GIB:
            entry["warning"] = "critically low free space; do not place artifacts or images here"
        elif free_gb < _LOW_FREE_GIB:
            entry["warning"] = "low free space; verify image sizes before writing"
        entries.append(entry)
    if not entries:
        notes.append("no drives could be enumerated with shutil.disk_usage")
    return entries


def _physical_disk_block(cim: dict[str, Any], notes: list[str]) -> list[dict[str, Any]]:
    """Build ``physical_disks`` from Win32_DiskDrive (empty when unavailable)."""
    raw = cim.get("physical_disks")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        notes.append("physical_disks unavailable (Win32_DiskDrive probe failed)")
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        model = item.get("Model")
        if not isinstance(model, str) or not model.strip():
            continue
        size = _as_int(item.get("Size"))
        entries.append(
            {
                "model": model.strip(),
                "size_gb": round(size / _GIB, 1) if size else None,
            }
        )
    return entries


def _command_version(command: str, timeout: float) -> Optional[str]:
    """Return the first line of ``<command> --version``, or ``None``."""
    try:
        completed = subprocess.run(
            [command, "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **( {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {} ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    text = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    if not text:
        return None
    return text.splitlines()[0].strip()


def _distribution_version(distribution: str) -> Optional[str]:
    """Return an installed distribution version via ``importlib.metadata``."""
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python < 3.8
        return None
    try:
        return metadata.version(distribution)
    except Exception:
        return None


def _toolchain_block(timeout: float, notes: list[str]) -> dict[str, Any]:
    """Report the toolchain versions relevant to M0 (PRD 6 WP00)."""
    toolchain: dict[str, Any] = {
        "python": {
            # sys.executable is an absolute path, and on this host that means
            # the profile directory -- i.e. the Windows username -- reaches a
            # file that is committed to the repository.  PRD 9.2 forbids it,
            # so the interpreter is named by its final component and the note
            # records what was withheld and why.
            "command": safe_location(sys.executable),
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "pytest": _distribution_version("pytest"),
            "pip": _distribution_version("pip"),
        }
    }
    notes.append(
        "toolchain.python.command is the interpreter's final path component; "
        "the absolute path embeds the Windows username and this inventory is "
        "committed (PRD 9.2)."
    )
    for name, command in (("git", "git"), ("gh", "gh"), ("node", "node")):
        version = _command_version(command, timeout)
        if version is None:
            notes.append(f"{name} not available on PATH or --version failed")
        toolchain[name] = version
    return toolchain


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def collect_host_inventory(
    *,
    probe_cim: bool = True,
    probe_timeout: float = 10.0,
    toolchain_timeout: float = 5.0,
    clock: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Collect the Windows host inventory (PRD 6 WP00, 9.2).

    Args:
        probe_cim: When ``True`` (default) a single PowerShell
            ``Get-CimInstance`` call enriches CPU/GPU/hypervisor/physical-disk
            facts.  Set ``False`` for a stdlib-only, subprocess-free snapshot.
        probe_timeout: Per-probe timeout in seconds (default 10).
        toolchain_timeout: Per-tool ``--version`` timeout in seconds.
        clock: Injectable ``time.time`` replacement for deterministic tests.

    Returns:
        A ``dict`` with ``schema_version == 1`` shaped like
        ``host-inventory.json``.  Any value that could not be measured is
        ``None`` (or an empty list) and the reason is recorded in ``notes``;
        the function never raises for a missing tool.

    Note:
        ``cpu.cores_physical`` prefers the measured
        ``Win32_Processor.NumberOfCores`` / ``psutil.cpu_count(logical=False)``.
        ``os.cpu_count()`` reports *logical* processors on Windows and is only
        used as a documented upper-bound fallback.
    """
    now = (clock or time.time)()
    notes: list[str] = []

    cim: dict[str, Any] = {}
    cim_note: Optional[str] = None
    if probe_cim:
        cim, cim_note = _probe_cim(probe_timeout)
        if cim_note is not None:
            notes.append(f"cim probe degraded: {cim_note}")
    else:
        cim_note = "cim probe disabled by caller"
        notes.append(f"cim probe skipped: {cim_note}")

    # psutil is an optional extra: it only fills gaps the CIM probe left open.
    _try_psutil(cim, notes)

    hypervisor_present, hypervisor_note = _hypervisor_block(cim, cim_note)

    inventory: dict[str, Any] = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "collected_at": _iso_date(now),
        "collected_at_epoch": int(now),
        "collector": _COLLECTOR,
        "host": _host_identity(),
        "cpu": _cpu_block(notes, cim),
        "memory": _memory_block(notes),
        "gpu": _gpu_block(cim, cim_note, notes),
        "disks": _disk_block(notes),
        "physical_disks": _physical_disk_block(cim, notes),
        "toolchain": _toolchain_block(toolchain_timeout, notes),
        "hypervisor_present": hypervisor_present,
        "hypervisor_note": hypervisor_note,
        "tcg_implications": _tcg_implications(hypervisor_present),
        "notes": notes,
    }
    inventory["cpu"]["hypervisor_present"] = hypervisor_present
    inventory["cpu"]["hypervisor_note"] = hypervisor_note
    # Convenience aliases for the two most-used host facts.  ``host`` keeps the
    # canonical host-inventory.json shape; these mirrors let callers read the
    # OS caption without knowing that nesting.
    inventory["os"] = inventory["host"]["os"]
    inventory["arch"] = inventory["host"]["arch"]
    return inventory


def _try_psutil(cim: dict[str, Any], notes: list[str]) -> bool:
    """Enrich CPU core counts with ``psutil`` when it is importable."""
    try:
        import psutil  # type: ignore import-not-found
    except Exception:
        return False
    try:
        physical = psutil.cpu_count(logical=False)
        logical = psutil.cpu_count(logical=True)
    except Exception:
        notes.append("psutil importable but cpu_count() failed")
        return False
    if isinstance(physical, int) and physical > 0 and "cpu_cores" not in cim:
        cim["cpu_cores"] = physical
        notes.append("cpu.cores_physical came from psutil.cpu_count(logical=False)")
    if isinstance(logical, int) and logical > 0 and "cpu_threads" not in cim:
        cim["cpu_threads"] = logical
        notes.append("cpu.threads_logical came from psutil.cpu_count(logical=True)")
    return True


def _iso_date(epoch: float) -> str:
    """Return the UTC ``YYYY-MM-DD`` stamp for an epoch value."""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def _tcg_implications(hypervisor_present: Optional[bool]) -> list[str]:
    """Fixed host->TCG implications recorded with every inventory (PRD 2.2)."""
    items = [
        "Intel host: no ARM64 execution; QEMU TCG is mandatory (PRD 2.2-1).",
        "The launcher refuses any accelerator other than explicit tcg (accelerator_not_tcg).",
    ]
    if hypervisor_present is True:
        items.append(
            "Hyper-V is present: probe WHPX at M0; if WHPX is active, TCG still runs but "
            "verify no conflict with Hyper-V memory partitioning."
        )
    elif hypervisor_present is None:
        items.append("Hypervisor state unknown: do not assume WHPX is available or absent.")
    return items


def write_host_inventory(path: str | Path, inventory: dict[str, Any]) -> Path:
    """Write ``inventory`` to ``path`` as pretty, key-sorted JSON.

    Args:
        path: Destination file; parent directories are created.
        inventory: A dictionary produced by :func:`collect_host_inventory`.

    Returns:
        The absolute path that was written.

    Raises:
        UniPadError: With ``artifact_manifest_invalid`` if ``inventory`` is not a
            mapping or does not declare ``schema_version``.
    """
    from unipad.errors import ErrorCode, UniPadError

    if not isinstance(inventory, dict):
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "host inventory must be a dict",
            received_type=type(inventory).__name__,
        )
    if inventory.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "host inventory must declare schema_version 1",
            schema_version=inventory.get("schema_version"),
        )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return target.resolve()
