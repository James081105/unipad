"""Tests for the real E2E runtime.

The load-bearing rule these pin: a host that has a bootable guest must be able to
say what that guest *is* and what it did, and a host that cannot answer a
question must fail with the question's name rather than pass with a guess.  Case
3's probe turns a truthy ``installed_before`` into a failure, so a runtime that
guessed ``False`` would be manufacturing a pass.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from unipad.acceptance.e2e import E2ERuntime, StepObservation
from unipad.acceptance import guest_runtime as gr
from unipad.acceptance.guest_runtime import (
    UNSUPPORTED_SAMPLE_NAMES,
    RealGuestRuntime,
)
from unipad.errors import ErrorCode, UniPadError
from unipad.host import unixnu_boot as ub
from unipad.ipa.package import open_ipa, sha256_file

from tests.fixtures.ipa_builder import build_ipa


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeProcess:
    """A QEMU stand-in that is alive until told otherwise."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stderr = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode if self.returncode is not None else 0

    def kill(self) -> None:
        self.returncode = -9


def _inputs(tmp_path: Path) -> ub.BootInputs:
    """A BootInputs whose five required paths exist as real files."""
    names = ("fw.bin", "kernel.macho", "kernel.macho.adt", "DarwinBoot.efi", "UniXNU.efi")
    paths = []
    for name in names:
        target = tmp_path / name
        target.write_bytes(b"\0" * 16)
        paths.append(target)
    return ub.BootInputs(*paths)


def _runtime(tmp_path: Path, **kwargs: object) -> RealGuestRuntime:
    return RealGuestRuntime(
        inputs=_inputs(tmp_path),
        qemu=tmp_path / "qemu-system-aarch64.exe",
        device_dir=tmp_path / "device",
        workdir=tmp_path / "run",
        **kwargs,
    )


def _stub_stage(
    runtime: RealGuestRuntime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """A staged run directory.

    ``start()`` asserts ``self._run`` is set, because every later read is relative
    to it, so a stub that only returns a path would leave the runtime in a state
    no real run can be in.
    """
    run = tmp_path / "run" / "guest"
    run.mkdir(parents=True, exist_ok=True)
    runtime._run = run
    monkeypatch.setattr(runtime, "_stage", lambda: run)
    return run


def _monkey_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    processes: tuple[object, ...] = (),
    guest_log: bytes = b"",
    shots: list[str] | None = None,
    **kwargs: object,
) -> RealGuestRuntime:
    """A runtime whose guest is a fake: no QEMU, no QMP, no kernel parse."""
    runtime = _runtime(tmp_path, **kwargs)

    class _Popen:
        def __call__(self, argv: list[str], **_: object) -> FakeProcess:
            return FakeProcess()

    monkeypatch.setattr(runtime, "_popen", _Popen())
    monkeypatch.setattr(runtime, "_sleep", lambda _: None)
    monkeypatch.setattr(
        runtime, "_guest_log", lambda serial, log: guest_log)

    # The kernel parse is the one genuinely expensive step; a runtime under test
    # must not need a 125 MB Mach-O to answer a question about its own argv.
    monkeypatch.setattr(
        ub, "_kernel_image",
        lambda kernel, digest: (object(), None))

    monkeypatch.setattr(
        runtime, "processes", lambda: processes)
    monkeypatch.setattr(
        runtime, "screendump", lambda target: (shots or [None])[0])
    # Case 2 opens its own QMP connection for the Home key.  There is no QEMU in
    # a unit test, so the seam is stubbed rather than the socket.
    monkeypatch.setattr(runtime, "_send_home", lambda: {"keys": ["home"]})
    return runtime


# ---------------------------------------------------------------------------
# the seam itself
# ---------------------------------------------------------------------------

def test_the_real_runtime_is_an_e2e_runtime() -> None:
    """It must satisfy the seam, or injecting it would be a type error at runtime."""
    assert issubclass(RealGuestRuntime, E2ERuntime)
    for name in (
        "start_new_qemu_process", "reach_springboard", "import_hello_ipa",
        "install_hello_ipa", "launch_hello_ipa", "tap_button_ten_times",
        "import_notes_ipa_and_save_text", "shutdown_and_reboot_from_disk",
        "repeat_cold_boot_three_times", "import_unsupported_platform_package",
    ):
        assert callable(getattr(RealGuestRuntime, name)), name


def test_every_unimplemented_case_names_what_it_needs(
    tmp_path: Path,
) -> None:
    """A stub reason must be specific, never the seam's generic ``no_qemu_runtime``.

    The default ``E2ERuntime`` returns ``no_qemu_runtime`` for all ten.  Once a
    runtime exists, that reason is a lie -- there *is* a runtime -- so each
    unimplemented case has to name the piece it is missing instead.
    """
    runtime = _runtime(tmp_path)
    for name in (
        "install_hello_ipa", "launch_hello_ipa", "tap_button_ten_times",
        "import_notes_ipa_and_save_text", "shutdown_and_reboot_from_disk",
    ):
        observation = getattr(runtime, name)()
        assert isinstance(observation, StepObservation)
        assert observation.ok is False, name
        assert observation.reason, name
        assert observation.reason != "no_qemu_runtime", name


# ---------------------------------------------------------------------------
# case 1
# ---------------------------------------------------------------------------

def test_case1_records_the_argv_the_pid_and_the_kernel_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 1 is "keep the actual configuration and version evidence".

    So the observation has to carry the argv the guest was actually started with,
    the child's pid, and the kernel's digest -- not the paths the caller passed in.
    """
    runtime = _monkey_runtime(
        tmp_path, monkeypatch, guest_log=b"Serial keyboard started\n")
    _stub_stage(runtime, monkeypatch, tmp_path)
    monkeypatch.setattr(gr, "_sha256_of", lambda path: "a" * 64)

    observation = runtime.start_new_qemu_process()

    assert observation.ok is True, observation.reason
    assert observation.facts["pid"] == 4242, observation.facts
    assert observation.facts["kernel_sha256"] == "a" * 64, observation.facts
    assert isinstance(observation.facts["argv"], list), observation.facts
    assert observation.facts["qmp_port"], observation.facts
    # The command file is the evidence a reader can diff against a later run.
    assert "command_file" in observation.facts, observation.facts


def test_case1_refuses_when_the_guest_never_reports_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guest that never comes up is a failure, not a pass with a short window."""
    runtime = _monkey_runtime(tmp_path, monkeypatch, guest_log=b"")
    _stub_stage(runtime, monkeypatch, tmp_path)
    monkeypatch.setattr(gr, "_sha256_of", lambda path: "b" * 64)

    with pytest.raises(UniPadError) as caught:
        runtime.start_new_qemu_process()

    assert caught.value.code == ErrorCode.QEMU_TIMEOUT, caught.value
    assert "never reported" in caught.value.message, caught.value.message


# ---------------------------------------------------------------------------
# case 2
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, pid: int, state: str, name: str) -> None:
        self.pid = pid
        self.state = state
        self.name = name


def test_case2_passes_when_springboard_is_up_and_the_picture_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The criterion is ``framebuffer_before != framebuffer_after``, and both halves
    have to be present: a SpringBoard pid with an unchanged picture is a FAIL."""
    runtime = _monkey_runtime(
        tmp_path, monkeypatch,
        processes=(_FakeProc(1, "run", "launchd"),
                   _FakeProc(27, "run", "SpringBoard")),
        guest_log=b"Serial keyboard started\n",
    )
    _stub_stage(runtime, monkeypatch, tmp_path)
    monkeypatch.setattr(gr, "_sha256_of", lambda path: "c" * 64)
    monkeypatch.setattr(runtime, "screendump", lambda target: "before-hash")

    class _ChangingScreendump:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, target: Path) -> str:
            self.calls += 1
            return "before-hash" if self.calls == 1 else "after-hash"

    monkeypatch.setattr(runtime, "screendump", _ChangingScreendump())

    observation = runtime.reach_springboard()

    assert observation.ok is True, observation.reason
    assert observation.facts["springboard_pid"] == 27, observation.facts
    assert observation.facts["springboard_state"] == "run", observation.facts
    assert observation.facts["framebuffer_before"] == "before-hash", observation.facts
    assert observation.facts["framebuffer_after"] == "after-hash", observation.facts
    # The key is home, and that is a measured fact rather than a preference.
    assert observation.facts["key_issued"] == ["home"], observation.facts


def test_case2_fails_when_the_framebuffer_does_not_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the guest's actual state today, and the harness must say so.

    SpringBoard is up, the Home key reaches the guest's VirtioInput driver
    (``native_virtio_input_keys`` 0 -> 2, measured), and the framebuffer is still
    byte-identical because the display driver never started.  A runtime that
    reported a pass here would be reporting a guest nobody observed.
    """
    runtime = _monkey_runtime(
        tmp_path, monkeypatch,
        processes=(_FakeProc(27, "run", "SpringBoard"),),
        guest_log=b"Serial keyboard started\n",
    )
    _stub_stage(runtime, monkeypatch, tmp_path)
    monkeypatch.setattr(gr, "_sha256_of", lambda path: "d" * 64)
    monkeypatch.setattr(runtime, "screendump", lambda target: "same-hash")

    observation = runtime.reach_springboard()

    assert observation.ok is False, observation
    assert observation.reason == "mouse_input_did_not_change_the_framebuffer", observation
    assert observation.facts["springboard_pid"] == 27, observation.facts
    assert observation.facts["framebuffer_before"] == "same-hash", observation.facts
    assert observation.facts["framebuffer_after"] == "same-hash", observation.facts


def test_case2_fails_when_springboard_is_not_in_the_task_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No SpringBoard is a different failure from an unchanged picture."""
    runtime = _monkey_runtime(
        tmp_path, monkeypatch,
        processes=(_FakeProc(1, "run", "launchd"),),
        guest_log=b"Serial keyboard started\n",
    )
    _stub_stage(runtime, monkeypatch, tmp_path)
    monkeypatch.setattr(gr, "_sha256_of", lambda path: "e" * 64)
    monkeypatch.setattr(runtime, "screendump", lambda target: "hash")

    observation = runtime.reach_springboard()

    assert observation.ok is False, observation
    assert observation.reason == "springboard_not_in_the_guest_task_list", observation
    assert observation.facts["springboard_pid"] is None, observation.facts


# ---------------------------------------------------------------------------
# case 3
# ---------------------------------------------------------------------------

def test_case3_refuses_to_guess_installed_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``installed_before`` asks the guest, and the guest cannot be asked yet.

    Case 3's probe turns a truthy ``installed_before`` into
    ``case3_package_was_already_installed``.  So a runtime that returned ``False``
    without asking would be manufacturing the pass the PRD wants.
    """
    package = tmp_path / "HelloUniPad.ipa"
    build_ipa(package)
    runtime = _runtime(tmp_path, samples={"HelloUniPad.ipa": package})

    observation = runtime.import_hello_ipa()

    assert observation.ok is False, observation
    assert observation.reason == "cannot_ask_the_guest_what_is_already_installed", observation
    # The digest it *did* establish is still reported, so the failure is useful.
    assert observation.facts["package_sha256"] == sha256_file(package), observation.facts
    assert observation.facts["bundle_id"], observation.facts


def test_case3_passes_when_the_guest_says_nothing_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same guard: an empty installed set is a real answer."""
    package = tmp_path / "HelloUniPad.ipa"
    build_ipa(package)
    runtime = _runtime(tmp_path, samples={"HelloUniPad.ipa": package})
    monkeypatch.setattr(runtime, "_installed_bundle_ids", lambda: set())

    observation = runtime.import_hello_ipa()

    assert observation.ok is True, observation.reason
    assert observation.facts["installed_before"] is False, observation.facts
    assert observation.facts["package_sha256"] == open_ipa(package).sha256, observation.facts


def test_case3_fails_when_the_package_is_absent(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, samples={})
    observation = runtime.import_hello_ipa()
    assert observation.ok is False, observation
    assert observation.reason == "hello_unipad_ipa_sample_absent", observation


# ---------------------------------------------------------------------------
# case 10
# ---------------------------------------------------------------------------

def _unsupported_package(tmp_path: Path) -> Path:
    path = tmp_path / "UnsupportedPlatform.ipa"
    build_ipa(path, corrupt="simulator")
    return path


def test_case10_rejects_a_simulator_package_with_a_precheck_code(
    tmp_path: Path,
) -> None:
    """Case 10 is answerable from the host: the precheck decides from the bytes.

    The probe accepts exactly three codes, and the simulator fixture produces
    ``unsupported_arch``.  The reason slug must be non-empty too.
    """
    package = _unsupported_package(tmp_path)
    runtime = _runtime(tmp_path, samples={"UnsupportedPlatform.ipa": package})

    observation = runtime.import_unsupported_platform_package()

    assert observation.ok is False, observation  # no ledger, so recoverability is unproven
    facts = observation.facts
    assert facts["error_code"] in (
        ErrorCode.UNSUPPORTED_PLATFORM, ErrorCode.UNSUPPORTED_ARCH,
        ErrorCode.INVALID_PACKAGE), facts
    assert facts["reason"], facts
    assert facts["bytes_transferred"] == 0, facts
    assert facts["install_attempted"] is False, facts


def test_case10_reports_recoverable_only_when_the_ledger_proves_it(
    tmp_path: Path,
) -> None:
    """With a ledger, the installed set is read back and compared.

    The PRD asks that the existing system and apps stay recoverable.  Reading the
    ledger before and after is what turns that from an assertion into a
    measurement -- and a host with no ledger must not get the same ``True``.
    """
    package = _unsupported_package(tmp_path)
    ledger = tmp_path / "install-ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    runtime = _runtime(
        tmp_path, samples={"UnsupportedPlatform.ipa": package}, ledger_path=ledger)

    observation = runtime.import_unsupported_platform_package()

    assert observation.ok is True, observation.reason
    assert observation.facts["previously_installed_recoverable"] is True, observation.facts
    assert observation.facts["ledger_checked"] is True, observation.facts
    assert observation.facts["ledger_unchanged"] is True, observation.facts


def test_case10_names_the_samples_it_looked_for(
    tmp_path: Path,
) -> None:
    """A host that registered the package under another spelling gets told which."""
    runtime = _runtime(tmp_path, samples={"SomeOtherName.ipa": tmp_path / "x.ipa"})

    observation = runtime.import_unsupported_platform_package()

    assert observation.ok is False, observation
    assert observation.reason == "no_unsupported_platform_package_registered", observation
    assert observation.facts["looked_for"] == sorted(UNSUPPORTED_SAMPLE_NAMES), observation.facts
    assert "SomeOtherName.ipa" in observation.facts["registered"], observation.facts


def test_case10_fails_when_the_registered_package_is_not_rejected(
    tmp_path: Path,
) -> None:
    """A good package registered as the bad one is a harness error, not a pass."""
    package = tmp_path / "UnsupportedPlatform.ipa"
    build_ipa(package)  # a valid device package
    runtime = _runtime(tmp_path, samples={"UnsupportedPlatform.ipa": package})

    observation = runtime.import_unsupported_platform_package()

    assert observation.ok is False, observation
    assert observation.reason == "the_registered_package_was_not_rejected", observation


# ---------------------------------------------------------------------------
# case 9
# ---------------------------------------------------------------------------

#: The three stages ``COLD_BOOT_STAGES`` names, as a cycle's result.
SPRINGBOARD_REACHED = "springboard_reached"
KERNEL_UP = "kernel_up_no_springboard"
NEVER_REPORTED = "guest_never_reported"


def _case9_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *stages: str,
) -> tuple[RealGuestRuntime, list[str]]:
    """A runtime whose cold boots land on ``stages``, in order.

    ``start()`` is the seam: it is what a real run waits on, and it is what
    raises when the guest never reports itself.  ``processes`` answers from the
    stage the last ``start()`` reached, so the two cannot disagree about which
    cycle they are in -- which is the mistake that would let a test pass while
    the runtime read a stale process list.
    """
    runtime = _runtime(tmp_path)
    staged: list[str] = []
    seen: list[str] = []

    class _Popen:
        def __call__(self, argv: list[str], **_: object) -> FakeProcess:
            return FakeProcess()

    monkeypatch.setattr(runtime, "_popen", _Popen())
    monkeypatch.setattr(runtime, "_sleep", lambda _: None)

    def _stage(base: Path | None = None) -> Path:
        run = (base if base is not None else tmp_path / "run") / "guest"
        run.mkdir(parents=True, exist_ok=True)
        staged.append(str(run))
        return run

    monkeypatch.setattr(runtime, "_stage", _stage)
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))

    def _start() -> None:
        stage = stages[len(seen)] if len(seen) < len(stages) else NEVER_REPORTED
        seen.append(stage)
        if stage == NEVER_REPORTED:
            raise UniPadError(
                ErrorCode.QEMU_TIMEOUT, "the guest never reported its serial keyboard")
        # ``start()`` owns the child, and case 9 reads the pid back off it -- so a
        # fake that returns without setting one would let the test pass while the
        # real runtime reported ``pid: None``.
        runtime._process = FakeProcess()

    monkeypatch.setattr(runtime, "start", _start)

    def _processes() -> tuple[Any, ...]:
        if seen and seen[-1] == SPRINGBOARD_REACHED:
            return (_FakeProc(1, "run", "launchd"), _FakeProc(27, "run", "SpringBoard"))
        return ()

    monkeypatch.setattr(runtime, "processes", _processes)
    return runtime, staged


def test_case9_passes_when_all_three_cold_boots_reach_springboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three identical cold boots are a pass, and each one is timed."""
    runtime, staged = _case9_runtime(
        tmp_path, monkeypatch, SPRINGBOARD_REACHED, SPRINGBOARD_REACHED,
        SPRINGBOARD_REACHED)

    observation = runtime.repeat_cold_boot_three_times()

    assert observation.ok is True, observation.reason
    cycles = observation.facts["cycles"]
    assert [c["cycle"] for c in cycles] == [1, 2, 3]
    for cycle in cycles:
        assert isinstance(cycle["duration_s"], (int, float)), cycle
        assert cycle["result"] == SPRINGBOARD_REACHED, cycle
        assert cycle["pid"] == 4242, cycle
    assert len(set(observation.facts["evidence_paths"])) == 3
    assert observation.facts["same_stage"] is True


def test_case9_fails_when_the_three_boots_reach_different_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated cold boot means the *same* boot.

    Two boots that reach SpringBoard and one that does not is a divergence, and
    the reason has to say so rather than averaging the three into a pass.
    """
    runtime, _ = _case9_runtime(
        tmp_path, monkeypatch, SPRINGBOARD_REACHED, KERNEL_UP, SPRINGBOARD_REACHED)

    observation = runtime.repeat_cold_boot_three_times()

    assert observation.ok is False, observation
    assert observation.reason == (
        "the_three_cold_boots_did_not_reach_the_same_stage"), observation
    assert observation.facts["same_stage"] is False
    assert observation.facts["stages"] == [
        SPRINGBOARD_REACHED, KERNEL_UP, SPRINGBOARD_REACHED]


def test_case9_fails_when_no_boot_reaches_springboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three identical *broken* boots are reproducible, but they are not a pass.

    This is the guard against the case passing on a guest that never comes up:
    ``same_stage`` is True here, so only the stage check keeps it red.
    """
    runtime, _ = _case9_runtime(tmp_path, monkeypatch, KERNEL_UP, KERNEL_UP, KERNEL_UP)

    observation = runtime.repeat_cold_boot_three_times()

    assert observation.ok is False, observation
    assert observation.reason == "no_cold_boot_reached_springboard:" + KERNEL_UP, (
        observation)
    assert observation.facts["same_stage"] is True


def test_case9_stages_each_cycle_into_its_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold boot must not read the previous cycle's serial log.

    Reusing one run directory would leave the previous ``serial.log`` in place,
    so ``start()`` would find the previous cycle's marker and return at once and
    every cycle after the first would report a millisecond boot.  The distinct
    staging paths are what make the three timings mean anything, and they are
    also what stops a failing cycle from overwriting the one before it.
    """
    runtime, staged = _case9_runtime(
        tmp_path, monkeypatch, SPRINGBOARD_REACHED, SPRINGBOARD_REACHED,
        SPRINGBOARD_REACHED)

    observation = runtime.repeat_cold_boot_three_times()

    assert len(staged) == 3, staged
    assert len(set(staged)) == 3, staged
    assert all("case9-cycle-" in path for path in staged), staged
    assert set(observation.facts["evidence_paths"]) == set(staged)


def test_case9_records_a_cycle_that_never_reported_the_guest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cycle that never came up is that cycle's *result*, not an exception.

    PRD 5.2 case 9 says a failing test must keep its evidence and not overwrite
    the last failure, so the second cycle's timeout has to land in the cycle
    list with its own directory -- and the first cycle's directory has to still
    be there afterwards.
    """
    runtime, staged = _case9_runtime(
        tmp_path, monkeypatch, SPRINGBOARD_REACHED, NEVER_REPORTED,
        SPRINGBOARD_REACHED)

    observation = runtime.repeat_cold_boot_three_times()

    assert observation.ok is False, observation
    cycles = observation.facts["cycles"]
    assert cycles[1]["result"] == NEVER_REPORTED, cycles
    assert cycles[1]["error_code"] == ErrorCode.QEMU_TIMEOUT, cycles
    assert "never reported" in str(cycles[1]["error"]), cycles
    assert cycles[0]["result"] == SPRINGBOARD_REACHED, cycles
    for path in staged:
        assert Path(path).is_dir(), path


def test_case9_never_claims_an_operable_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SpringBoard pid is not the §5.3 "operable desktop" target.

    Case 2 owns that claim -- the picture has to have changed -- and it is still
    failing on the framebuffer.  Recording the flag as ``False`` keeps a passing
    case 9 from being read as evidence the desktop works.
    """
    runtime, _ = _case9_runtime(
        tmp_path, monkeypatch, SPRINGBOARD_REACHED, SPRINGBOARD_REACHED,
        SPRINGBOARD_REACHED)

    observation = runtime.repeat_cold_boot_three_times()

    assert observation.ok is True, observation.reason
    assert observation.facts["operable_desktop"] is False, observation.facts


# ---------------------------------------------------------------------------
# the manifest path
# ---------------------------------------------------------------------------

def test_runtime_from_manifest_reads_the_manifest_own_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime boots the bytes the manifest describes, not whatever sits there.

    Every role the manifest names is read through its own ``path`` entry, so a
    directory whose files were renamed cannot silently produce a different boot.
    """
    from unipad.acceptance import guest_runtime as gr

    manifest_dir = tmp_path / "delivery"
    manifest_dir.mkdir()
    for role, name in (
        ("kernel", "kernel.macho"), ("devicetree", "kernel.macho.adt"),
        ("trust_cache", "original-static-system.bundle"),
        ("data_seed", "owner-data-1g.dbr"),
        ("os_root_image", "044-76040-038.dmg"),
        ("systemos_image", "044-76034-038.dmg"),
    ):
        (manifest_dir / name).write_bytes(b"\0" * 8)
    (manifest_dir / "044-76040-038.raw").write_bytes(b"\0" * 8)
    (manifest_dir / "044-76034-038.raw").write_bytes(b"\0" * 8)
    (manifest_dir / "material-manifest.json").write_text(json.dumps({
        "artifacts": {
            "kernel": {"path": "kernel.macho"},
            "devicetree": {"path": "kernel.macho.adt"},
            "trust_cache": {"path": "original-static-system.bundle"},
            "data_seed": {"path": "owner-data-1g.dbr"},
            "os_root_image": {"path": "044-76040-038.dmg"},
            "systemos_image": {"path": "044-76034-038.dmg"},
        }
    }), encoding="utf-8")
    for name in ("fw.bin", "DarwinBoot.efi", "UniXNU.efi"):
        (tmp_path / name).write_bytes(b"\0" * 8)

    runtime = gr.runtime_from_manifest(
        manifest_dir,
        device_dir=tmp_path / "device",
        qemu=tmp_path / "qemu.exe",
        darwinboot=tmp_path / "DarwinBoot.efi",
        unixnu=tmp_path / "UniXNU.efi",
        firmware=tmp_path / "fw.bin",
        workdir=tmp_path / "run",
    )

    assert runtime.inputs.kernel == manifest_dir / "kernel.macho", runtime.inputs
    assert runtime.inputs.devicetree == manifest_dir / "kernel.macho.adt", runtime.inputs
    assert runtime.inputs.trust_cache == manifest_dir / "original-static-system.bundle"
    assert runtime.inputs.data_ramdisk == manifest_dir / "owner-data-1g.dbr"
    assert runtime.root_image == manifest_dir / "044-76040-038.raw", runtime.root_image
    assert runtime.systemos_image == manifest_dir / "044-76034-038.raw"


def test_runtime_from_manifest_refuses_a_compressed_volume(
    tmp_path: Path,
) -> None:
    """A ``.dmg`` with no ``.raw`` beside it is refused, not attached raw.

    Attaching a UDIF container as a raw disk gives the guest a compressed
    container with no GPT, and the boot blocks on ``Waiting on IOMedia`` -- which
    looks exactly like a run with no volume at all.
    """
    from unipad.acceptance import guest_runtime as gr

    manifest_dir = tmp_path / "delivery"
    manifest_dir.mkdir()
    for name in ("kernel.macho", "kernel.macho.adt", "044-76040-038.dmg"):
        (manifest_dir / name).write_bytes(b"\0" * 8)
    (manifest_dir / "material-manifest.json").write_text(json.dumps({
        "artifacts": {
            "kernel": {"path": "kernel.macho"},
            "devicetree": {"path": "kernel.macho.adt"},
            "os_root_image": {"path": "044-76040-038.dmg"},
        }
    }), encoding="utf-8")
    for name in ("fw.bin", "DarwinBoot.efi", "UniXNU.efi"):
        (tmp_path / name).write_bytes(b"\0" * 8)

    with pytest.raises(UniPadError) as caught:
        gr.runtime_from_manifest(
            manifest_dir,
            device_dir=tmp_path / "device",
            qemu=tmp_path / "qemu.exe",
            darwinboot=tmp_path / "DarwinBoot.efi",
            unixnu=tmp_path / "UniXNU.efi",
            firmware=tmp_path / "fw.bin",
            workdir=tmp_path / "run",
        )

    assert "compressed" in caught.value.message, caught.value
    assert caught.value.details.get("expected", "").endswith(".raw"), caught.value.details


def test_runtime_from_manifest_refuses_a_missing_role(
    tmp_path: Path,
) -> None:
    from unipad.acceptance import guest_runtime as gr

    manifest_dir = tmp_path / "delivery"
    manifest_dir.mkdir()
    (manifest_dir / "material-manifest.json").write_text(
        json.dumps({"artifacts": {}}), encoding="utf-8")
    for name in ("fw.bin", "DarwinBoot.efi", "UniXNU.efi"):
        (tmp_path / name).write_bytes(b"\0" * 8)

    with pytest.raises(UniPadError) as caught:
        gr.runtime_from_manifest(
            manifest_dir,
            device_dir=tmp_path / "device",
            qemu=tmp_path / "qemu.exe",
            darwinboot=tmp_path / "DarwinBoot.efi",
            unixnu=tmp_path / "UniXNU.efi",
            firmware=tmp_path / "fw.bin",
            workdir=tmp_path / "run",
        )

    assert caught.value.details.get("role") == "kernel", caught.value.details


def test_the_runtime_hands_the_probe_two_paths_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second production call site, which had the same defect.

    ``unixnu_boot._watch`` is not the only caller of ``_guest_log_probe``: the
    runtime reads the guest's ring through it as well, and it passed one path for
    both the raw memory QEMU writes and the evidence file it then writes.  Fixing
    the probe's contract fixes neither caller -- the contract was correct the
    whole time the bug was live, because the bug was at both call sites.

    Reverting this one to ``scratch=log`` leaves the whole suite green without
    this test, and the consequence is the same as it was in ``_watch``: a probe
    that fails after its read leaves the guest's unscrubbed RAM at the path a
    human is pointed at.
    """
    runtime = _runtime(tmp_path)
    run = tmp_path / "run" / "guest"
    run.mkdir(parents=True, exist_ok=True)
    runtime._run = run
    monkeypatch.setattr(runtime, "_stage", lambda: run)
    seen: dict[str, object] = {}

    def spy(port: int, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"ready": False, "reason": "stubbed"}

    monkeypatch.setattr(ub, "_guest_log_probe", spy)
    monkeypatch.setattr(ub, "_kernel_image", lambda kernel, digest: (object(), None))
    runtime._image = object()

    log = run / "guest-kernel.log"
    assert runtime._guest_log(run / "serial.log", log) == b""

    scratch = Path(str(seen["scratch"]))
    assert "log" in seen, (
        "the runtime handed the probe one path for both the raw guest memory and "
        f"the evidence file: {sorted(seen)}"
    )
    assert Path(str(seen["log"])) == log, seen
    assert scratch != log, (
        f"the raw guest memory and the evidence file are the same path: {scratch}"
    )
    assert "memory" in scratch.name, (
        f"the raw-memory scratch no longer says what it holds: {scratch.name}"
    )


# ---------------------------------------------------------------------------
# the manifest's own digests
# ---------------------------------------------------------------------------

def _manifest_dir(tmp_path: Path, *, seed_digest: object = None) -> Path:
    """A delivery directory whose ``data_seed`` entry carries ``sha256``."""
    manifest_dir = tmp_path / "delivery"
    manifest_dir.mkdir()
    for name in ("kernel.macho", "kernel.macho.adt", "original-static-system.bundle",
                 "owner-data-1g.dbr", "044-76040-038.dmg", "044-76034-038.dmg"):
        (manifest_dir / name).write_bytes(b"\0" * 8)
    (manifest_dir / "044-76040-038.raw").write_bytes(b"\0" * 8)
    (manifest_dir / "044-76034-038.raw").write_bytes(b"\0" * 8)
    entry: dict[str, object] = {"path": "owner-data-1g.dbr"}
    if seed_digest is not None:
        entry["sha256"] = seed_digest
    (manifest_dir / "material-manifest.json").write_text(json.dumps({
        "artifacts": {
            "kernel": {"path": "kernel.macho"},
            "devicetree": {"path": "kernel.macho.adt"},
            "trust_cache": {"path": "original-static-system.bundle"},
            "data_seed": entry,
            "os_root_image": {"path": "044-76040-038.dmg"},
            "systemos_image": {"path": "044-76034-038.dmg"},
        }
    }), encoding="utf-8")
    for name in ("fw.bin", "DarwinBoot.efi", "UniXNU.efi"):
        (tmp_path / name).write_bytes(b"\0" * 8)
    return manifest_dir


def _delivered_runtime(tmp_path: Path, manifest_dir: Path):
    from unipad.acceptance import guest_runtime as gr
    return gr.runtime_from_manifest(
        manifest_dir,
        device_dir=tmp_path / "device",
        qemu=tmp_path / "qemu.exe",
        darwinboot=tmp_path / "DarwinBoot.efi",
        unixnu=tmp_path / "UniXNU.efi",
        firmware=tmp_path / "fw.bin",
        workdir=tmp_path / "run",
    )


def test_the_manifest_declared_seed_digest_is_checked(tmp_path: Path) -> None:
    """The delivery's own record of what it shipped, read and not ignored.

    ``runtime_from_manifest`` reads each role's ``path`` out of the manifest and
    then boots whatever is at it -- so a directory whose seed had been replaced
    produced a boot the manifest never described.  The ``sha256`` sits in the
    same entry as the ``path``; reading one and not the other is the gap.
    """
    manifest_dir = _manifest_dir(tmp_path, seed_digest="0" * 64)
    with pytest.raises(UniPadError) as caught:
        _delivered_runtime(tmp_path, manifest_dir)
    assert caught.value.code is ErrorCode.VERIFICATION_FAILED, caught.value
    assert "data_seed" in str(caught.value.details.get("role")), caught.value.details
    assert caught.value.details["declared"] == "0" * 64, caught.value.details


def test_the_manifest_declared_seed_digest_that_matches_is_accepted(tmp_path: Path) -> None:
    """The green control: a delivery that describes itself honestly is booted."""
    manifest_dir = _manifest_dir(tmp_path)
    digest = hashlib.sha256((manifest_dir / "owner-data-1g.dbr").read_bytes()).hexdigest()
    (manifest_dir / "material-manifest.json").write_text(json.dumps({
        "artifacts": {
            "kernel": {"path": "kernel.macho"},
            "devicetree": {"path": "kernel.macho.adt"},
            "trust_cache": {"path": "original-static-system.bundle"},
            "data_seed": {"path": "owner-data-1g.dbr", "sha256": digest},
            "os_root_image": {"path": "044-76040-038.dmg"},
            "systemos_image": {"path": "044-76034-038.dmg"},
        }
    }), encoding="utf-8")
    assert _delivered_runtime(tmp_path, manifest_dir).inputs.data_ramdisk == manifest_dir / "owner-data-1g.dbr"


def test_a_role_the_manifest_declares_without_a_digest_is_passed_through(tmp_path: Path) -> None:
    """Not every delivery carries one, and a refusal there would be a different bug.

    The check is "when the manifest declares a digest, honour it" -- not "refuse
    a manifest that omits one".
    """
    runtime = _delivered_runtime(tmp_path, _manifest_dir(tmp_path))
    assert runtime.inputs.data_ramdisk is not None
