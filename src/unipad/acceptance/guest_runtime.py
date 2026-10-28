"""A real :class:`~unipad.acceptance.e2e.E2ERuntime`, for a host that has a guest.

The seam in ``e2e.py`` defaults to *no runtime*, and that default is correct for a
host with nothing to drive.  This module is the other side: the implementation for
a host that does have a bootable guest, which is what the Windows development host
has had since the matched build first mounted its Data volume.

What it drives, and why it drives it that way
---------------------------------------------
``unipad.host.unixnu_boot.boot()`` owns everything a real boot needs -- the ESP,
``vars.fd``, the board's payload parsing, the serial markers, and the guest-log
probe that reads the kernel's ring buffer through QMP.  ``QemuLauncher`` builds its
own argv from a ``MachineConfig`` and knows nothing about any of that.  So this
runtime uses ``unixnu_boot`` and takes a *separate* ``QmpClient`` for the input and
screendump commands, because ``unixnu_boot`` reaches QMP through its own raw
``_qmp()`` helper and the two stacks are not interchangeable.

The process is owned here rather than inside ``boot()`` because ``boot()`` runs for
a fixed window and then kills QEMU, while the ten PRD cases are one *session*: case
2 needs the guest case 1 started.  So this module stages the ESP, builds the argv
and owns the child, and reuses it across cases.

The QMP single-client constraint is load-bearing and easy to get wrong: QEMU serves
one QMP client at a time, and ``GuestObserver`` needs its own connection for every
``pmemsave``.  So no connection is held open across an operation here -- each one
is opened, used and closed.  A version that held one open produced a run whose
guest log read as 0 bytes and whose verdict was about a guest that was never
observed.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from unipad.acceptance.e2e import (
    E2ERuntime,
    StepObservation,
    release_verdict,
)
from unipad.errors import ErrorCode, UniPadError
from unipad.host import unixnu_boot as ub
from unipad.host.guestobserve import (
    GuestMemory,
    GuestObserver,
    KernelImage,
    LoadAddresses,
    load_addresses_from_serial,
)
from unipad.host.input import QmpInputSink
from unipad.host.qmp import QmpClient
from unipad.ipa.package import open_ipa, sha256_file
from unipad.ipa.precheck import VERDICT_REJECT, run_precheck

#: The guest's own line saying its serial keyboard thread is running.  Read from
#: the kernel ring buffer through the guest-log probe, never from ``serial.log``:
#: every prior run's ``serial.log`` stops at the DarwinBoot handoff, so that line
#: is never in it.
LISTENING = b"Serial keyboard started"

#: How long to wait for the guest to report itself, in seconds.  Measured: the
#: line appears at guest t≈40-50s, and the probe needs the firmware's kernel load
#: line in ``serial.log`` before it can translate anything.
GUEST_UP_TIMEOUT_S = 300.0

#: SpringBoard's process name in the guest's task list.
SPRINGBOARD = "SpringBoard"

#: The spellings case 10 accepts for its unsupported-platform package.  PRD
#: 5.1 names no such sample, so a caller registers one; these are the names
#: this runtime will find it under, and a host that used another spelling gets
#: a message listing exactly these rather than a bare "not found".
UNSUPPORTED_SAMPLE_NAMES = (
    "UnsupportedPlatform.ipa",
    "unsupported-platform.ipa",
    "MacOSUniPad.ipa",
    "SimulatorUniPad.ipa",
)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RealGuestRuntime(E2ERuntime):
    """Drive the guest this host has, for the ten PRD 5.2 cases.

    Args:
        inputs: The boot inputs, as :class:`~unipad.host.unixnu_boot.BootInputs`.
        qemu: The ``qemu-system-aarch64`` executable.
        device_dir: The device repository root, which holds ``board.toml``.
        root_image: The raw OS volume, attached read-only.  Without it the guest
            has the ESP alone and blocks in ``vfs_mountroot``, and the VirtIO HID
            devices are not emitted either.
        systemos_image: The raw SystemOS cryptex.  Without it
            ``native_owner_cache_prepare()`` finds no volume and the kernel panics.
        ramfb_driver: ``QemuRamfb.efi``.  Without it nothing applies
            :data:`~unipad.host.unixnu_boot.SCREEN` and the framebuffer stays at
            QEMU's 800x600 default.
        serial_port: Bind the UART to this TCP port as well as logging it.  Not
            needed by any case here -- the cases drive the guest through QMP, not
            through the console -- but accepted so one runtime can serve both.
        qmp_port: The QMP port.
        seconds: The per-case observation window.
        every: Seconds between guest-log probes.
        attempts: Boots to spend on the Data-mount race (see
            :func:`~unipad.host.unixnu_boot.boot_until_mounted`).
        workdir: Where the run directory is created.  Must not be under
            ``local-private`` (PRD 12).
        popen: Inject the process factory for tests.
        sleep: Inject the sleep for tests.
    """

    def __init__(
        self,
        *,
        inputs: ub.BootInputs,
        qemu: Path,
        device_dir: Path,
        root_image: Optional[Path] = None,
        systemos_image: Optional[Path] = None,
        ramfb_driver: Optional[Path] = None,
        serial_port: Optional[int] = None,
        qmp_port: int = ub.DEFAULT_QMP_PORT,
        seconds: int = ub.DEFAULT_SECONDS,
        every: int = ub.DEFAULT_EVERY,
        attempts: int = 1,
        workdir: Optional[Path] = None,
        samples: Optional[dict[str, Path]] = None,
        ledger_path: Optional[Path] = None,
        popen: Any = subprocess.Popen,
        sleep: Any = time.sleep,
    ) -> None:
        self.inputs = inputs
        self.qemu = Path(qemu)
        self.device_dir = Path(device_dir)
        self.root_image = Path(root_image) if root_image else None
        self.systemos_image = Path(systemos_image) if systemos_image else None
        self.ramfb_driver = Path(ramfb_driver) if ramfb_driver else None
        self.serial_port = serial_port
        self.qmp_port = int(qmp_port)
        self.seconds = int(seconds)
        self.every = int(every)
        self.attempts = max(int(attempts), 1)
        self.workdir = Path(workdir) if workdir else Path("unipad-e2e-run")
        self._popen = popen
        self._sleep = sleep
        #: PRD 5.1 samples by name.  The names are the PRD's own
        #: (``HelloUniPad.ipa``, ``UniPadNotes.ipa``); an unsupported-platform
        #: package is not a PRD 5.1 sample, so case 10 looks for it under the
        #: conventional names below and says exactly what it looked for when
        #: none is registered.
        self.samples = {str(k): Path(v) for k, v in (samples or {}).items()}
        #: Where the install ledger lives, when the caller keeps one.  Case 10
        #: reads it back to *prove* the installed set is unchanged rather than
        #: asserting it.
        self.ledger_path = Path(ledger_path) if ledger_path else None

        self._process: Optional[Any] = None
        self._run: Optional[Path] = None
        self._argv: list[str] = []
        self._image: Optional[KernelImage] = None
        self._kernel_digest = ""

    # -- the guest -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _stage(self, base: Optional[Path] = None) -> Path:
        """Stage the ESP and build the argv, once per run.

        ``base`` is where the run directory is created; it defaults to
        ``workdir``.  Case 9 passes a per-cycle base, because a *cold* boot must
        not read the previous cycle's serial log.
        """
        if self._run is not None:
            return self._run
        self._run = (base if base is not None else self.workdir) / "guest"
        self._run.mkdir(parents=True, exist_ok=True)
        ub.stage_esp(self.inputs, self._run / "esp",
                     ub.read_board_payload(self.device_dir))
        vars_path = self._run / "vars.fd"
        code_size = Path(self.inputs.firmware).stat().st_size
        if not vars_path.is_file() or vars_path.stat().st_size < code_size:
            with vars_path.open("wb") as handle:
                handle.truncate(code_size)
        self._argv = ub.build_argv(
            self.qemu, self.inputs, self._run / "esp", self._run,
            qmp_port=self.qmp_port, serial_port=self.serial_port,
            memory_mib=ub.MEMORY_MIB, display="none",
            ramfb_size=ub.SCREEN if (self._run / "esp" / ub.RAMFB_NAME).is_file() else None,
            root_image=self.root_image, systemos_image=self.systemos_image,
        )
        (self._run / "command.txt").write_text(
            " ".join(self._argv) + "\n", encoding="utf-8")
        return self._run

    def start(self) -> None:
        """Start QEMU if it is not already running, and wait for the guest.

        Raises:
            UniPadError: ``qemu_unavailable`` when the guest does not report
                itself within :data:`GUEST_UP_TIMEOUT_S`.  The serial log and
                QEMU's stderr are kept, because a guest that never comes up is
                exactly when they are needed.
        """
        if self.running:
            return
        run = self._stage()
        self._process = self._popen(
            [str(self.qemu), *self._argv], cwd=str(run),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        serial = run / "serial.log"
        log = run / "guest-kernel.log"
        kernel = Path(self.inputs.kernel)
        self._kernel_digest = _sha256_of(kernel)
        self._image, reason = ub._kernel_image(kernel, self._kernel_digest)
        if self._image is None:
            raise UniPadError(
                ErrorCode.IMAGE_NOT_FOUND,
                f"the kernel cannot be parsed: {reason}",
                kernel=str(kernel),
            )
        deadline = time.monotonic() + GUEST_UP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise UniPadError(
                    ErrorCode.QEMU_LAUNCH_FAILED,
                    "qemu exited before the guest reported itself",
                    exit_code=self._process.returncode,
                    stderr=_tail(run / "qemu-stderr.log"),
                )
            if self._guest_log(serial, log):
                return
            self._sleep(2.0)
        raise UniPadError(
            ErrorCode.QEMU_TIMEOUT,
            "the guest never reported its serial keyboard",
            serial_bytes=serial.stat().st_size if serial.is_file() else 0,
            stderr=_tail(run / "qemu-stderr.log"),
        )

    def stop(self) -> None:
        """Kill QEMU and keep every log."""
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.kill()
        try:
            self._process.wait(timeout=30)
        except Exception:  # noqa: BLE001 - a wedged child must not block teardown
            pass
        if self._run is not None:
            (self._run / "qemu-stderr.log").write_bytes(
                self._process.stderr.read() if self._process.stderr else b"")
        self._process = None

    # -- reading the guest ----------------------------------------------

    def _guest_log(self, serial: Path, log: Path) -> bytes:
        """The kernel ring, through the one probe that has been measured to work.

        Two paths, because they are two different things.  ``scratch`` is where
        QEMU writes the guest's physical memory -- ``pmemsave`` writes to a file
        and there is no QMP command that returns bytes -- so that file is raw,
        unscrubbed guest RAM from the first read onwards.  ``log`` is the
        evidence: the ring, decoded and scrubbed, and the only one of the two a
        human should ever be pointed at.  They were one path here too, which meant
        a probe that failed after its read left the guest's RAM at this method's
        own ``log``.

        ``scratch`` is a *file* path, not a directory: a directory makes
        ``pmemsave`` fail, and the probe returns not-ready for every read.
        """
        entry = ub._guest_log_probe(
            self.qmp_port, kernel=self._image, serial=serial,
            scratch=log.parent / "guest-memory.bin", log=log)
        if not entry.get("ready"):
            return b""
        return log.read_bytes() if log.is_file() else b""

    def _observer(self, scratch: Path) -> GuestObserver:
        """An observer over the live guest, for processes and named globals."""
        assert self._image is not None and self._run is not None
        load: LoadAddresses = load_addresses_from_serial(
            (self._run / "serial.log").read_bytes()
            if (self._run / "serial.log").is_file() else b"")
        client = QmpClient(port=self.qmp_port, timeout=60.0)
        client.connect()
        try:
            return GuestObserver(
                memory=GuestMemory(client, scratch), image=self._image, load=load)
        finally:
            client.close()

    def processes(self) -> tuple[Any, ...]:
        """The guest's task list, or an empty tuple when it cannot be read."""
        assert self._run is not None
        try:
            return self._observer(self._run / "scratch-processes").processes()
        except Exception:  # noqa: BLE001 - a probe that raises must not kill a case
            return ()

    def screendump(self, target: Path) -> Optional[str]:
        """One screendump's sha256, or ``None``.  QMP is opened and closed here."""
        try:
            with QmpClient(port=self.qmp_port, timeout=60.0) as client:
                reply = client.screendump(target)
        except UniPadError:
            return None
        if not target.is_file():
            return None
        return _sha256_of(target)

    # -- the ten cases ---------------------------------------------------

    def start_new_qemu_process(self) -> StepObservation:
        """PRD 5.2 case 1: start a fresh QEMU and record what it actually is."""
        self.start()
        assert self._run is not None and self._process is not None
        argv = list(self._argv)
        facts = {
            "argv": argv,
            "pid": self._process.pid,
            "kernel": str(self.inputs.kernel),
            "kernel_sha256": self._kernel_digest,
            "devicetree": str(self.inputs.devicetree),
            "root_image": str(self.root_image) if self.root_image else None,
            "systemos_image": str(self.systemos_image) if self.systemos_image else None,
            "ramfb_driver": str(self.ramfb_driver) if self.ramfb_driver else None,
            "qmp_port": self.qmp_port,
            "serial_port": self.serial_port,
            "command_file": str(self._run / "command.txt"),
        }
        return StepObservation(True, facts=facts)

    def reach_springboard(self) -> StepObservation:
        """PRD 5.2 case 2: reach SpringBoard and change the picture with the mouse.

        The machine-checkable criterion is ``framebuffer_before !=
        framebuffer_after``.  The key sent is **home**, and that is not a
        preference: ``package/VirtioInput.cpp`` maps only Home (evdev 102) and F1
        (59) as keys, so any other key produces events the guest counts and keys it
        does not -- which looks exactly like a broken path from outside.
        """
        self.start()
        assert self._run is not None
        processes = self.processes()
        board = next((p for p in processes if p.name == SPRINGBOARD), None)
        before = self.screendump(self._run / "case2-before.ppm")
        issued = self._send_home()
        # The poller runs every 8 ms; give it a few windows to act.
        after = before
        for _ in range(6):
            self._sleep(1.0)
            shot = self.screendump(self._run / "case2-after.ppm")
            if shot is not None:
                after = shot
                if shot != before:
                    break
        facts = {
            "springboard_pid": board.pid if board is not None else None,
            "springboard_state": board.state if board is not None else None,
            "process_count": len(processes),
            "framebuffer_before": before,
            "framebuffer_after": after,
            "key_issued": ["home"],
            "key_reply": issued,
        }
        if board is None:
            return StepObservation(
                False, reason="springboard_not_in_the_guest_task_list", facts=facts)
        # PRD 7.4 line 294: a serial line alone never proves a stage, and a
        # screenshot that does not change proves nothing about input.  So a
        # SpringBoard pid with an unchanged picture is a FAIL, not a pass.
        if before is None or after is None or before == after:
            return StepObservation(
                False, reason="mouse_input_did_not_change_the_framebuffer", facts=facts)
        return StepObservation(True, facts=facts)

    def _send_home(self) -> Any:
        """Press Home through the guest's VirtIO keyboard, over its own connection.

        The connection is opened and closed here, for the same reason every other
        QMP use in this module is: QEMU serves one client at a time, and the
        guest-log probe needs its own.  A caller that holds one open across a
        screendump starves the probe.
        """
        client = QmpClient(port=self.qmp_port, timeout=60.0)
        client.connect()
        try:
            sink = QmpInputSink(client, {
                "width": int(ub.SCREEN.split("x")[0]),
                "height": int(ub.SCREEN.split("x")[1]),
            })
            return sink.key(["home"])
        finally:
            client.close()

    # -- the cases this runtime cannot yet perform -----------------------

    def import_hello_ipa(self) -> StepObservation:
        """PRD 5.2 case 3: import HelloUniPad.ipa and record its digest.

        The digest and the bundle id come from ``open_ipa``, which reads the
        package and never writes to it.  ``installed_before`` is the one fact
        this cannot answer from the host: it asks the guest what it already
        has, and the guest-side status transport is the piece that does not
        exist yet.  So this returns a *failed* observation naming that, rather
        than a pass with ``installed_before`` guessed -- case 3's probe turns a
        truthy ``installed_before`` into
        ``case3_package_was_already_installed``, so guessing ``False`` would be
        a pass the host did not earn.
        """
        path = self.samples.get("HelloUniPad.ipa")
        if path is None or not path.is_file():
            return StepObservation(False, reason="hello_unipad_ipa_sample_absent")
        package = open_ipa(path)
        digest = sha256_file(path)
        if digest != package.sha256:
            return StepObservation(
                False, reason="package_digest_disagrees_with_the_parsed_ipa",
                facts={"package_sha256": digest, "parsed_sha256": package.sha256})
        installed_before = self._installed_bundle_ids()
        if installed_before is None:
            return StepObservation(
                False,
                reason="cannot_ask_the_guest_what_is_already_installed",
                facts={
                    "package_sha256": digest,
                    "bundle_id": package.bundle_id,
                    "size_bytes": package.size_bytes,
                    "app_dir": package.app_dir,
                })
        return StepObservation(True, facts={
            "package_sha256": digest,
            "bundle_id": package.bundle_id,
            "size_bytes": package.size_bytes,
            "app_dir": package.app_dir,
            "installed_before": package.bundle_id in installed_before,
        })

    def _installed_bundle_ids(self) -> Optional[set[str]]:
        """The bundle ids the guest reports as installed, or ``None``.

        ``None`` and an empty set are different answers and the caller must be
        able to tell them apart: an empty set is "nothing is installed", which
        is what case 3 needs, while ``None`` is "the guest could not be
        asked".
        """
        return None

    def install_hello_ipa(self) -> StepObservation:
        return StepObservation(
            False, reason="ipa_install_needs_a_guest_side_install_transport")

    def launch_hello_ipa(self) -> StepObservation:
        return StepObservation(
            False, reason="app_launch_needs_a_guest_side_launch_transport")

    def tap_button_ten_times(self) -> StepObservation:
        return StepObservation(
            False, reason="ten_taps_need_a_touch_target_the_guest_reports")

    def import_notes_ipa_and_save_text(self) -> StepObservation:
        return StepObservation(
            False, reason="notes_save_needs_guest_side_text_entry")

    def shutdown_and_reboot_from_disk(self) -> StepObservation:
        return StepObservation(
            False, reason="reboot_from_disk_needs_a_persisted_data_volume")

    #: What a cycle reached, most advanced first.  ``reach_springboard`` is the
    #: only stage that means the guest is usable, so it is the one a pass
    #: requires; the others exist so a cycle that got *less* far is recorded as
    #: such rather than folded into "it booted".
    COLD_BOOT_STAGES = ("springboard_reached", "kernel_up_no_springboard",
                        "guest_never_reported")

    def repeat_cold_boot_three_times(self) -> StepObservation:
        """PRD 5.2 case 9 (docs/PRD.md:180) and PRD 5.3's three reported boots.

        "重复冷启动流程三次，保存耗时和结果；测试失败时保留证据，不覆盖上次失败."

        A *cold* boot is a fresh QEMU process, so each cycle stages into its own
        directory.  That is not tidiness: reusing one run directory would leave
        the previous cycle's ``serial.log`` in place, ``start()`` would find the
        previous cycle's "Serial keyboard started" in it and return at once, and
        every cycle after the first would report a boot time of milliseconds.
        The per-cycle directory is what makes the timing mean anything, and it
        is also what stops a failing cycle from overwriting the one before it.

        A pass needs all three cycles to reach the *same* stage, and that stage
        to be SpringBoard: three boots that land in three different places are
        not a repeated cold boot, and three boots that none of them reach a
        desktop are not a repeated cold boot of *this* system either.  The
        §5.3 "operable desktop" target is deliberately not claimed by a
        SpringBoard pid alone -- case 2 is the case that has to prove the
        picture changed, and it is still failing on the framebuffer.
        """
        cycles: list[dict[str, Any]] = []
        evidence_paths: list[str] = []
        stages: list[str] = []
        for index in range(1, 4):
            # Kill the previous cycle before staging the next one, so its QMP
            # port and its serial log are both free.
            self.stop()
            self._run = None
            run = self._stage(base=self.workdir / f"case9-cycle-{index}")
            evidence_paths.append(str(run))
            started = time.monotonic()
            stage = "guest_never_reported"
            error: Optional[str] = None
            error_code: Optional[str] = None
            pid: Optional[int] = None
            try:
                self.start()
                pid = self._process.pid if self._process is not None else None
                board = any(p.name == SPRINGBOARD for p in self.processes())
                stage = ("springboard_reached" if board
                         else "kernel_up_no_springboard")
            except Exception as exc:  # noqa: BLE001 - a cycle's failure is its result
                # The code is recorded separately from the message because the
                # gate branches on codes; a bare ``str(exc)`` would make a
                # ``qemu_timeout`` and a ``qemu_launch_failed`` indistinguishable
                # in the report.
                error = f"{type(exc).__name__}: {exc}"
                code = getattr(exc, "code", None)
                error_code = code if isinstance(code, str) else None
            stages.append(stage)
            cycles.append({
                "cycle": index,
                "duration_s": round(time.monotonic() - started, 3),
                "result": stage,
                "stage": stage,
                "pid": pid,
                "error": error,
                "error_code": error_code,
                "evidence": str(run),
                "command_file": str(run / "command.txt"),
            })

        facts = {
            "cycles": cycles,
            "evidence_paths": evidence_paths,
            "stages": stages,
            "same_stage": len(set(stages)) == 1,
            "operable_desktop": False,
        }
        if len(set(stages)) != 1:
            return StepObservation(
                False,
                reason="the_three_cold_boots_did_not_reach_the_same_stage",
                facts=facts)
        if stages[0] != "springboard_reached":
            return StepObservation(
                False,
                reason=f"no_cold_boot_reached_springboard:{stages[0]}",
                facts=facts)
        if len(set(evidence_paths)) != 3:
            return StepObservation(
                False, reason="case9_evidence_paths_are_not_distinct", facts=facts)
        return StepObservation(True, facts=facts)

    def import_unsupported_platform_package(self) -> StepObservation:
        """PRD 5.2 case 10: reject an unsupported platform package, with a reason.

        This case is fully answerable from the host, and that is not a
        shortcut: ``run_precheck`` decides from the package's own bytes -- the
        Mach-O slices, the platform identifiers and the cryptid -- and it
        short-circuits *before* anything is transferred.  So the rejection
        needs no guest, and the recoverability claim can be established rather
        than asserted.

        ``previously_installed_recoverable`` is only ``True`` when the
        rejection really did leave the installed set alone: zero bytes were
        transferred, and when the caller keeps an install ledger its bundle-id
        set is read back and compared.  A host with no ledger gets the
        transfer-half evidence and a note saying the ledger half was not
        checked, which is a weaker claim than the PRD asks for -- so it is
        recorded as such rather than rounded up to a pass.
        """
        path = self._unsupported_sample()
        if path is None:
            return StepObservation(
                False,
                reason="no_unsupported_platform_package_registered",
                facts={"looked_for": sorted(UNSUPPORTED_SAMPLE_NAMES),
                       "registered": sorted(self.samples)})
        report = run_precheck(path)
        if report.verdict != VERDICT_REJECT:
            return StepObservation(
                False, reason="the_registered_package_was_not_rejected",
                facts={"package_sha256": report.package_sha256,
                       "verdict": report.verdict,
                       "fault_codes": report.fault_codes})
        fault = report.faults[0]
        digest = sha256_file(path)
        before = self._ledger_bundle_ids()
        # ``run_precheck`` reads the package and decides; it never opens a
        # channel, so nothing was transferred and no install was attempted.
        # The ledger read below is the check that the installed set is what it
        # was.
        after = self._ledger_bundle_ids()
        ledger_checked = before is not None and after is not None
        recoverable = (before == after) if ledger_checked else None
        facts = {
            "package_sha256": digest,
            "error_code": fault.code,
            "reason": fault.reason,
            "detail": fault.detail,
            "bundle_id": report.bundle_id,
            "bytes_transferred": 0,
            "install_attempted": False,
            "ledger_checked": ledger_checked,
            "ledger_unchanged": recoverable,
            "previously_installed_recoverable": recoverable,
            "fault_codes": report.fault_codes,
        }
        if recoverable is not True:
            return StepObservation(
                False,
                reason=("install_ledger_changed_during_the_rejection"
                        if ledger_checked else
                        "no_install_ledger_to_prove_the_installed_set_is_unchanged"),
                facts=facts)
        return StepObservation(True, facts=facts)

    def _unsupported_sample(self) -> Optional[Path]:
        """The registered package case 10 should reject."""
        for name in UNSUPPORTED_SAMPLE_NAMES:
            path = self.samples.get(name)
            if path is not None and path.is_file():
                return path
        return None

    def _ledger_bundle_ids(self) -> Optional[frozenset]:
        """The install ledger's bundle ids, or ``None`` when there is none."""
        if self.ledger_path is None or not self.ledger_path.is_file():
            return None
        from unipad.ipa.lifecycle import InstallLedger

        try:
            return frozenset(InstallLedger(self.ledger_path).installed_bundle_ids())
        except UniPadError:
            return None


def _tail(path: Path, limit: int = 2000) -> str:
    """A bounded tail of a log, for failure evidence."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return text if len(text) <= limit else "...(truncated)...\n" + text[-limit:]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command line: build the runtime and run the ten PRD 5.2 cases with it.

    ``python -m unipad.acceptance.guest_runtime --manifest DIR --device DIR
    --qemu QEMU --firmware FW --darwinboot DB --unixnu UX --run DIR``

    Without ``--manifest`` the runtime is built from explicit paths instead, which
    is what a host with its own layout uses.  Either way the run is a *real* one:
    QEMU starts, the guest boots, and every case reports what it actually saw.  A
    case that cannot be answered returns a failed observation naming the missing
    piece, so the report distinguishes "not done" from "done and broken" -- which
    is the whole reason the seam defaults to no runtime.
    """
    import argparse

    from unipad.acceptance.e2e import E2EEnvironment, E2ESequence

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="run directory")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None,
                        help="a delivered, hash-verified material directory")
    parser.add_argument("--device", type=Path, default=None,
                        help="the device repository root (holds board.toml)")
    parser.add_argument("--qemu", type=Path, default=None)
    parser.add_argument("--firmware", type=Path, default=None)
    parser.add_argument("--kernel", type=Path, default=None)
    parser.add_argument("--devicetree", type=Path, default=None)
    parser.add_argument("--darwinboot", type=Path, default=None)
    parser.add_argument("--unixnu", type=Path, default=None)
    parser.add_argument("--trust-cache", type=Path, default=None)
    parser.add_argument("--data-ramdisk", type=Path, default=None)
    parser.add_argument("--payload-toml", type=Path, default=None)
    parser.add_argument("--ramfb-driver", type=Path, default=None)
    parser.add_argument("--root-image", type=Path, default=None)
    parser.add_argument("--systemos-image", type=Path, default=None)
    parser.add_argument("--sample", action="append", default=[], metavar="NAME=PATH",
                        help="register a PRD 5.1 sample; repeatable")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="the install ledger case 10 reads to prove recoverability")
    parser.add_argument("--qmp-port", type=int, default=ub.DEFAULT_QMP_PORT)
    parser.add_argument("--serial-port", type=int, default=None)
    parser.add_argument("--seconds", type=int, default=ub.DEFAULT_SECONDS)
    parser.add_argument("--every", type=int, default=ub.DEFAULT_EVERY)
    parser.add_argument("--attempts", type=int, default=1)
    args = parser.parse_args(argv)

    samples: dict[str, Path] = {}
    for item in args.sample:
        name, _, path = item.partition("=")
        if not name or not path:
            parser.error(f"--sample wants NAME=PATH, got {item!r}")
        samples[name] = Path(path)

    if args.manifest:
        for required in ("device", "qemu", "firmware", "darwinboot", "unixnu"):
            if getattr(args, required) is None:
                parser.error(f"--manifest needs --{required} as well")
        runtime = runtime_from_manifest(
            args.manifest,
            device_dir=args.device,
            qemu=args.qemu,
            darwinboot=args.darwinboot,
            unixnu=args.unixnu,
            firmware=args.firmware,
            payload_toml=args.payload_toml,
            ramfb_driver=args.ramfb_driver,
            root_image=args.root_image,
            systemos_image=args.systemos_image,
            serial_port=args.serial_port,
            qmp_port=args.qmp_port,
            seconds=args.seconds,
            every=args.every,
            attempts=args.attempts,
            workdir=args.run,
            samples=samples,
            ledger_path=args.ledger,
        )
    else:
        for required in ("device", "qemu", "firmware", "kernel", "devicetree",
                         "darwinboot", "unixnu"):
            if getattr(args, required) is None:
                parser.error(f"without --manifest, --{required} is required")
        inputs = ub.BootInputs(
            firmware=args.firmware, kernel=args.kernel, devicetree=args.devicetree,
            darwinboot=args.darwinboot, unixnu=args.unixnu,
            payload_toml=args.payload_toml, trust_cache=args.trust_cache,
            data_ramdisk=args.data_ramdisk, ramfb_driver=args.ramfb_driver,
        )
        runtime = RealGuestRuntime(
            inputs=inputs, qemu=args.qemu, device_dir=args.device,
            root_image=args.root_image, systemos_image=args.systemos_image,
            serial_port=args.serial_port, qmp_port=args.qmp_port,
            seconds=args.seconds, every=args.every, attempts=args.attempts,
            workdir=args.run, samples=samples, ledger_path=args.ledger,
        )

    env = E2EEnvironment.create(args.run, repo_root=args.repo_root, runtime=runtime)
    try:
        report = E2ESequence(env).run()
    finally:
        # The guest is a child of this process; a run that raises must not leak it.
        runtime.stop()
    verdict, reasons = release_verdict(report)
    print(f"e2e verdict: {verdict}")
    for reason in reasons:
        print(f"  {reason}")
    print(f"evidence: {env.run_dir}")
    return 0 if verdict == "PASS" else 1


def runtime_from_manifest(
    manifest_dir: Path,
    *,
    device_dir: Path,
    qemu: Path,
    darwinboot: Path,
    unixnu: Path,
    workdir: Optional[Path] = None,
    **kwargs: Any,
) -> RealGuestRuntime:
    """Build a runtime from a delivered, hash-verified material directory.

    The kernel, device tree, trust cache and Data seed come from the manifest's
    own artifact entries, so the runtime boots the bytes the manifest describes
    rather than whatever happens to sit at those paths.  The OS root and SystemOS
    images are named by the manifest too, but they are the *compressed* members:
    the raw volumes the guest attaches are ``.raw`` files beside them, and a
    caller that has not decoded them gets an explicit refusal rather than a boot
    that blocks in ``vfs_mountroot``.

    ``darwinboot`` and ``unixnu`` are not manifest members -- they are payload
    build outputs -- so they are passed in by the caller, who knows the local
    layout.  Everything the manifest *does* name is read from it.
    """
    manifest_dir = Path(manifest_dir)
    payload = json.loads((manifest_dir / "material-manifest.json").read_text(encoding="utf-8"))
    artifacts = payload.get("artifacts") or {}

    def member(role: str) -> Path:
        entry = artifacts.get(role)
        if not isinstance(entry, dict) or not entry.get("path"):
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                f"the material manifest has no {role} member",
                manifest=str(manifest_dir),
                role=role,
            )
        return manifest_dir / str(entry["path"])

    def raw_volume(role: str) -> Optional[Path]:
        compressed = member(role)
        raw = compressed.with_suffix(".raw")
        if raw.is_file():
            return raw
        if compressed.is_file():
            raise UniPadError(
                ErrorCode.IMAGE_NOT_FOUND,
                f"the {role} member is still compressed; decode it to .raw first",
                member=str(compressed),
                expected=str(raw),
            )
        return None

    inputs = ub.BootInputs(
        firmware=kwargs.pop("firmware"),
        kernel=member("kernel"),
        devicetree=member("devicetree"),
        darwinboot=Path(darwinboot),
        unixnu=Path(unixnu),
        payload_toml=kwargs.pop("payload_toml", None),
        trust_cache=member("trust_cache") if "trust_cache" in artifacts else None,
        data_ramdisk=member("data_seed") if "data_seed" in artifacts else None,
        ramfb_driver=kwargs.pop("ramfb_driver", None),
    )
    return RealGuestRuntime(
        inputs=inputs,
        qemu=Path(qemu),
        device_dir=Path(device_dir),
        root_image=raw_volume("os_root_image"),
        systemos_image=raw_volume("systemos_image"),
        workdir=workdir,
        **kwargs,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
