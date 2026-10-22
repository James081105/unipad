"""Unit tests for :mod:`unipad.guest.fake` (PRD 5.2, 9, 10; M9b/M10/M10a/M11).

``FakeGuestService`` is the simulation backbone for every milestone that needs a
guest: it models the *guest side* of the IPA install chain with the same stage
progression the real guest must exhibit, and it can inject each PRD 10 failure
at the matching point.  These tests pin the properties the milestone gates lean
on:

* a successful install **earns** ``registered`` -- container created, icon
  registered, trust decision actually recorded and reported;
* failure injection (signature / install_service / registration / dyld /
  dependency) with mode ``always``/``once`` maps onto the exact PRD 10 code, and
  ``once`` recovers on retry;
* ``persist(dir)`` / ``load(dir)`` round-trip preserves installed apps **and**
  app container data (M9b), and a fresh service instance loading the same
  directory sees the same state (the simulated reboot);
* the fake never writes into the repository working tree;
* the TCP pair drives the service over a real loopback socket.

Simulation only (docs/testing-strategy.md D3): a green run here proves the host
side drove the protocol correctly.  It is never evidence that a guest, a
display or an input device exists -- field status stays ``UNVERIFIED``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.client import GuestClient
from unipad.guest.fake import (
    DEFAULT_TRUST_MODE,
    INSTALL_FAILURE_POINTS,
    FakeGuestService,
    FakeTransport,
    derive_challenge_value,
    start_tcp_server,
)
from unipad.guest.protocol import (
    MSG_CHALLENGE_REQUEST,
    MSG_INSTALL_REQUEST,
    MSG_INSTALL_RESPONSE,
    MSG_STATUS_REQUEST,
    Message,
)
from tests.fixtures.ipa_builder import build_ipa, hello_ipa, notes_ipa

REPO_ROOT = Path(__file__).resolve().parents[2]
NONCE = "run-2026-10-22-nonce"

#: Injection point -> the PRD 10 code the guest must report for it.
EXPECTED_FAILURE_CODES = {
    "structure": ErrorCode.INVALID_PACKAGE,
    "transfer": ErrorCode.TRANSFER_FAILED,
    "signature": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "install_service": ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
    "registration": ErrorCode.REGISTRATION_FAILED,
    "dependency": ErrorCode.MISSING_DEPENDENCY,
    "dyld": ErrorCode.DYLD_FAILED,
}


def _deliver(service: FakeGuestService, tmp_path, *, name: str = "HelloUniPad.ipa") -> tuple[bytes, dict]:
    """Arrange: build a real-shaped IPA, stage it, and return (bytes, meta)."""
    ipa_path = hello_ipa(tmp_path / name)
    data = ipa_path.read_bytes()
    meta = {
        "package_name": name,
        "package_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "declared_min_os": "17.0",
        "signature_mode": "self",
        "bundle_id": "com.unipad.hello",
    }
    service.stage_package(data, meta)
    return data, meta


@pytest.fixture
def service(tmp_path) -> tuple[FakeGuestService, bytes, dict]:
    """A fake guest with one staged, real-shaped IPA ready to install."""
    guest = FakeGuestService()
    data, meta = _deliver(guest, tmp_path)
    return guest, data, meta


# ---------------------------------------------------------------------------
# the success path (PRD 5.2 step 4, M10)
# ---------------------------------------------------------------------------


def test_successful_install_registers_an_icon_and_a_container(service):
    """PRD 5.2 step 4: install + registration + a real icon, with a bundle id."""
    guest, data, meta = service

    response = guest.install(meta["package_name"], data, meta)

    assert response.stage == "registered"
    assert response.bundle_id == "com.unipad.hello"
    assert response.trust_mode == DEFAULT_TRUST_MODE
    assert response.error_code is None
    assert response.ok is True
    entry = guest.installed["com.unipad.hello"]
    assert entry["icon"] is True
    assert entry["package_sha256"] == meta["package_sha256"]
    assert entry["trust_mode"] == DEFAULT_TRUST_MODE
    container = guest.containers["com.unipad.hello"]
    assert container["markers"] == {} and container["files"] == []
    assert guest.install_log[-1] == {
        "bundle_id": "com.unipad.hello",
        "package_sha256": meta["package_sha256"],
        "stage": "registered",
    }


def test_install_progress_records_the_chain_in_order(service):
    """PRD 9.1: the stage progression is observable evidence, not prose."""
    guest, data, meta = service

    guest.install(meta["package_name"], data, meta)

    stages = [entry["stage"] for entry in guest.progress_log]
    assert stages == ["received", "checked", "installed", "registered"]


def test_trust_mode_is_recorded_not_assumed(service):
    """PRD 5.1: the trust decision is observed and reported, never invented."""
    guest, data, meta = service

    guest.install(meta["package_name"], data, meta)

    decision = guest.trust_log[-1]
    assert decision["accepted"] is True
    assert decision["signature_mode"] == "self"
    assert decision["trust_mode"] == DEFAULT_TRUST_MODE
    assert decision["declared_min_os"] == "17.0"
    assert decision["guest_os"] == guest.guest_os


def test_trust_policy_rejection_is_reported_as_a_policy_fault(service):
    """PRD 10: a policy refusal is ``signature_or_policy_rejected``, not success."""
    guest, data, meta = service
    guest.trust_accept = False

    response = guest.install(meta["package_name"], data, meta)

    assert response.stage == "failed"
    assert response.error_code == ErrorCode.SIGNATURE_OR_POLICY_REJECTED
    assert guest.trust_log[-1]["accepted"] is False
    assert guest.trust_log[-1]["trust_mode"] is None
    assert "com.unipad.hello" not in guest.installed


def test_unaccepted_signature_mode_is_rejected(service):
    """PRD 5.1: signature modes are not interchangeable support categories."""
    guest, data, meta = service
    guest.trust_accept_modes = ["enterprise"]

    response = guest.install(meta["package_name"], data, meta)

    assert response.error_code == ErrorCode.SIGNATURE_OR_POLICY_REJECTED


def test_custom_trust_label_is_what_gets_reported(service):
    """The recorded label is the policy's, so callers cannot confuse modes."""
    guest, data, meta = service
    guest.trust_mode_label = "enterprise-supervised"

    response = guest.install(meta["package_name"], data, meta)

    assert response.trust_mode == "enterprise-supervised"
    assert guest.installed["com.unipad.hello"]["trust_mode"] == "enterprise-supervised"


# ---------------------------------------------------------------------------
# failure injection: PRD 10 taxonomy (PRD section 10 table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("point", sorted(EXPECTED_FAILURE_CODES))
def test_each_failure_point_maps_to_its_prd_code(service, point):
    """PRD 10: every injectable point reports its own taxonomy code."""
    guest, data, meta = service
    guest.set_failures({"fail_at": point, "mode": "always"})

    response = guest.install(meta["package_name"], data, meta)

    assert response.stage == "failed"
    assert response.error_code == EXPECTED_FAILURE_CODES[point]
    assert response.error_message
    assert "com.unipad.hello" not in guest.installed


@pytest.mark.parametrize("point", sorted(EXPECTED_FAILURE_CODES))
def test_always_mode_keeps_failing_on_every_attempt(service, point):
    """``always`` must not quietly degrade into a one-shot fault."""
    guest, data, meta = service
    guest.set_failures({"fail_at": point, "mode": "always"})

    first = guest.install(meta["package_name"], data, meta)
    second = guest.install(meta["package_name"], data, meta)

    assert first.error_code == EXPECTED_FAILURE_CODES[point]
    assert second.error_code == EXPECTED_FAILURE_CODES[point]


@pytest.mark.parametrize("point", sorted(EXPECTED_FAILURE_CODES))
def test_once_mode_recovers_on_retry(service, point):
    """``once`` fires exactly once; the retry must complete the chain."""
    guest, data, meta = service
    guest.set_failures({"fail_at": point, "mode": "once"})

    first = guest.install(meta["package_name"], data, meta)
    second = guest.install(meta["package_name"], data, meta)

    assert first.stage == "failed"
    assert first.error_code == EXPECTED_FAILURE_CODES[point]
    assert second.stage == "registered"
    assert second.error_code is None
    assert "com.unipad.hello" in guest.installed


def test_invalid_package_taxonomy_name_resolves_to_the_structure_point(service):
    """A taxonomy name must fire, not validate cleanly and then do nothing."""
    guest, data, meta = service
    guest.set_failures({"fail_at": "invalid_package", "mode": "always"})

    response = guest.install(meta["package_name"], data, meta)

    assert response.error_code == ErrorCode.INVALID_PACKAGE


def test_set_failures_clears_injection(service):
    guest, data, meta = service
    guest.set_failures({"fail_at": "signature", "mode": "always"})
    guest.set_failures(None)

    assert guest.install(meta["package_name"], data, meta).stage == "registered"


def test_set_failures_replaces_the_previous_configuration(service):
    guest, data, meta = service
    guest.set_failures({"fail_at": "signature", "mode": "always"})
    guest.set_failures({"fail_at": "registration", "mode": "always"})

    response = guest.install(meta["package_name"], data, meta)

    assert response.error_code == ErrorCode.REGISTRATION_FAILED


@pytest.mark.parametrize(
    "failures",
    [
        {"fail_at": "nope"},
        {"fail_at": "signature", "mode": "sometimes"},
        {"fail_at": "signature", "mode": None},
    ],
)
def test_unknown_failure_configuration_is_rejected(failures):
    """A typo in the injection config must not look like a healthy fake."""
    with pytest.raises(UniPadError) as excinfo:
        FakeGuestService(failures=failures)

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_failure_points_are_the_documented_chain_order():
    assert INSTALL_FAILURE_POINTS == (
        "structure", "transfer", "signature", "install_service", "registration", "dependency", "dyld",
    )
    assert set(EXPECTED_FAILURE_CODES) == set(INSTALL_FAILURE_POINTS)


# ---------------------------------------------------------------------------
# package staging and the transfer/structure checks (PRD 10)
# ---------------------------------------------------------------------------


def test_stage_package_accepts_the_declared_digest(tmp_path):
    guest = FakeGuestService()
    ipa_path = hello_ipa(tmp_path / "HelloUniPad.ipa")
    data = ipa_path.read_bytes()
    meta = {
        "package_name": "HelloUniPad.ipa",
        "package_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "declared_min_os": "17.0",
        "signature_mode": "self",
    }

    receipt = guest.stage_package(data, meta)

    assert receipt["staged_sha256"] == meta["package_sha256"]
    assert receipt["size_bytes"] == len(data)


def test_stage_package_rejects_a_size_mismatch(tmp_path):
    """PRD 10: a transfer that does not match the declared size is ``transfer_failed``."""
    guest = FakeGuestService()
    data = hello_ipa(tmp_path / "HelloUniPad.ipa").read_bytes()
    meta = {
        "package_name": "HelloUniPad.ipa",
        "package_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data) + 1,
        "declared_min_os": "17.0",
        "signature_mode": "self",
    }

    with pytest.raises(UniPadError) as excinfo:
        guest.stage_package(data, meta)

    assert excinfo.value.code == ErrorCode.TRANSFER_FAILED
    assert excinfo.value.details["declared"] == len(data) + 1


def test_stage_package_rejects_a_hash_mismatch(tmp_path):
    guest = FakeGuestService()
    data = hello_ipa(tmp_path / "HelloUniPad.ipa").read_bytes()
    meta = {
        "package_name": "HelloUniPad.ipa",
        "package_sha256": "0" * 64,
        "size_bytes": len(data),
        "declared_min_os": "17.0",
        "signature_mode": "self",
    }

    with pytest.raises(UniPadError) as excinfo:
        guest.stage_package(data, meta)

    assert excinfo.value.code == ErrorCode.TRANSFER_FAILED
    assert excinfo.value.details["actual"] == hashlib.sha256(data).hexdigest()


def test_install_rejects_bytes_that_are_not_an_ipa(tmp_path):
    """PRD 10: structure validation is a real check, not a suffix lookup."""
    guest = FakeGuestService()
    junk = b"this is not an ipa at all"
    meta = {
        "package_name": "Fake.ipa",
        "package_sha256": hashlib.sha256(junk).hexdigest(),
        "size_bytes": len(junk),
        "declared_min_os": "17.0",
        "signature_mode": "self",
    }

    response = guest.install("Fake.ipa", junk, meta)

    assert response.stage == "failed"
    assert response.error_code == ErrorCode.INVALID_PACKAGE


def test_install_rejects_bytes_whose_digest_disagrees_with_the_metadata(tmp_path):
    """PRD 10: the transfer check compares bytes against the declared digest."""
    guest = FakeGuestService()
    good = hello_ipa(tmp_path / "HelloUniPad.ipa").read_bytes()
    tampered = good[:-16] + b"\x00" * 16
    meta = {
        "package_name": "HelloUniPad.ipa",
        "package_sha256": hashlib.sha256(good).hexdigest(),
        "size_bytes": len(good),
        "declared_min_os": "17.0",
        "signature_mode": "self",
        "bundle_id": "com.unipad.hello",
    }

    response = guest.install("HelloUniPad.ipa", tampered, meta)

    assert response.error_code == ErrorCode.TRANSFER_FAILED
    assert "com.unipad.hello" not in guest.installed


def test_empty_package_is_an_invalid_package():
    guest = FakeGuestService()

    response = guest.install("Empty.ipa", b"", {
        "package_name": "Empty.ipa",
        "package_sha256": hashlib.sha256(b"").hexdigest(),
        "size_bytes": 0,
        "declared_min_os": "17.0",
        "signature_mode": "self",
    })

    assert response.error_code == ErrorCode.INVALID_PACKAGE


def test_install_requires_complete_metadata():
    guest = FakeGuestService()

    with pytest.raises(UniPadError) as excinfo:
        guest.install("HelloUniPad.ipa", b"PK\x03\x04", {"package_name": "HelloUniPad.ipa"})

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_install_of_a_package_that_was_never_delivered_is_a_transfer_failure():
    """PRD 10: bytes the guest never received cannot be verified, so cannot install.

    The declared digest has to be checked against bytes that actually arrived.
    Skipping that check would let an install request for a package nobody
    transferred report ``registered`` -- a fabricated success, which is exactly
    what PRD 9.1 and the module contract forbid.
    """
    guest = FakeGuestService()
    meta = {
        "package_name": "HelloUniPad.ipa",
        "package_sha256": "a" * 64,
        "size_bytes": 4096,
        "declared_min_os": "17.0",
        "signature_mode": "self",
        "bundle_id": "com.unipad.hello",
    }

    response = guest.install(meta["package_name"], None, meta)

    assert response.stage == "failed"
    assert response.error_code == ErrorCode.TRANSFER_FAILED
    assert response.error_message
    assert response.ok is False
    assert guest.installed == {}
    assert guest.containers == {}
    assert guest.status().installed == []
    assert guest.install_log[-1]["stage"] == "failed"
    # The chain stopped before any container, icon or trust decision existed.
    assert guest.trust_log == []
    assert [entry["stage"] for entry in guest.progress_log] == ["received", "checked"]


def test_message_api_install_without_staged_bytes_is_a_transfer_failure():
    """The wire path must not bypass the transfer verification either.

    The message API resolves the package by its declared digest; when nothing
    was staged under it the guest has no bytes and must say so.
    """
    guest = FakeGuestService()
    meta = {
        "package_name": "HelloUniPad.ipa",
        "package_sha256": "a" * 64,
        "size_bytes": 4096,
        "declared_min_os": "17.0",
        "signature_mode": "self",
    }
    envelope = Message(type=MSG_INSTALL_REQUEST, id="corr-1", payload=meta)

    reply = guest.handle(envelope.to_dict())

    assert reply["type"] == MSG_INSTALL_RESPONSE
    assert reply["id"] == "corr-1"
    assert reply["payload"]["stage"] == "failed"
    assert reply["payload"]["error_code"] == ErrorCode.TRANSFER_FAILED
    assert reply["payload"]["bundle_id"] == "HelloUniPad"
    assert guest.installed == {}
    assert guest.containers == {}


# ---------------------------------------------------------------------------
# late-chain rollback: status() can never contradict a reported failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("point", ["dependency", "dyld"])
def test_late_chain_failure_withdraws_the_registration(service, point):
    """PRD 10: an app that failed to load is not left half-registered."""
    guest, data, meta = service
    guest.set_failures({"fail_at": point, "mode": "always"})

    response = guest.install(meta["package_name"], data, meta)

    assert response.error_code == EXPECTED_FAILURE_CODES[point]
    assert "com.unipad.hello" not in guest.installed
    assert "com.unipad.hello" not in guest.containers
    assert guest.status().installed == []
    assert guest.install_log[-1]["rolled_back"] is True


def test_launch_of_an_installed_app_reports_running(service):
    """M11 precondition: an installed app can actually be asked to start."""
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)

    launched = guest.launch("com.unipad.hello")

    assert launched["stage"] == "running"
    assert launched["trust_mode"] == DEFAULT_TRUST_MODE


def test_launch_of_an_unknown_app_is_a_registration_fault(service):
    guest, _data, _meta = service

    with pytest.raises(UniPadError) as excinfo:
        guest.launch("com.unipad.absent")

    assert excinfo.value.code == ErrorCode.REGISTRATION_FAILED


@pytest.mark.parametrize("point", ["dependency", "dyld"])
def test_launch_failure_is_distinguishable_from_install_failure(service, point):
    """M11: a launch-time fault is not reported as an install-time fault."""
    guest, data, meta = service
    assert guest.install(meta["package_name"], data, meta).stage == "registered"
    guest.set_failures({"fail_at": point, "mode": "always"})

    with pytest.raises(UniPadError) as excinfo:
        guest.launch("com.unipad.hello")

    assert excinfo.value.code == EXPECTED_FAILURE_CODES[point]
    # The app itself did install: only the launch is refused.
    assert "com.unipad.hello" in guest.installed


def test_uninstall_removes_the_app_and_its_container(service):
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)

    result = guest.uninstall("com.unipad.hello")

    assert result["stage"] == "uninstalled"
    assert "com.unipad.hello" not in guest.installed
    assert "com.unipad.hello" not in guest.containers


def test_uninstall_of_an_unknown_app_is_a_registration_fault(service):
    guest, _data, _meta = service

    with pytest.raises(UniPadError) as excinfo:
        guest.uninstall("com.unipad.absent")

    assert excinfo.value.code == ErrorCode.REGISTRATION_FAILED


# ---------------------------------------------------------------------------
# data volume (M9b / M12)
# ---------------------------------------------------------------------------


def test_data_marker_round_trip_inside_the_container(service):
    """PRD 5.2 step 7: text written to the container can be read back."""
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)

    guest.set_data_marker("com.unipad.hello", "Documents/notes.txt", "hello from this run")
    stored = guest.get_data_marker("com.unipad.hello", "Documents/notes.txt")

    assert stored == "hello from this run"
    assert guest.data_markers() == ["com.unipad.hello:Documents/notes.txt"]
    assert guest.containers["com.unipad.hello"]["files"] == [
        {"name": "Documents/notes.txt", "size_bytes": len("hello from this run".encode("utf-8"))}
    ]


def test_missing_marker_is_a_persistence_fault(service):
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)

    with pytest.raises(UniPadError) as excinfo:
        guest.get_data_marker("com.unipad.hello", "Documents/absent.txt")

    assert excinfo.value.code == ErrorCode.PERSISTENCE_FAILED


def test_data_for_an_uninstalled_app_is_a_registration_fault(service):
    guest, _data, _meta = service

    with pytest.raises(UniPadError) as excinfo:
        guest.set_data_marker("com.unipad.hello", "k", "v")

    assert excinfo.value.code == ErrorCode.REGISTRATION_FAILED


def test_installed_app_without_a_container_is_a_persistence_fault(service):
    """PRD 10: losing the container is ``persistence_failed``, not registration."""
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)
    guest.containers.pop("com.unipad.hello")

    with pytest.raises(UniPadError) as excinfo:
        guest.get_data_marker("com.unipad.hello", "k")

    assert excinfo.value.code == ErrorCode.PERSISTENCE_FAILED


def test_status_reports_icons_trust_modes_and_markers(service):
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)
    guest.set_data_marker("com.unipad.hello", "Documents/notes.txt", "x")

    status = guest.status()

    assert status.installed == [
        {
            "bundle_id": "com.unipad.hello",
            "version": "1.0",
            "icon": True,
            "trust_mode": DEFAULT_TRUST_MODE,
        }
    ]
    assert status.data_markers == ["com.unipad.hello:Documents/notes.txt"]


# ---------------------------------------------------------------------------
# challenge (M11)
# ---------------------------------------------------------------------------


def test_challenge_is_deterministic_for_the_same_run(service):
    """M11: the value must be reproducible so evidence can be compared."""
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)

    first = guest.challenge(NONCE)
    second = guest.challenge(NONCE)

    assert first.value == second.value
    assert first.value == guest.expected_challenge("com.unipad.hello", NONCE)
    assert len(first.value) == 64
    int(first.value, 16)  # lowercase hex digest, nothing secret-shaped


def test_challenge_value_matches_the_documented_derivation(service):
    """The fixture derivation is pinned: sha256(app_seed || nonce)."""
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)

    seed = hashlib.sha256(b"unipad-fake-guest-app-seed:com.unipad.hello").digest()
    expected = hashlib.sha256(seed + NONCE.encode("utf-8")).hexdigest()

    assert guest.challenge(NONCE).value == expected
    assert derive_challenge_value("com.unipad.hello", NONCE) == expected


def test_challenge_differs_per_app_and_per_nonce(service):
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)

    hello = guest.challenge(NONCE, "com.unipad.hello").value
    other = guest.challenge(NONCE, "com.unipad.other").value
    other_nonce = guest.challenge("another-nonce", "com.unipad.hello").value

    assert len({hello, other, other_nonce}) == 3


def test_challenge_defaults_to_the_most_recent_install(tmp_path, service):
    guest, _data, _meta = service
    notes_path = notes_ipa(tmp_path / "UniPadNotes.ipa")
    notes = notes_path.read_bytes()
    notes_meta = {
        "package_name": notes_path.name,
        "package_sha256": hashlib.sha256(notes).hexdigest(),
        "size_bytes": len(notes),
        "declared_min_os": "17.0",
        "signature_mode": "self",
        "bundle_id": "com.unipad.notes",
    }
    guest.stage_package(notes, notes_meta)
    guest.install(notes_path.name, notes, notes_meta)

    response = guest.challenge(NONCE)

    assert response.value == guest.expected_challenge("com.unipad.notes", NONCE)
    assert guest.challenge_log[-1]["bundle_id"] == "com.unipad.notes"


def test_challenge_without_any_app_still_answers_deterministically():
    guest = FakeGuestService()

    response = guest.challenge(NONCE)

    assert response.nonce == NONCE
    assert response.value == derive_challenge_value("unipad.fake.noapp", NONCE)


@pytest.mark.parametrize("nonce", ["", None, 7])
def test_challenge_rejects_a_bad_nonce(nonce):
    with pytest.raises(UniPadError) as excinfo:
        FakeGuestService().challenge(nonce)

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# persistence (M9b): apps AND container data survive a simulated reboot
# ---------------------------------------------------------------------------


def test_persist_and_load_round_trip_preserves_apps_and_container_data(tmp_path, service):
    """PRD 5.2 step 8 / M9b: apps and their data survive persist + reload."""
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)
    guest.set_data_marker("com.unipad.hello", "Documents/notes.txt", "saved before shutdown")
    data_dir = tmp_path / "data"

    persisted = guest.persist(data_dir)
    restored = FakeGuestService(device_id="a-brand-new-qemu-process")
    summary = restored.load(data_dir)

    assert persisted["installed"] == 1
    assert persisted["containers"] == 1
    assert persisted["markers"] == 1
    assert summary == {"installed": 1, "containers": 1}
    assert set(restored.installed) == {"com.unipad.hello"}
    assert restored.installed["com.unipad.hello"]["icon"] is True
    assert restored.installed["com.unipad.hello"]["trust_mode"] == DEFAULT_TRUST_MODE
    assert restored.get_data_marker("com.unipad.hello", "Documents/notes.txt") == "saved before shutdown"
    assert restored.containers["com.unipad.hello"] == guest.containers["com.unipad.hello"]
    assert restored.status().to_dict() == guest.status().to_dict()
    assert restored.guest_os == guest.guest_os
    assert restored.device_id == guest.device_id


def test_a_fresh_service_sees_the_same_state_after_a_simulated_reboot(tmp_path, service):
    """M9b: a new QEMU process reads the same apps and data back from disk."""
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)
    guest.set_data_marker("com.unipad.hello", "Documents/notes.txt", "run-1-value")
    guest.persist(tmp_path / "data")

    # "Power off, start a brand-new QEMU process."
    rebooted = FakeGuestService()
    rebooted.load(tmp_path / "data")

    assert [app["bundle_id"] for app in rebooted.status().installed] == ["com.unipad.hello"]
    assert rebooted.get_data_marker("com.unipad.hello", "Documents/notes.txt") == "run-1-value"
    assert rebooted.challenge(NONCE).value == guest.challenge(NONCE).value
    # The rebooted guest can still install more apps on top of the restored state.
    notes_path = notes_ipa(tmp_path / "UniPadNotes.ipa")
    notes = notes_path.read_bytes()
    notes_meta = {
        "package_name": notes_path.name,
        "package_sha256": hashlib.sha256(notes).hexdigest(),
        "size_bytes": len(notes),
        "declared_min_os": "17.0",
        "signature_mode": "self",
        "bundle_id": "com.unipad.notes",
    }
    rebooted.stage_package(notes, notes_meta)
    assert rebooted.install(notes_path.name, notes, notes_meta).stage == "registered"
    assert set(rebooted.installed) == {"com.unipad.hello", "com.unipad.notes"}


def test_persist_is_byte_deterministic(tmp_path, service):
    """D3: two services reaching the same state serialise to the same bytes."""
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)
    guest.set_data_marker("com.unipad.hello", "Documents/notes.txt", "same value")
    twin = FakeGuestService()
    twin.stage_package(data, meta)
    twin.install(meta["package_name"], data, meta)
    twin.set_data_marker("com.unipad.hello", "Documents/notes.txt", "same value")

    guest.persist(tmp_path / "a")
    twin.persist(tmp_path / "b")

    assert (tmp_path / "a" / "guest-state.json").read_bytes() == (tmp_path / "b" / "guest-state.json").read_bytes()


def test_persisted_state_is_versioned_json(tmp_path, service):
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)

    guest.persist(tmp_path / "data")
    state = json.loads((tmp_path / "data" / "guest-state.json").read_text(encoding="utf-8"))

    assert state["schema_version"] == 1
    assert set(state["installed"]) == {"com.unipad.hello"}
    assert "com.unipad.hello" in state["containers"]


# ---------------------------------------------------------------------------
# no secrets in persisted state (PRD 9.2)
# ---------------------------------------------------------------------------

#: Incompressible fixture payload (hash-derived, so it is both deterministic
#: and genuinely large): embedding the package in the persisted state would be
#: unmistakable.
_INCOMPRESSIBLE_BLOB = b"".join(
    hashlib.sha256(index.to_bytes(8, "little")).digest() for index in range(2048)
)

#: Words that must never appear in the persistent volume (PRD 9.2: logs and
#: evidence carry no account tokens, signing private keys or user documents).
FORBIDDEN_PERSISTED_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "private_key",
    "privatekey",
    "-----begin",
    "bearer ",
    "authorization",
    "cookie",
    "credential",
    "passcode",
    "keychain",
)

#: The closed set of field names the persisted state may use.  A closed schema
#: is what keeps a free-form field from smuggling credential material in later.
ALLOWED_STATE_KEYS = frozenset({
    "schema_version", "guest_os", "guest_build", "device_id",
    "trust_policy", "installed", "containers", "challenge_log", "trust_log",
    "install_log",
    "accept_modes", "trust_mode", "accept",
    "version", "icon", "container", "package_name", "package_sha256",
    "size_bytes", "declared_min_os", "signature_mode",
    "markers", "files", "name",
    "bundle_id", "nonce", "value", "accepted",
    "stage", "error_code", "rolled_back",
})


#: Persisted maps whose *keys* are data (bundle ids, app-chosen marker names)
#: rather than schema field names.
_DATA_KEYED_MAPS = frozenset({"installed", "containers", "markers"})


def _schema_keys(node) -> set:
    """Collect the field names a persisted structure uses.

    ``installed``, ``containers`` and ``markers`` are keyed by data -- bundle
    ids and app-chosen file names -- so their keys are not schema fields and
    are not collected; their values are still walked.
    """
    names: set = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _DATA_KEYED_MAPS:
                assert isinstance(value, dict)
                if key == "markers":
                    assert all(isinstance(v, str) for v in value.values())
                    continue
                names |= _schema_keys(list(value.values()))
                continue
            names.add(key)
            names |= _schema_keys(value)
    elif isinstance(node, list):
        for item in node:
            names |= _schema_keys(item)
    return names


def test_persisted_state_holds_no_secrets_and_only_documented_fields(tmp_path):
    """PRD 9.2: the persistent Data volume carries no credential material.

    What the fake records about *itself* -- device label, trust policy, install
    and challenge logs, digests -- must be label- and digest-shaped only.  App
    container contents are the simulated user data M9b exists to preserve; the
    claim under test is that the guest adds nothing secret of its own, and that
    the schema stays closed so it cannot start to.
    """
    guest = FakeGuestService()
    ipa_path = build_ipa(
        tmp_path / "HelloUniPad.ipa",
        extra_files={"Documents/blob.bin": _INCOMPRESSIBLE_BLOB},
    )
    data = ipa_path.read_bytes()
    meta = {
        "package_name": "HelloUniPad.ipa",
        "package_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "declared_min_os": "17.0",
        "signature_mode": "self",
        "bundle_id": "com.unipad.hello",
    }
    guest.stage_package(data, meta)
    assert guest.install(meta["package_name"], data, meta).stage == "registered"
    guest.set_data_marker("com.unipad.hello", "Documents/blob.bin", "text the app saved")
    guest.challenge(NONCE)
    guest.persist(tmp_path / "data")

    text = (tmp_path / "data" / "guest-state.json").read_text(encoding="utf-8")
    state = json.loads(text)

    # 1. no credential material anywhere in the persisted volume
    lowered = text.lower()
    for marker in FORBIDDEN_PERSISTED_MARKERS:
        assert marker not in lowered, f"persisted guest state contains '{marker}'"

    # 2. the schema is closed -- no free-form field to smuggle anything into
    assert _schema_keys(state) <= ALLOWED_STATE_KEYS

    # 3. the guest keeps digests and sizes, never the package payload
    assert len(text.encode("utf-8")) * 10 < len(data)
    assert state["installed"]["com.unipad.hello"]["package_sha256"] == meta["package_sha256"]

    # 4. challenge values are derived per run, not stored secrets
    entry = state["challenge_log"][-1]
    assert entry["nonce"] == NONCE
    assert len(entry["value"]) == 64
    assert entry["value"] == derive_challenge_value("com.unipad.hello", NONCE)

    # 5. the trust policy records labels and a decision, not credentials
    assert state["trust_policy"] == {
        "accept_modes": ["self"],
        "trust_mode": DEFAULT_TRUST_MODE,
        "accept": True,
    }


def test_persisted_state_after_a_failure_holds_no_extra_material(tmp_path, service):
    """A failed install persists its evidence, and nothing beyond it."""
    guest, data, meta = service
    guest.set_failures({"fail_at": "dyld", "mode": "always"})
    assert guest.install(meta["package_name"], data, meta).stage == "failed"
    guest.persist(tmp_path / "data")

    state = json.loads((tmp_path / "data" / "guest-state.json").read_text(encoding="utf-8"))

    assert state["installed"] == {}
    assert state["containers"] == {}
    assert state["install_log"][-1]["stage"] == "failed"
    assert state["install_log"][-1]["error_code"] == ErrorCode.DYLD_FAILED
    assert _schema_keys(state) <= ALLOWED_STATE_KEYS


@pytest.mark.parametrize("write", ["missing", "corrupt", "wrong-schema"])
def test_load_rejects_unusable_state(tmp_path, write):
    """PRD 10: unreadable state is ``persistence_failed``, never an empty guest."""
    data_dir = tmp_path / "data"
    if write != "missing":
        data_dir.mkdir(parents=True)
        if write == "corrupt":
            (data_dir / "guest-state.json").write_text("{not json", encoding="utf-8")
        else:
            (data_dir / "guest-state.json").write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    guest = FakeGuestService()

    with pytest.raises(UniPadError) as excinfo:
        guest.load(data_dir)

    assert excinfo.value.code == ErrorCode.PERSISTENCE_FAILED


# ---------------------------------------------------------------------------
# the fake never writes into the repository working tree
# ---------------------------------------------------------------------------


def test_fake_only_writes_where_it_is_told(tmp_path, monkeypatch, service):
    """D3 + PRD 9.2: no stray evidence files land in the working tree."""
    guest, data, meta = service
    monkeypatch.chdir(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}

    guest.install(meta["package_name"], data, meta)
    guest.set_data_marker("com.unipad.hello", "Documents/notes.txt", "v")
    guest.challenge(NONCE)
    guest.status()
    with start_tcp_server(guest, timeout_s=5.0) as pair:
        pair.transport.request(Message(type=MSG_STATUS_REQUEST, id="probe", payload={}).to_dict())
    guest.persist(tmp_path / "data")

    after = {p.name for p in tmp_path.iterdir()}
    assert after - before == {"data"}
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == ["guest-state.json"]
    assert not (REPO_ROOT / "guest-state.json").exists()


def test_persist_into_a_nested_directory_creates_it(tmp_path, service):
    guest, data, meta = service
    guest.install(meta["package_name"], data, meta)

    target = tmp_path / "nested" / "deeper" / "data"
    persisted = guest.persist(target)

    assert (target / "guest-state.json").is_file()
    assert persisted["path"] == str(target / "guest-state.json")


# ---------------------------------------------------------------------------
# message dispatch and the real loopback socket
# ---------------------------------------------------------------------------


def test_install_over_the_message_api_uses_the_package_name(tmp_path, service):
    """The wire contract has five fields, so the bundle id is the ``.ipa`` stem."""
    guest, data, meta = service
    envelope = Message(type=MSG_INSTALL_REQUEST, id="corr-1", payload={
        "package_name": meta["package_name"],
        "package_sha256": meta["package_sha256"],
        "size_bytes": meta["size_bytes"],
        "declared_min_os": meta["declared_min_os"],
        "signature_mode": meta["signature_mode"],
    })

    reply = guest.handle(envelope.to_dict())

    assert reply["type"] == "install.response"
    assert reply["id"] == "corr-1"
    assert reply["payload"]["stage"] == "registered"
    assert reply["payload"]["bundle_id"] == "HelloUniPad"


def test_unsupported_message_type_returns_a_structured_error():
    """PRD 9.1: an unknown type is answered with a stable code, not ignored."""
    guest = FakeGuestService()

    reply = guest.handle({"type": "install.bogus", "id": "corr-1", "payload": {}})

    assert reply["type"] == "error"
    assert reply["id"] == "corr-1"
    assert reply["payload"]["code"] == ErrorCode.UNSUPPORTED_COMMAND


def test_malformed_envelope_raises_a_structured_error():
    """A non-conforming envelope is a protocol error, not an ignored message."""
    guest = FakeGuestService()

    with pytest.raises(UniPadError) as excinfo:
        guest.handle({"payload": {}})

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_malformed_envelope_over_the_socket_is_answered_with_a_code(tmp_path, service):
    """The TCP half converts the same failure into a structured error envelope."""
    guest, _data, _meta = service
    with start_tcp_server(guest, timeout_s=5.0) as pair:
        reply = pair.transport.request({"payload": {}})

    assert reply["type"] == "error"
    assert reply["payload"]["code"] == ErrorCode.QMP_PROTOCOL_ERROR


def test_challenge_without_a_nonce_is_a_protocol_error():
    guest = FakeGuestService()

    reply = guest.handle({"type": MSG_CHALLENGE_REQUEST, "id": "corr-1", "payload": {}})

    assert reply["payload"]["code"] == ErrorCode.QMP_PROTOCOL_ERROR


def test_in_process_transport_drives_the_service_over_messages(tmp_path, service):
    """Both transports share one dispatch seam, so they cannot diverge."""
    guest, data, meta = service
    transport = FakeTransport(guest)
    client = GuestClient(transport, attempts=2, backoff_s=0.0)

    assert client.install_package(data, meta).stage == "registered"
    assert client.challenge(NONCE).value == guest.expected_challenge("HelloUniPad", NONCE)
    assert [app["bundle_id"] for app in client.status().installed] == ["HelloUniPad"]
    assert [call["type"] for call in transport.calls] == [
        MSG_INSTALL_REQUEST, MSG_CHALLENGE_REQUEST, MSG_STATUS_REQUEST,
    ]


def test_client_to_service_over_a_real_loopback_socket(tmp_path, service):
    """M10/M11/M12 simulation: the host client drives a real TCP guest service."""
    guest, data, meta = service
    with start_tcp_server(guest, timeout_s=5.0) as pair:
        assert pair.address[0] == "127.0.0.1"
        assert pair.address[1] > 0
        client = GuestClient(pair.transport, attempts=2, backoff_s=0.0)

        installed = client.install_package(data, meta)
        assert installed.stage == "registered"
        assert installed.bundle_id == "HelloUniPad"

        guest.set_data_marker("HelloUniPad", "Documents/notes.txt", "over the socket")
        status = client.status()
        assert [app["bundle_id"] for app in status.installed] == ["HelloUniPad"]
        assert status.data_markers == ["HelloUniPad:Documents/notes.txt"]

        challenge = client.challenge(NONCE)
        assert challenge.value == guest.expected_challenge("HelloUniPad", NONCE)
        assert challenge.value == derive_challenge_value("HelloUniPad", NONCE)


def test_tcp_pair_reports_an_unsupported_message_type(tmp_path, service):
    """The real socket must answer with the same structured error as in-process."""
    guest, _data, _meta = service
    with start_tcp_server(guest, timeout_s=5.0) as pair:
        reply = pair.transport.request({"type": "nope.nope", "id": "corr-1", "payload": {}})

    assert reply["type"] == "error"
    assert reply["payload"]["code"] == ErrorCode.UNSUPPORTED_COMMAND


def test_tcp_pair_keeps_serving_after_a_bad_message(tmp_path, service):
    """A refused message is answered, not fatal: the socket stays usable."""
    guest, _data, _meta = service
    with start_tcp_server(guest, timeout_s=5.0) as pair:
        refused = pair.transport.request({"type": "nope.nope", "id": "corr-1", "payload": {}})
        assert refused["payload"]["code"] == ErrorCode.UNSUPPORTED_COMMAND

        answered = pair.transport.request(Message(type=MSG_STATUS_REQUEST, id="corr-2", payload={}).to_dict())

    assert answered["type"] == "status.response"
    assert answered["id"] == "corr-2"


def test_tcp_pair_close_is_idempotent_and_drops_the_link(service):
    guest, _data, _meta = service
    pair = start_tcp_server(guest, timeout_s=5.0)
    pair.close()

    pair.close()  # must not raise

    with pytest.raises(OSError):
        pair.transport.request(Message(type=MSG_STATUS_REQUEST, id="corr-1", payload={}).to_dict())
