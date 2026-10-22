"""IPA MVP end-to-end simulation (PRD 5.2 steps 3-10, 5.1, 9.1, 10; FR05-FR07/FR10).

This is the code-stage stand-in for the PRD 5.2 acceptance run.  It walks the
same ten steps, in order, against the **real** host chain
(:class:`unipad.ipa.installer.IpaInstaller`, :class:`unipad.ipa.transfer`,
:class:`unipad.ipa.precheck`, :class:`unipad.guest.client.GuestClient`) driving
the **fake** guest service (:class:`unipad.guest.fake.FakeGuestService`) and,
for input, the real host input sink against the fake QEMU input journal.

What is *not* here, on purpose
------------------------------
There is no iPadOS, no SpringBoard and no install service on this machine, so
nothing in this file may be read as "an IPA runs on Windows".  What it does
prove is that the host side of every PRD 5.2 step produces the evidence that
step demands, and that the verifier refuses to pass when it does not
(docs/testing-strategy.md D2/D3).  Field status stays ``UNVERIFIED``.

Load-bearing rules pinned here
------------------------------
* PRD 9.1 -- "文件传输成功不能返回 ``installed``": a channel that only ever
  transfers the package can never yield ``installed``/``registered``;
* PRD 10 -- every injected failure mode comes back as ``stage='failed'`` with
  its stable taxonomy code and leaves nothing registered;
* PRD 5.2 step 8/9 -- a *new* guest instance loading the same data directory
  (the fresh-QEMU-process equivalent) sees both apps and every random marker;
* PRD 5.3 -- each cold-boot cycle uses a **new random** marker;
* PRD FR10 -- a corrupted Data image is restored from a hash-verified
  checkpoint, and never restored in place (PRD 7.2);
* PRD 9.1 -- ``trust_mode`` is whatever the guest reported; this file never
  asserts that it equals any particular Apple trust mechanism.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.client import GuestClient
from unipad.guest.fake import (
    FakeGuestService,
    FakeTransport,
    derive_challenge_value,
)
from unipad.host.images import ImageManager
from unipad.host.input import QmpInputSink
from unipad.host.logs import EvidenceBundle
from unipad.host.qmp import QmpClient
from unipad.host.inventory import collect_host_inventory, write_host_inventory
from unipad.ipa.installer import EVENTS_NAME, RECORD_NAME, IpaInstaller
from unipad.ipa.precheck import run_precheck
from unipad.ipa.transfer import TransferReceipt
from unipad.models import (
    ArtifactEntry,
    ArtifactManifest,
    ExperimentRecord,
    MachineConfig,
    read_json,
    write_json,
)
from unipad.controller.verifier import Verifier, load_facts
from tests.fakes import fake_qemu as fq
from tests.fixtures.ipa_builder import build_ipa, hello_ipa, notes_ipa

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE_CONFIG_PATH = REPO_ROOT / "config" / "machine.json"

HELLO_BUNDLE_ID = "com.unipad.hello"
NOTES_BUNDLE_ID = "com.unipad.notes"

#: Guest-side registry keys.  :class:`unipad.guest.protocol.InstallRequest` is a
#: frozen five-field contract, so the JSON-Lines message path cannot carry the
#: bundle id and the fake guest keys its registry by the package file name
#: (``FakeGuestService._bundle_id_for``, pinned by
#: ``tests/unit/test_guest_fake.py::test_install_over_the_message_api_uses_the_package_name``).
#: The **authoritative** bundle id is the one the host's ``unipad.ipa`` layer
#: reads out of ``Info.plist`` and reports in ``InstallResult.bundle_id``.
#: These tests assert both and link them through the package digest rather than
#: pretending the two keys are the same string.
HELLO_GUEST_KEY = "HelloUniPad"
NOTES_GUEST_KEY = "UniPadNotes"

#: ``signature_mode`` values this fake guest's trust policy accepts.  The host
#: declares what the Mach-O *shows* (``code-signature-present`` for the
#: fixtures); the guest decides what it trusts.  Both sides are recorded.
ACCEPTED_SIGNATURE_MODES = ("code-signature-present", "unsigned", "self")

#: Every PRD 10 install-chain failure point -> the stable code it must report.
FAULT_MATRIX: dict[str, str] = {
    "structure": ErrorCode.INVALID_PACKAGE,
    "transfer": ErrorCode.TRANSFER_FAILED,
    "signature": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "install_service": ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
    "registration": ErrorCode.REGISTRATION_FAILED,
    "dependency": ErrorCode.MISSING_DEPENDENCY,
    "dyld": ErrorCode.DYLD_FAILED,
}

#: How long the input-sink test waits for the fake QEMU child to be reachable.
QMP_WAIT_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# the guest-side byte channel (PRD 10 "主客体传输")
# ---------------------------------------------------------------------------


class GuestByteChannel:
    """Guest side of the package byte channel.

    ``unipad.ipa.transfer`` only needs a ``send_package``; the real guest-side
    receiver (virtio-serial link, shared folder, controlled helper) does not
    exist yet, so this stands in for it *on the guest side of the boundary*:
    the bytes land in :meth:`FakeGuestService.stage_package`, which validates
    the declared size and digest exactly as a real receiver must, and the
    receiver's own digest is returned so the host-side verification in
    :class:`~unipad.ipa.transfer.HostSideTransfer` runs for real.
    """

    def __init__(
        self,
        service: FakeGuestService,
        *,
        declared_min_os: str = "17.0",
        signature_mode: str = "code-signature-present",
    ) -> None:
        self.service = service
        self.declared_min_os = declared_min_os
        self.signature_mode = signature_mode
        self.delivered: list[dict] = []

    def send_package(self, name: str, data: bytes) -> TransferReceipt:
        """Deliver the package bytes into the guest staging area."""
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        staged = self.service.stage_package(
            payload,
            {
                "package_name": name,
                "package_sha256": digest,
                "size_bytes": len(payload),
                "declared_min_os": self.declared_min_os,
                "signature_mode": self.signature_mode,
            },
        )
        self.delivered.append(
            {
                "name": name,
                "sha256": digest,
                "size_bytes": len(payload),
                "staged_sha256": staged["staged_sha256"],
            }
        )
        return TransferReceipt(
            name=name,
            sha256=digest,
            size_bytes=len(payload),
            verified=True,
            chunk_count=1,
            chunk_size=len(payload),
            detail="fake-guest-byte-channel",
        )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def display() -> dict:
    """The guest display block of ``config/machine.json`` (M12 geometry)."""
    return MachineConfig.from_dict(read_json(MACHINE_CONFIG_PATH)).display


@pytest.fixture
def stack(tmp_path):
    """A fake guest, its host client and the installer wired to both."""
    service = FakeGuestService(
        trust_policy={"accept_modes": list(ACCEPTED_SIGNATURE_MODES)}
    )
    run_dir = tmp_path / "runs" / "exp-ipa-mvp"
    byte_channel = GuestByteChannel(service)
    client = GuestClient(FakeTransport(service), attempts=1, backoff_s=0.0)
    installer = IpaInstaller(client, run_dir=run_dir, byte_channel=byte_channel)
    return SimpleNamespace(
        service=service,
        client=client,
        installer=installer,
        byte_channel=byte_channel,
        run_dir=run_dir,
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def desktop_session(stack, tmp_path):
    """PRD 5.2 steps 3-7 replayed: both apps installed, marker saved, persisted."""
    hello = stack.installer.install(hello_ipa(tmp_path / "import" / "HelloUniPad.ipa"))
    notes = stack.installer.install(notes_ipa(tmp_path / "import" / "UniPadNotes.ipa"))
    assert hello.stage == "registered" and notes.stage == "registered"

    marker = uuid.uuid4().hex
    stack.service.set_data_marker(NOTES_GUEST_KEY, "note", marker)
    persist = stack.service.persist(stack.data_dir)
    return SimpleNamespace(
        hello=hello,
        notes=notes,
        marker=marker,
        persist=persist,
        installed_before=set(stack.service.installed),
    )


def _import_dir(tmp_path: Path) -> Path:
    """The directory a freshly imported IPA lands in (created on demand)."""
    target = tmp_path / "import"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _stage_sequence(run_dir: Path) -> list[str]:
    """The stage names written to the append-only install journal."""
    events = (run_dir / EVENTS_NAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["stage"] for line in events if line.strip()]


def _fresh_guest(data_dir: Path) -> FakeGuestService:
    """A brand-new guest instance loading the same Data volume.

    This is the M9b "new QEMU process reads the data back" equivalent: nothing
    is shared with the previous instance except the persisted state on disk.
    """
    guest = FakeGuestService()
    guest.load(data_dir)
    return guest


# ---------------------------------------------------------------------------
# PRD 5.2 steps 3-4: import, hash, precheck, install, icon
# ---------------------------------------------------------------------------


def test_import_hello_ipa_records_hash_prechecks_and_registers_icon(stack, tmp_path):
    """PRD 5.2 steps 3-4: hash recorded, precheck accepts, icon registered."""
    ipa = hello_ipa(_import_dir(tmp_path) / "HelloUniPad.ipa")
    package_bytes = ipa.read_bytes()
    expected_sha256 = hashlib.sha256(package_bytes).hexdigest()

    # step 3: prove it was not installed before anything else happened
    assert stack.service.status().installed == []

    report = run_precheck(ipa)
    assert report.verdict == "accept"
    assert report.faults == []
    assert report.fault_codes == []
    assert report.has_arm64_slice is True
    assert report.supported_platforms == ["iPhoneOS"]
    assert report.min_os == "17.0"
    assert report.package_sha256 == expected_sha256

    result = stack.installer.install(ipa)

    # step 4: install result with the furthest stage actually completed
    assert result.stage == "registered"
    assert result.ok is True
    assert result.error_code is None
    assert result.error_message is None
    assert result.package_sha256 == expected_sha256      # recorded input hash
    assert result.bundle_id == HELLO_BUNDLE_ID           # real Info.plist bundle id
    assert result.log_path == str(stack.run_dir / RECORD_NAME)

    # the SpringBoard icon really is registered in the guest.  The guest keys
    # its registry by package name over the five-field wire contract (see
    # HELLO_GUEST_KEY); the authoritative bundle id is asserted above on the
    # host's install result, and the two are linked by the package digest.
    entry = stack.service.installed[HELLO_GUEST_KEY]
    assert entry["icon"] is True
    assert entry["package_name"] == "HelloUniPad.ipa"
    assert entry["package_sha256"] == expected_sha256
    assert entry["size_bytes"] == len(package_bytes)
    assert entry["declared_min_os"] == "17.0"
    assert stack.service.containers[HELLO_GUEST_KEY]["markers"] == {}
    assert {
        app["package_sha256"] for app in stack.service.installed.values()
    } == {expected_sha256}

    # every PRD 9.1 stage was walked, in order, and persisted
    stages = _stage_sequence(stack.run_dir)
    assert stages[0] == "received"
    assert stages[-1] == "registered"
    assert stages.index("checked") < stages.index("transferred") < stages.index("registered")
    record = read_json(stack.run_dir / RECORD_NAME)
    assert record["stage"] == "registered"
    assert record["bundle_id"] == HELLO_BUNDLE_ID
    assert record["package_sha256"] == expected_sha256

    # the package bytes really reached the guest, with the host's digest
    assert len(stack.byte_channel.delivered) == 1
    delivery = stack.byte_channel.delivered[0]
    assert delivery["sha256"] == expected_sha256
    assert delivery["size_bytes"] == len(package_bytes)
    assert delivery["staged_sha256"] == expected_sha256

    # the trust decision was *recorded* by the guest, on the declared facts
    trust = stack.service.trust_log[-1]
    assert trust["bundle_id"] == HELLO_GUEST_KEY
    assert trust["accepted"] is True
    assert trust["signature_mode"] == stack.byte_channel.signature_mode
    assert trust["declared_min_os"] == "17.0"
    assert trust["guest_os"] == stack.service.guest_os


# ---------------------------------------------------------------------------
# PRD 5.2 steps 5-6: launch, challenge value, ten taps
# ---------------------------------------------------------------------------


def test_launch_challenge_value_matches_the_guest_derivation(stack, tmp_path):
    """PRD 5.2 step 5: the run's challenge value matches the derivation."""
    stack.installer.install(hello_ipa(_import_dir(tmp_path) / "HelloUniPad.ipa"))
    nonce = "unipad-acceptance-nonce-0001"

    challenge = stack.client.challenge(nonce)

    assert challenge.nonce == nonce                      # attributed to this run
    # the guest answers for the app it registered (keyed by package name over
    # the wire contract); the host's authoritative id is on the install result
    assert challenge.value == derive_challenge_value(HELLO_GUEST_KEY, nonce)
    assert challenge.value == stack.service.expected_challenge(HELLO_GUEST_KEY, nonce)
    assert len(challenge.value) == 64
    assert all(char in "0123456789abcdef" for char in challenge.value)

    logged = stack.service.challenge_log[-1]
    assert logged == {"bundle_id": HELLO_GUEST_KEY, "nonce": nonce, "value": challenge.value}

    # the app is also launchable: M11 precondition, with the guest's own trust
    launch = stack.service.launch(HELLO_GUEST_KEY)
    assert launch["stage"] == "running"
    assert launch["trust_mode"] == stack.service.installed[HELLO_GUEST_KEY]["trust_mode"]


def test_ten_taps_are_recorded_by_the_guest_input_journal(stack, display, tmp_path):
    """PRD 5.2 step 6: ten consecutive taps all reach the guest input channel."""
    stack.installer.install(hello_ipa(_import_dir(tmp_path) / "HelloUniPad.ipa"))

    tap_dir = tmp_path / "qemu-input"
    with fq.fake_qemu_process(workdir=tap_dir, boot_ms=0) as fake:
        with QmpClient(port=fake.port, timeout=QMP_WAIT_TIMEOUT) as qmp:
            sink = QmpInputSink(qmp, display, key_delay=0.0)
            for index in range(10):
                sink.tap(180 + index * 12, 300)

            # the sink's own structured record of what it issued
            taps = [entry for entry in sink.event_log() if entry["action"] == "tap"]
            assert len(taps) == 10
            assert [entry["x"] for entry in taps] == [180 + index * 12 for index in range(10)]

        # the guest journal: every tap is a move + press + release
        journal = fq.read_jsonl(tap_dir / fq.INPUT_EVENTS_FILENAME)
        assert len(journal) == 30, "10 taps must produce 30 input commands"

        delivered = [event for entry in journal for event in entry.get("events", [])]
        presses = [e for e in delivered if e["type"] == "btn" and e["data"]["down"] is True]
        releases = [e for e in delivered if e["type"] == "btn" and e["data"]["down"] is False]
        assert len(presses) == 10
        assert len(releases) == 10
        assert all(e["data"]["button"] == "left" for e in presses + releases)
        assert len([e for e in delivered if e["type"] == "abs"]) == 20


# ---------------------------------------------------------------------------
# PRD 5.2 step 7: notes app, random marker, save and reopen
# ---------------------------------------------------------------------------


def test_notes_app_install_writes_a_marker_and_reads_it_back(stack, tmp_path):
    """PRD 5.2 step 7: a random marker is saved to the container and re-read."""
    stack.installer.install(hello_ipa(_import_dir(tmp_path) / "HelloUniPad.ipa"))
    notes = stack.installer.install(notes_ipa(_import_dir(tmp_path) / "UniPadNotes.ipa"))
    assert notes.stage == "registered"
    assert notes.bundle_id == NOTES_BUNDLE_ID
    assert notes.trust_mode

    marker = uuid.uuid4().hex
    written = stack.service.set_data_marker(NOTES_GUEST_KEY, "note", marker)
    assert written["stored"] is True

    # closing and reopening the app reads the same value back
    assert stack.service.get_data_marker(NOTES_GUEST_KEY, "note") == marker
    assert f"{NOTES_GUEST_KEY}:note" in stack.service.data_markers()

    container = stack.service.containers[NOTES_GUEST_KEY]
    assert container["markers"]["note"] == marker
    assert container["files"][-1] == {
        "name": "note",
        "size_bytes": len(marker.encode("utf-8")),
    }


# ---------------------------------------------------------------------------
# PRD 5.2 step 8: normal shutdown, fresh process, everything still there
# ---------------------------------------------------------------------------


def test_normal_shutdown_and_cold_boot_preserve_apps_and_marker(stack, desktop_session):
    """PRD 5.2 step 8 / M9b: a fresh guest process sees both apps and the marker."""
    persist = desktop_session.persist
    assert persist["installed"] == 2
    assert persist["containers"] == 2
    assert persist["markers"] == 1
    assert persist["schema_version"] == 1
    assert (stack.data_dir / "guest-state.json").is_file()

    # a brand-new instance (the fresh QEMU process equivalent)
    rebooted = _fresh_guest(stack.data_dir)

    installed = {app["bundle_id"] for app in rebooted.status().installed}
    assert installed == {HELLO_GUEST_KEY, NOTES_GUEST_KEY}
    assert all(app["icon"] is True for app in rebooted.status().installed)
    assert rebooted.get_data_marker(NOTES_GUEST_KEY, "note") == desktop_session.marker
    assert f"{NOTES_GUEST_KEY}:note" in rebooted.data_markers()

    # the rebooted guest still reports the trust decision it recorded, and the
    # apps are still launchable
    for guest_key in (HELLO_GUEST_KEY, NOTES_GUEST_KEY):
        entry = rebooted.installed[guest_key]
        assert entry["trust_mode"] == desktop_session.hello.trust_mode
        assert rebooted.launch(guest_key)["stage"] == "running"


# ---------------------------------------------------------------------------
# PRD 5.2 step 9 / 5.3: three cold-boot cycles, a new random marker each time
# ---------------------------------------------------------------------------


def test_three_cold_boot_cycles_retain_a_new_random_marker_each_time(stack, desktop_session):
    """PRD 5.2 step 9 / 5.3: three reboots, three new markers, all retained."""
    durations: list[float] = []
    markers: list[str] = [desktop_session.marker]

    for cycle in range(3):
        started = time.monotonic()
        # a fresh guest process, exactly like step 8
        guest = _fresh_guest(stack.data_dir)
        # whatever the previous boot wrote is still on the volume
        assert guest.get_data_marker(NOTES_GUEST_KEY, "note") == markers[-1]

        marker = uuid.uuid4().hex
        guest.set_data_marker(NOTES_GUEST_KEY, "note", marker)
        guest.set_data_marker(NOTES_GUEST_KEY, f"note-{cycle}", marker)
        guest.persist(stack.data_dir)
        markers.append(marker)
        durations.append(round(time.monotonic() - started, 6))

    # ... and a fourth boot reads all of them back
    final = _fresh_guest(stack.data_dir)
    assert {app["bundle_id"] for app in final.status().installed} == {
        HELLO_GUEST_KEY,
        NOTES_GUEST_KEY,
    }
    assert final.get_data_marker(NOTES_GUEST_KEY, "note") == markers[-1]
    for cycle in range(3):
        assert final.get_data_marker(NOTES_GUEST_KEY, f"note-{cycle}") == markers[cycle + 1]

    # PRD 5.3 asks for the timing and result of each cycle to be recorded;
    # these are simulation timings, never field performance numbers.
    assert len(durations) == 3
    assert all(isinstance(value, float) and value >= 0.0 for value in durations)
    assert len(set(markers)) == 4, "every cycle must use a new random marker"


# ---------------------------------------------------------------------------
# PRD 5.2 step 10: an unsupported platform package
# ---------------------------------------------------------------------------


def test_unsupported_platform_package_is_rejected_and_apps_survive(stack, desktop_session, tmp_path):
    """PRD 5.2 step 10: a Simulator build is refused with a clear reason."""
    delivered_before = len(stack.byte_channel.delivered)

    simulator = build_ipa(
        _import_dir(tmp_path) / "UniPadSimulator.ipa",
        app_name="UniPadSimulator",
        bundle_id="com.unipad.simulator",
        executable="UniPadSimulator",
        corrupt="simulator",
    )
    report = run_precheck(simulator)
    assert report.verdict == "reject"
    assert ErrorCode.UNSUPPORTED_ARCH in report.fault_codes
    assert "simulator_platform" in {fault.reason for fault in report.faults}
    assert report.supported_platforms == ["iPhoneSimulator"]
    assert report.has_arm64_slice is False

    result = stack.installer.install(simulator)

    assert result.stage == "failed"
    assert result.ok is False
    assert result.error_code == ErrorCode.UNSUPPORTED_ARCH
    assert "simulator" in result.error_message.lower()
    assert result.bundle_id == "com.unipad.simulator"    # identified, just refused

    # nothing was transferred for the rejected package (PRD 10 "包预检")
    assert len(stack.byte_channel.delivered) == delivered_before
    assert "UniPadSimulator" not in stack.service.installed

    # ... and the previously installed system is still usable and recoverable
    status = stack.service.status()
    assert {app["bundle_id"] for app in status.installed} == desktop_session.installed_before
    assert stack.service.get_data_marker(NOTES_GUEST_KEY, "note") == desktop_session.marker
    assert stack.service.launch(HELLO_GUEST_KEY)["stage"] == "running"


# ---------------------------------------------------------------------------
# FR10: roll back a corrupted Data image from a verified checkpoint
# ---------------------------------------------------------------------------


def test_rollback_restores_a_corrupted_data_image(stack, desktop_session, tmp_path):
    """PRD FR10 / 7.2: restore proves the checkpoint hash and never writes in place."""
    manager = ImageManager(tmp_path / "artifacts")
    image = tmp_path / "data.img"
    created = manager.create_data_image(image, size_mb=1)
    assert created["size_bytes"] == 1024 * 1024

    # a known-good checkpoint, taken while the guest data was intact
    checkpoint_dir = tmp_path / "artifacts" / "checkpoints" / "cp-known-good"
    backup = manager.backup(image, checkpoint_dir, note="ipa-mvp acceptance checkpoint")
    assert backup["sha256"] == manager.sha256_file(image)
    manifest = read_json(checkpoint_dir / "manifest.json")
    assert manifest["entries"][0]["sha256"] == backup["sha256"]

    # the live image is corrupted
    image.write_bytes(b"corrupted-data-volume" * 64)
    assert manager.sha256_file(image) != backup["sha256"]

    restored = manager.restore_checkpoint(
        checkpoint_dir, target_dir=tmp_path / "artifacts" / "restored" / "cp-known-good"
    )
    assert restored["verified"] is True
    assert restored["skipped"] == []
    assert [entry["name"] for entry in restored["restored"]] == ["data.img"]
    assert restored["restored"][0]["sha256"] == backup["sha256"]
    restored_image = Path(restored["restored"][0]["path"])
    assert restored_image.read_bytes() == (checkpoint_dir / "data.img").read_bytes()

    # the corrupted original is untouched: restore never writes in place
    assert manager.sha256_file(image) != backup["sha256"]

    with pytest.raises(UniPadError) as excinfo:
        manager.restore_checkpoint(
            checkpoint_dir, target_dir=tmp_path / "artifacts" / "restored" / "cp-known-good"
        )
    assert excinfo.value.code == ErrorCode.BLOCKED

    # a checkpoint whose bytes no longer match its own manifest is refused:
    # FR10 only allows restoring *verified* bytes (PRD 7.4: no unlabelled old
    # package).  Without this the digest comparison above proves nothing.
    pristine = tmp_path / "pristine.img"
    pristine.write_bytes(b"known-good-data-volume" * 64)
    tampered_dir = tmp_path / "artifacts" / "checkpoints" / "cp-tampered"
    manager.backup(pristine, tampered_dir, note="tampered checkpoint")
    (tampered_dir / "pristine.img").write_bytes(b"bytes that do not match the manifest digest")
    with pytest.raises(UniPadError) as excinfo:
        manager.restore_checkpoint(
            tampered_dir, target_dir=tmp_path / "artifacts" / "restored" / "cp-tampered"
        )
    assert excinfo.value.code == ErrorCode.RESTORE_FAILED
    assert excinfo.value.details["expected_sha256"] != excinfo.value.details["actual_sha256"]

    # and the guest state that was persisted alongside still loads
    assert _fresh_guest(stack.data_dir).get_data_marker(
        NOTES_GUEST_KEY, "note"
    ) == desktop_session.marker


# ---------------------------------------------------------------------------
# PRD 10 fault matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_point", sorted(FAULT_MATRIX))
def test_injected_fault_reports_its_prd10_code_and_registers_nothing(
    failure_point, tmp_path
):
    """PRD 10: every failure mode maps to its stable code, never to a success."""
    expected_code = FAULT_MATRIX[failure_point]
    service = FakeGuestService(
        trust_policy={"accept_modes": list(ACCEPTED_SIGNATURE_MODES)},
        failures={"fail_at": failure_point, "mode": "always"},
    )
    byte_channel = GuestByteChannel(service)
    installer = IpaInstaller(
        GuestClient(FakeTransport(service), attempts=1, backoff_s=0.0),
        run_dir=tmp_path / "runs" / f"exp-fault-{failure_point}",
        byte_channel=byte_channel,
    )

    result = installer.install(hello_ipa(_import_dir(tmp_path) / "HelloUniPad.ipa"))

    assert result.stage == "failed"
    assert result.ok is False
    assert result.error_code == expected_code
    assert result.error_message
    assert result.trust_mode is None, "a rejected package records no trust mode"

    # nothing is left registered: a verifier's status() can never contradict it
    assert service.status().installed == []
    assert HELLO_GUEST_KEY not in service.installed
    assert HELLO_GUEST_KEY not in service.containers
    assert service.install_log[-1]["stage"] == "failed"
    assert service.install_log[-1]["error_code"] == expected_code

    # and the failure is persisted as evidence with the same code
    record = read_json(installer.run_dir / RECORD_NAME)
    assert record["stage"] == "failed"
    assert record["error_code"] == expected_code
    assert _stage_sequence(installer.run_dir)[-1] == "failed"

    # the package really reached the guest in every case: what failed is the
    # guest-side install step, never the host-side byte transfer
    assert len(byte_channel.delivered) == 1
    assert byte_channel.delivered[0]["staged_sha256"] == result.package_sha256


# ---------------------------------------------------------------------------
# negative guards (PRD 9.1, 7.4, 9.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("guest_status", [None, "transferred"])
def test_a_channel_that_only_transfers_never_reports_installed(guest_status, tmp_path):
    """PRD 9.1: file transfer success must never be reported as ``installed``."""
    from tests.fixtures.fake_guest_channel import RecordingGuestChannel

    ipa = hello_ipa(_import_dir(tmp_path) / "HelloUniPad.ipa")
    expected_sha256 = hashlib.sha256(ipa.read_bytes()).hexdigest()
    reply = None if guest_status is None else {"status": guest_status}

    channel = RecordingGuestChannel(reply)
    run_dir = tmp_path / "runs" / "exp-transfer-only"
    installer = IpaInstaller(channel, run_dir=run_dir)

    result = installer.install(ipa)

    # the transfer itself succeeded -- that is what makes this a real guard
    assert len(channel.sent_packages) == 1
    assert channel.sent_packages[0]["sha256"] == expected_sha256
    assert channel.sent_packages[0]["size_bytes"] == ipa.stat().st_size
    assert channel.transferred_bytes == ipa.stat().st_size

    # ... and it is still not an install
    assert result.stage not in ("installed", "registered")
    assert result.ok is False
    assert result.error_code is not None
    assert result.package_sha256 == expected_sha256

    record = read_json(run_dir / RECORD_NAME)
    assert record["stage"] not in ("installed", "registered")
    assert record["error_code"] == result.error_code


def test_verifier_refuses_a_gate_with_incomplete_evidence(tmp_path):
    """PRD 7.4 / 9.2: missing evidence fails closed with ``evidence_incomplete``."""
    run_dir = tmp_path / "runs" / "exp-incomplete"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "qemu-args.txt").write_text("-accel\ntcg\n", encoding="utf-8")

    from unipad.models import EvidenceSet

    evidence = EvidenceSet(run_id="exp-incomplete")
    evidence.qemu_args = "qemu-args.txt"      # the only item this run produced

    outcome = Verifier().evaluate_detailed("ipa-mvp", "M10", evidence, run_dir=run_dir)

    assert outcome.result.passed is False
    assert outcome.result.milestone == "M10"
    assert outcome.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert "evidence_incomplete" in outcome.result.failure_reasons
    assert "missing:serial_log" in outcome.result.failure_reasons
    assert "missing:experiment_json" in outcome.result.failure_reasons
    assert outcome.missing
    assert read_json(run_dir / "verification.json")["passed"] is False


def test_trust_mode_is_recorded_from_the_guest_and_never_assumed(tmp_path):
    """PRD 9.1: ``trust_mode`` is the guest's record, not a host-side guess.

    The assertions deliberately compare the two guest-reported labels with each
    other and never with a name of any real Apple trust mechanism: what the
    host must guarantee is that it *records* what the guest said and invents
    nothing when the guest refuses.
    """
    def _install(policy: dict) -> tuple:
        service = FakeGuestService(trust_policy=policy)
        installer = IpaInstaller(
            GuestClient(FakeTransport(service), attempts=1, backoff_s=0.0),
            run_dir=tmp_path / "runs" / f"exp-trust-{policy['trust_mode']}",
            byte_channel=GuestByteChannel(service),
        )
        return service, installer.install(
            hello_ipa(_import_dir(tmp_path) / "HelloUniPad.ipa")
        )

    accepted_service, accepted = _install(
        {"accept_modes": list(ACCEPTED_SIGNATURE_MODES), "trust_mode": "self-signed-dev-trust"}
    )
    refused_service, refused = _install(
        {"accept_modes": ["self"], "trust_mode": "enterprise-supervised"}
    )

    # accepted: the recorded label is exactly what the guest policy declared
    assert accepted.stage == "registered"
    assert accepted.trust_mode == accepted_service.trust_mode_label
    assert accepted.trust_mode == accepted_service.installed[HELLO_GUEST_KEY]["trust_mode"]
    assert accepted_service.trust_log[-1]["accepted"] is True

    # refused: no trust mode is invented for a package the guest rejected
    assert refused.stage == "failed"
    assert refused.error_code == ErrorCode.SIGNATURE_OR_POLICY_REJECTED
    assert refused.trust_mode is None
    assert refused_service.trust_log[-1]["accepted"] is False
    assert refused_service.trust_log[-1]["trust_mode"] is None

    # the two labels differ, so the host cannot have hard-coded either one
    assert accepted_service.trust_mode_label != refused_service.trust_mode_label


# ---------------------------------------------------------------------------
# the positive gate: complete simulation evidence passes ipa-mvp
# ---------------------------------------------------------------------------


def test_ipa_mvp_gate_passes_on_complete_simulation_evidence(stack, desktop_session, tmp_path):
    """PRD 6 / 7.4: the ``ipa-mvp`` gate passes on complete simulation evidence."""
    run_dir = tmp_path / "runs" / "exp-ipa-mvp-gate"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = "exp-ipa-mvp-gate"

    # --- the PRD 9.2 minimum evidence set, as real files -----------------
    write_json(
        run_dir / "experiment.json",
        ExperimentRecord(
            experiment_id=run_id,
            milestone="M12",
            status="running",
            hypothesis="a runtime-imported IPA installs, registers and survives a reboot",
            base_commit=None,               # source pinning is an M0 open item
            source_lock_sha256=None,
            runtime={"host_inventory": "host-inventory.json", "qemu_version": None, "accel": "tcg"},
        ).to_dict(),
    )
    write_host_inventory(
        run_dir / "host-inventory.json", collect_host_inventory(probe_cim=False)
    )
    write_json(
        run_dir / "artifact-manifest.json",
        ArtifactManifest(
            run_id=run_id,
            target="tcg",
            artifacts=[
                ArtifactEntry(
                    name="HelloUniPad.ipa",
                    sha256=desktop_session.hello.package_sha256,
                    size_bytes=Path(desktop_session.hello.log_path).stat().st_size,
                    arch="arm64",
                    role="auxiliary",
                )
            ],
            toolchain={"guest": "FakeGuestService (simulation fixture)"},
        ).to_dict(),
    )

    # sources that live outside the run directory, so the bundle copies them in
    argv_path = tmp_path / "qemu-args.txt"
    argv_path.write_text(
        "\n".join(["-accel", "tcg", "-machine", "virt", "-qmp", "tcp:127.0.0.1:4444,server=on,wait=off"])
        + "\n",
        encoding="utf-8",
    )
    serial_path = tmp_path / "serial.log"
    serial_path.write_text(
        "M1: UEFI firmware started\nM2: UniXNU boot manager running\n"
        "M3: DarwinBoot handoff prepared\nM4: XNU kernel entered\n"
        "SpringBoard: home screen interactive\n",
        encoding="utf-8",
    )
    guest_log_path = tmp_path / "guest.log"
    guest_log_path.write_text(
        "launchd[1]: SpringBoard up\ncontainermanagerd: container for "
        f"{NOTES_BUNDLE_ID} established\n",
        encoding="utf-8",
    )
    screenshot_path = tmp_path / "m11-app-window.ppm"
    screenshot_path.write_bytes(b"P6\n4 4\n255\n" + bytes(48))

    bundle = EvidenceBundle(run_dir, run_id)
    bundle.set_experiment_json(run_dir / "experiment.json")
    bundle.set_host_inventory(run_dir / "host-inventory.json")
    bundle.set_artifact_manifest(run_dir / "artifact-manifest.json")
    bundle.add_qemu_args(argv_path.read_text(encoding="utf-8").splitlines())
    bundle.add_serial_log(serial_path)
    bundle.add_guest_log(guest_log_path)
    bundle.add_qmp_log('{"event": "SHUTDOWN", "data": {"guest": false}}\n')
    bundle.add_screenshot(screenshot_path)

    # --- the structured facts the install/persistence conditions read -----
    write_json(
        run_dir / "install.json",
        {
            "stage": desktop_session.notes.stage,
            "bundle_id": desktop_session.notes.bundle_id,
            "package_sha256": desktop_session.notes.package_sha256,
            "trust_mode": desktop_session.notes.trust_mode,
            "error_code": None,
        },
    )
    write_json(
        run_dir / "persistence.json",
        {"marker": desktop_session.marker, "read_back": desktop_session.marker},
    )
    facts = load_facts(run_dir / "install.json", run_dir / "persistence.json")

    evidence = bundle.build(strict=False)
    assert bundle.missing == ["verification_json"]

    outcome = Verifier().evaluate_detailed(
        "ipa-mvp", "M12", evidence, run_dir=run_dir, facts=facts
    )

    assert outcome.result.passed is True
    assert outcome.result.milestone == "M12"
    assert outcome.result.failure_reasons == []
    assert outcome.code is None
    assert outcome.result.verifier_id == "unipad.controller.verifier@1.0.0"
    assert bundle.missing == []

    verification = read_json(run_dir / "verification.json")
    assert verification["passed"] is True
    assert verification["gate_id"] == "ipa-mvp"
    assert verification["error_code"] is None
    assert {row["condition_id"] for row in verification["conditions"]} == {
        "evidence.complete",
        "evidence.files_exist",
        "evidence.screenshots_present",
        "evidence.guest_log_present",
        "install.stage_terminal",
        "install.bundle_id_recorded",
        "install.error_free",
        "persistence.marker_matches",
    }
    assert all(row["passed"] for row in verification["conditions"])

    # the same gate refuses when the persisted marker does not read back
    write_json(
        run_dir / "persistence.json",
        {"marker": desktop_session.marker, "read_back": uuid.uuid4().hex},
    )
    drifted = Verifier().evaluate_detailed(
        "ipa-mvp", "M12", evidence, run_dir=run_dir,
        facts=load_facts(run_dir / "install.json", run_dir / "persistence.json"),
    )
    assert drifted.result.passed is False
    assert "persistence_marker_mismatch" in drifted.result.failure_reasons
    assert drifted.code == ErrorCode.VERIFICATION_FAILED
