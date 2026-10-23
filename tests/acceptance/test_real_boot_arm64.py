"""A REAL guest on a REAL QEMU: the host-side chain, end to end (PRD 6 M1/M4, 4.2).

Everything else in this suite drives ``tests/fakes/fake_qemu.py``.  A fake proves
the host side copes with a well-behaved child; it cannot prove that the argv
UniPad builds actually starts a guest.  On this host the real boot target --
``kernel.macho``, a patched XNU -- cannot be produced at all, so the gap would
otherwise stay open for exactly the milestone that is supposed to close it.

``tests/fixtures/arm64/boot-stub.elf`` closes it for the host side.  It is a
genuine AArch64 bare-metal kernel: cross-compiled on this machine, executed by
QEMU's TCG, writing to the PL011 UART.  Booting it proves

* the exact argv :meth:`QemuLauncher.build_args` produces is accepted by the
  real QEMU, every clause of it;
* real ARM64 instructions run, so the machine description (CPU, memory, machine
  type) is not merely well-formed but sufficient;
* the serial log the launcher captures carries guest output;
* QMP readiness, ``screendump`` and a graceful ``quit`` work against a live
  guest rather than a script.

It proves **none** of the following, and no claim about a device boot may be
derived from it: that EDK2 hands off, that XNU starts, that UniXNU's payload
loads, or that an iPadOS userland appears.  This is a host-side proof with a
real guest in the loop -- see docs/testing-strategy.md D2/D3.

The test skips itself when QEMU is not installed, so it is hermetic on a host
without it rather than failing for an environmental reason.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from unipad.host.logs import Screenshotter
from unipad.host.qemu import QemuLauncher
from unipad.models import MachineConfig

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]
STUB = REPO_ROOT / "tests" / "fixtures" / "arm64" / "boot-stub.elf"

#: Markers the stub writes to the PL011 UART.  Quoted verbatim so a change to
#: the stub cannot silently satisfy an old expectation.
BOOT_MARKERS = (
    "UNIPAD-ARMSTUB: hello from AArch64",
    "UNIPAD-ARMSTUB: cpu0 up, uart alive",
    "UNIPAD-ARMSTUB: entering idle",
)

#: How long to wait for the guest to reach the idle marker before giving up.
#: The stub parks in ``wfe`` afterwards, so the run only ends via QMP.
SERIAL_TIMEOUT = 30.0

#: Where this host keeps its QEMU install.  It is not on PATH -- the NSIS
#: installer does not add itself -- so a PATH-only lookup would skip a test that
#: can in fact run.  ``local-private/`` is not committed, hence the existence
#: check rather than a hard-coded path.
LOCAL_QEMU = REPO_ROOT / "local-private" / "tools" / "qemu" / "qemu-system-aarch64.exe"


def _qemu_binary() -> str | None:
    """The real QEMU to boot the stub with, or ``None`` if there is none."""
    found = shutil.which("qemu-system-aarch64")
    if found:
        return found
    return str(LOCAL_QEMU) if LOCAL_QEMU.exists() else None


requires_qemu = pytest.mark.skipif(
    _qemu_binary() is None or not STUB.exists(),
    reason="no qemu-system-aarch64 on PATH or in local-private/tools/qemu, or the stub fixture is absent",
)


def _wait_for_markers(serial_path: Path, markers: tuple[str, ...], timeout: float) -> str:
    """Block until every marker is in the serial log, or the timeout expires."""
    deadline = time.monotonic() + timeout
    seen = ""
    while time.monotonic() < deadline:
        try:
            seen = serial_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            seen = ""
        if all(marker in seen for marker in markers):
            return seen
        time.sleep(0.1)
    return seen


def _ascii_workdir(tmp_path: Path) -> Path:
    """An ASCII-only run directory under ``tmp_path``.

    ``tmp_path`` itself is under the user profile, which on this host contains
    non-ASCII characters; the Windows QEMU build resolves paths with the ANSI
    code page and fails to open them.  Hashing the path is not enough -- the
    *prefix* is the problem -- so the run directory is placed under a fixed
    ASCII root on the same drive instead.
    """
    if str(tmp_path).isascii():
        workdir = tmp_path / "run"
    else:
        root = Path(tempfile.gettempdir())
        if not str(root).isascii():
            root = Path("F:/iosvirtual/local-private/tmp/real-boot")
        workdir = root / f"unipad-stub-{os.getpid()}-{id(tmp_path) & 0xFFFFFF:x}"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


@pytest.fixture
def machine() -> MachineConfig:
    """A small machine for the stub: it needs no more than this to run."""
    return MachineConfig(
        name="arm64-stub",
        accel="tcg",
        cpu_model="cortex-a710",
        cores=1,
        memory_mb=512,
        machine_type="virt,virtualization=off,secure=off,gic-version=3",
        clock={"rtc": "utc"},
        display={"width": 1376, "height": 1024, "format": "ramfb"},
    )


@requires_qemu
def test_the_boot_stub_fixture_is_a_real_arm64_image():
    """The fixture must be an AArch64 ELF, not a placeholder.

    A stub that had been replaced by a text file would still make the boot test
    "pass" if QEMU happened to accept it, so the image is checked here where a
    silent substitution is visible.
    """
    assert STUB.is_file(), "the boot stub fixture is missing"
    header = STUB.read_bytes()[:20]
    # ELF64 little-endian (0x7f 'E' 'L' 'F', EI_CLASS=2, EI_DATA=1) and
    # e_type = ET_EXEC (2).  e_machine is checked as AArch64 (0xb7) too.
    assert header[:4] == b"\x7fELF", header[:4]
    assert header[4] == 2, "expected ELF64"
    assert header[5] == 1, "expected little-endian"
    e_type = int.from_bytes(header[16:18], "little")
    e_machine = int.from_bytes(header[18:20], "little")
    assert e_type == 2, f"expected ET_EXEC, got {e_type}"
    assert e_machine == 0xB7, f"expected AArch64, got {e_machine:#x}"


@requires_qemu
def test_a_real_guest_boots_and_is_controlled_over_qmp(tmp_path, machine):
    """PRD 6 M1/M4 + 4.2: launch the stub in the real QEMU and drive it."""
    from tests.fakes import fake_qemu as fq

    port = fq.free_tcp_port()
    # The Windows QEMU build cannot open a path containing non-ASCII characters,
    # and pytest's tmp_path is under this host's non-ASCII user directory.  An
    # ASCII work directory is therefore part of the fixture, not an accident:
    # the launcher now refuses a non-ASCII path outright (see
    # test_a_non_ascii_run_path_is_refused_before_launch).
    workdir = _ascii_workdir(tmp_path)
    serial_path = workdir / "serial.log"

    launcher = QemuLauncher(machine, qmp_timeout=20.0, poll_interval=0.05)
    args = launcher.build_args(
        # No firmware and no disks: a -kernel boot needs neither, and the
        # launcher must not emit them anyway.
        STUB,
        STUB,
        None,
        workdir,
        port,
        # Not headless: ``-display none`` leaves no display device, and QMP
        # ``screendump`` then has nothing to capture.  A real window is not
        # opened -- ``-display none`` and a screendump are mutually exclusive,
        # so the capture is what chooses.
        headless=False,
        serial_log=serial_path,
        kernel=STUB,
    )

    binary = _qemu_binary()
    assert binary is not None  # guarded by requires_qemu
    launched = launcher.launch(binary, args, workdir=workdir)
    try:
        # --- readiness is proved, never assumed (PRD 7.4 / FR08) ----------
        readiness = launcher.wait_for_qmp(timeout=30.0)
        assert readiness.ready is True, readiness.evidence
        assert readiness.evidence["status"]["status"] == "running"
        assert launcher.is_running() is True

        # --- real ARM64 code ran, and its output reached our serial log ----
        seen = _wait_for_markers(serial_path, BOOT_MARKERS, SERIAL_TIMEOUT)
        for marker in BOOT_MARKERS:
            assert marker in seen, f"serial log never carried {marker!r}: {seen!r}"

        # --- the guest is controllable over QMP, not just observable ------
        client = launcher.qmp_client()
        assert client is not None
        # The dump path must be ASCII too: QEMU writes the file itself, so
        # the same code-page limit applies and the launcher's own check
        # cannot see this one.
        shot = Screenshotter(client, workdir / "shots").capture("stub")
        assert shot["ok"] is True, shot
        assert Path(shot["path"]).is_file()

        stop = launcher.stop(graceful=True, timeout=30.0)
        assert stop.ok is True
        exit_code = launcher.wait_for_exit(timeout=30.0)
        assert exit_code == 0
    finally:
        launcher.close()

    # The argv that did this is the evidence PRD 9.2 asks for; it must survive
    # the run so a later reader can see exactly what was started.
    assert _kernel_arg(launcher) == STUB.as_posix()


def _kernel_arg(launcher: QemuLauncher) -> str | None:
    """The ``-kernel`` value from the argv the launcher actually started."""
    argv = launcher.last_args
    for index, value in enumerate(argv):
        if value == "-kernel" and index + 1 < len(argv):
            return argv[index + 1]
    return None
