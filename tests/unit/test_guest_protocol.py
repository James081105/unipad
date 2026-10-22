"""Unit tests for :mod:`unipad.guest.protocol` (PRD 9.1, 10; M10/M10a).

PRD 9.1 fixes the install interface contract: ``unipad install <path-to-ipa>``
must return at least the package hash, the bundle ID, the current stage, the
trust mode, the error type and the log location, and *a successful file
transfer must never report* ``installed``.  PRD 10 fixes the fault taxonomy the
guest reports.  This module is the wire vocabulary both sides speak, so these
tests pin:

* every message shape round-trips through JSON Lines (one object, one line);
* a malformed or **unknown** message is surfaced as a structured
  :class:`~unipad.errors.UniPadError` carrying a stable code -- never silently
  dropped, never guessed at;
* the stage vocabulary and the failure-code table stay aligned with PRD 10.

Simulation only (docs/testing-strategy.md D3): nothing here touches QEMU, a
real guest, the network or the wall clock.  Field verification of the real
guest install service stays ``UNVERIFIED`` -- these tests prove the host-side
contract, not that any guest exists.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.protocol import (
    FAILURE_CODES,
    KNOWN_TYPES,
    MSG_CHALLENGE_REQUEST,
    MSG_CHALLENGE_RESPONSE,
    MSG_ERROR,
    MSG_INSTALL_PROGRESS,
    MSG_INSTALL_REQUEST,
    MSG_INSTALL_RESPONSE,
    MSG_STATUS_REQUEST,
    MSG_STATUS_RESPONSE,
    STAGES,
    ChallengeRequest,
    ChallengeResponse,
    InstallProgress,
    InstallRequest,
    InstallResponse,
    Message,
    StatusRequest,
    StatusResponse,
    decode,
    encode,
    error_message,
    new_message_id,
)
from tests.fixtures.ipa_builder import hello_ipa

# --- PRD 10 taxonomy codes the install chain may report (PRD section 10 table)
INSTALL_CHAIN_CODES = {
    "unsupported_platform",
    "unsupported_arch",
    "invalid_package",
    "transfer_failed",
    "signature_or_policy_rejected",
    "install_service_unavailable",
    "registration_failed",
    "missing_dependency",
    "dyld_failed",
    "render_failed",
    "input_failed",
    "persistence_failed",
}


def _real_ipa_request(tmp_path) -> tuple[InstallRequest, bytes]:
    """Arrange: a real-shaped Device IPA plus the metadata that describes it.

    Reuses the shared builder instead of hand-rolling a zip, so the shapes the
    protocol must carry are the shapes the rest of the suite already trusts.
    """
    ipa_path = hello_ipa(tmp_path / "HelloUniPad.ipa")
    data = ipa_path.read_bytes()
    request = InstallRequest(
        package_name=ipa_path.name,
        package_sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        declared_min_os="17.0",
        signature_mode="self",
    )
    return request, data


def _payload_round_trip(payload) -> Message:
    """Act: push one payload through the JSON Lines wire and read the frame back.

    The envelope type is irrelevant to the payload shapes under test; each test
    re-parses ``message.payload`` with the dataclass it cares about.
    """
    return decode(encode(Message(type=MSG_INSTALL_REQUEST, id="corr-1", payload=payload)))


# ---------------------------------------------------------------------------
# InstallRequest / InstallProgress / InstallResponse
# ---------------------------------------------------------------------------


def test_install_request_round_trips_through_json_lines(tmp_path):
    """PRD 9.1: the install request carries package hash, size, min OS, mode."""
    request, data = _real_ipa_request(tmp_path)

    message = _payload_round_trip(request.to_dict())

    assert message.type == MSG_INSTALL_REQUEST
    assert message.payload == request.to_dict()
    assert InstallRequest.from_dict(message.payload) == request
    assert InstallRequest.from_dict(message.payload).package_sha256 == hashlib.sha256(data).hexdigest()


def test_install_request_ignores_extra_diagnostic_keys(tmp_path):
    """PRD 9.1: extra diagnostics must not widen or break the frozen contract.

    ``unipad.ipa.installer`` hands the guest a superset of these five keys; a
    sixth key must be ignored rather than rejected or silently invented.
    """
    request, _data = _real_ipa_request(tmp_path)
    payload = dict(request.to_dict())
    payload["bundle_id"] = "com.unipad.hello"
    payload["transfer"] = {"hash": "deadbeef"}

    decoded = InstallRequest.from_dict(_payload_round_trip(payload).payload)

    assert decoded == request
    assert "bundle_id" not in decoded.to_dict()


def test_install_progress_round_trips_and_only_names_real_stages():
    """PRD 9.1: progress is informational; it may never claim a success stage."""
    progress = InstallProgress(stage="transferred", detail="4096 bytes verified")

    decoded = InstallProgress.from_dict(_payload_round_trip(progress.to_dict()).payload)

    assert decoded == progress
    assert decoded.stage in STAGES
    assert decoded["stage"] == "transferred"


def test_install_response_success_round_trip_records_trust_mode():
    """PRD 9.1: a registered install reports the trust mode it was given."""
    response = InstallResponse(stage="registered", bundle_id="com.unipad.hello", trust_mode="self-signed-dev-trust")

    decoded = InstallResponse.from_dict(_payload_round_trip(response.to_dict()).payload)

    assert decoded == response
    assert decoded.ok is True
    assert decoded.trust_mode == "self-signed-dev-trust"
    assert decoded.error_code is None


def test_install_response_failure_round_trip_keeps_code_verbatim():
    """PRD 10: guest codes pass through verbatim -- never re-mapped or invented."""
    response = InstallResponse(
        stage="failed",
        bundle_id="com.unipad.hello",
        error_code=ErrorCode.DYLD_FAILED,
        error_message="entry point did not execute",
    )

    decoded = InstallResponse.from_dict(_payload_round_trip(response.to_dict()).payload)

    assert decoded == response
    assert decoded.ok is False
    assert decoded.error_code == "dyld_failed"
    assert decoded.stage == "failed"


@pytest.mark.parametrize("stage", ["received", "checked", "transferred"])
def test_transfer_only_stage_is_never_ok(stage):
    """PRD 9.1: "文件传输成功不能返回 installed" -- enforced in the contract itself."""
    response = InstallResponse(stage=stage, bundle_id="com.unipad.hello")

    assert response.ok is False


def test_challenge_round_trips_nonce_and_value():
    """PRD 5.2 step 5: the challenge answer must be attributable to this run."""
    request = ChallengeRequest(nonce="run-42-nonce")
    response = ChallengeResponse(nonce="run-42-nonce", value="a" * 64)

    decoded_request = ChallengeRequest.from_dict(_payload_round_trip(request.to_dict()).payload)
    decoded_response = ChallengeResponse.from_dict(_payload_round_trip(response.to_dict()).payload)

    assert decoded_request == request
    assert decoded_response == response
    assert decoded_response.nonce == "run-42-nonce"


def test_status_request_has_an_empty_payload():
    """M9b/M12: the status query carries no fields, and none are fabricated."""
    decoded = StatusRequest.from_dict(_payload_round_trip(StatusRequest().to_dict()).payload)

    assert decoded.to_dict() == {}


def test_status_response_round_trips_apps_and_markers():
    """M9b/M12: installed apps plus Data-volume markers survive the wire."""
    response = StatusResponse(
        installed=[
            {"bundle_id": "com.unipad.hello", "version": "1.0", "icon": True, "trust_mode": "self-signed-dev-trust"},
            {"bundle_id": "com.unipad.notes", "version": "1.0", "icon": True, "trust_mode": None},
        ],
        data_markers=["com.unipad.notes:Documents/container-data.txt"],
    )

    decoded = StatusResponse.from_dict(_payload_round_trip(response.to_dict()).payload)

    assert decoded == response
    assert [app["bundle_id"] for app in decoded.installed] == ["com.unipad.hello", "com.unipad.notes"]
    assert decoded.data_markers == ["com.unipad.notes:Documents/container-data.txt"]


# ---------------------------------------------------------------------------
# JSON Lines framing
# ---------------------------------------------------------------------------


def test_encoded_message_is_exactly_one_utf8_line(tmp_path):
    """One message == one line: a payload with newlines must not split the frame."""
    request, _data = _real_ipa_request(tmp_path)
    payload = dict(request.to_dict())
    payload["package_name"] = "Hello\nUniPad\r\n.ipa"

    line = encode(Message(type=MSG_INSTALL_REQUEST, id="corr-1", payload=payload))

    assert isinstance(line, bytes)
    assert b"\n" not in line and b"\r" not in line
    assert line.endswith(b"}")


def test_unicode_payload_survives_the_payload_round_trip():
    """Determinism: no locale-dependent escaping; UTF-8 in, UTF-8 out."""
    progress = InstallProgress(stage="installed", detail="容器已建立 / 图标已注册")

    decoded = InstallProgress.from_dict(decode(encode(Message(
        type=MSG_INSTALL_PROGRESS, id="corr-1", payload=progress.to_dict()
    ))).payload)

    assert decoded.detail == "容器已建立 / 图标已注册"


def test_decode_accepts_bytes_and_text():
    """Both wire forms decode; neither mutates the envelope."""
    envelope = Message(type=MSG_STATUS_REQUEST, id="corr-1", payload={})

    from_bytes = decode(encode(envelope))
    from_text = decode(encode(envelope).decode("utf-8"))

    assert from_bytes.to_dict() == envelope.to_dict()
    assert from_text.to_dict() == envelope.to_dict()


def test_decode_accepts_an_envelope_without_a_payload():
    """``payload`` is optional on the wire and defaults to an empty object."""
    line = b'{"id":"corr-1","type":"status.request"}'

    message = decode(line)

    assert message.type == MSG_STATUS_REQUEST
    assert message.payload == {}


def test_decode_rejects_unknown_message_type():
    """PRD 9.1/10: an unknown type is a structured error, never a silent drop."""
    line = encode(Message(type="install.bogus", id="corr-1", payload={}))

    with pytest.raises(UniPadError) as excinfo:
        decode(line)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_COMMAND
    assert excinfo.value.details["message_type"] == "install.bogus"
    # Control: every real type still decodes, so the guard is not over-broad.
    for known in (MSG_INSTALL_REQUEST, MSG_INSTALL_RESPONSE, MSG_INSTALL_PROGRESS,
                  MSG_CHALLENGE_REQUEST, MSG_CHALLENGE_RESPONSE, MSG_STATUS_REQUEST,
                  MSG_STATUS_RESPONSE, MSG_ERROR):
        assert decode(encode(Message(type=known, id="corr-1", payload={}))).type == known


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"   ",
        b"not json at all",
        b"[1, 2, 3]",
        b'"a bare string"',
        b"null",
        b'{"id": "corr-1", "payload": {}}',
        b'{"type": "", "id": "corr-1", "payload": {}}',
        b'{"type": 7, "id": "corr-1", "payload": {}}',
        b'{"type": "status.request", "payload": {}}',
        b'{"type": "status.request", "id": 7, "payload": {}}',
        b'{"type": "status.request", "id": "", "payload": {}}',
        b'{"type": "status.request", "id": "corr-1", "payload": []}',
        b'{"type": "status.request", "id": "corr-1", "payload": null}',
        b'{"type": "status.request", "id": "corr-1", "payload": "x"}',
    ],
)
def test_decode_rejects_malformed_lines_with_a_stable_code(line):
    """A malformed frame is diagnosable: stable code, never a bare ValueError."""
    with pytest.raises(UniPadError) as excinfo:
        decode(line)

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_decode_rejects_non_utf8_bytes():
    """Invalid UTF-8 on the control channel is a protocol error, not a crash."""
    with pytest.raises(UniPadError) as excinfo:
        decode(b'{"type": "status.request", "id": "\xff\xfe", "payload": {}}')

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


# ---------------------------------------------------------------------------
# payload-level validation (no fabrication, no guessing)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"package_name": "HelloUniPad.ipa"},
        {"package_name": "HelloUniPad.ipa", "package_sha256": "a" * 64},
        {"package_name": "HelloUniPad.ipa", "package_sha256": "a" * 64, "size_bytes": 10},
        {"package_name": "HelloUniPad.ipa", "package_sha256": "a" * 64, "size_bytes": 10, "declared_min_os": "17.0"},
        {
            "package_name": "HelloUniPad.ipa",
            "package_sha256": "a" * 64,
            "size_bytes": "10",
            "declared_min_os": "17.0",
            "signature_mode": "self",
        },
        {
            "package_name": "HelloUniPad.ipa",
            "package_sha256": "a" * 64,
            "size_bytes": True,
            "declared_min_os": "17.0",
            "signature_mode": "self",
        },
        {
            "package_name": "",
            "package_sha256": "a" * 64,
            "size_bytes": 10,
            "declared_min_os": "17.0",
            "signature_mode": "self",
        },
    ],
)
def test_install_request_rejects_incomplete_or_wrongly_typed_fields(payload):
    """PRD 9.1: every field is required and typed; nothing is defaulted in."""
    with pytest.raises(UniPadError) as excinfo:
        InstallRequest.from_dict(payload)

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_install_progress_requires_a_stage():
    with pytest.raises(UniPadError) as excinfo:
        InstallProgress.from_dict({"detail": "no stage"})

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_install_response_requires_a_stage():
    with pytest.raises(UniPadError) as excinfo:
        InstallResponse.from_dict({"bundle_id": "com.unipad.hello"})

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_challenge_response_requires_nonce_and_value():
    for payload in ({"nonce": "n"}, {"value": "v"}, {}):
        with pytest.raises(UniPadError) as excinfo:
            ChallengeResponse.from_dict(payload)
        assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


@pytest.mark.parametrize(
    "payload",
    [
        {"installed": "nope", "data_markers": []},
        {"installed": [1, 2], "data_markers": []},
        {"installed": [{"bundle_id": "com.unipad.hello"}], "data_markers": "x"},
        {"installed": [], "data_markers": [1]},
    ],
)
def test_status_response_rejects_non_conforming_shapes(payload):
    """M9b/M12: a status reply that is not the documented shape is an error."""
    with pytest.raises(UniPadError) as excinfo:
        StatusResponse.from_dict(payload)

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


# ---------------------------------------------------------------------------
# error envelopes, taxonomy, mapping access
# ---------------------------------------------------------------------------


def test_error_message_envelope_round_trips_the_code():
    """PRD 10: the guest's own code travels in an ``error`` envelope verbatim."""
    envelope = error_message(ErrorCode.MISSING_DEPENDENCY, "required dependency missing", message_id="corr-1")

    decoded = decode(encode(envelope))

    assert decoded.type == MSG_ERROR
    assert decoded.id == "corr-1"
    assert decoded.payload["code"] == "missing_dependency"
    assert decoded.payload["message"] == "required dependency missing"


def test_failure_codes_match_the_prd_taxonomy():
    """PRD 10: the injection table maps only onto real taxonomy codes."""
    assert set(FAILURE_CODES.values()) <= INSTALL_CHAIN_CODES
    assert FAILURE_CODES["signature"] == ErrorCode.SIGNATURE_OR_POLICY_REJECTED
    assert FAILURE_CODES["install_service"] == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert FAILURE_CODES["registration"] == ErrorCode.REGISTRATION_FAILED
    assert FAILURE_CODES["dependency"] == ErrorCode.MISSING_DEPENDENCY
    assert FAILURE_CODES["dyld"] == ErrorCode.DYLD_FAILED
    assert FAILURE_CODES["transfer"] == ErrorCode.TRANSFER_FAILED
    assert FAILURE_CODES["invalid_package"] == ErrorCode.INVALID_PACKAGE
    # The install-chain subset of PRD 10 is covered, not just a happy path.
    assert set(FAILURE_CODES.values()) == {
        ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
        ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
        ErrorCode.REGISTRATION_FAILED,
        ErrorCode.MISSING_DEPENDENCY,
        ErrorCode.DYLD_FAILED,
        ErrorCode.TRANSFER_FAILED,
        ErrorCode.INVALID_PACKAGE,
    }


def test_stage_vocabulary_is_the_documented_closed_list():
    """PRD 9.1: the stage vocabulary is frozen; ``registered`` is the last success."""
    assert STAGES == ("received", "checked", "transferred", "installed", "registered", "failed")
    assert STAGES[-1] == "failed"
    assert "registered" in STAGES


def test_dict_view_mapping_access(tmp_path):
    """Typed APIs and mapping access must agree (callers and tests use both)."""
    request, _data = _real_ipa_request(tmp_path)
    response = InstallResponse(stage="registered", bundle_id="com.unipad.hello", trust_mode="self-signed-dev-trust")

    assert request["package_name"] == "HelloUniPad.ipa"
    assert "trust_mode" in response
    assert response.get("error_code") is None
    assert dict(response) == response.to_dict()
    assert list(response.keys()) == list(response.to_dict().keys())
    with pytest.raises(KeyError):
        response["no_such_field"]


def test_message_equality_against_a_plain_mapping():
    """A decoded envelope compares equal to the dict it came from."""
    envelope = Message(type=MSG_STATUS_REQUEST, id="corr-1", payload={})

    assert decode(encode(envelope)) == envelope.to_dict()
    assert decode(encode(envelope)).to_json() == envelope.to_json()


def test_new_message_id_is_unique_per_message():
    """Correlation ids are fresh per message (a retry must not reuse one)."""
    ids = {new_message_id() for _ in range(32)}

    assert len(ids) == 32
    assert all(len(i) == 32 and i == i.lower() for i in ids)
    assert Message(type=MSG_STATUS_REQUEST).id != Message(type=MSG_STATUS_REQUEST).id


def test_to_json_is_deterministic_and_compact():
    """Same payload -> same bytes (evidence files must be diffable)."""
    envelope = Message(type=MSG_STATUS_REQUEST, id="corr-1", payload={"b": 1, "a": 2})

    first = envelope.to_json()
    second = json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    assert first == second
    assert first == envelope.to_json()
    assert " " not in first


# ---------------------------------------------------------------------------
# the whole closed vocabulary, one round trip per type
# ---------------------------------------------------------------------------

#: One representative payload per type in :data:`KNOWN_TYPES`, paired with the
#: dataclass that models it (``None`` for the error envelope, which is a plain
#: ``{code, message}`` object).  Adding a type to the wire vocabulary without
#: adding it here fails this table, so coverage cannot silently rot.
REPRESENTATIVE_PAYLOADS: tuple[tuple[str, dict, object], ...] = (
    (
        MSG_INSTALL_REQUEST,
        {
            "package_name": "HelloUniPad.ipa",
            "package_sha256": "a" * 64,
            "size_bytes": 4096,
            "declared_min_os": "17.0",
            "signature_mode": "self",
        },
        InstallRequest,
    ),
    (MSG_INSTALL_PROGRESS, {"stage": "transferred", "detail": "4096 bytes verified"}, InstallProgress),
    (
        MSG_INSTALL_RESPONSE,
        {"stage": "registered", "bundle_id": "com.unipad.hello", "trust_mode": "self-signed-dev-trust"},
        InstallResponse,
    ),
    (MSG_CHALLENGE_REQUEST, {"nonce": "run-42-nonce"}, ChallengeRequest),
    (MSG_CHALLENGE_RESPONSE, {"nonce": "run-42-nonce", "value": "c" * 64}, ChallengeResponse),
    (MSG_STATUS_REQUEST, {}, StatusRequest),
    (
        MSG_STATUS_RESPONSE,
        {
            "installed": [{"bundle_id": "com.unipad.hello", "version": "1.0", "icon": True, "trust_mode": None}],
            "data_markers": ["com.unipad.hello:Documents/notes.txt"],
        },
        StatusResponse,
    ),
    (
        MSG_ERROR,
        {"code": ErrorCode.MISSING_DEPENDENCY, "message": "required dependency missing"},
        None,
    ),
)


@pytest.mark.parametrize(
    ("mtype", "payload", "model"),
    REPRESENTATIVE_PAYLOADS,
    ids=[entry[0] for entry in REPRESENTATIVE_PAYLOADS],
)
def test_every_message_type_round_trips_through_the_wire(mtype, payload, model):
    """PRD 9.1: every type in the closed vocabulary survives the JSON Lines wire.

    The round trip must be lossless *and* plain-JSON: no custom encoder, no
    type information beyond the envelope itself, so any peer that speaks JSON
    can read what we write.
    """
    message = Message(type=mtype, id="corr-1", payload=payload)

    wire = encode(message)

    assert isinstance(wire, bytes)
    assert wire.count(b"\n") == 0 and wire.count(b"\r") == 0
    decoded = decode(wire)
    assert decoded.type == mtype
    assert decoded.id == "corr-1"
    assert decoded.payload == payload
    assert decoded == message.to_dict()
    assert decoded.to_json() == message.to_json()
    # JSON-serializable in both directions without any custom encoder.
    assert json.loads(wire.decode("utf-8")) == message.to_dict()
    if model is not None:
        assert model.from_dict(decoded.payload) == model.from_dict(payload)


def test_the_round_trip_table_covers_the_whole_closed_vocabulary():
    """The table above is exhaustive: no wire type is left unexercised."""
    assert {entry[0] for entry in REPRESENTATIVE_PAYLOADS} == set(KNOWN_TYPES)


def test_decode_accepts_every_wire_buffer_form():
    """bytes, bytearray and str are all valid frames for the same message."""
    envelope = Message(type=MSG_STATUS_RESPONSE, id="corr-1", payload={"installed": [], "data_markers": []})
    wire = encode(envelope)

    for form in (bytes, bytearray):
        assert decode(form(wire)).to_dict() == envelope.to_dict()
    assert decode(wire.decode("utf-8")).to_dict() == envelope.to_dict()
