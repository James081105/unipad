"""Unit tests for :mod:`unipad.host.qemu` (PRD 2.2, 4.1, 4.2, 6 WP02/WP04).

The launcher owns the one guard that must never be relaxed: an Intel Windows
host can only emulate AArch64 with **explicit** TCG, so any other accelerator is
refused with ``accelerator_not_tcg`` (PRD 2.2-1) -- there is no fallback and no
"best effort" mode.

The Windows transport decisions are pinned too (PRD 4.2):

- QMP is ``tcp:127.0.0.1:<port>,server=on,wait=off`` because Windows has no
  ``AF_UNIX`` QMP socket;
- the serial log is a file so control and serial never contend;
- the system volume is attached read-only and the Data volume writable;
- the guest display uses the ramfb/GOP path first.

Readiness is proved, never assumed (PRD 7.4 / FR08): the launcher drives the
real QMP client against the real fake QEMU process and only reports ready after
a greeting, an accepted capability negotiation and a ``query-status`` reply.  A
process that is alive but silent must **not** be reported ready, which is what
``test_readiness_never_comes_from_a_countdown`` pins.

All of this is host-side behaviour proved against a simulation fixture; nothing
here is evidence that a guest, a display or a disk exists
(docs/testing-strategy.md D2/D3).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.qemu import (
    NON_ASCII_PATH_REASON,
    QemuLauncher,
    _parse_drive_file,
)
from unipad.models import MachineConfig, read_json
from tests.fakes import fake_qemu as fq
from tests.fakes.fake_qemu import BOOT_MARKERS, PARTIAL_BOOT_MARKERS

#: The shipped machine definition, read from disk so a drift between the
#: repository configuration and the launcher is caught here and not in the field.
MACHINE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "machine.json"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _fake_launcher_command() -> tuple[str, list[str]]:
    """``(binary, argv_prefix)`` that makes the launcher run the fake QEMU.

    The launcher takes one executable plus an argv list, so the fake is started
    as ``[sys.executable, <fake_qemu.py>, *qemu_args]``.
    """
    return fq.fake_qemu_launcher()


@pytest.fixture
def images(tmp_path) -> dict[str, Path]:
    """Firmware / system / data images that really exist on disk."""
    paths = {
        "firmware": tmp_path / "firmware.bin",
        "system": tmp_path / "system.img",
        "data": tmp_path / "data.img",
    }
    for path in paths.values():
        path.write_bytes(b"unipad-test-image")
    return paths


@pytest.fixture
def machine() -> MachineConfig:
    """A TCG machine definition shaped like ``config/machine.json``."""
    return MachineConfig(
        name="ipad-tcg",
        accel="tcg",
        cpu_model=BOARD["cpu_model"],
        cores=BOARD["cores"],
        memory_mb=BOARD["memory_mb"],
        machine_type=BOARD["machine_type"],
        clock={"rtc": "utc"},
        display={
            "width": BOARD["width"],
            "height": BOARD["height"],
            "format": "ramfb",
        },
        devices={"firmware": {"attach": "pflash"}},
    )


@pytest.fixture
def launcher(machine, tmp_path) -> QemuLauncher:
    """A launcher wired for fast, deterministic readiness polling."""
    return QemuLauncher(machine, qmp_timeout=10.0, poll_interval=0.05, process_timeout=10.0)


def _args(launcher: QemuLauncher, images: dict[str, Path], tmp_path: Path, port: int) -> list[str]:
    return launcher.build_args(
        images["firmware"], images["system"], images["data"], tmp_path, port
    )


def _real_qemu_binary() -> str | None:
    """A real ``qemu-system-aarch64`` on PATH or in this host's install dir."""
    found = shutil.which("qemu-system-aarch64")
    if found:
        return found
    candidate = MACHINE_CONFIG_PATH.parents[1] / "local-private" / "tools" / "qemu" / "qemu-system-aarch64.exe"
    return str(candidate) if candidate.exists() else None


def _option_value(args: list[str], flag: str) -> str | None:
    """Return the value that follows ``flag`` in an argv list."""
    for index, token in enumerate(args):
        if token == flag and index + 1 < len(args):
            return args[index + 1]
    return None


# ---------------------------------------------------------------------------
# the accelerator guard (PRD 2.2-1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("accel", ["whpx", "hvf", "kvm", "hax", "xen", "auto", "tcg,thread=multi"])
def test_non_tcg_accelerator_is_refused_at_construction(accel):
    """PRD 2.2-1: anything but explicit ``tcg`` is refused, with no fallback."""
    machine = MachineConfig(accel=accel)

    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher(machine)

    error = excinfo.value
    assert error.code == ErrorCode.ACCELERATOR_NOT_TCG
    assert error.details["requested_accel"] == accel
    assert error.details["required_accel"] == "tcg"


@pytest.mark.parametrize("accel", ["tcg", "TCG", " tcg", "tcg "])
def test_only_spelling_variants_of_tcg_are_accepted(accel):
    """PRD 2.2-1: the guard rejects non-TCG, not cosmetic spelling."""
    assert QemuLauncher(MachineConfig(accel=accel)).machine.accel == accel


def test_empty_accelerator_is_refused():
    """PRD 2.2-1: an absent accelerator would let QEMU pick one; refuse it."""
    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher(MachineConfig(accel=""))

    assert excinfo.value.code == ErrorCode.ACCELERATOR_NOT_TCG


def test_build_args_always_emits_explicit_tcg(launcher, images, tmp_path):
    """PRD 2.2-1: the accelerator is explicit in the argv, never inferred."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    # Not a positional assertion: the firmware spelling now leads, so where
    # ``-accel`` sits is an implementation detail.  What matters is that it is
    # present, paired with ``tcg``, and that ``tcg`` is never spelled twice.
    assert _option_value(args, "-accel") == "tcg"
    assert args.count("tcg") == 1


def test_non_machine_config_is_rejected():
    """PRD 9.1: a malformed machine definition is a usage error."""
    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher(object())  # type: ignore[arg-type]

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# build_args (PRD 4.2)
# ---------------------------------------------------------------------------


def test_build_args_contains_the_machine_definition(launcher, images, tmp_path):
    """PRD 4.2: CPU model, machine type, cores and memory are all explicit."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    # Asserted against the fixture's own values, not literals: the point is that
    # whatever the machine says reaches the argv verbatim, so re-stating the
    # numbers here would only test that this file agrees with itself.
    machine = launcher.machine
    assert _option_value(args, "-cpu") == machine.cpu_model
    assert _option_value(args, "-machine") == machine.machine_type
    assert _option_value(args, "-smp") == str(machine.cores)
    assert _option_value(args, "-m") == str(machine.memory_mb)
    assert _option_value(args, "-rtc") == "base=utc"
    # ... and the firmware reaches QEMU in exactly one of the two exclusive
    # forms, never neither (see the pflash section below).
    assert (_option_value(args, "-bios") is not None) ^ (
        any("if=pflash" in value for value in args)
    )


def test_build_args_uses_a_loopback_tcp_qmp_socket(launcher, images, tmp_path):
    """PRD 4.2: Windows has no AF_UNIX QMP socket, so QMP is TCP on loopback."""
    port = 45999
    args = _args(launcher, images, tmp_path, port)

    assert _option_value(args, "-qmp") == f"tcp:127.0.0.1:{port},server=on,wait=off"


def test_build_args_keeps_the_monitor_off_the_serial_path(launcher, images, tmp_path):
    """PRD 4.2: control and serial must not contend."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    assert _option_value(args, "-monitor") == "none"


def test_build_args_writes_the_serial_log_to_a_file(launcher, images, tmp_path):
    """PRD 4.2: the serial log is a file in the run directory."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    assert _option_value(args, "-serial") == f"file:{(tmp_path / 'serial.log').as_posix()}"


def test_build_args_accepts_a_serial_log_override(launcher, images, tmp_path):
    """PRD 4.2: a caller may redirect the serial log."""
    custom = tmp_path / "logs" / "boot.log"

    args = launcher.build_args(
        images["firmware"],
        images["system"],
        images["data"],
        tmp_path,
        fq.free_tcp_port(),
        serial_log=custom,
    )

    assert _option_value(args, "-serial") == f"file:{custom.as_posix()}"


def test_build_args_names_the_vm_for_window_lookup(launcher, images, tmp_path):
    """PRD 4.2: the QEMU window is found by title/pid, never by GPU adapter."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    assert _option_value(args, "-name") == "unipad-ipad-tcg"


def test_build_args_attaches_the_system_volume_read_only(launcher, images, tmp_path):
    """PRD 4.2: the system volume must never be written by the guest."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())
    drives = [value for flag, value in zip(args, args[1:]) if flag == "-drive"]

    system_drive = next(d for d in drives if Path(images["system"]).as_posix() in d)
    assert "if=virtio" in system_drive
    assert "format=raw" in system_drive
    assert "readonly=on" in system_drive
    assert "media=disk" in system_drive


def test_build_args_attaches_the_data_volume_writable(launcher, images, tmp_path):
    """PRD 4.2: the Data volume is a separate, writable, persistent image."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())
    drives = [value for flag, value in zip(args, args[1:]) if flag == "-drive"]

    data_drive = next(d for d in drives if Path(images["data"]).as_posix() in d)
    assert "readonly=off" in data_drive
    assert data_drive != next(d for d in drives if "readonly=on" in d)


def test_build_args_omits_the_data_volume_when_none_is_given(launcher, images, tmp_path):
    """PRD 4.2: a RAM-disk-only diagnostic run carries no Data drive."""
    args = launcher.build_args(
        images["firmware"], images["system"], None, tmp_path, fq.free_tcp_port()
    )
    drives = [value for flag, value in zip(args, args[1:]) if flag == "-drive"]

    # Count the *disk* drives, not every -drive: the pflash pair is firmware,
    # not a volume, so a plain len() would count the firmware and report a
    # missing Data volume that is correctly absent.
    disk_drives = [d for d in drives if "if=virtio" in d]
    assert len(disk_drives) == 1
    assert "readonly=on" in disk_drives[0]
    assert Path(images["data"]).as_posix() not in " ".join(drives)


def test_build_args_uses_the_ramfb_display_path_by_default(launcher, images, tmp_path):
    """PRD 4.2: reuse the ramfb/GOP path first; only headless drops the display."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    assert _option_value(args, "-device") == "ramfb"
    assert "-display" not in args


def test_build_args_headless_disables_the_display(launcher, images, tmp_path):
    """PRD 4.2: ``-display none`` is emitted only for an explicit headless run."""
    args = launcher.build_args(
        images["firmware"],
        images["system"],
        images["data"],
        tmp_path,
        fq.free_tcp_port(),
        headless=True,
    )

    assert _option_value(args, "-display") == "none"


def test_build_args_selects_virtio_gpu_when_configured(tmp_path):
    """PRD 4.3: the GPU route is a config choice, not a silent default.

    The geometry is deliberately *not* the pinned one: the route is chosen by
    ``format`` alone, and a test that happened to use the pinned numbers would
    pass even if the route were chosen by geometry.
    """
    machine = MachineConfig(display={"width": 2048, "height": 1536, "format": "virtio-gpu"})
    launcher = QemuLauncher(machine)
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"fw")
    system = tmp_path / "sys.img"
    system.write_bytes(b"sys")

    args = launcher.build_args(firmware, system, None, tmp_path, 4444)

    assert _option_value(args, "-device") == "virtio-gpu-pci"


def test_build_args_refuses_a_resolution_outside_the_catalogue(tmp_path, images):
    """A framebuffer nobody designed must fail, not silently get built.

    ``build_args`` delegates the geometry to :mod:`unipad.host.display`, whose
    catalogue is closed on purpose: an unsupported resolution is refused rather
    than snapped to a neighbour, because a snapped framebuffer makes the
    recorded geometry disagree with what the guest draws and every screenshot
    taken from it worthless as PRD 9.2 evidence.

    1112x834 is what this test used to configure.  It was accepted, because the
    old inline block forwarded whatever numbers the config carried straight into
    ``-fw_cfg``.
    """
    machine = MachineConfig(display={"width": 1112, "height": 834, "format": "ramfb"})
    launcher = QemuLauncher(machine)

    with pytest.raises(UniPadError) as excinfo:
        launcher.build_args(
            images["firmware"], images["system"], images["data"], tmp_path, 4444
        )

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "unsupported display resolution" in str(excinfo.value)


def test_build_args_appends_extra_arguments_verbatim(launcher, images, tmp_path):
    """PRD 4.1: a caller can add board-specific arguments without a rewrite."""
    args = launcher.build_args(
        images["firmware"],
        images["system"],
        images["data"],
        tmp_path,
        fq.free_tcp_port(),
        extra_args=("-global", "virtio-mmio.force-bit=1"),
    )

    assert args[-2:] == ["-global", "virtio-mmio.force-bit=1"]


# ---------------------------------------------------------------------------
# the network decision (PRD 3.2 P1 (1))
# ---------------------------------------------------------------------------


def test_build_args_states_the_network_decision_in_the_argv(launcher, images, tmp_path):
    """A disabled network is spelled ``-nic none``, not left to QEMU's default.

    ``config/machine.json`` declares ``net.enabled: false``.  Emitting no
    network argument at all means the argv does not carry that decision, and
    the guest's connectivity would be whatever QEMU happens to default to for
    the machine type -- a decision recorded nowhere.
    """
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    assert _option_value(args, "-nic") == "none"
    assert "-netdev" not in args
    assert "-net" not in args


def test_build_args_never_emits_two_network_decisions(launcher, images, tmp_path):
    """One ``-nic`` per launch: two would be ambiguous about which one wins."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    assert args.count("-nic") == 1


def test_a_machine_that_enables_the_network_gets_an_explicit_backend(tmp_path):
    """PRD 3.2 P1 (1): enabling the network is a config choice with a backend.

    The default stays loopback-only: an operator who flips ``net.enabled``
    must also say what the guest may reach, so no configuration accidentally
    puts the guest on the public internet through the host.
    """
    machine = MachineConfig(
        devices={
            "net": {
                "enabled": True,
                "model": "virtio-net",
                "mode": "user",
                "restrict": "on",
            }
        }
    )
    launcher = QemuLauncher(machine)
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"fw")
    system = tmp_path / "sys.img"
    system.write_bytes(b"sys")

    args = launcher.build_args(firmware, system, None, tmp_path, 4444)

    nic = _option_value(args, "-nic")
    assert nic is not None
    assert "virtio-net" in nic
    # loopback-only unless the operator said otherwise
    assert "restrict=on" in nic


def test_an_unknown_network_mode_is_refused_rather_than_guessed(tmp_path):
    """A typo must not silently become a network the operator did not ask for."""
    machine = MachineConfig(devices={"net": {"enabled": True, "mode": "bridged"}})
    launcher = QemuLauncher(machine)
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"fw")
    system = tmp_path / "sys.img"
    system.write_bytes(b"sys")

    with pytest.raises(UniPadError) as excinfo:
        launcher.build_args(firmware, system, None, tmp_path, 4444)

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert "mode" in excinfo.value.message


def test_a_partial_network_config_fails_closed(tmp_path):
    """``enabled`` with no mode is an unfinished decision, not a default."""
    machine = MachineConfig(devices={"net": {"enabled": True}})
    launcher = QemuLauncher(machine)
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"fw")
    system = tmp_path / "sys.img"
    system.write_bytes(b"sys")

    with pytest.raises(UniPadError) as excinfo:
        launcher.build_args(firmware, system, None, tmp_path, 4444)

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_the_repository_machine_config_agrees_with_the_launcher():
    """``config/machine.json`` must not claim a network the argv does not build.

    This reads the real file rather than a fixture: the drift this guards
    against is between the shipped configuration and the shipped launcher.
    """
    machine = MachineConfig.from_dict(read_json(MACHINE_CONFIG_PATH))
    launcher = QemuLauncher(machine)
    args = launcher.build_args(
        MACHINE_CONFIG_PATH, MACHINE_CONFIG_PATH, None, Path.cwd(), 4444
    )

    net = dict(machine.devices or {}).get("net", {})
    if net.get("enabled"):
        assert args.count("-nic") == 1
        assert _option_value(args, "-nic") != "none"
    else:
        assert _option_value(args, "-nic") == "none"


@pytest.mark.parametrize("port", [0, -1, 65536, "4444"])
def test_build_args_rejects_an_unusable_qmp_port(launcher, images, tmp_path, port):
    """PRD 4.2: QMP must be a real loopback TCP port."""
    with pytest.raises(UniPadError) as excinfo:
        launcher.build_args(images["firmware"], images["system"], images["data"], tmp_path, port)  # type: ignore[arg-type]

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_build_args_remembers_the_last_argv(launcher, images, tmp_path):
    """PRD 9.2: ``launch()`` can reuse the argv that was just built."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    assert launcher._last_args == args  # noqa: SLF001 - documented reuse contract


# ---------------------------------------------------------------------------
# launch failures
# ---------------------------------------------------------------------------


def test_missing_binary_raises_qemu_launch_failed(launcher, images, tmp_path):
    """PRD 6 M0 open item: no QEMU on the host is a named failure."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    with pytest.raises(UniPadError) as excinfo:
        launcher.launch("qemu-system-aarch64-not-installed", args)

    error = excinfo.value
    assert error.code == ErrorCode.QEMU_LAUNCH_FAILED
    assert error.details["attempted_path"] == "qemu-system-aarch64-not-installed"


def test_launch_refuses_when_an_image_is_missing(launcher, images, tmp_path):
    """PRD 7.4: never start a run whose artifacts are not all present."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())
    images["firmware"].unlink()

    with pytest.raises(UniPadError) as excinfo:
        launcher.launch(sys.executable, args)

    error = excinfo.value
    assert error.code == ErrorCode.IMAGE_NOT_FOUND
    assert any("firmware.bin" in item for item in error.details["missing"])


def test_launch_without_arguments_raises_config_invalid(launcher):
    """PRD 9.1: an empty command line is not evidence of a run."""
    with pytest.raises(UniPadError) as excinfo:
        launcher.launch(sys.executable, [])

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_wait_for_qmp_without_a_port_raises_config_invalid(launcher):
    """PRD 7.4: readiness cannot be claimed for a run that was never built."""
    readiness = launcher.wait_for_qmp(timeout=0.2)

    assert readiness.ready is False
    assert readiness.error_code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# launch + readiness against the real fake QEMU process
# ---------------------------------------------------------------------------


def test_launch_then_wait_for_qmp_succeeds_with_qmp_evidence(launcher, images, tmp_path):
    """PRD 4.2 / 7.4: readiness is a greeting + capabilities + query-status."""
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    result = launcher.launch(binary, [*prefix, *args], workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"})
    try:
        readiness = launcher.wait_for_qmp(timeout=15.0)
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    assert result.ok is True
    assert result.pid and result.pid > 0
    assert result.qmp_port == port
    assert readiness.ready is True
    assert readiness.error_code is None
    assert readiness.attempts >= 1
    # Evidence, not inference: all three QMP facts are recorded.
    assert readiness.evidence["greeting"]["QMP"]["version"]["qemu"]["major"] == 9
    assert readiness.evidence["capabilities"] == {}
    assert readiness.evidence["status"]["status"] == "running"
    assert readiness.status == "running"
    assert readiness.evidence["port"] == port


def test_launched_process_receives_the_built_argv(launcher, images, tmp_path):
    """PRD 9.2: the fake records the argv the launcher really used."""
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launcher.launch(binary, [*prefix, *args], workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"})
    try:
        assert launcher.wait_for_qmp(timeout=15.0).ready is True
        record = fq.read_jsonl(tmp_path / "fake-qemu-args.json")[0]
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    parsed = record["parsed"]
    assert parsed["accel"] == "tcg"
    assert parsed["qmp"]["transport"] == "tcp"
    assert parsed["qmp"]["port"] == port
    assert parsed["qmp"]["options"] == {"server": "on", "wait": "off"}
    assert parsed["serial"]["mode"] == "file"
    assert Path(parsed["serial"]["path"]).name == "serial.log"
    # Every argument the launcher built reached the child.
    for token in args:
        assert token in parsed["argv"]


def test_is_running_flips_after_a_graceful_stop(launcher, images, tmp_path):
    """PRD 5.2 case 8: QMP quit ends the process and the launcher notices."""
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launcher.launch(binary, [*prefix, *args], workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"})
    try:
        assert launcher.wait_for_qmp(timeout=15.0).ready is True
        assert launcher.is_running() is True

        stop = launcher.stop(graceful=True, timeout=10.0)
    finally:
        launcher.close()

    assert stop.ok is True
    assert stop.method == "qmp-quit"
    assert stop.exit_code == 0
    assert launcher.is_running() is False


def test_stop_escalates_when_qmp_quit_is_ignored(launcher, images, tmp_path):
    """PRD 7.4: a guest that will not shut down is still terminated, with proof."""
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launcher.launch(
        binary,
        [*prefix, *args],
        workdir=tmp_path,
        env={"UNIPAD_FAKE_BOOT_MS": "0", "UNIPAD_FAKE_FAIL_MODE": "ignore-quit"},
    )
    try:
        assert launcher.wait_for_qmp(timeout=15.0).ready is True
        stop = launcher.stop(graceful=True, timeout=2.0)
    finally:
        launcher.close()

    assert stop.ok is True
    assert stop.method == "terminate"
    assert launcher.is_running() is False


def test_stop_without_a_process_is_a_noop(launcher):
    """PRD 9.1: stopping something that never started must not raise."""
    stop = launcher.stop(graceful=True)

    assert stop.ok is True
    assert stop.method == "noop"
    assert stop.already_stopped is True


def test_stop_reports_an_already_exited_process(launcher, images, tmp_path):
    """PRD 9.2: an early exit is recorded, not rediscovered as a fresh stop."""
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launcher.launch(
        binary,
        [*prefix, *args],
        workdir=tmp_path,
        env={"UNIPAD_FAKE_BOOT_MS": "0", "UNIPAD_FAKE_FAIL_MODE": "exit-after-boot"},
    )
    try:
        exit_code = launcher.wait_for_exit(timeout=15.0)
        stop = launcher.stop(graceful=True)
    finally:
        launcher.close()

    assert exit_code == 4
    assert stop.method == "already-exited"
    assert stop.already_stopped is True
    assert stop.exit_code == 4


def test_context_manager_stops_and_closes(launcher, images, tmp_path):
    """PRD 5.2: leaving the ``with`` block must not leak the QEMU process."""
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    with QemuLauncher(
        launcher.machine, qmp_timeout=10.0, poll_interval=0.05, process_timeout=10.0
    ) as running:
        running.launch(
            binary, [*prefix, *args], workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"}
        )
        assert running.wait_for_qmp(timeout=15.0).ready is True
        assert running.is_running() is True

    assert running.is_running() is False


# ---------------------------------------------------------------------------
# readiness is QMP evidence, never a countdown (PRD 7.4)
# ---------------------------------------------------------------------------


def test_readiness_never_comes_from_a_countdown(launcher, images, tmp_path):
    """PRD 7.4: an alive-but-silent process is NOT ready and says why."""
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launcher.launch(
        binary,
        [*prefix, *args],
        workdir=tmp_path,
        env={"UNIPAD_FAKE_BOOT_MS": "0", "UNIPAD_FAKE_FAIL_MODE": "no-qmp"},
    )
    try:
        readiness = launcher.wait_for_qmp(timeout=3.0)
    finally:
        launcher.stop(graceful=False)
        launcher.close()

    assert readiness.ready is False
    assert readiness.error_code == ErrorCode.QMP_UNAVAILABLE
    # "status" is never synthesised: None means QMP never answered.
    assert readiness.status is None
    assert "status" not in readiness.evidence
    # The failure is diagnosable from the launcher's own evidence.
    assert readiness.evidence["stderr_tail"] == "" or "qmp" in readiness.evidence["stderr_tail"]


def test_readiness_arrives_before_the_boot_markers_finish(launcher, images, tmp_path):
    """PRD 7.4: readiness tracks the control channel, not a boot countdown.

    The fake stages its boot markers over four seconds; readiness must be
    reported as soon as QMP answers, long before the log claims completion.
    """
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launcher.launch(
        binary,
        [*prefix, *args],
        workdir=tmp_path,
        env={"UNIPAD_FAKE_BOOT_MS": "4000"},
    )
    try:
        readiness = launcher.wait_for_qmp(timeout=15.0)
        serial = (tmp_path / "serial.log").read_text(encoding="utf-8", errors="replace")
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    assert readiness.ready is True
    assert readiness.waited_seconds < 3.0
    assert "boot complete" not in serial


def test_early_exit_is_reported_as_qemu_launch_failed(launcher, images, tmp_path):
    """PRD 7.4: a QEMU that dies before QMP answers is a launch failure."""
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launcher.launch(
        binary,
        [*prefix, *args],
        workdir=tmp_path,
        env={"UNIPAD_FAKE_BOOT_MS": "0", "UNIPAD_FAKE_FAIL_MODE": "exit-early"},
    )
    try:
        readiness = launcher.wait_for_qmp(timeout=5.0)
    finally:
        launcher.stop(graceful=False)
        launcher.close()

    assert readiness.ready is False
    assert readiness.error_code == ErrorCode.QEMU_LAUNCH_FAILED
    assert readiness.evidence["qemu_exit_code"] == 3


def test_serial_markers_alone_are_not_a_gate(launcher, images, tmp_path):
    """PRD 7.4: serial text locates a stage; it never proves one on its own.

    The fake's ``partial-boot`` mode writes M1/M2 markers and then stops, so a
    launcher that trusted the log would report a stage that never happened.
    """
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launcher.launch(
        binary,
        [*prefix, *args],
        workdir=tmp_path,
        env={"UNIPAD_FAKE_BOOT_MS": "0", "UNIPAD_FAKE_FAIL_MODE": "partial-boot"},
    )
    try:
        readiness = launcher.wait_for_qmp(timeout=15.0)
        serial = (tmp_path / "serial.log").read_text(encoding="utf-8", errors="replace")
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    # QMP still answers (the host side is fine) ...
    assert readiness.ready is True
    # ... but the serial evidence only ever reached M2.
    assert any(marker in serial for marker in PARTIAL_BOOT_MARKERS)
    assert "M4: XNU kernel entered" not in serial
    assert BOOT_MARKERS[-1] not in serial


def test_the_fake_process_itself_refuses_a_non_tcg_accelerator(tmp_path):
    """PRD 2.2-1: the guard holds at the process boundary, not just in Python."""
    with fq.fake_qemu_process(extra_args=["-accel", "kvm"], workdir=tmp_path, wait_for_port=False) as fake:
        exit_code = fake.process.wait(timeout=15.0)

    record = fq.read_jsonl(tmp_path / "fake-qemu-args.json")[0]
    assert exit_code == 2
    assert record["rejected"]["reason"] == "accelerator_not_tcg"
    assert record["rejected"]["requested_accel"] == "kvm"
    assert record["rejected"]["required_accel"] == "tcg"


# ---------------------------------------------------------------------------
# QMP spec parsing (PRD 4.2)
# ---------------------------------------------------------------------------


def test_parse_qmp_spec_extracts_host_and_port():
    """PRD 4.2: the TCP spec is parsed into the endpoint the client needs."""
    assert QemuLauncher._parse_qmp_spec(  # noqa: SLF001 - documented helper
        "tcp:127.0.0.1:4444,server=on,wait=off"
    ) == ("127.0.0.1", 4444)


@pytest.mark.parametrize("spec", ["unix:/tmp/qmp.sock", "none", "tcp:127.0.0.1", "tcp:127.0.0.1:abc"])
def test_parse_qmp_spec_rejects_a_non_tcp_or_malformed_spec(spec):
    """PRD 4.2: a unix-socket QMP would silently disable control on Windows."""
    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher._parse_qmp_spec(spec)  # noqa: SLF001 - documented helper

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_adopt_args_re_derives_the_port_from_a_caller_supplied_argv(launcher, images, tmp_path):
    """PRD 4.2: ``launch(args=...)`` targets the port in *that* argv."""
    port = fq.free_tcp_port()
    custom = launcher.build_args(
        images["firmware"], images["system"], images["data"], tmp_path, port
    )
    binary, prefix = _fake_launcher_command()

    launcher.launch(binary, [*prefix, *custom], workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"})
    try:
        assert launcher.wait_for_qmp(timeout=15.0).ready is True
        assert launcher.qmp_client() is not None
        assert launcher.qmp_client().port == port
    finally:
        launcher.stop(graceful=True)
        launcher.close()


# ---------------------------------------------------------------------------
# launch() takes the command a caller already has (list or spaced string)
# ---------------------------------------------------------------------------


def _quoted(command: list[str]) -> str:
    """Join ``command`` into one quoted Windows command line."""
    return " ".join(f'"{token}"' if (" " in token or "\t" in token) else token for token in command)


def test_launch_accepts_a_command_list(launcher, images, tmp_path):
    """PRD 4.2 / 9.1: a caller may hand the launcher the command it already built.

    :func:`tests.fakes.fake_qemu.fake_qemu_command` returns *one* list
    (``[sys.executable, <fake_qemu.py>, *qemu_args]``), which is exactly what a
    launcher adapter has in hand.  Making every caller split that list back
    apart is duplicated work with a silent-failure mode, so ``launch()`` takes
    the list itself and the interpreter tokens never reach QEMU's argv.
    """
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()
    command = [binary, *prefix, *args]

    result = launcher.launch(command, workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"})
    try:
        readiness = launcher.wait_for_qmp(timeout=15.0)
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    assert result.ok is True
    assert result.qmp_port == port
    assert readiness.ready is True
    assert readiness.status == "running"
    record = fq.read_jsonl(tmp_path / "fake-qemu-args.json")[0]
    # The interpreter/script tokens are consumed by the launcher, so the argv the
    # child parsed is exactly the QEMU argument list.
    assert record["parsed"]["argv"] == args


def test_launch_accepts_a_spaced_command_line(launcher, images, tmp_path):
    """PRD 4.2: the same command as one string is split, not rejected.

    ``qemu_launch_failed`` for a command the launcher could have run is a
    misleading diagnosis, so a spaced command line whose head resolves to an
    executable is split (Windows ``shlex`` semantics, backslashes kept).
    """
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()
    command_line = _quoted([binary, *prefix, *args])

    result = launcher.launch(command_line, workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"})
    try:
        readiness = launcher.wait_for_qmp(timeout=15.0)
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    assert result.ok is True
    assert result.qmp_port == port
    assert readiness.ready is True
    assert readiness.status == "running"


def test_launch_keeps_a_real_path_with_spaces_unsplit(launcher, images, tmp_path):
    """A Windows path may contain spaces; it is never mistaken for a command line."""
    # ASCII on purpose: the non-ASCII path guard (see the ASCII section below)
    # would otherwise fire first and this test would stop being about command
    # splitting at all.
    root = tmp_path if str(tmp_path).isascii() else Path("F:/iosvirtual/local-private/tmp/spaces")
    spaced = root / "Program Files" / "qemu-system-aarch64.exe"
    spaced.parent.mkdir(parents=True, exist_ok=True)
    spaced.write_bytes(b"not a real qemu")

    # The images are ASCII for the same reason: the launcher refuses a
    # non-ASCII path outright now, and that guard is the correct behaviour --
    # but it is not what this test is about.
    ascii_images = tmp_path if str(tmp_path).isascii() else root
    fw = ascii_images / "fw.bin"
    fw.write_bytes(b"fw")
    sys_img = ascii_images / "sys.img"
    sys_img.write_bytes(b"sys")
    port = fq.free_tcp_port()
    clean_args = launcher.build_args(fw, sys_img, None, ascii_images, port)

    for candidate in (str(spaced), spaced):
        with pytest.raises(UniPadError) as excinfo:
            launcher.launch(candidate, clean_args)

        error = excinfo.value
        assert error.code == ErrorCode.QEMU_LAUNCH_FAILED
        assert error.details["attempted_path"] == str(candidate)


def test_split_command_documents_its_accepted_forms(launcher, tmp_path):
    """The command splitter is the single place the argv is derived from."""
    split = QemuLauncher._split_command  # noqa: SLF001 - documented helper
    spaced = tmp_path / "Program Files" / "qemu.exe"
    spaced.parent.mkdir(parents=True, exist_ok=True)
    spaced.write_bytes(b"not a real qemu")

    # A bare name resolves on PATH and is never split.
    assert split("qemu-system-aarch64") == ([], "qemu-system-aarch64")
    # A sequence is taken verbatim, whatever it holds.
    assert split(["python", "fake.py", "-accel", "tcg"]) == (
        ["fake.py", "-accel", "tcg"],
        "python",
    )
    # A spaced, quoted command line is split and unquoted (Windows semantics).
    assert split(f'"{spaced}" -accel tcg') == (["-accel", "tcg"], str(spaced))
    # An unusable string is handed back unchanged so the failure names it.
    assert split("definitely-not-a-real-binary") == ([], "definitely-not-a-real-binary")
    # An empty command is a usage error, not a crash.
    with pytest.raises(UniPadError) as excinfo:
        split([])
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_launch_rejects_an_empty_command(launcher):
    """PRD 9.1: an empty command line is a usage error, not a crash."""
    with pytest.raises(UniPadError) as excinfo:
        launcher.launch([])

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_full_lifecycle_through_a_command_list_ends_in_a_graceful_shutdown(
    launcher, images, tmp_path
):
    """PRD 5.2 case 8 / FR08: launch -> QMP ready -> graceful stop -> exit 0.

    Every step is asserted on evidence: the QMP readiness block, the SHUTDOWN
    event the client actually received, and the child's exit code.
    """
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launched = launcher.launch(
        [binary, *prefix, *args],
        workdir=tmp_path,
        env={"UNIPAD_FAKE_BOOT_MS": "0", "UNIPAD_FAKE_EXIT_CODE": "0"},
    )
    try:
        readiness = launcher.wait_for_qmp(timeout=15.0)
        assert readiness.ready is True
        assert readiness.evidence["status"]["status"] == "running"
        assert launcher.is_running() is True

        client = launcher.qmp_client()
        assert client is not None
        stop = launcher.stop(graceful=True, timeout=15.0)
        exit_code = launcher.wait_for_exit(timeout=15.0)
        events = [event["event"] for event in client.events()]
    finally:
        launcher.close()

    assert stop.ok is True
    assert stop.method == "qmp-quit"
    assert stop.exit_code == 0
    assert exit_code == 0
    assert "SHUTDOWN" in events
    assert launcher.is_running() is False


# ---------------------------------------------------------------------------
# the board we intend to boot (PRD 6 WP02/WP04)
#
# These numbers come from the pinned device repository
# (local-private/upstream/UniXNU-qemu), which is NOT part of this repository,
# so they are recorded here rather than read.  A drift between what
# config/machine.json declares and what that board actually needs is caught
# here instead of in the field, where it would surface as a guest that
# describes memory or CPUs that do not exist.
# ---------------------------------------------------------------------------

#: What the pinned board demands, with the citation for each value.
#: ``board.toml`` lives under local-private/ and is deliberately not committed.
BOARD = {
    "cpu_model": "cortex-a710",  # run-qemu.py:155
    "machine_type": "virt,virtualization=off,secure=off,gic-version=3",  # run-qemu.py:154
    "cores": 1,  # board.toml:143 cpu_regs="0" -> make-qemu-adt.py:78 cpus=1
    "memory_mb": 6144,  # board.toml:92 dram_size="0x180000000"
    "width": 1376,  # run-qemu.py:39-43
    "height": 1024,
}


def test_the_shipped_machine_config_agrees_with_the_pinned_board():
    """The repository config must describe the board we mean to boot."""
    machine = read_json(MACHINE_CONFIG_PATH)

    assert machine["cpu_model"] == BOARD["cpu_model"]
    assert machine["machine_type"] == BOARD["machine_type"]
    assert machine["cores"] == BOARD["cores"]
    assert machine["memory_mb"] == BOARD["memory_mb"]
    assert machine["display"]["width"] == BOARD["width"]
    assert machine["display"]["height"] == BOARD["height"]


def test_build_args_describes_the_machine_the_board_expects(tmp_path):
    """cpu / machine / smp / -m all come from the config, unmodified."""
    machine = MachineConfig(
        name="ipad-tcg",
        accel="tcg",
        cpu_model=BOARD["cpu_model"],
        cores=BOARD["cores"],
        memory_mb=BOARD["memory_mb"],
        machine_type=BOARD["machine_type"],
        display={"width": BOARD["width"], "height": BOARD["height"], "format": "ramfb"},
    )
    launcher = QemuLauncher(machine)
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"fw")
    system = tmp_path / "system.img"
    system.write_bytes(b"sys")

    args = launcher.build_args(firmware, system, None, tmp_path, 4444)

    assert _option_value(args, "-cpu") == BOARD["cpu_model"]
    assert _option_value(args, "-machine") == BOARD["machine_type"]
    assert _option_value(args, "-smp") == str(BOARD["cores"])
    assert _option_value(args, "-m") == str(BOARD["memory_mb"])


def test_build_args_stops_a_triple_fault_loop_rather_than_spinning_forever():
    """PRD 4.2: without -no-reboot a faulting guest never terminates the run."""
    machine = MachineConfig(accel="tcg")
    launcher = QemuLauncher(machine)
    args = launcher.build_args(
        Path("fw.bin"), Path("sys.img"), None, Path("."), 4444
    )
    assert "-no-reboot" in args


def test_build_args_tells_the_guest_driver_the_screen_geometry():
    """PRD 4.2: QemuRamfb reads its geometry from fw_cfg, not from a guess."""
    machine = MachineConfig(
        accel="tcg", display={"width": 1376, "height": 1024, "format": "ramfb"}
    )
    launcher = QemuLauncher(machine)
    args = launcher.build_args(
        Path("fw.bin"), Path("sys.img"), None, Path("."), 4444
    )
    assert (
        "-fw_cfg",
        "name=opt/org.unixnu/ramfb-size,string=1376x1024",
    ) in [(args[i], args[i + 1]) for i in range(len(args) - 1)]


# ---------------------------------------------------------------------------
# firmware attachment: -bios and -drive if=pflash are mutually exclusive
#
# qemu-virt.c:1907-1916 exits(1) when both are present, so a launcher that
# emits -bios for a board whose firmware is a pflash pair starts QEMU only
# to have it die immediately with a message that names neither.
# ---------------------------------------------------------------------------


def _pflash_config(**overrides) -> MachineConfig:
    devices = {"firmware": {"attach": "pflash"}}
    devices.update(overrides)
    return MachineConfig(accel="tcg", devices=devices)


def test_pflash_attachment_never_emits_bios(tmp_path):
    """The two spellings cannot coexist; pflash must displace -bios entirely."""
    firmware = tmp_path / "code.fd"
    firmware.write_bytes(b"x" * 64)
    launcher = QemuLauncher(_pflash_config())

    args = launcher.build_args(firmware, tmp_path / "sys.img", None, tmp_path, 4444)

    assert "-bios" not in args
    drives = [args[i + 1] for i, v in enumerate(args) if v == "-drive"]
    code = [d for d in drives if "if=pflash" in d and "readonly=on" in d]
    vars_ = [d for d in drives if "if=pflash" in d and "readonly=off" in d]
    assert len(code) == 1 and firmware.as_posix() in code[0]
    assert len(vars_) == 1


def test_pflash_vars_file_is_created_at_the_code_size_before_the_existence_check(tmp_path):
    """run-qemu.py:151-152 truncates vars.fd to the code firmware's size.

    Nothing creates it today, so a launcher that emits the drive but not the
    file hands QEMU a path that does not exist -- and its own pre-flight check
    would refuse to start first.
    """
    code = tmp_path / "code.fd"
    code.write_bytes(b"c" * 4096)
    system = tmp_path / "sys.img"
    system.write_bytes(b"s")
    launcher = QemuLauncher(_pflash_config())
    args = launcher.build_args(code, system, None, tmp_path, 4444)

    vars_spec = next(
        args[i + 1]
        for i, v in enumerate(args)
        if v == "-drive" and "if=pflash" in args[i + 1] and "readonly=off" in args[i + 1]
    )
    vars_path = _parse_drive_file(vars_spec)
    assert vars_path is not None
    # launch() materialises it; the size comes from the code drive.
    launcher.launch([sys.executable, "-c", "pass"], args=args, workdir=tmp_path)
    try:
        assert Path(vars_path).stat().st_size == 4096
    finally:
        launcher.close()


def test_bios_attachment_is_still_available_when_asked_for(tmp_path):
    """The single-blob spelling still works for a machine that wants it.

    ``pflash`` is the default because that is what the booted board needs, but
    a firmware with no persistent variables is legitimately a ``-bios`` blob, so
    the spelling must remain reachable rather than being removed with the
    default.
    """
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"fw")
    launcher = QemuLauncher(
        MachineConfig(accel="tcg", devices={"firmware": {"attach": "bios"}})
    )
    args = launcher.build_args(firmware, tmp_path / "sys.img", None, tmp_path, 4444)
    assert _option_value(args, "-bios") == firmware.as_posix()
    assert not [a for a in args if "if=pflash" in a]


def test_an_unknown_firmware_attachment_is_refused():
    """A spelling that would silently pick one of two exclusive forms is a
    usage error, not a default."""
    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher(MachineConfig(accel="tcg", devices={"firmware": {"attach": "nvram"}}))
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# the ESP is a directory, and QEMU's fat: driver says so
#
# The only ESP form that boots UniXNU is file=fat:<dir>.  _parse_drive_file
# used to return the whole "fat:F:/..." string, which Path(...).exists() then
# reported as missing -- so launch() aborted with image_not_found on the one
# disk layout the boot path actually needs.
# ---------------------------------------------------------------------------


def test_a_fat_esp_directory_is_not_reported_as_a_missing_image(tmp_path):
    esp = tmp_path / "esp"
    esp.mkdir()
    launcher = QemuLauncher(MachineConfig(accel="tcg"))
    args = [
        "-drive",
        f"if=none,id=esp,format=raw,readonly=on,file=fat:{esp.as_posix()}",
    ]
    assert launcher._missing_paths(args) == []


def test_a_fat_esp_that_does_not_exist_is_still_reported_missing(tmp_path):
    launcher = QemuLauncher(MachineConfig(accel="tcg"))
    args = ["-drive", f"if=none,id=esp,format=raw,file=fat:{tmp_path / 'nope'}"]
    missing = launcher._missing_paths(args)
    assert len(missing) == 1 and "fat:" in missing[0]


def test_a_host_device_node_is_left_for_qemu_to_judge():
    """/dev/rdiskN exists on macOS and never on Windows; the launcher cannot
    judge it, so it must not claim it is missing."""
    launcher = QemuLauncher(MachineConfig(accel="tcg"))
    args = ["-drive", "if=none,id=os,format=raw,file=host_device:/dev/rdisk2"]
    assert launcher._missing_paths(args) == []


def test_the_machine_config_defaults_are_the_shipped_config():
    """``MachineConfig()`` and ``config/machine.json`` must be one machine.

    Two definitions of "the machine" drift silently: a caller that builds a
    config in code gets a different guest from one that reads the file, and
    nothing in either place notices.  The shipped file is the authority, so the
    defaults are asserted against it rather than restated.
    """
    defaults = MachineConfig()
    shipped = read_json(MACHINE_CONFIG_PATH)

    assert defaults.name == shipped["name"]
    assert defaults.accel == shipped["accel"]
    assert defaults.cpu_model == shipped["cpu_model"]
    assert defaults.cores == shipped["cores"]
    assert defaults.memory_mb == shipped["memory_mb"]
    assert defaults.machine_type == shipped["machine_type"]
    assert defaults.clock.get("rtc") == shipped["clock"]["rtc"]
    assert defaults.display["width"] == shipped["display"]["width"]
    assert defaults.display["height"] == shipped["display"]["height"]
    assert defaults.display["format"] == shipped["display"]["format"]
    # ... and the firmware decision too, or a default-built launcher attaches
    # the firmware in the form the board cannot use.
    assert defaults.devices.get("firmware", {}).get("attach", "bios") == shipped["devices"]["firmware"]["attach"]


# ---------------------------------------------------------------------------
# a real bare-metal boot: -kernel (PRD 4.2, 6 M1/M4)
#
# A UEFI hand-off is not the only way a guest starts, and on this host it is
# the one that is blocked: it needs a kernel image that cannot be produced
# here.  ``-kernel`` loads an AArch64 image directly at the CPU's reset
# vector, which means the whole host-side chain -- launcher, QMP readiness,
# serial capture, screendump, stop -- can be proved against a real QEMU
# running real ARM64 code, instead of only against a fake.
# ---------------------------------------------------------------------------


def test_build_args_loads_a_kernel_image_when_one_is_given(tmp_path):
    """``-kernel`` replaces the firmware drives; the two cannot both apply."""
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"fw")
    system = tmp_path / "sys.img"
    system.write_bytes(b"sys")
    kernel = tmp_path / "stub.elf"
    kernel.write_bytes(b"kernel")
    launcher = QemuLauncher(MachineConfig(accel="tcg"))

    args = launcher.build_args(
        firmware, system, None, tmp_path, 4444, kernel=kernel
    )

    assert _option_value(args, "-kernel") == kernel.as_posix()
    # A UEFI firmware would be started instead of the kernel, so emitting both
    # is not "extra", it is a contradiction.
    assert _option_value(args, "-bios") is None
    assert not [a for a in args if "if=pflash" in a]


def test_build_args_without_a_kernel_keeps_the_firmware_path(launcher, images, tmp_path):
    """The default is unchanged: no kernel means the firmware is attached."""
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())
    assert "-kernel" not in args
    assert any("if=pflash" in value for value in args)


def test_a_missing_kernel_image_is_reported_before_launch(tmp_path):
    """PRD 7.4: a kernel path that does not exist stops the run, not QEMU."""
    launcher = QemuLauncher(MachineConfig(accel="tcg"))
    (tmp_path / "fw.bin").write_bytes(b"fw")
    (tmp_path / "sys.img").write_bytes(b"sys")
    args = launcher.build_args(
        tmp_path / "fw.bin", tmp_path / "sys.img", None, tmp_path, 4444,
        kernel=tmp_path / "absent.elf",
    )
    missing = launcher._missing_paths(args)
    assert len(missing) == 1
    assert "-kernel" in missing[0]


# ---------------------------------------------------------------------------
# the ANSI code-page limit of the Windows QEMU build
#
# The Windows QEMU resolves file paths with the ANSI code page, so any path
# containing a non-ASCII character -- a user directory such as
# ``C:/Users/<CJK name>/AppData/Local/Temp``, which is exactly where tempfile
# lands on this host -- makes QEMU exit 1 with ``open <path> failed``.  That
# message names the file but not the reason, so it reads as a missing artifact.
# ---------------------------------------------------------------------------


def test_a_non_ascii_run_path_is_refused_before_launch(launcher, images, tmp_path):
    """The failure is diagnosed up front, not discovered as a dead child."""
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    # tmp_path is non-ASCII on this host, so the args already carry one.
    if str(tmp_path).isascii():
        pytest.skip("this host's tmp_path is ASCII; the guard cannot be provoked here")

    # The guard runs after the binary is resolved -- "the binary is missing" is
    # the more fundamental error -- so it can only be provoked where a real
    # QEMU is installed.
    from unipad.host.qemu import NON_ASCII_PATH_REASON

    binary = _real_qemu_binary()
    if binary is None:
        pytest.skip("no qemu-system-aarch64 installed; the guard cannot be provoked")

    with pytest.raises(UniPadError) as excinfo:
        launcher.launch(binary, args, workdir=tmp_path)

    error = excinfo.value
    assert error.code == ErrorCode.CONFIG_INVALID
    assert error.details["option"] in ("-serial", "-drive", "-bios", "-kernel")
    assert "ANSI" in error.details["reason"]
    assert error.details["reason"] == NON_ASCII_PATH_REASON


def test_an_ascii_run_path_is_accepted(launcher, images, tmp_path):
    """The guard rejects the limitation, not paths in general."""
    if not str(tmp_path).isascii():
        pytest.skip("this host's tmp_path is not ASCII")
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    # No raise: the check is a no-op on a path the build can open.
    assert launcher._check_ascii_paths(args) is None


def test_the_guard_does_not_apply_to_a_substitute_binary(launcher, images, tmp_path):
    """A fake QEMU is a Python script; it has no code-page limitation."""
    if str(tmp_path).isascii():
        pytest.skip("needs a non-ASCII path to prove the point")
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    launched = launcher.launch(
        [binary, *prefix, *args], workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"}
    )
    try:
        assert launched.ok is True
    finally:
        launcher.stop(graceful=True)
        launcher.close()


def test_a_kernel_boot_carries_no_disk_drives(tmp_path):
    """QEMU rejects a virtio drive backed by an ELF, so none may be emitted."""
    kernel = tmp_path / "stub.elf"
    kernel.write_bytes(b"kernel")
    launcher = QemuLauncher(MachineConfig(accel="tcg"))
    args = launcher.build_args(
        tmp_path / "fw.bin", tmp_path / "sys.img", tmp_path / "data.img",
        tmp_path, 4444, kernel=kernel,
    )
    drives = [a for a in args if a.startswith("if=virtio")]
    assert drives == [], drives
    assert _option_value(args, "-kernel") == kernel.as_posix()


def test_last_args_is_a_copy_not_the_live_list(launcher, images, tmp_path):
    """PRD 9.2: the recorded argv is evidence, so it must not be rewritable.

    A caller that mutates the returned list would silently change what the run
    claims it started.  The copy is what makes the property hold, and without a
    test the guard is invisible -- reverting it to returning ``self._last_args``
    directly breaks nothing observable.
    """
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    launcher._last_args = list(args)

    returned = launcher.last_args
    assert returned == args
    returned.append("-injected")
    returned[0] = "clobbered"

    assert launcher.last_args == args, "the recorded argv was mutated through the accessor"


# ---------------------------------------------------------------------------
# the display integration must not move the real-boot argv
# ---------------------------------------------------------------------------

#: The display block as it was spelled inline in ``build_args`` before the
#: P1(4) integration (commit fd949b0) replaced it with
#: ``DisplayDecision.to_qemu_fragments()``.  Transcribed verbatim so the test
#: can compare against the behaviour that the real-boot acceptance tests were
#: written against, rather than against a paraphrase of it.
_LEGACY_DISPLAY_BLOCK = """\
display = dict(machine.display or {})
display_format = str(display.get("format", "ramfb")).lower()
if display_format.startswith("virtio"):
    args.extend(["-device", "virtio-gpu-pci"])
else:
    args.extend(["-device", "ramfb"])
    args.extend(
        [
            "-fw_cfg",
            "name=opt/org.unixnu/ramfb-size,string="
            f"{int(display.get('width', 0))}x{int(display.get('height', 0))}",
        ]
    )
if headless:
    args.extend(["-display", "none"])
"""


def _legacy_display_block(display: dict, headless: bool) -> list[str]:
    """Run the pre-integration display block and return what it would emit.

    The source above is executed, not re-implemented, so this helper cannot
    drift into paraphrasing the block it is supposed to be the witness for.
    The namespace supplies exactly the three names the block reads.
    """
    args: list[str] = []
    exec(  # noqa: S102 - the source is a module-level constant, not caller input
        _LEGACY_DISPLAY_BLOCK,
        {"machine": SimpleNamespace(display=display), "args": args, "headless": headless},
    )
    return args


def _display_block_of(argv: list[str]) -> list[str]:
    """The slice of ``argv`` the display decision owns.

    It runs from the first ``-device`` to the ``-nic`` that follows it, which is
    exactly the region the integration replaced.
    """
    start = argv.index("-device")
    return argv[start : argv.index("-nic", start)]


@pytest.mark.parametrize("headless", [False, True])
def test_the_display_integration_did_not_move_the_real_boot_argv(tmp_path, headless):
    """The shipped config's argv must be byte-identical to the pre-integration one.

    ``build_args`` now delegates the display block to
    :mod:`unipad.host.display`.  That refactor is only safe if it changed nothing
    about the run the real-boot acceptance tests exercise -- and a refactor that
    quietly adds, drops or reorders a display argument would not fail any
    existing test, because every one of them asserts a property of the argv
    rather than the argv itself.

    So the old block is kept here verbatim and run side by side with the new
    one.  If a future change to ``display.py`` alters what the shipped config
    emits, this fails and names the difference.
    """
    config = read_json(MACHINE_CONFIG_PATH)
    machine = MachineConfig.from_dict(config)
    launcher = QemuLauncher(machine)

    argv = launcher.build_args(
        tmp_path / "fw.bin", tmp_path / "sys.img", None, tmp_path, 4444, headless=headless
    )

    assert _display_block_of(argv) == _legacy_display_block(dict(machine.display or {}), headless)


def test_the_legacy_block_helper_actually_runs_the_legacy_source():
    """The comparison above is only meaningful if the legacy source still runs.

    A transcription that silently became a copy of the new behaviour would make
    the previous test vacuous -- it would compare the code against itself.  This
    pins the legacy block to the one thing it did that the new one does not:
    forward an unsupported resolution straight into ``-fw_cfg``.
    """
    fragments = _legacy_display_block({"width": 1112, "height": 834}, False)

    assert fragments == [
        "-device",
        "ramfb",
        "-fw_cfg",
        "name=opt/org.unixnu/ramfb-size,string=1112x834",
    ]


def test_a_rotated_framebuffer_reaches_fw_cfg_rotated(tmp_path):
    """The fw_cfg geometry must be the *rotated* one, not the requested one.

    This is the guard the parametrised legacy comparison above cannot be: the
    pre-integration block had no rotation support at all, so for a rotated
    config there is no legacy behaviour to diff against -- and rotation=0 makes
    ``base`` and ``framebuffer`` identical, so a bug that emits ``base`` instead
    of ``framebuffer`` is invisible in every unrotated case.

    It is not invisible in practice.  The guest driver reads the framebuffer
    geometry out of fw_cfg (QemuRamfbPkg), so a rotated 1376x1024 asked for as
    1024x1376 and delivered as 1376x1024 gives the guest a framebuffer whose
    rows are the wrong length -- every row misaligned, and no test asserting a
    property of the argv would notice.
    """
    machine = MachineConfig(
        display={"width": 1376, "height": 1024, "format": "ramfb", "rotation": 90}
    )
    launcher = QemuLauncher(machine)

    argv = launcher.build_args(
        tmp_path / "fw.bin", tmp_path / "sys.img", None, tmp_path, 4444
    )

    assert _option_value(argv, "-fw_cfg") == (
        "name=opt/org.unixnu/ramfb-size,string=1024x1376"
    ), "the framebuffer reached the guest unrotated"


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [(0, "1376x1024"), (90, "1024x1376"), (180, "1376x1024"), (270, "1024x1376")],
)
def test_every_quarter_turn_produces_the_geometry_it_should(tmp_path, rotation, expected):
    """All four turns, so a swap between two of them cannot pass by symmetry.

    90 and 270 produce the same pair, and 0 and 180 produce the same pair, so
    any single pair is consistent with two different bugs.  Asserting the whole
    table is what distinguishes "the axes are swapped" from "the turn is wrong".
    """
    machine = MachineConfig(
        display={"width": 1376, "height": 1024, "format": "ramfb", "rotation": rotation}
    )
    launcher = QemuLauncher(machine)

    argv = launcher.build_args(
        tmp_path / "fw.bin", tmp_path / "sys.img", None, tmp_path, 4444
    )

    assert _option_value(argv, "-fw_cfg") == f"name=opt/org.unixnu/ramfb-size,string={expected}"


# ---------------------------------------------------------------------------
# the host_forwards rules (PRD 3.2 P1 (1)) -- refuted by mutation, so pinned here
# ---------------------------------------------------------------------------


def test_host_forwards_refuses_a_bare_string_instead_of_iterating_it():
    """A string is a sequence of characters, so the type gate is load-bearing.

    Without it ``"host_forwards": "tcp:1234"`` iterates into ``'t'``, ``'c'``,
    ``'p'`` ... and each character is checked as a rule.  Whatever happens next,
    the operator asked for one thing and got a per-character interpretation of
    it.  Refusing the shape is the only reading that cannot be silently wrong.
    """
    from unipad.host.qemu import _host_forwards

    for bad in ("tcp:1234", b"tcp:1234", 8443, True, {"host_port": 8443}):
        with pytest.raises(UniPadError) as excinfo:
            _host_forwards({"host_forwards": bad})
        assert excinfo.value.code is ErrorCode.CONFIG_INVALID, bad


def test_host_forwards_refuses_a_non_mapping_rule_without_partial_output():
    """A half-valid rule list must be refused whole, not applied in part.

    The docstring promises every rule is validated before any fragment is
    produced.  If that is not true, a list whose *second* rule is malformed
    still opens the first rule's forwarding -- the operator gets a port forward
    they did not successfully ask for, and an error message about a rule they
    may believe was never applied.
    """
    from unipad.host.qemu import _host_forwards

    with pytest.raises(UniPadError) as excinfo:
        _host_forwards({"host_forwards": [{"host_port": 9443}, "not-a-rule"]})

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert excinfo.value.details.get("index") == 1, (
        "the refusal did not name the offending rule, so it cannot be acted on"
    )


@pytest.mark.parametrize("bad_port", ["8443", 8443.0, None, True, [8443]])
def test_host_forwards_refuses_a_non_integer_port(bad_port):
    """A port that is not an integer is a configuration bug, never a default.

    The refusal must also name *which* rule was wrong.  ``httpsvc.hostfwd_spec``
    validates ports too, so a version of this check that was removed would still
    raise -- one layer down, with a message that cannot tell the operator which
    of their ten rules to fix.  Asserting the index is what distinguishes "the
    rule was refused" from "the rule was refused somewhere else".
    """
    from unipad.host.qemu import _host_forwards

    with pytest.raises(UniPadError) as excinfo:
        _host_forwards(
            {
                "host_forwards": [
                    {"host_port": 9443, "guest_port": 8443},
                    {"host_port": bad_port, "guest_port": 8443},
                ]
            }
        )

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert excinfo.value.details.get("index") == 1, (
        "the refusal did not name the offending rule, so it cannot be acted on"
    )


@pytest.mark.parametrize("rule", [{"host_port": 0}, {"host_port": 0, "guest_port": 8443},
                                  {"host_port": 9443, "guest_port": 0}])
def test_host_forwards_refuses_port_zero(rule):
    """Port 0 is an ephemeral-port request, and ``hostfwd`` cannot honour one.

    QEMU would pick the host port itself, so the forwarding the operator
    configured is not the forwarding that exists -- and nothing in the argv says
    which port was actually opened.
    """
    from unipad.host.qemu import _host_forwards

    with pytest.raises(UniPadError) as excinfo:
        _host_forwards({"host_forwards": [rule]})

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_host_forwards_produces_one_fragment_per_rule_in_order():
    """The rules are applied in the order given, one fragment each."""
    from unipad.host.qemu import _host_forwards

    fragments = _host_forwards(
        {
            "host_forwards": [
                {"host_port": 9443, "guest_port": 8443},
                {"host_port": 9444, "guest_port": 8444},
            ]
        }
    )

    assert fragments == [
        "hostfwd=tcp:127.0.0.1:9443-10.0.2.15:8443",
        "hostfwd=tcp:127.0.0.1:9444-10.0.2.15:8444",
    ]


def test_an_omitted_restrict_is_confined_not_open():
    """``restrict`` defaults to ON.  This is the default the whole design rests on.

    A flipped default survives every test that spells ``restrict`` explicitly,
    which is why it is pinned here against the *omitted* case.  If this ever
    becomes ``off``, a configuration that never mentioned egress silently puts
    the guest on the public internet -- the one change the project requires
    explicit operator authorisation for.
    """
    from unipad.host.qemu import _network_argument

    assert "restrict=on" in _network_argument({"enabled": True, "mode": "user"})
    assert "restrict=off" not in _network_argument({"enabled": True, "mode": "user"})


def test_the_display_decision_is_emitted_exactly_once(launcher, images, tmp_path):
    """``-device ramfb`` appears once, never twice.

    The integration replaced the inline display block rather than adding to it,
    because QEMU rejects a duplicated device outright.  A regression that emits
    both would produce an argv QEMU refuses -- but nothing asserts the count, so
    the failure would surface as a boot that will not start rather than as a
    failing test.
    """
    args = _args(launcher, images, tmp_path, fq.free_tcp_port())

    assert args.count("ramfb") == 1
    assert args.count("-device") == 1


@pytest.mark.parametrize(
    ("configured", "expected_fw_cfg", "expects_audio"),
    [
        ({"rotation": 90}, "name=opt/org.unixnu/ramfb-size,string=1024x1376", False),
        ({"audio": "hda"}, "name=opt/org.unixnu/ramfb-size,string=1376x1024", True),
    ],
)
def test_the_configured_rotation_and_audio_reach_the_argv(
    tmp_path, configured, expected_fw_cfg, expects_audio
):
    """The delegation must forward what the config asked for, not the defaults.

    ``decide_display`` is called with ``rotation`` and ``audio`` read out of
    ``machine.display``.  If either is dropped, the run silently becomes an
    unrotated, silent one -- and because the shipped config sets neither, no
    test using the shipped config can see it.
    """
    display = {"width": 1376, "height": 1024, "format": "ramfb", **configured}
    machine = MachineConfig(display=display)
    launcher = QemuLauncher(machine)

    args = launcher.build_args(tmp_path / "fw.bin", tmp_path / "sys.img", None, tmp_path, 4444)

    assert _option_value(args, "-fw_cfg") == expected_fw_cfg
    assert ("-audiodev" in args) is expects_audio
    assert ("intel-hda" in args) is expects_audio


# ---------------------------------------------------------------------------
# the P1 review round's qemu findings, each closed by a test written first
#
# Every test below was run against the behaviour it replaces and shown to fail
# there; the fix landed afterwards.  A test that passes both before and after
# its fix is not evidence, so the ones that only guard the fix against a
# future over-correction say so in their docstrings.
# ---------------------------------------------------------------------------


# -- D2: a host_forwards rule missing guest_port is not 8443 -----------------


def test_a_host_forwards_rule_must_name_both_ports():
    """``{"host_port": 9443}`` is an unfinished rule, not a mapping to 8443.

    The docstring and ``config/machine.json``'s ``host_forwards_note`` both
    document a rule as ``{"host_port": int, "guest_port": int}``.  Everything
    else in this chain fails closed -- "an unfinished configuration is a usage
    error, never a guess" -- and this one key silently invented the other half
    of a port mapping the operator never asked for.
    """
    from unipad.host.qemu import _host_forwards

    with pytest.raises(UniPadError) as excinfo:
        _host_forwards({"host_forwards": [{"host_port": 9443}]})

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert excinfo.value.details.get("index") == 0, (
        "the refusal must name the rule it refused, or an operator with ten "
        "rules cannot tell which one to fix"
    )
    assert "guest_port" in excinfo.value.message


def test_a_host_port_without_a_guest_port_still_refuses_its_own_port_zero():
    """The two keys are checked independently: a bad ``host_port`` is reported
    as a bad port, not as a missing partner."""
    from unipad.host.qemu import _host_forwards

    with pytest.raises(UniPadError) as excinfo:
        _host_forwards({"host_forwards": [{"host_port": 0}]})

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "host_port" in excinfo.value.message


# -- D3: one port validator, one range, one message --------------------------


@pytest.mark.parametrize(
    "rule",
    [
        {"host_port": 99999, "guest_port": 8443},
        {"host_port": -1, "guest_port": 8443},
        {"host_port": 9443, "guest_port": 99999},
        {"host_port": 9443, "guest_port": -1},
    ],
)
def test_a_host_forwards_port_outside_the_tcp_range_is_refused_here(rule):
    """A port above 65535 must be refused by *this* module, naming the rule.

    ``host_port: 99999`` used to pass the check in ``_host_forwards`` and be
    caught one layer down inside :func:`unipad.host.httpsvc.hostfwd_spec`,
    whose message cannot say which of the operator's rules was wrong.  Two
    implementations of one rule is how the range went missing from the one the
    operator reads.
    """
    from unipad.host.qemu import _host_forwards

    with pytest.raises(UniPadError) as excinfo:
        _host_forwards(
            {"host_forwards": [{"host_port": 9443, "guest_port": 8443}, rule]}
        )

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert excinfo.value.details.get("index") == 1
    assert "httpsvc" not in excinfo.value.message


@pytest.mark.parametrize("port", [0, -1, 65536, "4444"])
def test_the_qmp_socket_and_a_forward_share_one_port_validator(launcher, images, tmp_path, port):
    """One range for every port this module puts on a QEMU command line.

    The QMP check said "1..65535" and the forward check said nothing at all,
    which is the drift a single validator exists to prevent.  Sharing a helper
    is only real if the two refusals are the *same* refusal, so the QMP one is
    compared against the helper's own output rather than against a paraphrase:
    two implementations of one rule read alike until one of them is edited.
    """
    from unipad.host.qemu import _host_forwards, _validate_tcp_port

    with pytest.raises(UniPadError) as first:
        launcher.build_args(images["firmware"], images["system"], images["data"], tmp_path, port)  # type: ignore[arg-type]
    with pytest.raises(UniPadError) as direct:
        _validate_tcp_port(port, "qmp_port")
    with pytest.raises(UniPadError) as second:
        _host_forwards({"host_forwards": [{"host_port": port, "guest_port": 8443}]})

    assert first.value.code is second.value.code is ErrorCode.CONFIG_INVALID
    assert (first.value.message, first.value.details) == (
        direct.value.message,
        direct.value.details,
    ), "the qmp_port refusal must be the shared validator's refusal, not a second copy of it"
    assert "host_port" in second.value.message
    assert second.value.details.get("index") == 0


# -- D4: a falsy devices.net is a typo, not "network off" --------------------


@pytest.mark.parametrize("net", [False, 0, "", [], "user", 5, {"enabled": True}])
def test_a_net_value_that_is_not_an_object_is_refused_not_read_as_off(launcher, images, tmp_path, net):
    """``"net": false`` must not silently disable the guest network.

    ``_network_argument`` refuses every non-mapping, and the caller's trailing
    ``or {}`` converted each *falsy* one into ``{}`` -- so the guard fired for
    the values an operator is least likely to typo (``"user"``, ``5``) and was
    bypassed for the ones they are (``false``, ``0``, ``""``, ``[]``).
    """
    machine = MachineConfig(devices={"net": net, "firmware": {"attach": "pflash"}})

    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher(machine).build_args(
            images["firmware"], images["system"], images["data"], tmp_path, 4444
        )

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize("devices", [{}, {"net": None}, None])
def test_only_an_absent_net_section_means_the_network_is_off(devices):
    """The one reading that may stay silent is a section that is not there.

    ``null`` is "not specified" rather than a typo, and an absent section is
    the dataclass default; both must keep yielding ``-nic none`` without
    becoming a refusal.
    """
    args = QemuLauncher(MachineConfig(devices=devices)).build_args(
        Path("fw.bin"), Path("s.img"), None, Path("."), 4444
    )

    assert _option_value(args, "-nic") == "none"


# -- D5: a firmware section that names no attachment is not -bios ------------


@pytest.mark.parametrize(
    "devices",
    [
        {"firmware": None},
        {"firmware": {}},
        {"firmware": []},
        {"firmware": "pflash"},
        False,
        0,
        "",
        [],
        "net",
    ],
)
def test_a_devices_section_with_no_usable_firmware_is_refused(devices):
    """``models.py:199-201`` exists so an empty default cannot mean ``-bios``.

    That comment is about the *default*: ``MachineConfig.devices`` defaults to
    ``{"firmware": {"attach": "pflash"}}`` because the board we boot cannot use
    the single-blob spelling.  ``_firmware_attachment`` answered ``"bios"``
    instead, so four config typos each silently changed the machine's firmware
    form -- the exact failure the model comment was written to prevent,
    re-introduced one layer up.
    """
    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher(MachineConfig(accel="tcg", devices=devices))

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_the_firmware_default_is_the_one_the_model_defaults_to():
    """Two definitions of "the machine" drift silently; this pins them together."""
    launcher = QemuLauncher(MachineConfig(accel="tcg", devices={}))

    assert launcher.firmware_attachment == MachineConfig().devices["firmware"]["attach"]


def test_a_declared_attachment_still_wins_over_the_default():
    """Refusing the unusable spellings must not make the default unreachable."""
    for attach in ("bios", "pflash"):
        launcher = QemuLauncher(
            MachineConfig(accel="tcg", devices={"firmware": {"attach": attach}})
        )
        assert launcher.firmware_attachment == attach


# -- D6: the rtc clock source is pinned to QEMU's vocabulary -----------------


@pytest.mark.parametrize("rtc", ["utx", "UTC", "gmt", "", "localtime ", True, 0, 1])
def test_an_unknown_rtc_base_is_refused_rather_than_forwarded(rtc):
    """``-rtc base=<value>`` used to accept any truthy string verbatim.

    QEMU answers a base it does not know by exiting, and the message names the
    option and not the typo -- so the diagnosis belongs here, next to the
    catalogue that defines what a clock source is.
    """
    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher(MachineConfig(clock={"rtc": rtc})).build_args(
            Path("fw.bin"), Path("s.img"), None, Path("."), 4444
        )

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize("clock", [False, 0, "", [], "utc", 5])
def test_a_clock_section_that_is_not_an_object_is_refused_not_dropped(clock):
    """``"clock": false`` used to drop ``-rtc`` from the argv entirely.

    Same falsy-swallow shape as ``devices.net``: the guard exists, and the
    caller's ``or {}`` made it unreachable for the value most likely to be
    typed.
    """
    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher(MachineConfig(clock=clock)).build_args(
            Path("fw.bin"), Path("s.img"), None, Path("."), 4444
        )

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_the_shipped_clock_source_still_reaches_the_argv():
    """The pin must not move the real boot: ``config/machine.json`` says utc."""
    machine = MachineConfig.from_dict(read_json(MACHINE_CONFIG_PATH))
    args = QemuLauncher(machine).build_args(
        Path("fw.bin"), Path("s.img"), None, Path("."), 4444
    )

    assert _option_value(args, "-rtc") == "base=utc"


def test_an_absent_clock_section_emits_no_rtc_decision():
    """An unspecified clock is a documented zero value, not a refusal."""
    args = QemuLauncher(MachineConfig(clock={})).build_args(
        Path("fw.bin"), Path("s.img"), None, Path("."), 4444
    )

    assert "-rtc" not in args


# -- D7: a launch that is going to be refused writes nothing -----------------


def test_a_refused_launch_writes_no_pflash_vars_file(tmp_path):
    """``vars.fd`` is 64 MiB on this board; a refused run must not leave one.

    ``launch()`` materialised the writable pflash file *before* checking that
    the images exist, so a run refused for a missing image had already written
    the file -- and the error message named the missing path without
    mentioning the bytes it had just put on disk.

    Two shapes of "missing", because only one of them is decided by the
    ordering alone.  A missing *code* drive is caught twice over: the vars file
    cannot be sized from a code drive that is not there, so it is refused
    before it is created whichever way round the checks run.  A missing
    *system* image is the one where the write used to happen first -- the code
    drive is present and correctly sized, so nothing but the order stands
    between the operator and an orphaned 64 MiB file.
    """
    code = tmp_path / "code.fd"
    code.write_bytes(b"c" * 4096)
    system = tmp_path / "sys.img"
    system.write_bytes(b"s")
    launcher = QemuLauncher(_pflash_config())

    for firmware, system_image in (
        (tmp_path / "absent.fd", system),
        (code, tmp_path / "absent.img"),
    ):
        args = launcher.build_args(firmware, system_image, None, tmp_path, 4444)

        with pytest.raises(UniPadError) as excinfo:
            launcher.launch([sys.executable, "-c", "pass"], args=args, workdir=tmp_path)

        assert excinfo.value.code is ErrorCode.IMAGE_NOT_FOUND
        assert not (tmp_path / "vars.fd").exists(), (
            "the run was refused, so the writable pflash file it was going to "
            "create must not exist"
        )


def test_a_pflash_refusal_names_the_file_without_naming_the_host(tmp_path):
    """PRD 9.2: the vars-drive refusals name the file, not the account.

    ``_materialise_pflash_vars`` put ``str(path)`` into the error detail, and on
    this host an absolute path embeds the Windows username -- a run directory
    under ``AppData\\Local\\Temp`` names the account twice.  The final component
    is what tells two candidate files apart; the rest is what PRD 9.2 protects.

    Both refusals are covered because only one of them was introduced by the
    repair: the ``OSError`` branch carried the leak from the start, and the
    ``size <= 0`` branch was added by the ordering fix.  A test that pins only
    the new one leaves the old one free to regress.
    """
    code = tmp_path / "code.fd"
    code.write_bytes(b"c" * 4096)
    launcher = QemuLauncher(_pflash_config())
    args = launcher.build_args(code, tmp_path / "sys.img", None, tmp_path, 4444)

    with pytest.raises(UniPadError) as excinfo:
        launcher._materialise_pflash_vars(
            [
                "-drive",
                f"file={tmp_path / 'vars.fd'},if=pflash,format=raw,readonly=off",
            ],
            tmp_path,
        )

    details = excinfo.value.details
    assert details["path"] == "vars.fd"
    # The whole rendered document is what an operator or a log reader sees, so
    # assert on that rather than on the one detail we happened to fix.
    rendered = json.dumps(excinfo.value.to_dict())
    assert str(tmp_path) not in rendered
    assert "vars.fd" in rendered


def test_the_two_path_redaction_helpers_agree():
    """``httpsvc._safe_location`` and ``qemu``'s redaction are one behaviour.

    Both modules had their own copy of "name only the final component"; they now
    share :func:`unipad.host.paths.safe_location`.  This test is what stops a
    future edit from making one of them leak again on its own -- the httpsvc
    suite exercises the httpsvc copy, the qemu suite the qemu copy, and nothing
    exercised the pair until now.
    """
    from unipad.host import httpsvc
    from unipad.host.paths import safe_location

    for candidate in (
        Path("F:/iosvirtual/runs/exp-1/vars.fd"),
        Path("vars.fd"),
        Path("/"),
        Path(""),
    ):
        assert httpsvc._safe_location(candidate) == safe_location(candidate)


def test_a_non_ascii_refusal_writes_no_pflash_vars_file(images, tmp_path):
    """The code-page guard fires before anything is written, not after.

    The two refusals in ``launch()`` are independent, and only one of them ran
    before the 64 MiB write.  This is the one the operator hits on this host,
    where ``%TEMP%`` is under a non-ASCII user directory.
    """
    if str(tmp_path).isascii():
        pytest.skip("this host's tmp_path is ASCII; the guard cannot be provoked here")
    binary = _real_qemu_binary()
    if binary is None:
        pytest.skip("no qemu-system-aarch64 installed; the guard cannot be provoked")

    launcher = QemuLauncher(_pflash_config())
    args = launcher.build_args(
        images["firmware"], images["system"], images["data"], tmp_path, 4444
    )

    with pytest.raises(UniPadError) as excinfo:
        launcher.launch(binary, args, workdir=tmp_path)

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert not (tmp_path / "vars.fd").exists()


def test_a_viable_launch_still_writes_the_vars_file(tmp_path):
    """Reordering the check must not stop the file being created at all."""
    code = tmp_path / "code.fd"
    code.write_bytes(b"c" * 8192)
    system = tmp_path / "sys.img"
    system.write_bytes(b"s")
    launcher = QemuLauncher(_pflash_config())
    args = launcher.build_args(code, system, None, tmp_path, 4444)

    launcher.launch([sys.executable, "-c", "pass"], args=args, workdir=tmp_path)
    try:
        assert (tmp_path / "vars.fd").stat().st_size == 8192
    finally:
        launcher.close()


# -- D8: a stale vars.fd is repaired, not reused -----------------------------


def test_a_zero_length_vars_file_is_repaired_not_reused(tmp_path):
    """A 0-byte ``vars.fd`` is the residue of a failed launch, not variables.

    ``_materialise_pflash_vars`` skipped any existing file, so the next boot
    got a 0-length writable pflash for UEFI variables -- measured: attempt one
    refused for a missing code drive leaves 0 bytes, attempt two with the code
    drive present still finds 0 bytes.
    """
    code = tmp_path / "code.fd"
    code.write_bytes(b"c" * 4096)
    system = tmp_path / "sys.img"
    system.write_bytes(b"s")
    (tmp_path / "vars.fd").write_bytes(b"")
    launcher = QemuLauncher(_pflash_config())
    args = launcher.build_args(code, system, None, tmp_path, 4444)

    launcher.launch([sys.executable, "-c", "pass"], args=args, workdir=tmp_path)
    try:
        assert (tmp_path / "vars.fd").stat().st_size == 4096
    finally:
        launcher.close()


def test_a_short_vars_file_is_extended_without_losing_what_it_holds(tmp_path):
    """A short file is extended, not truncated: the variables survive.

    QEMU cannot use a backing file shorter than the flash device, and simply
    refusing would leave the operator with a run that cannot start and a file
    they are told nothing about.  Extending keeps the leading bytes -- which
    are the variables -- and makes the device usable.
    """
    code = tmp_path / "code.fd"
    code.write_bytes(b"c" * 4096)
    system = tmp_path / "sys.img"
    system.write_bytes(b"s")
    (tmp_path / "vars.fd").write_bytes(b"VARS")
    launcher = QemuLauncher(_pflash_config())
    args = launcher.build_args(code, system, None, tmp_path, 4444)

    launcher.launch([sys.executable, "-c", "pass"], args=args, workdir=tmp_path)
    try:
        repaired = (tmp_path / "vars.fd").read_bytes()
        assert len(repaired) == 4096
        assert repaired[:4] == b"VARS"
    finally:
        launcher.close()


def test_a_correctly_sized_vars_file_is_left_alone(tmp_path):
    """A guard, not a bug-catcher: persisting variables is the whole point.

    This passes before and after the repair above.  It is here so a future
    "repair" cannot quietly become "always truncate", which would destroy the
    settings UEFI stored across boots -- the one thing the second flash device
    exists for.
    """
    code = tmp_path / "code.fd"
    code.write_bytes(b"c" * 4096)
    system = tmp_path / "sys.img"
    system.write_bytes(b"s")
    (tmp_path / "vars.fd").write_bytes(b"V" * 4096)
    launcher = QemuLauncher(_pflash_config())
    args = launcher.build_args(code, system, None, tmp_path, 4444)

    launcher.launch([sys.executable, "-c", "pass"], args=args, workdir=tmp_path)
    try:
        assert (tmp_path / "vars.fd").read_bytes() == b"V" * 4096
    finally:
        launcher.close()


# -- D9: the code-page guard is about the binary, not its name ---------------


def test_the_ascii_guard_survives_renaming_the_binary(tmp_path):
    """A renamed QEMU has the same code-page limitation as a named one.

    The guard was keyed on ``"qemu-system" in Path(binary).name``, so a build
    installed or wrapped under any other name silently lost the up-front
    diagnosis and reproduced the opaque ``open <path> failed`` the check exists
    to prevent.  The copy is never executed: the refusal happens before
    ``Popen``, which is exactly what this test is proving.
    """
    if str(tmp_path).isascii():
        pytest.skip("this host's tmp_path is ASCII; the guard cannot be provoked here")
    source = _real_qemu_binary()
    if source is None:
        pytest.skip("no qemu-system-aarch64 installed; the guard cannot be provoked")

    renamed = tmp_path / ("arm64-emulator" + Path(source).suffix)
    shutil.copy2(source, renamed)
    (tmp_path / "code.fd").write_bytes(b"c" * 4096)
    (tmp_path / "sys.img").write_bytes(b"s")
    launcher = QemuLauncher(_pflash_config())
    args = launcher.build_args(
        tmp_path / "code.fd", tmp_path / "sys.img", None, tmp_path, 4444
    )

    with pytest.raises(UniPadError) as excinfo:
        launcher.launch(str(renamed), args, workdir=tmp_path)

    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert excinfo.value.details["reason"] == NON_ASCII_PATH_REASON


def test_the_interpreter_is_the_only_binary_exempt_from_the_guard():
    """The exemption is identity, not a name we happen to recognise.

    The test fake is ``[sys.executable, fake_qemu.py, ...]`` -- a Python script
    with no ANSI file backend -- so refusing a run on its behalf would block a
    path that works.  Naming that exemption by identity keeps it true when the
    fake is renamed and keeps the guard on when QEMU is.
    """
    from unipad.host.qemu import _is_substitute_binary

    assert _is_substitute_binary(sys.executable) is True
    assert _is_substitute_binary("qemu-system-aarch64") is False
    assert _is_substitute_binary("arm64-emulator.exe") is False
    assert _is_substitute_binary("C:/QEMU/qemu-wrapper.bat") is False


# -- D10: the block-driver catalogue is documented and it works --------------


def test_every_block_driver_prefix_is_documented():
    """``BLOCK_DRIVER_PREFIXES`` is only discoverable by reading this module.

    ``fat:`` and ``host_device:`` at least appear in
    ``docs/implementation-plan.md``; ``json:`` appeared nowhere at all -- not in
    a doc, not in ``config/machine.json``, not in a note.  The catalogue is a
    device vocabulary, so the vocabulary now carries its own definitions and a
    prefix cannot be added without one.
    """
    from unipad.host import qemu as qemu_module

    documented = set(qemu_module.BLOCK_DRIVER_PREFIX_MEANINGS)
    assert documented == set(qemu_module.BLOCK_DRIVER_PREFIXES), (
        "a block-driver prefix was added without documenting what it means"
    )
    assert all(meaning.strip() for meaning in qemu_module.BLOCK_DRIVER_PREFIX_MEANINGS.values())


def test_a_json_drive_value_is_not_judged_as_a_missing_image():
    """``json:`` is an inline blockdev description, not a path on disk.

    ``_strip_block_driver`` returned the JSON text as the path to check, so
    ``_missing_paths`` reported a perfectly valid ``json:`` drive as a missing
    image and ``launch()`` refused the run.  The entry was in the catalogue,
    undocumented, and did not work.
    """
    launcher = QemuLauncher(MachineConfig(accel="tcg"))
    args = [
        "-drive",
        'if=none,id=os,format=raw,readonly=on,file=json:{"driver":"raw",'
        '"file":{"filename":"os.img"}}',
    ]

    assert launcher._missing_paths(args) == []
    assert _parse_drive_file(args[1]) is None


def test_a_fat_directory_and_a_host_device_are_still_judged_the_same_way(tmp_path):
    """The other two catalogue entries keep the behaviour they already had."""
    launcher = QemuLauncher(MachineConfig(accel="tcg"))
    esp = tmp_path / "esp"
    esp.mkdir()

    assert launcher._missing_paths(
        ["-drive", f"if=none,id=esp,format=raw,readonly=on,file=fat:{esp.as_posix()}"]
    ) == []
    assert launcher._missing_paths(
        ["-drive", f"if=none,id=esp,format=raw,readonly=on,file=fat:{tmp_path / 'nope'}"]
    ) != []
    assert launcher._missing_paths(
        ["-drive", "if=none,id=os,format=raw,readonly=on,file=host_device:/dev/rdisk2"]
    ) == []


# ---------------------------------------------------------------------------
# the launch evidence describes the command line that was started (PRD 9.2)
# ---------------------------------------------------------------------------


def _caller_argv(
    port: int,
    *,
    accel: Optional[str] = "tcg",
    machine: str = "other-board",
    name: str = "unipad-other-board",
    serial: bool = False,
    qmp: bool = True,
) -> list[str]:
    """A complete, self-contained argv naming a *different* machine.

    ``launch()`` accepts an argv the caller assembled -- :meth:`_adopt_args
    <unipad.host.qemu.QemuLauncher._adopt_args>` exists for exactly that -- so
    a run is not obliged to belong to the ``MachineConfig`` the launcher was
    constructed with.  This helper is that case, spelled out.
    """
    argv: list[str] = []
    if accel is not None:
        argv.extend(["-accel", accel])
    argv.extend(["-machine", machine, "-name", name, "-smp", "1", "-m", "1024"])
    if serial:
        argv.extend(["-serial", "file:serial.log"])
    if qmp:
        argv.extend(["-qmp", f"tcp:127.0.0.1:{port},server=on,wait=off"])
    return argv


def test_the_recorded_machine_is_the_one_the_argv_starts(launcher, images, tmp_path):
    """PRD 9.2: ``launch.json`` must describe the command line that was started.

    ``LaunchResult.evidence`` used to take ``machine`` from the
    ``MachineConfig`` the launcher was built with and ``accel`` from a literal,
    while ``qmp_port``/``serial_path`` were re-derived from the argv by
    ``_adopt_args``.  So a caller-supplied argv belonging to another machine
    produced a record naming *this* launcher's machine: the evidence and the
    command line described two different machines, and a reader reproducing
    the run from the evidence would reproduce the wrong one.
    """
    port = fq.free_tcp_port()
    binary, prefix = _fake_launcher_command()
    argv = _caller_argv(port)

    result = launcher.launch(
        binary, [*prefix, *argv], workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"}
    )
    try:
        evidence = result.to_dict()["evidence"]
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    # Every machine fact in the evidence is the argv's own value, not the
    # launcher's configuration.
    assert evidence["machine"] == _option_value(argv, "-machine") == "other-board"
    assert evidence["name"] == _option_value(argv, "-name") == "unipad-other-board"
    assert evidence["accel"] == _option_value(argv, "-accel") == "tcg"
    # ...and the launcher's own machine is nowhere in the record.
    assert launcher.machine.name not in (evidence["machine"], evidence["name"])


def test_a_caller_supplied_argv_without_explicit_tcg_is_refused(launcher, images, tmp_path):
    """PRD 2.2-1: the accelerator guard is on the command line, not the config.

    The constructor refuses a non-TCG ``MachineConfig``, but ``launch(args=...)``
    took a caller-supplied argv verbatim -- so an argv that stated no
    accelerator at all (QEMU then picks its own, which on a Windows host can be
    WHPX) started a guest while the recorded evidence still said ``tcg``.
    """
    port = fq.free_tcp_port()
    binary, prefix = _fake_launcher_command()
    argv = _caller_argv(port, accel=None)

    try:
        with pytest.raises(UniPadError) as excinfo:
            launcher.launch(binary, [*prefix, *argv], workdir=tmp_path)
    finally:
        # The refusal is the fix; until it lands the child really does start, so
        # the test must not leak one either way.
        launcher.stop(graceful=True)
        launcher.close()

    error = excinfo.value
    assert error.code == ErrorCode.ACCELERATOR_NOT_TCG
    # Nothing was started and nothing was written.
    assert launcher.is_running() is False
    assert not (tmp_path / "qemu-stdout.log").exists()


@pytest.mark.parametrize("accel", ["whpx", "hvf", "kvm", "hax", "xen", "auto"])
def test_a_caller_supplied_argv_with_a_non_tcg_accel_is_refused(launcher, images, tmp_path, accel):
    """PRD 2.2-1: an argv naming another accelerator is refused, not started.

    This is the same guard the constructor applies to the configuration.  It has
    to apply to the argv too, because the argv is what QEMU reads: a record that
    says ``tcg`` beside a command line that says ``whpx`` is the disagreement
    this section exists to make impossible.
    """
    port = fq.free_tcp_port()
    binary, prefix = _fake_launcher_command()
    argv = _caller_argv(port, accel=accel)

    try:
        with pytest.raises(UniPadError) as excinfo:
            launcher.launch(binary, [*prefix, *argv], workdir=tmp_path)
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    error = excinfo.value
    assert error.code == ErrorCode.ACCELERATOR_NOT_TCG
    assert error.details["received_accel"] == accel
    assert launcher.is_running() is False


def test_the_evidence_agrees_with_the_argv_for_a_built_launch(launcher, images, tmp_path):
    """The control: for a ``build_args`` run the evidence still names the machine.

    Deriving the evidence from the argv must not empty it.  A launcher that
    reports nothing would satisfy every test above while recording no evidence
    at all, which is the failure mode this pins.
    """
    port = fq.free_tcp_port()
    args = _args(launcher, images, tmp_path, port)
    binary, prefix = _fake_launcher_command()

    result = launcher.launch(
        binary, [*prefix, *args], workdir=tmp_path, env={"UNIPAD_FAKE_BOOT_MS": "0"}
    )
    try:
        evidence = result.to_dict()["evidence"]
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    assert evidence["accel"] == "tcg"
    assert evidence["machine"] == _option_value(args, "-machine")
    assert evidence["name"] == f"unipad-{launcher.machine.name}"
    assert evidence["started_at_epoch"] > 0


def test_the_recorded_serial_path_is_the_one_the_argv_states(launcher, images, tmp_path):
    """``serial_path`` is a path the command line names, or nothing.

    ``_adopt_args`` re-derives the serial path only when the argv *carries* a
    ``-serial file:`` token, so the value remembered by an earlier
    ``build_args()`` survived into a run whose argv named no serial log at all.
    A launch record that points at another run's serial file is the same defect
    as a machine fact taken from the configuration instead of the argv.
    """
    built_dir = tmp_path / "built"
    other_dir = tmp_path / "other"
    built_dir.mkdir()
    other_dir.mkdir()
    port = fq.free_tcp_port()
    # Remember a serial log in *built*.
    _args(launcher, images, built_dir, port)

    binary, prefix = _fake_launcher_command()
    argv = _caller_argv(port, serial=False)
    result = launcher.launch(
        binary, [*prefix, *argv], workdir=other_dir, env={"UNIPAD_FAKE_BOOT_MS": "0"}
    )
    try:
        record = result.to_dict()
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    # The argv names no serial log, so the record must not name one either --
    # and in particular must not name the earlier run's.
    assert record["serial_path"] == ""
    assert str(built_dir) not in record["serial_path"]


def test_the_recorded_qmp_port_is_the_one_the_argv_states(launcher, images, tmp_path):
    """``qmp_port``/``wait_for_qmp`` follow the argv, not an earlier ``build_args``.

    ``_adopt_args`` re-derived the serial path only when the argv carried a
    ``-serial`` token; the QMP slot had the same shape and was left out.  A
    caller-supplied argv with no ``-qmp`` therefore kept the port an earlier
    ``build_args()`` had reserved, so the launch record named a control channel
    the started command line never opened and ``wait_for_qmp`` probed it.
    """
    built_dir = tmp_path / "built"
    other_dir = tmp_path / "other"
    built_dir.mkdir()
    other_dir.mkdir()
    stale_port = fq.free_tcp_port()
    # Reserve a port with build_args(), whose argv carries ``-qmp``.
    _args(launcher, images, built_dir, stale_port)

    binary, prefix = _fake_launcher_command()
    argv = _caller_argv(fq.free_tcp_port(), serial=False, qmp=False)

    result = launcher.launch(
        binary, [*prefix, *argv], workdir=other_dir, env={"UNIPAD_FAKE_BOOT_MS": "0"}
    )
    try:
        record = result.to_dict()
        readiness = launcher.wait_for_qmp(timeout=0.2)
    finally:
        launcher.stop(graceful=True)
        launcher.close()

    # The record claims no control channel, and readiness says so rather than
    # reporting on a port nothing is listening to.
    assert record["qmp_port"] == 0
    assert record["qmp_port"] != stale_port
    assert readiness.ready is False
    assert readiness.error_code == ErrorCode.CONFIG_INVALID
