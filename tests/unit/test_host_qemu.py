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

import shutil
import sys
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.qemu import QemuLauncher, _parse_drive_file
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
    """PRD 4.3: the GPU route is a config choice, not a silent default."""
    machine = MachineConfig(display={"width": 1112, "height": 834, "format": "virtio-gpu"})
    launcher = QemuLauncher(machine)
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(b"fw")
    system = tmp_path / "sys.img"
    system.write_bytes(b"sys")

    args = launcher.build_args(firmware, system, None, tmp_path, 4444)

    assert _option_value(args, "-device") == "virtio-gpu-pci"


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
