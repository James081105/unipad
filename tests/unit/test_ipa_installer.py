"""Install orchestration tests (PRD 9.1, 10; PRD 5.2 steps 3-4).

``unipad.ipa.installer.IpaInstaller`` drives one IPA through the PRD 9.1
stages -- ``received`` -> ``checked`` -> ``transferred`` -> ``installed`` ->
``registered`` -- and returns a :class:`unipad.models.InstallResult` whose
``stage`` is the furthest stage *actually completed*.

The load-bearing rule of this whole chain (PRD 9.1, quoted in the PRD's
command contract: "文件传输成功不能返回 ``installed``") is pinned by
:func:`test_transfer_only_channel_never_reports_installed`: a channel that
delivers the package perfectly but never confirms an install must leave the
result at ``transferred`` or ``failed`` -- **never** ``installed``.  Everything
else in this file exists to make that rule observable from several angles.

Also pinned here:

* every guest-reported error identifier of the PRD 10 taxonomy maps through
  to ``stage='failed'`` with the same stable code (no invented successes, no
  swallowed codes);
* each stage transition persists ``install-record.json`` plus an append-only
  ``install-events.jsonl`` journal into the run directory;
* ``InstallResult.ok`` is true only for a guest-confirmed install with no
  error code;
* a precheck rejection short-circuits *before* any bytes are transferred;
* ``trust_mode`` is whatever the guest reported -- never assumed.

The guest channel is duck-typed (``send_package`` + ``request_install``), so
the stages run on Windows without an iPad, a socket or an install service
(docs/testing-strategy.md D3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.ipa import IpaInstaller, LoopbackTransport, run_precheck
from unipad.ipa.installer import EVENTS_NAME, RECORD_NAME, GuestClientChannel
from unipad.ipa.package import open_ipa
from unipad.models import InstallResult
from tests.fixtures.fake_guest_channel import RecordingGuestChannel
from tests.fixtures.ipa_builder import build_ipa, hello_ipa
from tests.fixtures.macho_builder import build_thin_macho

#: Every guest-reported error identifier of the PRD 10 taxonomy that the
#: installer must map through verbatim (stage='failed').
GUEST_ERROR_CODES = (
    "signature_or_policy_rejected",
    "install_service_unavailable",
    "registration_failed",
    "missing_dependency",
    "dyld_failed",
)

#: A guest reply that means "installed and registered" (the furthest stage).
REGISTERED_REPLY = {
    "status": "registered",
    "installed": True,
    "registered": True,
    "trust_mode": "self-signed-test",
}


class FakeStore:
    """Duck-typed experiment store (PRD 7.3 journal hook)."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.recorded: list[InstallResult] = []

    def record_install(self, result: InstallResult) -> None:
        self.recorded.append(result)


def _stage_sequence(run_dir: Path) -> list[str]:
    """Return the stage names written to the append-only events journal."""
    events = (run_dir / EVENTS_NAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["stage"] for line in events if line.strip()]


def _read_record(run_dir: Path) -> dict:
    """Return the persisted install record as a mapping."""
    return json.loads((run_dir / RECORD_NAME).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# happy path: the full PRD 9.1 stage progression
# --------------------------------------------------------------------------


def test_full_stage_progression_to_registered(tmp_path):
    """PRD 9.1: received -> checked -> transferred -> installed -> registered."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel(REGISTERED_REPLY)
    run_dir = tmp_path / "run"
    installer = IpaInstaller(channel, run_dir=run_dir)

    # act
    result = installer.install(ipa)

    # assert -- the furthest stage actually completed
    assert result.stage == "registered"
    assert result.ok is True
    assert result.error_code is None
    assert result.error_message is None
    assert result.bundle_id == "com.unipad.hello"
    assert result.package_sha256 == open_ipa(ipa).sha256
    assert result.trust_mode == "self-signed-test"
    assert result.log_path == str(run_dir / RECORD_NAME)

    # ... and every stage was walked, in order
    stages = _stage_sequence(run_dir)
    canonical = ["received", "checked", "transferred", "installed", "registered"]
    assert [canonical.index(stage) for stage in stages] == sorted(
        canonical.index(stage) for stage in stages
    )
    assert stages[0] == "received"
    assert "checked" in stages
    assert "transferred" in stages
    assert stages[-1] == "registered"


def test_stage_stops_at_installed_when_the_guest_does_not_register(tmp_path):
    """``installed`` and ``registered`` are separate guest confirmations."""
    # arrange -- the guest confirms the install but not the icon registration
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel({"status": "installed", "installed": True})
    run_dir = tmp_path / "run"

    # act
    result = IpaInstaller(channel, run_dir=run_dir).install(ipa)

    # assert
    assert result.stage == "installed"
    assert result.ok is True
    assert _read_record(run_dir)["stage"] == "installed"
    assert _stage_sequence(run_dir)[-1] == "installed"
    # the registration stage was never reached
    assert "registered" not in _stage_sequence(run_dir)


def test_transfer_receipt_is_handed_to_the_guest(tmp_path):
    """The guest sees the verified transfer evidence, not a bare name."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel(REGISTERED_REPLY)

    # act
    IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert len(channel.install_requests) == 1
    meta = channel.install_requests[0]
    assert meta["package_sha256"] == open_ipa(ipa).sha256
    assert meta["size_bytes"] == ipa.stat().st_size
    assert meta["transfer"]["verified"] is True
    assert meta["transfer"]["sha256"] == meta["package_sha256"]
    assert meta["transfer"]["chunk_size"] == 1024 * 1024


def test_bundle_meta_matches_the_install_request_contract(tmp_path):
    """PRD 9.1: package hash, bundle id, stage, trust mode, error, log path."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel(REGISTERED_REPLY)

    # act
    IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    meta = channel.install_requests[0]
    assert meta["package_name"] == "HelloUniPad.ipa"
    assert meta["bundle_id"] == "com.unipad.hello"
    assert meta["declared_min_os"] == "17.0"
    assert meta["app_dir"] == "Payload/HelloUniPad.app"
    assert meta["executable"] == "Payload/HelloUniPad.app/HelloUniPad"
    assert meta["supported_platforms"] == ["iPhoneOS"]
    assert meta["linked_dylibs"][0] == "/System/Library/Frameworks/UIKit.framework/UIKit"


# --------------------------------------------------------------------------
# HARD RULE (PRD 9.1): transfer success is not ``installed``
# --------------------------------------------------------------------------


def test_transfer_only_channel_never_reports_installed(tmp_path):
    """PRD 9.1: 文件传输成功不能返回 ``installed``.

    The channel delivers the package and its digest verifies perfectly, but it
    never confirms an install.  The furthest honest stage is ``transferred``
    (or ``failed``); ``installed``/``registered`` may only be reported by the
    guest install service.
    """
    # arrange -- a receiver that accepts the bytes and says nothing usable
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel()          # default reply is non-confirming
    run_dir = tmp_path / "run"

    # act
    result = IpaInstaller(channel, run_dir=run_dir).install(ipa)

    # assert -- the transfer really happened and really verified
    assert len(channel.sent_packages) == 1
    assert channel.sent_packages[0]["sha256"] == open_ipa(ipa).sha256
    assert channel.transferred_bytes == ipa.stat().st_size
    # ... and yet no install may be claimed
    assert result.stage in {"transferred", "failed"}
    assert result.stage != "installed"
    assert result.stage != "registered"
    assert result.ok is False
    assert result.trust_mode is None
    assert _read_record(run_dir)["stage"] == result.stage
    assert _read_record(run_dir)["stage"] != "installed"
    # The journal must not contain an install/registration transition either:
    # a verified file transfer is the whole story here (PRD 9.1).
    journal = _stage_sequence(run_dir)
    assert "installed" not in journal
    assert "registered" not in journal
    assert "transferred" in journal


def test_channel_that_raises_after_transfer_is_failed(tmp_path):
    """A guest crash after a verified transfer is ``failed``, not installed."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel(
        raise_exception=UniPadError(
            ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
            "install service crashed",
            reason="service_crash",
        )
    )

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.stage != "installed"
    assert result.error_code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert len(channel.sent_packages) == 1


def test_unexpected_guest_crash_is_failed_with_a_mapped_code(tmp_path):
    """A non-UniPadError guest bug maps to a stable code, never to success."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel(raise_exception=RuntimeError("guest exploded"))

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.ok is False
    assert result.error_code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert "RuntimeError" in (result.error_message or "")


def test_loopback_transport_default_reply_never_installs(tmp_path):
    """The shipped fake's default reply cannot be mistaken for a success."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    transport = LoopbackTransport()            # install_reply = unavailable

    # act
    result = IpaInstaller(transport, run_dir=tmp_path / "run").install(ipa)

    # assert -- the file transfer verified, the install did not happen
    assert ipa.name in transport.received
    assert result.stage == "failed"
    assert result.stage != "installed"
    assert result.error_code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE


def test_unrecognised_guest_reply_is_failed_not_successful(tmp_path):
    """Only an explicit installed/registered marker counts as success."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel({"status": "maybe", "progress": 40})

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.ok is False
    assert result.error_code == ErrorCode.BLOCKED


def test_empty_guest_reply_is_failed(tmp_path):
    """No reply at all is a failure with a concrete reason."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")

    class SilentChannel(RecordingGuestChannel):
        def request_install(self, bundle_meta):
            self.install_requests.append(dict(bundle_meta))
            return None

    # act
    result = IpaInstaller(SilentChannel(), run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.error_code == ErrorCode.BLOCKED
    assert "no recognisable reply" in (result.error_message or "")


def test_json_string_guest_reply_is_normalised(tmp_path):
    """The real guest speaks JSON Lines, so a string reply must decode."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel(
        reply_json=json.dumps(
            {"status": "registered", "registered": True, "trust_mode": "guest-trust"}
        )
    )

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "registered"
    assert result.ok is True
    assert result.trust_mode == "guest-trust"


# --------------------------------------------------------------------------
# fault taxonomy (PRD 10)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("guest_code", GUEST_ERROR_CODES)
def test_guest_error_codes_map_through_with_stage_failed(tmp_path, guest_code):
    """PRD 10: every guest-reported code arrives as stage=failed + same code."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel({"status": "error", "error_code": guest_code})

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.ok is False
    assert result.error_code == guest_code
    assert guest_code in (result.error_message or "")
    assert result.trust_mode is None
    # ... and the failure is persisted with the same code
    assert _read_record(tmp_path / "run")["error_code"] == guest_code


@pytest.mark.parametrize(
    "guest_code,expected",
    [
        ("signature_rejected", ErrorCode.SIGNATURE_OR_POLICY_REJECTED),
        ("policy_rejected", ErrorCode.SIGNATURE_OR_POLICY_REJECTED),
        ("untrusted", ErrorCode.SIGNATURE_OR_POLICY_REJECTED),
        ("no_service", ErrorCode.INSTALL_SERVICE_UNAVAILABLE),
        ("timeout", ErrorCode.INSTALL_SERVICE_UNAVAILABLE),
        ("denied", ErrorCode.SIGNATURE_OR_POLICY_REJECTED),
    ],
)
def test_guest_error_aliases_map_onto_the_stable_taxonomy(tmp_path, guest_code, expected):
    """Guest spellings collapse onto the PRD 10 codes, never onto success."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel({"status": "error", "error_code": guest_code})

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.error_code == expected


def test_unknown_guest_code_is_blocked_not_guessed(tmp_path):
    """An unrecognised guest code is recorded as blocked (PRD 7.4)."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel({"status": "error", "error_code": "wat_is_this"})

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.error_code == ErrorCode.BLOCKED


def test_transfer_failure_is_failed_with_transfer_failed(tmp_path):
    """A tampered receiver during an install is ``transfer_failed``."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    transport = LoopbackTransport(corrupt_receiver_hash=True)

    # act
    result = IpaInstaller(transport, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.error_code == ErrorCode.TRANSFER_FAILED
    assert transport.received[ipa.name] == ipa.read_bytes()


# --------------------------------------------------------------------------
# precheck short-circuit (PRD 5.2 step 10 / 10 "包预检")
# --------------------------------------------------------------------------


def test_precheck_rejection_short_circuits_before_any_transfer(tmp_path):
    """An unsupported package never reaches the guest channel."""
    # arrange -- a FairPlay-encrypted binary (PRD 5.1: samples must be DRM-free)
    ipa = build_ipa(tmp_path / "encrypted.ipa", corrupt="encrypted")
    channel = RecordingGuestChannel(REGISTERED_REPLY)
    run_dir = tmp_path / "run"

    # act
    result = IpaInstaller(channel, run_dir=run_dir).install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.ok is False
    assert result.error_code == ErrorCode.UNSUPPORTED_PLATFORM
    assert "encrypted_binary" in (result.error_message or "")
    assert result.package_sha256 == open_ipa(ipa).sha256
    # nothing was transferred and nothing was asked of the guest
    assert channel.sent_packages == []
    assert channel.install_requests == []
    assert channel.transferred_bytes == 0
    # the failure is still persisted for the run journal
    assert _read_record(run_dir)["stage"] == "failed"
    assert _read_record(run_dir)["error_code"] == ErrorCode.UNSUPPORTED_PLATFORM


def test_simulator_package_short_circuits_before_transfer(tmp_path):
    """A Simulator build is rejected before any bytes leave the host."""
    # arrange
    ipa = build_ipa(tmp_path / "sim.ipa", corrupt="simulator")
    channel = RecordingGuestChannel(REGISTERED_REPLY)

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.error_code == ErrorCode.UNSUPPORTED_ARCH
    assert channel.sent_packages == []
    assert channel.install_requests == []


def test_caller_supplied_precheck_is_reused(tmp_path):
    """A caller that already prechecked is not made to hash/unzip twice."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel(REGISTERED_REPLY)
    installer = IpaInstaller(channel, run_dir=tmp_path / "run")
    report = run_precheck(ipa)

    # act
    result = installer.install(ipa, precheck=report)

    # assert
    assert result.stage == "registered"
    assert len(channel.install_requests) == 1


def test_broken_package_is_failed_before_the_channel_is_used(tmp_path):
    """A structurally broken package fails at the ``received`` stage."""
    # arrange
    ipa = build_ipa(tmp_path / "bad.ipa", corrupt="no_payload")
    channel = RecordingGuestChannel(REGISTERED_REPLY)

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.error_code == ErrorCode.INVALID_PACKAGE
    assert result.package_sha256 == ""          # nothing could be opened
    assert result.bundle_id is None
    assert channel.sent_packages == []


def test_missing_package_is_failed_with_invalid_package(tmp_path):
    """A package that does not exist is ``invalid_package``, not a crash."""
    # arrange
    channel = RecordingGuestChannel(REGISTERED_REPLY)

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(
        tmp_path / "absent.ipa"
    )

    # assert
    assert result.stage == "failed"
    assert result.error_code == ErrorCode.INVALID_PACKAGE
    assert result.error_message
    assert channel.sent_packages == []


# --------------------------------------------------------------------------
# persistence (PRD 9.1 / 7.3)
# --------------------------------------------------------------------------


def test_install_record_is_persisted_per_stage(tmp_path):
    """Every stage transition persists a record plus a journal line."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    run_dir = tmp_path / "run"

    # act
    result = IpaInstaller(
        RecordingGuestChannel(REGISTERED_REPLY), run_dir=run_dir
    ).install(ipa)

    # assert -- the record file holds the final stage...
    record = _read_record(run_dir)
    assert record["stage"] == result.stage == "registered"
    assert set(record) == set(InstallResult(package_sha256="").to_dict())
    assert record["bundle_id"] == "com.unipad.hello"
    assert record["package_sha256"] == result.package_sha256
    assert record["error_code"] is None

    # ... and the append-only journal holds one line per transition
    lines = (run_dir / EVENTS_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 5
    for line in lines:
        event = json.loads(line)
        assert set(event) == {
            "at",
            "stage",
            "bundle_id",
            "package_sha256",
            "error_code",
            "error_message",
        }
        assert event["at"]                       # ISO-8601 evidence stamp


def test_failed_install_record_carries_the_fault(tmp_path):
    """A failed stage is persisted with its stable error code."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel(
        {"status": "error", "error_code": "registration_failed"}
    )

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    record = _read_record(tmp_path / "run")
    assert record["stage"] == "failed"
    assert record["error_code"] == ErrorCode.REGISTRATION_FAILED
    assert record["error_message"] == result.error_message


def test_store_receives_every_stage_transition(tmp_path):
    """The experiment journal hook sees each transition (PRD 7.3)."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    store = FakeStore(tmp_path / "run")

    # act
    result = IpaInstaller(RecordingGuestChannel(REGISTERED_REPLY), store=store).install(ipa)

    # assert
    stages = [entry.stage for entry in store.recorded]
    assert stages[0] == "received"
    assert stages[-1] == result.stage == "registered"
    assert "transferred" in stages
    assert len(stages) >= 5


def test_run_dir_comes_from_the_store_when_not_explicit(tmp_path):
    """``store.run_dir`` resolves where install evidence is written."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    store = FakeStore(tmp_path / "from-store")

    # act
    installer = IpaInstaller(RecordingGuestChannel(REGISTERED_REPLY), store=store)
    result = installer.install(ipa)

    # assert
    assert installer.run_dir == tmp_path / "from-store"
    assert _read_record(tmp_path / "from-store")["stage"] == result.stage


def test_without_a_run_dir_the_result_is_still_authoritative(tmp_path):
    """Persistence is best-effort; the returned contract never depends on it."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")

    # act
    installer = IpaInstaller(RecordingGuestChannel(REGISTERED_REPLY))
    result = installer.install(ipa)

    # assert
    assert installer.run_dir is None
    assert result.stage == "registered"
    assert result.ok is True
    assert result.log_path is None


# --------------------------------------------------------------------------
# InstallResult semantics (PRD 9.1)
# --------------------------------------------------------------------------


def test_install_result_ok_semantics(tmp_path):
    """``ok`` means a guest-confirmed install *and* no error code."""
    # arrange / act -- a registered install
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    registered = IpaInstaller(
        RecordingGuestChannel(REGISTERED_REPLY), run_dir=tmp_path / "r1"
    ).install(ipa)
    installed = IpaInstaller(
        RecordingGuestChannel({"status": "installed", "installed": True}),
        run_dir=tmp_path / "r2",
    ).install(ipa)
    failed = IpaInstaller(
        RecordingGuestChannel({"status": "error", "error_code": "dyld_failed"}),
        run_dir=tmp_path / "r3",
    ).install(ipa)

    # assert
    assert registered.ok is True
    assert installed.ok is True
    assert failed.ok is False
    # a bare successful transfer is not ``ok`` either (PRD 9.1)
    transferred_only = InstallResult(package_sha256="a" * 64, stage="transferred")
    assert transferred_only.ok is False
    # ... and a stage of ``installed`` *with* an error code is not ``ok``
    contradictory = InstallResult(
        package_sha256="a" * 64, stage="installed", error_code="dyld_failed"
    )
    assert contradictory.ok is False


def test_install_result_to_dict_round_trips(tmp_path):
    """PRD 9.1: the result is JSON for the controller and the verifier."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")

    # act
    result = IpaInstaller(
        RecordingGuestChannel(REGISTERED_REPLY), run_dir=tmp_path / "run"
    ).install(ipa)
    payload = json.loads(json.dumps(result.to_dict(), sort_keys=True))

    # assert
    assert payload["stage"] == "registered"
    assert payload["bundle_id"] == "com.unipad.hello"
    assert payload["trust_mode"] == "self-signed-test"
    assert payload["error_code"] is None
    assert payload["package_sha256"] == open_ipa(ipa).sha256
    assert payload["log_path"].endswith(RECORD_NAME)


# --------------------------------------------------------------------------
# trust and signature declarations (PRD 9.1 "信任方式")
# --------------------------------------------------------------------------


def test_trust_mode_is_never_invented(tmp_path):
    """A guest that does not report a trust mode leaves the field empty."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel({"status": "installed", "installed": True})

    # act
    result = IpaInstaller(channel, run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "installed"
    assert result.trust_mode is None
    assert _read_record(tmp_path / "run")["trust_mode"] is None


def test_signature_mode_reports_what_the_macho_shows(tmp_path):
    """LC_CODE_SIGNATURE presence is an observation, not a trust claim."""
    # arrange -- a signed image and an unsigned one
    signed = hello_ipa(tmp_path / "signed.ipa")
    unsigned = build_ipa(
        tmp_path / "unsigned.ipa",
        macho_bytes=build_thin_macho(has_code_signature=False),
    )
    signed_channel = RecordingGuestChannel(REGISTERED_REPLY)
    unsigned_channel = RecordingGuestChannel(REGISTERED_REPLY)

    # act
    IpaInstaller(signed_channel, run_dir=tmp_path / "r1").install(signed)
    IpaInstaller(unsigned_channel, run_dir=tmp_path / "r2").install(unsigned)

    # assert
    assert signed_channel.install_requests[0]["signature_mode"] == "code-signature-present"
    assert unsigned_channel.install_requests[0]["signature_mode"] == "unsigned"


def test_caller_declared_signature_mode_wins(tmp_path):
    """A host declaration is passed through, not overridden by the parser."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    channel = RecordingGuestChannel(REGISTERED_REPLY)

    # act
    IpaInstaller(channel, run_dir=tmp_path / "run").install(
        ipa, signature_mode="host-declared-unsigned"
    )

    # assert
    assert channel.install_requests[0]["signature_mode"] == "host-declared-unsigned"


# --------------------------------------------------------------------------
# channel adaptation (duck-typing, PRD 9.1)
# --------------------------------------------------------------------------


def test_guest_client_without_a_byte_channel_cannot_transfer(tmp_path):
    """A control message alone is never a verified package delivery."""
    # arrange -- a ``GuestClient``-shaped object with no byte channel
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    delivered: list[tuple[int, dict]] = []

    class StubGuestClient:
        def install_package(self, data, meta):
            delivered.append((len(data), dict(meta)))
            return {"status": "registered", "registered": True}

    # act
    result = IpaInstaller(StubGuestClient(), run_dir=tmp_path / "run").install(ipa)

    # assert
    assert result.stage == "failed"
    assert result.error_code == ErrorCode.TRANSFER_FAILED
    assert delivered == []          # the guest was never asked to install


def test_guest_client_with_a_byte_channel_completes(tmp_path):
    """The same client completes once a byte channel is attached."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    delivered: list[tuple[int, dict]] = []

    class StubGuestClient:
        def install_package(self, data, meta):
            delivered.append((len(data), dict(meta)))
            return {"status": "registered", "registered": True, "trust_mode": "guest"}

    installer = IpaInstaller(
        StubGuestClient(),
        byte_channel=LoopbackTransport(),
        run_dir=tmp_path / "run",
    )

    # act
    result = installer.install(ipa)

    # assert
    assert result.stage == "registered"
    assert result.ok is True
    assert result.trust_mode == "guest"
    assert delivered[0][0] == ipa.stat().st_size


def test_guest_client_channel_reports_an_unverified_receipt():
    """Without a byte channel the adapter says "unverified" out loud."""
    # arrange
    adapter = GuestClientChannel(object(), byte_channel=None)

    # act
    receipt = adapter.send_package("p.ipa", b"package bytes")

    # assert
    assert receipt.verified is False
    assert receipt.sha256 == ""
    assert receipt.size_bytes == len(b"package bytes")


class BrokenStore:
    """A store that cannot record -- the realistic failure is a full disk."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.attempts = 0

    def record_install(self, result: InstallResult) -> None:
        self.attempts += 1
        raise OSError("No space left on device")


def test_a_store_that_cannot_record_makes_the_failure_visible(tmp_path):
    """A silently dropped history entry is a lost audit trail (PRD 3.2 P1 (3)).

    The install itself has already happened, so the run must not be reported as
    a failure; but the record that the history is incomplete has to survive
    somewhere the evidence bundle and the verifier read.
    """
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    store = BrokenStore(tmp_path / "run")

    # act
    result = IpaInstaller(RecordingGuestChannel(REGISTERED_REPLY), store=store).install(ipa)

    # assert: the install still succeeded, and the gap is recorded
    assert result.stage == "registered"
    assert result.error_code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert "install history could not be recorded" in result.error_message
    assert "OSError" in result.error_message
    assert store.attempts >= 1
