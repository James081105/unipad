"""A real UEFI firmware boots in the real QEMU on this Windows host.

The bare-metal stub test (``test_real_boot_arm64.py``) proves the machine and the
CPU: a real AArch64 ELF executes and its UART output reaches us.  This file
proves the next layer up -- the *firmware*.  The EDK2 image pinned in
``config/sources.lock.json`` is started under the same real QEMU, and the run is
judged on what the firmware itself prints.

What this proves, and what it does not:

**Proves.**  QEMU's ``virt`` machine with the pinned board parameters brings up a
real UEFI implementation.  The firmware runs PEI, DXE and BDS; BDS finds a boot
option, starts it, and the option it starts is the EDK2 UEFI Interactive Shell,
which reaches an interactive ``Shell>`` prompt.  The whole path from argv to a
running, interactive firmware is exercised against the real binary, not a fake.

**Does not prove.**  Anything about XNU, UniXNU, iPadOS, a kernel image, or an
application.  The shell is the firmware's own built-in option, found in its
firmware volume -- it is not our payload and says nothing about ours.  No ESP is
attached, so nothing of *ours* has booted.

The firmware is not committed -- it lives in ``local-private/`` (PRD 12) -- so
this test skips when it is absent, exactly as ``requires_qemu`` does for the
binary.  What *is* committed is the pin, and the test verifies the image against
it before starting anything: booting an unpinned firmware would prove something
about a file we cannot name.  That check has already earned its keep -- the
copy under ``local-private/recon`` is a *different* build with a different
digest, and its behaviour differs (it reports no bootable option rather than
starting a shell), so an unpinned run would have asserted the wrong thing.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.acceptance.test_real_boot_arm64 import _RealQemuLock, _ascii_workdir, _qemu_binary
from unipad.models import MachineConfig

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_LOCK = REPO_ROOT / "config" / "sources.lock.json"

#: How long the firmware gets to reach the shell prompt.  The observed run gets
#: there in a few seconds; the margin covers a loaded host.
FIRMWARE_TIMEOUT = 90.0

#: Markers, in the order the pinned EDK2 build prints them.  Quoted verbatim
#: from an observed run so a change in the firmware's behaviour fails here
#: rather than passing silently.
#:
#: The banner prefix is deliberately not the full version string: the digest in
#: ``sources.lock.json`` is what identifies *which* firmware ran, and pinning the
#: build date in a test as well would make the test fail on a firmware update
#: that changed nothing about the boot path.
BANNER = "UEFI firmware (version edk2-stable"
#: BDS found a boot option and is starting it.
BDS_STARTING = 'BdsDxe: starting Boot0001 "EFI Internal Shell"'
#: The option it started is actually running.
SHELL_BANNER = "UEFI Interactive Shell"
#: ...and it is interactive, not merely printed a banner and stopped.
SHELL_PROMPT = "Shell>"


def _firmware_pin() -> dict | None:
    """The firmware pin from ``sources.lock.json``, or ``None`` if unpinned."""
    if not SOURCES_LOCK.is_file():
        return None
    try:
        lock = json.loads(SOURCES_LOCK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pin = lock.get("tools", {}).get("qemu", {}).get("firmware")
    return pin if isinstance(pin, dict) else None


def _firmware_path() -> Path | None:
    """The pinned firmware image, when it is present on this host."""
    pin = _firmware_pin()
    if not pin or not pin.get("path"):
        return None
    path = REPO_ROOT / pin["path"]
    return path if path.is_file() else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


requires_firmware = pytest.mark.skipif(
    _qemu_binary() is None or _firmware_path() is None,
    reason="no qemu-system-aarch64, or the pinned EDK2 firmware is not on this host",
)


@pytest.fixture
def board() -> MachineConfig:
    """A machine for the firmware: it needs no more than this to run."""
    return MachineConfig(
        name="arm64-edk2",
        accel="tcg",
        cpu_model="cortex-a710",
        cores=1,
        memory_mb=2048,
        machine_type="virt,virtualization=off,secure=off,gic-version=3",
        clock={"rtc": "utc"},
    )


def test_the_pinned_firmware_is_present_and_matches_its_pin() -> None:
    """The image we boot must be the image the pin names.

    Without this the test would happily boot whatever ``edk2-aarch64-code.fd``
    happened to be lying around, and a passing run would prove something about a
    file nobody can identify.
    """
    pin = _firmware_pin()
    assert pin is not None, "sources.lock.json has no tools.qemu.firmware pin"
    assert pin.get("sha256"), "the firmware pin records no digest"

    path = _firmware_path()
    if path is None:
        pytest.skip("the pinned firmware is not on this host")

    assert path.stat().st_size == pin["size"], (
        f"{path} is {path.stat().st_size} bytes; the pin says {pin['size']}"
    )
    assert _sha256_file(path) == pin["sha256"], (
        f"{path} does not match its pin; refusing to boot an unidentified firmware"
    )


@requires_firmware
def test_a_real_uefi_firmware_boots_to_an_interactive_shell(tmp_path, board):
    """Start the pinned EDK2 in the real QEMU and read what it says."""
    from tests.acceptance.test_real_boot_arm64 import _wait_for_markers

    workdir = _ascii_workdir(tmp_path)
    serial_path = workdir / "serial.log"
    firmware = _firmware_path()
    assert firmware is not None  # guarded by requires_firmware

    # -bios, not -kernel: this run is about the firmware, and the two spellings
    # are mutually exclusive in qemu-virt.c:1907-1916.
    argv = [
        "-machine", board.machine_type,
        "-cpu", board.cpu_model,
        "-smp", str(board.cores),
        "-m", str(board.memory_mb),
        "-bios", str(firmware),
        # No boot media: the firmware's own internal shell is the only option it
        # can find, and that is exactly what makes the result unambiguous.
        "-display", "none",
        "-serial", f"file:{serial_path}",
        "-monitor", "none",
        "-no-reboot",
    ]
    binary = _qemu_binary()
    assert binary is not None

    # One real QEMU at a time, for the same reason as the stub test.
    with _real_qemu_lock():
        process = subprocess.Popen([binary, *argv], cwd=str(workdir))
        try:
            seen = _wait_for_markers(
                serial_path,
                (BANNER, BDS_STARTING, SHELL_BANNER, SHELL_PROMPT),
                FIRMWARE_TIMEOUT,
            )
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=10)
            except Exception:  # pragma: no cover - a kill that does not land
                pass

    # --- the firmware identified itself -------------------------------------
    assert BANNER in seen, f"no UEFI banner in the serial log: {seen[-2000:]!r}"
    # --- BDS found a boot option and started it ------------------------------
    assert BDS_STARTING in seen, f"BDS never started an option: {seen[-2000:]!r}"
    # --- that option is actually running -------------------------------------
    assert SHELL_BANNER in seen, f"the shell never started: {seen[-2000:]!r}"
    # --- and it is interactive, not a banner and a halt ----------------------
    assert SHELL_PROMPT in seen, f"no shell prompt was reached: {seen[-2000:]!r}"


def _real_qemu_lock() -> _RealQemuLock:
    """The same lock the stub test uses: one real QEMU at a time, process-wide."""
    root = Path("F:/iosvirtual/local-private/tmp/real-boot")
    if not root.parent.is_dir():
        root = Path(tempfile.gettempdir()) / "unipad-real-qemu"
    return _RealQemuLock(root / "real-qemu.lock")
