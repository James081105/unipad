"""M1-M4 boot simulation (PRD 6 M1..M4, PRD 4.2, 7.4, 9.2; FR02/FR08).

Every test here drives the **real** :class:`unipad.host.qemu.QemuLauncher`
against the fake QEMU process in ``tests/fakes``: the real argv builder, the
real TCG guard, the real :class:`unipad.host.qmp.QmpClient`, the real serial
tailer, the real screenshot parser and the real evidence bundle.  Nothing is
stubbed at the host/guest boundary, so a green run proves the host side of
UniPad behaves as PRD 6 demands -- and nothing more (there is no UEFI
firmware, no XNU and no iPadOS on this machine; field status stays
UNVERIFIED, docs/testing-strategy.md D3).

What is asserted, and why it is *evidence*
-----------------------------------------
* readiness comes from a QMP greeting + capability negotiation + a
  ``query-status`` reply -- never from a countdown (PRD 7.4 / FR08);
* the M1..M4 markers appear **in order** in the serial log, and a boot that
  stops at M2 is *not* reported as M4 (PRD 6 "不足以通过的情况");
* the recorded QEMU argv shows ``-accel tcg`` and QMP on ``tcp:127.0.0.1``
  (PRD 2.2-1 / 4.2: Windows has no AF_UNIX QMP socket);
* a screendump is a parseable ``P6`` PPM (M1 firmware display evidence);
* pointer move / tap / drag / Home / text land in the fake's input journal
  (M12 host-side contract);
* a graceful stop yields a QMP ``SHUTDOWN`` event, exit code 0 and a complete
  evidence bundle, so the M4 gate passes with a stamped ``verifier_id``;
* a hard kill leaves the evidence set incomplete, and the verifier **refuses**
  to pass (``evidence_incomplete``) -- the negative case is the point;
* a non-TCG machine config is refused *before* launch
  (``accelerator_not_tcg``).
"""

from __future__ import annotations

import contextlib
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.logs import EvidenceBundle, Screenshotter, SerialLogTailer, read_ppm_header
from unipad.host.qemu import QemuLauncher
from unipad.host.input import QmpInputSink
from unipad.models import (
    ArtifactEntry,
    ArtifactManifest,
    ExperimentRecord,
    GateResult,
    MachineConfig,
    read_json,
    write_json,
)
from unipad.controller.verifier import VERIFIER_ID, VERIFIER_VERSION, Verifier
from unipad.host.images import sha256_file
from unipad.host.inventory import collect_host_inventory, write_host_inventory
from tests.fakes import fake_qemu as fq
from tests.fakes.fake_qemu import BOOT_MARKERS

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE_CONFIG_PATH = REPO_ROOT / "config" / "machine.json"

#: What the pinned UniXNU-qemu board demands.  board.toml lives under
#: local-private/ and is not committed, so the values are recorded here with
#: their citations rather than read (see tests/unit/test_host_qemu.py:BOARD).
BOARD = {
    "cpu_model": "cortex-a710",  # run-qemu.py:155
    "machine_type": "virt,virtualization=off,secure=off,gic-version=3",  # run-qemu.py:154
    "cores": 1,  # board.toml:143 cpu_regs="0" -> make-qemu-adt.py:78 cpus=1
    "memory_mb": 6144,  # board.toml:92 dram_size="0x180000000"
    "width": 1376,  # run-qemu.py:39-43
    "height": 1024,
}

#: The M1..M4 markers the staged boot log must emit, in order (PRD 6).
#: Derived from the fake's own contract rather than re-typed here, so a change
#: to the boot stages cannot silently invalidate this assertion.
MILESTONE_MARKERS = tuple(
    marker for marker in BOOT_MARKERS if marker[:2] in {"M1", "M2", "M3", "M4"} and ":" in marker
)
assert [m.split(":", 1)[0] for m in MILESTONE_MARKERS] == ["M1", "M2", "M3", "M4"]

#: How long a test waits for the staged serial markers before giving up.  This
#: is a *bounded evidence wait* (the fake emits them over ``boot_ms``), not a
#: countdown that declares success: the assertion is on the log's contents.
SERIAL_EVIDENCE_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def machine_config() -> dict:
    """``config/machine.json`` as parsed by the controller."""
    return read_json(MACHINE_CONFIG_PATH)


@pytest.fixture
def machine(machine_config) -> MachineConfig:
    """A :class:`MachineConfig` built from the repository machine definition."""
    return MachineConfig.from_dict(machine_config)


@pytest.fixture
def images(tmp_path) -> dict[str, Path]:
    """Firmware / system / data images that really exist on disk."""
    paths = {
        "firmware": tmp_path / "firmware.bin",
        "system": tmp_path / "system.img",
        "data": tmp_path / "data.img",
    }
    for path in paths.values():
        path.write_bytes(b"unipad-acceptance-image")
    return paths


@pytest.fixture
def port() -> int:
    """A free loopback TCP port for this test's QMP endpoint."""
    return fq.free_tcp_port()


def _launcher(machine: MachineConfig) -> QemuLauncher:
    """A launcher wired for fast, deterministic readiness polling."""
    return QemuLauncher(
        machine, qmp_timeout=15.0, poll_interval=0.05, process_timeout=10.0
    )


@contextlib.contextmanager
def running_guest(
    tmp_path: Path,
    machine: MachineConfig,
    images: dict[str, Path],
    port: int,
    *,
    name: str = "run",
    fail_mode: str | None = None,
    boot_ms: int = 300,
    graceful_stop: bool = True,
):
    """Launch the fake QEMU through the real launcher and yield the evidence.

    The child is always stopped (gracefully unless ``graceful_stop`` says
    otherwise) and the launcher is always closed, including when the test body
    raises -- a leaked QEMU process would make the next test's port flaky.
    """
    binary, prefix = fq.fake_qemu_launcher()
    workdir = tmp_path / name
    workdir.mkdir(parents=True, exist_ok=True)
    serial_path = workdir / "serial.log"

    launcher = _launcher(machine)
    args = launcher.build_args(
        images["firmware"],
        images["system"],
        images["data"],
        workdir,
        port,
        serial_log=serial_path,
    )
    env = {"UNIPAD_FAKE_BOOT_MS": str(int(boot_ms))}
    if fail_mode:
        env["UNIPAD_FAKE_FAIL_MODE"] = fail_mode

    launched = launcher.launch(binary, [*prefix, *args], workdir=workdir, env=env)
    try:
        readiness = launcher.wait_for_qmp(timeout=15.0)
        yield SimpleNamespace(
            launcher=launcher,
            launched=launched,
            readiness=readiness,
            workdir=workdir,
            serial_path=serial_path,
            args=args,
            port=port,
        )
    finally:
        launcher.stop(graceful=graceful_stop, timeout=10.0)
        launcher.close()


def _option_value(args: list[str], flag: str) -> str | None:
    """Return the value that follows ``flag`` in an argv list."""
    for index, token in enumerate(args):
        if token == flag and index + 1 < len(args):
            return args[index + 1]
    return None


def _wait_for_serial_markers(path: Path, markers: tuple[str, ...]) -> list[str]:
    """Follow the serial log until every marker is present (or the budget ends).

    The wait is bounded and the *result* is what gets asserted: a log that
    never produces a marker is a failed boot, not a slow one.
    """
    tailer = SerialLogTailer(path, from_end=False, poll_interval=0.05)
    lines: list[str] = []
    deadline = time.monotonic() + SERIAL_EVIDENCE_TIMEOUT
    for line in tailer.follow(timeout=max(0.0, deadline - time.monotonic())):
        lines.append(line)
        if all(marker in lines for marker in markers):
            break
    tailer.close()
    return lines


def _recorded_args(run_dir: Path) -> dict:
    """Read ``fake-qemu-args.json`` -- the child's own record of its argv."""
    records = fq.read_jsonl(run_dir / fq.ARGS_FILENAME)
    assert records, "the fake QEMU child must record its argv as evidence"
    return records[0]


def _cli_paths(tmp_path: Path) -> list[str]:
    """Controller options scoped to ``tmp_path`` (never the repo working tree)."""
    return [
        "--runs-root",
        str(tmp_path / "runs"),
        "--budget-config",
        str(REPO_ROOT / "config" / "budget.json"),
        "--budget-usage",
        str(tmp_path / "budget-usage.json"),
        "--artifacts-root",
        str(tmp_path / "artifacts"),
    ]


# ---------------------------------------------------------------------------
# M1: launch with the repository machine config, QMP ready (PRD 6 M1)
# ---------------------------------------------------------------------------


def test_launch_with_machine_config_reaches_qmp_ready_running(machine, images, tmp_path, port):
    """PRD 6 M1 / FR02: the machine config launches and QMP proves readiness."""
    assert machine.accel == "tcg"
    # The shipped config is the authority, so this reads it rather than
    # re-stating the numbers: a config that drifts from the board we boot must
    # fail here, not in the field.
    assert (machine.display["width"], machine.display["height"]) == (
        BOARD["width"],
        BOARD["height"],
    )

    with running_guest(tmp_path, machine, images, port, name="m1") as guest:
        launched, readiness = guest.launched, guest.readiness

        # --- the process really started --------------------------------
        assert launched.ok is True
        assert launched.pid and launched.pid > 0
        assert launched.error_code is None
        assert guest.launcher.is_running() is True

        # --- readiness is proved by the QMP exchange, not by liveness ---
        assert readiness.ready is True
        assert readiness.error_code is None
        assert readiness.status == "running"
        evidence = readiness.evidence
        assert evidence["host"] == "127.0.0.1"
        assert evidence["port"] == port
        assert evidence["greeting"]["QMP"]["version"]["qemu"]["major"] == 9
        assert evidence["capabilities"] == {}
        assert evidence["status"]["status"] == "running"
        assert evidence["status"]["running"] is True

        # --- the live control channel answers more than one command -----
        qmp = guest.launcher.qmp_client()
        assert qmp is not None and qmp.is_connected is True
        assert qmp.query_name()["name"] == fq.FAKE_NAME
        assert qmp.query_version()["package"] == "fake-qemu"
        assert isinstance(qmp.query_block(), list)
        assert [entry["command"] for entry in qmp.command_log()][:1] == ["qmp_capabilities"]


def test_serial_log_shows_m1_to_m4_markers_in_order(machine, images, tmp_path, port):
    """PRD 6 M1-M4: the staged boot markers appear in order (serial evidence)."""
    with running_guest(tmp_path, machine, images, port, name="markers") as guest:
        lines = _wait_for_serial_markers(guest.serial_path, BOOT_MARKERS)

        positions = [lines.index(marker) for marker in MILESTONE_MARKERS]
        assert all(position >= 0 for position in positions)
        assert positions == sorted(positions), (
            f"boot markers out of order in {guest.serial_path}: {lines}"
        )
        # the whole staged sequence, in order, is the M1..M4 evidence
        assert [line for line in lines if line in BOOT_MARKERS] == list(BOOT_MARKERS)


def test_partial_boot_is_never_reported_as_m4(machine, images, tmp_path, port):
    """PRD 6: a boot that stops at M2 fails the gate; it is not "probably M4"."""
    with running_guest(
        tmp_path, machine, images, port, name="partial", fail_mode="partial-boot"
    ) as guest:
        reached = tuple(m for m in MILESTONE_MARKERS if m in _wait_for_serial_markers(
            guest.serial_path, MILESTONE_MARKERS[:2]
        ))
        assert reached == MILESTONE_MARKERS[:2]
        for missing in MILESTONE_MARKERS[2:]:
            assert missing not in guest.serial_path.read_text(encoding="utf-8")

        # ... and a gate condition that requires all four markers refuses,
        # even though everything else in the evidence set is present.
        bundle = _assemble_evidence(guest, images, "exp-m4-partial")
        verifier = Verifier()
        outcome = verifier.evaluate_detailed(
            "M4",
            "M4",
            bundle.evidence,
            extra_checks=[
                (
                    "boot.markers_complete",
                    lambda: [] if all(m in reached for m in MILESTONE_MARKERS)
                    else [f"missing_boot_marker:{m}" for m in MILESTONE_MARKERS if m not in reached],
                )
            ],
            run_dir=bundle.run_dir,
        )
        assert outcome.result.passed is False
        assert outcome.code == ErrorCode.VERIFICATION_FAILED
        # exactly the two stages the staged log never reached
        marker_reasons = [
            reason
            for reason in outcome.result.failure_reasons
            if reason.startswith("missing_boot_marker:")
        ]
        assert len(marker_reasons) == 2
        assert any(reason.startswith("missing_boot_marker:M3") for reason in marker_reasons)
        assert any(reason.startswith("missing_boot_marker:M4") for reason in marker_reasons)
        assert "evidence_incomplete" not in outcome.result.failure_reasons
        assert read_json(bundle.run_dir / "verification.json")["passed"] is False


# ---------------------------------------------------------------------------
# M1: argv evidence -- explicit TCG, QMP on loopback TCP (PRD 2.2-1 / 4.2)
# ---------------------------------------------------------------------------


def test_recorded_qemu_args_show_tcg_and_loopback_qmp(machine, images, tmp_path, port):
    """PRD 2.2-1 / 4.2: the argv that really ran carries TCG and TCP loopback QMP."""
    with running_guest(tmp_path, machine, images, port, name="args") as guest:
        # --- the child's own record of what it was started with ---------
        record = _recorded_args(guest.workdir)
        parsed = record["parsed"]
        assert parsed["accel"] == "tcg"
        assert parsed["accels"] == ["tcg"]
        assert parsed["qmp"]["transport"] == "tcp"
        assert parsed["qmp"]["host"] == "127.0.0.1"
        assert parsed["qmp"]["port"] == port
        assert parsed["qmp"]["listening"] is True
        assert parsed["qmp"]["options"] == {"server": "on", "wait": "off"}
        assert parsed["serial"]["mode"] == "file"
        assert parsed["serial"]["path"] == guest.serial_path.as_posix()
        assert parsed["machine"] == machine.machine_type
        assert parsed["name"] == f"unipad-{machine.name}"
        assert record["fail_mode"] is None

        # --- the launcher's argv, asserted option by option -------------
        args = guest.args
        assert _option_value(args, "-accel") == "tcg"
        assert _option_value(args, "-cpu") == machine.cpu_model
        assert _option_value(args, "-machine") == machine.machine_type
        assert _option_value(args, "-smp") == str(machine.cores)
        assert _option_value(args, "-m") == str(machine.memory_mb)
        # Exactly one firmware attachment, and the pflash pair the board needs:
        # -bios and -drive if=pflash are mutually exclusive in QEMU, so asserting
        # one spelling would pass while the other silently replaced it.
        assert _option_value(args, "-bios") is None
        pflash = [v for v in args if "if=pflash" in v]
        assert len(pflash) == 2, pflash
        assert f"file={images['firmware'].as_posix()}" in pflash[0]
        assert _option_value(args, "-qmp") == f"tcp:127.0.0.1:{port},server=on,wait=off"
        assert _option_value(args, "-monitor") == "none"      # control != serial
        assert _option_value(args, "-serial") == f"file:{guest.serial_path.as_posix()}"
        assert _option_value(args, "-rtc") == f"base={machine.clock['rtc']}"
        assert _option_value(args, "-name") == f"unipad-{machine.name}"

        drives = [
            value for flag, value in zip(args, args[1:]) if flag == "-drive"
        ]
        system_drive = next(d for d in drives if images["system"].as_posix() in d)
        data_drive = next(d for d in drives if images["data"].as_posix() in d)
        assert "readonly=on" in system_drive      # system volume is read-only
        assert "readonly=off" in data_drive       # Data volume is writable
        assert "if=virtio" in system_drive and "if=virtio" in data_drive
        assert _option_value(args, "-device") == "ramfb"   # ramfb/GOP path first

        # --- and the same argv is written as a diffable evidence file ---
        bundle = EvidenceBundle(guest.workdir / "evidence", "m1-args")
        written = bundle.add_qemu_args(args)
        assert written.read_text(encoding="utf-8").splitlines() == args

        # --- a non-TCP QMP endpoint is refused, not silently ignored ------
        # Windows has no AF_UNIX QMP socket (PRD 4.2): a caller-supplied argv
        # naming one must fail instead of starting a guest with no control.
        binary, prefix = fq.fake_qemu_launcher()
        refused = [
            "unix:/tmp/unipad-qmp.sock,server=on,wait=off"
            if token == f"tcp:127.0.0.1:{port},server=on,wait=off"
            else token
            for token in args
        ]
        with pytest.raises(UniPadError) as excinfo:
            guest.launcher.launch(binary, [*prefix, *refused], workdir=guest.workdir)
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID
        assert excinfo.value.details["qmp_spec"] == "unix:/tmp/unipad-qmp.sock,server=on,wait=off"
        # the running guest is untouched by the refused launch
        assert guest.launcher.is_running() is True


# ---------------------------------------------------------------------------
# M1: screendump is real PPM evidence (PRD 6 M1, 9.2)
# ---------------------------------------------------------------------------


def test_screendump_produces_a_valid_ppm(machine, images, tmp_path, port):
    """PRD 6 M1: the firmware display path yields a parseable P6 screenshot."""
    with running_guest(tmp_path, machine, images, port, name="screen") as guest:
        qmp = guest.launcher.qmp_client()
        screenshotter = Screenshotter(qmp, guest.workdir)

        shot = screenshotter.capture("m1-firmware")

        assert shot["ok"] is True
        assert shot["error_code"] is None
        assert shot["bytes"] and shot["bytes"] > 0
        assert shot["name"] == "m1-firmware.ppm"
        assert shot["width"] == fq.DEFAULT_SCREEN_WIDTH
        assert shot["height"] == fq.DEFAULT_SCREEN_HEIGHT
        assert shot["timestamp"].endswith("Z")
        assert len(shot["sha256"]) == 64

        path = Path(shot["path"])
        assert path.is_file()
        header = read_ppm_header(path)
        assert header == {
            "width": fq.DEFAULT_SCREEN_WIDTH,
            "height": fq.DEFAULT_SCREEN_HEIGHT,
            "maxval": 255,
            "header_bytes": len(f"P6\n{fq.DEFAULT_SCREEN_WIDTH} {fq.DEFAULT_SCREEN_HEIGHT}\n255\n"),
        }
        assert path.stat().st_size == shot["bytes"]

        # the screenshot is accepted into the evidence bundle (PRD 9.2)
        bundle = EvidenceBundle(guest.workdir / "evidence", "m1-screen")
        recorded = bundle.add_screenshot(path)
        assert recorded.is_file()
        assert bundle.evidence.screenshots == [str(recorded)]

        # ... and something that is not a *binary* P6 PPM is never screenshot
        # evidence.  P5 is a valid PPM magic that still is not a screendump, so
        # this fails if the parser stops checking the magic at all.
        not_a_ppm = guest.workdir / "grayscale.ppm"
        not_a_ppm.write_bytes(b"P5\n4 4\n255\n" + bytes(16))
        with pytest.raises(UniPadError) as excinfo:
            read_ppm_header(not_a_ppm)
        assert excinfo.value.code == ErrorCode.RENDER_FAILED
        assert excinfo.value.details["magic"] == "P5"

        with pytest.raises(UniPadError) as excinfo:
            read_ppm_header(guest.workdir / "never-captured.ppm")
        assert excinfo.value.code == ErrorCode.RENDER_FAILED


# ---------------------------------------------------------------------------
# M12 host-side contract: input events reach the guest journal
# ---------------------------------------------------------------------------


def test_input_events_are_recorded_by_the_guest(machine, images, tmp_path, port):
    """PRD 6 M12 / FR04: move, tap, drag, Home and text are all delivered."""
    with running_guest(tmp_path, machine, images, port, name="input") as guest:
        qmp = guest.launcher.qmp_client()
        sink = QmpInputSink(qmp, machine.display, key_delay=0.0)

        sink.pointer_move(120, 240)
        sink.tap(300, 400)
        sink.drag(10, 20, 500, 600, steps=4, delay=0.0)
        sink.home()
        typed = sink.text("ab1")

        # --- the sink's own structured log ------------------------------
        actions = [entry["action"] for entry in sink.event_log()]
        assert actions == ["pointer_move", "tap", "drag", "home", "text"]
        drag_entry = sink.event_log()[2]
        assert drag_entry["moves"] == 4
        assert typed == {"typed": 3, "skipped": []}

        # --- what the guest journal actually received -------------------
        journal = fq.read_jsonl(guest.workdir / fq.INPUT_EVENTS_FILENAME)
        channels = [entry["channel"] for entry in journal]
        # pointer_move(1) + tap(3) + drag(1 press + 4 moves + 1 release) = 10
        assert channels.count("input-send-event") == 10
        # home (1 chord) + "ab1" (3 characters)
        assert channels.count("send-key") == 4

        delivered = [event for entry in journal for event in entry.get("events", [])]
        presses = [e for e in delivered if e["type"] == "btn" and e["data"]["down"] is True]
        releases = [e for e in delivered if e["type"] == "btn" and e["data"]["down"] is False]
        absolutes = [e for e in delivered if e["type"] == "abs"]
        assert len(presses) == 2          # tap + drag
        assert len(releases) == 2
        assert len(absolutes) == 12       # 2 axes per move: 1 + 1 + 4 + 4 moves
        assert all(0 <= e["data"]["value"] <= 0x7FFF for e in absolutes)

        keys = [entry["keys"] for entry in journal if entry["channel"] == "send-key"]
        assert keys == [["ctrl", "alt", "home"], ["a"], ["b"], ["1"]]


# ---------------------------------------------------------------------------
# graceful stop, evidence bundle, and the M4 gate (PRD 7.4, 9.2, FR08/FR09)
# ---------------------------------------------------------------------------


def _assemble_evidence(guest, images: dict[str, Path], run_id: str) -> EvidenceBundle:
    """Assemble the full PRD 9.2 evidence set for one running guest.

    Shared by the passing-gate test and the boot-marker refusal test so the
    *only* difference between them is the evidence being judged, never the
    amount of evidence collected.
    """
    run_dir = guest.workdir / "evidence"
    run_dir.mkdir(parents=True, exist_ok=True)

    bundle = EvidenceBundle(run_dir, run_id)
    bundle.set_experiment_json(_write_experiment_journal(run_dir, run_id))
    bundle.set_host_inventory(
        write_host_inventory(
            run_dir / "host-inventory.json", collect_host_inventory(probe_cim=False)
        )
    )
    bundle.set_artifact_manifest(_write_artifact_manifest(run_dir, run_id, images))
    bundle.add_qemu_args(guest.args)
    bundle.add_serial_log(guest.serial_path)
    # the guest-side log lives outside the run directory: PRD 7.4 keeps it
    # separate from the serial log because serial text alone is not proof.
    guest_log = guest.workdir / "guest.log"
    guest_log.write_text(
        "M4: XNU kernel entered\nmach_vm_init: page tables active\n", encoding="utf-8"
    )
    bundle.add_guest_log(guest_log)
    bundle.add_qmp_log(
        json.dumps({"greeting": guest.readiness.evidence["greeting"]}, sort_keys=True) + "\n"
    )
    screenshot = Screenshotter(guest.launcher.qmp_client(), run_dir).capture("m4")
    assert screenshot["ok"] is True, screenshot
    bundle.add_screenshot(Path(screenshot["path"]))
    return bundle


def _write_experiment_journal(run_dir: Path, run_id: str) -> Path:
    """Write a PRD 7.3 experiment journal for the run."""
    record = ExperimentRecord(
        experiment_id=run_id,
        milestone="M4",
        status="running",
        hypothesis="M1-M4 boot markers appear in order under explicit TCG on Windows",
        base_commit=None,          # source pinning is an M0 open item
        source_lock_sha256=None,
        runtime={"host_inventory": "host-inventory.json", "qemu_version": None, "accel": "tcg"},
    )
    path = run_dir / "experiment.json"
    write_json(path, record.to_dict())
    return path


def _write_artifact_manifest(run_dir: Path, run_id: str, images: dict[str, Path]) -> Path:
    """Write a PRD 8.1 artifact manifest for the images this run used."""
    roles = {"firmware": "firmware", "system": "system_image", "data": "data_image"}
    manifest = ArtifactManifest(
        run_id=run_id,
        target="tcg",
        artifacts=[
            ArtifactEntry(
                name=path.name,
                sha256=sha256_file(path),
                size_bytes=path.stat().st_size,
                arch="aarch64",
                role=roles.get(path.stem, "auxiliary"),
                path=path.as_posix(),
            )
            for path in images.values()
        ],
        toolchain={"qemu": "fake-qemu 1.0 (simulation fixture)"},
    )
    path = run_dir / "artifact-manifest.json"
    write_json(path, manifest.to_dict())
    return path


def test_graceful_stop_emits_shutdown_and_exits_zero(machine, images, tmp_path, port):
    """PRD FR08: a graceful stop is a QMP quit -> SHUTDOWN event -> exit code 0."""
    with running_guest(tmp_path, machine, images, port, name="stop") as guest:
        qmp = guest.launcher.qmp_client()
        assert qmp.events() == []          # nothing shut down yet

        stop = guest.launcher.stop(graceful=True, timeout=10.0)

        assert stop.ok is True
        assert stop.method == "qmp-quit"
        assert stop.exit_code == 0
        assert stop.evidence["graceful"] is True
        assert guest.launcher.is_running() is False

        events = qmp.events()
        assert any(event.get("event") == "SHUTDOWN" for event in events), events
        shutdown = next(e for e in events if e.get("event") == "SHUTDOWN")
        assert shutdown["data"]["guest"] is False


def test_complete_evidence_bundle_passes_the_m4_gate(machine, images, tmp_path, port):
    """PRD 6 M4 / 9.2: a complete bundle passes, with a stamped verifier id."""
    with running_guest(tmp_path, machine, images, port, name="m4-gate") as guest:
        # boot evidence first: the markers must be on disk before collecting
        _wait_for_serial_markers(guest.serial_path, MILESTONE_MARKERS)

        run_id = "exp-m4-acceptance"
        bundle = _assemble_evidence(guest, images, run_id)
        run_dir = bundle.run_dir

        # verification.json is written *by the verifier*, so it is the one
        # item the bundle may still be missing when build() runs.
        evidence = bundle.build(strict=False)
        assert bundle.missing == ["verification_json"]

        verifier = Verifier()
        outcome = verifier.evaluate_detailed("M4", "M4", evidence, run_dir=run_dir)
        result: GateResult = outcome.result

        assert result.passed is True
        assert result.failure_reasons == []
        assert result.milestone == "M4"
        assert result.verifier_id == f"{VERIFIER_ID}@{VERIFIER_VERSION}"
        assert outcome.code is None
        assert outcome.missing == []

        # the bundle is complete once the verifier has written its output
        assert bundle.missing == []
        assert evidence.check_complete() == []

        # ... and the verdict is on disk as PRD 9.2 requires
        verification = read_json(run_dir / "verification.json")
        assert verification["passed"] is True
        assert verification["gate_id"] == "M4"
        assert verification["milestone"] == "M4"
        assert verification["verifier_id"] == VERIFIER_ID
        assert verification["verifier_version"] == VERIFIER_VERSION
        assert verification["error_code"] is None
        assert all(row["passed"] for row in verification["conditions"])
        assert {row["condition_id"] for row in verification["conditions"]} == {
            "evidence.complete",
            "evidence.files_exist",
        }
        assert verification["self_provided_by_verifier"] == ["verification_json"]

        # every referenced evidence path resolves to a real file
        document = read_json(run_dir / "evidence.json")
        # the collection-time snapshot names the one item the verifier supplies
        assert document["missing"] == ["verification_json"]
        # ... and re-collecting after verification sees a complete set
        bundle.build(strict=False)
        assert read_json(run_dir / "evidence.json")["missing"] == []


def test_hard_kill_leaves_evidence_incomplete_and_the_gate_refuses(machine, images, tmp_path, port):
    """PRD 9.2 / 7.4: a hard kill is not a graceful shutdown, and the gate says so."""
    with running_guest(
        tmp_path, machine, images, port, name="hard-kill", graceful_stop=False
    ) as guest:
        qmp = guest.launcher.qmp_client()
        assert qmp.events() == []

        stop = guest.launcher.stop(graceful=False, timeout=10.0)

        # --- the process died, but not gracefully ----------------------
        assert stop.ok is True
        assert stop.method in ("terminate", "kill")
        assert stop.evidence["graceful"] is False
        assert guest.launcher.is_running() is False
        # no QMP SHUTDOWN event: a hard kill produces no guest shutdown evidence
        assert not any(event.get("event") == "SHUTDOWN" for event in qmp.events())

        # --- the evidence a hard-killed run can produce is incomplete ---
        run_dir = guest.workdir / "evidence"
        run_dir.mkdir(parents=True, exist_ok=True)
        bundle = EvidenceBundle(run_dir, "exp-m4-hardkill")
        bundle.add_qemu_args(guest.args)
        bundle.add_serial_log(guest.serial_path)

        evidence = bundle.build(strict=False)
        missing = bundle.missing
        assert "experiment_json" in missing
        assert "host_inventory" in missing
        assert "artifact_manifest" in missing
        assert "verification_json" in missing

        # strict collection refuses to call this a complete set
        with pytest.raises(UniPadError) as excinfo:
            bundle.build(strict=True)
        assert excinfo.value.code == ErrorCode.EVIDENCE_INCOMPLETE
        assert set(excinfo.value.details["missing"]) == set(missing)

        # ... and so does the verifier, even though it can write its own output
        outcome = Verifier().evaluate_detailed("M4", "M4", evidence, run_dir=run_dir)
        assert outcome.result.passed is False
        assert outcome.code == ErrorCode.EVIDENCE_INCOMPLETE
        assert "evidence_incomplete" in outcome.result.failure_reasons
        assert "missing:experiment_json" in outcome.result.failure_reasons
        assert outcome.missing
        assert (run_dir / "verification.json").is_file()
        assert read_json(run_dir / "verification.json")["passed"] is False


# ---------------------------------------------------------------------------
# the TCG guard: refused before launch (PRD 2.2-1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("accel", ["whpx", "hvf", "kvm", "hax", "xen", "auto", "tcg,thread=multi"])
def test_non_tcg_machine_config_is_refused_before_launch(machine_config, tmp_path, accel):
    """PRD 2.2-1: any accelerator but explicit ``tcg`` is refused at construction."""
    config = dict(machine_config)
    config["accel"] = accel

    with pytest.raises(UniPadError) as excinfo:
        QemuLauncher(MachineConfig.from_dict(config))

    error = excinfo.value
    assert error.code == ErrorCode.ACCELERATOR_NOT_TCG
    assert error.details["requested_accel"] == accel
    assert error.details["required_accel"] == "tcg"
    assert "accelerator_not_tcg" in error.message

    # refused *before* launch: no argv was built and no process was started
    assert not (tmp_path / "qemu-stdout.log").exists()
    assert not (tmp_path / "serial.log").exists()


def test_cli_start_refuses_a_non_tcg_profile(machine_config, tmp_path):
    """PRD 9.1 / 2.2-1: ``unipad start --profile whpx`` never reaches QEMU."""
    from unipad.controller import cli

    buffer = io.StringIO()
    exit_code = cli.main(
        [
            "start",
            "--profile",
            "whpx",
            "--artifact",
            str(tmp_path / "artifact-manifest.json"),
            "--machine-config",
            str(MACHINE_CONFIG_PATH),
            *_cli_paths(tmp_path),
        ],
        stream=buffer,
    )
    document = json.loads(buffer.getvalue())

    assert exit_code != 0
    assert document["status"] == "failed"
    assert document["error"]["code"] == ErrorCode.ACCELERATOR_NOT_TCG
    assert document["data"] == {}
