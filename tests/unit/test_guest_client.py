"""Unit tests for :mod:`unipad.guest.client` (PRD 9.1, 10; M10/M10a/M11/M12).

``GuestClient`` is the host-side half of the install chain.  The properties
these tests pin are the ones PRD 9.1 and 10 actually depend on:

* **install_package success** -> ``stage='registered'`` with the guest's own
  ``trust_mode`` recorded (never assumed);
* **every injected failure** surfaces the exact PRD 10 code, verbatim;
* **a guest that never answers** -> ``install_service_unavailable``.  A timeout
  is never turned into a fabricated success;
* **retries are bounded** -- the attempt count is observable, and a definitive
  guest answer is never retried;
* **challenge(nonce)** returns the deterministic derived value for this run;
* **status()** reflects what the guest service actually holds.

Everything runs against :class:`unipad.guest.fake.FakeGuestService` behind
either the in-process transport or a stub transport that injects one fault at a
time (docs/testing-strategy.md D3).  No QEMU, no network, no wall clock: field
verification of the real guest stays ``UNVERIFIED``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.client import DEFAULT_ATTEMPTS, GuestClient
from unipad.guest.fake import (
    DEFAULT_TRUST_MODE,
    FakeGuestService,
    FakeTransport,
    derive_challenge_value,
)
from unipad.guest.protocol import (
    MSG_CHALLENGE_REQUEST,
    MSG_CHALLENGE_RESPONSE,
    MSG_ERROR,
    MSG_INSTALL_REQUEST,
    MSG_INSTALL_RESPONSE,
    MSG_STATUS_REQUEST,
    MSG_STATUS_RESPONSE,
    error_message,
)
from tests.fixtures.ipa_builder import build_ipa, hello_ipa

NONCE = "run-2026-10-22-nonce"


# ---------------------------------------------------------------------------
# test doubles (fault injection is the point of these tests)
# ---------------------------------------------------------------------------


class ScriptedTransport:
    """Transport that replays one canned reply (or raises) per call.

    By default the reply's ``id`` is rewritten to the id of the request being
    answered, so a test targets *one* validation rule at a time instead of
    tripping the correlation-id check first.  Pass ``echo_id=False`` to test
    the id check itself.
    """

    def __init__(self, replies=None, *, always=None, echo_id: bool = True):
        self._replies = list(replies or [])
        self._always = always
        self._echo_id = echo_id
        self.calls: list[dict] = []

    def request(self, message: dict):
        self.calls.append(dict(message))
        if self._always is not None:
            if isinstance(self._always, Exception):
                raise self._always
            if isinstance(self._always, Mapping):
                reply = dict(self._always)
                if self._echo_id and "id" in reply:
                    reply["id"] = message["id"]
                return reply
            return self._always
        return self._replies.pop(0)


class SilentTransport:
    """A peer that accepts the message and never answers."""

    def __init__(self, exc=None):
        self.exc = exc or TimeoutError("guest never answered")
        self.calls: list[dict] = []

    def request(self, message: dict):
        self.calls.append(dict(message))
        raise self.exc


class RetryingTransport:
    """Fails the first ``fail_times`` calls, then delegates to ``inner``.

    Counts *every* attempt (``FakeTransport`` only records the ones that reach
    the service), which is what the bounded-retry assertions need.
    """

    def __init__(self, inner, *, fail_times: int = 1):
        self.inner = inner
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def request(self, message: dict):
        self.calls.append(dict(message))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("simulated transport failure")
        return self.inner.request(message)


def _install_meta(data: bytes, *, name: str = "HelloUniPad.ipa") -> dict:
    return {
        "package_name": name,
        "package_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "declared_min_os": "17.0",
        "signature_mode": "self",
    }


def _client(transport, *, attempts: int = 1, **kwargs) -> GuestClient:
    """A client with sleeping disabled unless a test asserts on it."""
    kwargs.setdefault("backoff_s", 0.0)
    return GuestClient(transport, attempts=attempts, **kwargs)


@pytest.fixture
def staged_service(tmp_path):
    """Arrange: a fake guest with one real-shaped IPA already delivered."""
    service = FakeGuestService()
    ipa_path = hello_ipa(tmp_path / "HelloUniPad.ipa")
    data = ipa_path.read_bytes()
    meta = _install_meta(data)
    meta["bundle_id"] = "com.unipad.hello"
    service.stage_package(data, meta)
    return service, data, meta


# ---------------------------------------------------------------------------
# install_package: the success path (PRD 9.1)
# ---------------------------------------------------------------------------


def test_install_package_success_reports_registered_with_trust_mode(staged_service):
    """PRD 9.1: success reports the furthest stage plus the recorded trust mode."""
    service, data, meta = staged_service
    transport = FakeTransport(service)
    client = _client(transport)

    response = client.install_package(data, meta)

    assert response.stage == "registered"
    # The wire contract carries no bundle id, so the simulation uses the .ipa
    # stem; the authoritative id stays with unipad.ipa (see _bundle_id_for).
    assert response.bundle_id == "HelloUniPad"
    assert response.trust_mode == DEFAULT_TRUST_MODE
    assert response.trust_mode == service.trust_mode_label
    assert response.error_code is None
    assert response.ok is True
    # The guest really did the work: container + icon + recorded trust decision.
    assert service.installed["HelloUniPad"]["icon"] is True
    assert "HelloUniPad" in service.containers
    assert service.trust_log[-1]["accepted"] is True
    assert service.trust_log[-1]["trust_mode"] == DEFAULT_TRUST_MODE


def test_install_package_records_the_delivered_digest_as_evidence(staged_service):
    """PRD 9.1: the package hash is part of the install return contract."""
    service, data, meta = staged_service
    client = _client(FakeTransport(service))

    client.install_package(data, meta)

    delivery = client.last_delivery
    assert delivery["sha256"] == hashlib.sha256(data).hexdigest()
    assert delivery["size_bytes"] == len(data)
    assert delivery["declared_sha256"] == meta["package_sha256"]
    assert delivery["matches_declared"] is True


def test_install_package_never_puts_bytes_on_the_control_channel(tmp_path):
    """PRD 9.2: the control channel carries metadata only -- no bytes, no secrets."""
    service = FakeGuestService()
    # Hash-derived, so the payload is deterministic *and* genuinely
    # incompressible: the archive really is tens of kilobytes, which makes the
    # size claim below meaningful rather than a coin flip on zip deflation.
    blob = b"".join(hashlib.sha256(i.to_bytes(8, "little")).digest() for i in range(2048))
    ipa_path = build_ipa(tmp_path / "BigUniPad.ipa", extra_files={"Documents/blob.bin": blob})
    data = ipa_path.read_bytes()
    meta = _install_meta(data, name="BigUniPad.ipa")
    transport = FakeTransport(service)
    client = _client(transport)
    service.stage_package(data, meta)

    client.install_package(data, meta)

    sent = transport.calls[0]
    assert sent["type"] == MSG_INSTALL_REQUEST
    assert set(sent["payload"]) == {
        "package_name", "package_sha256", "size_bytes", "declared_min_os", "signature_mode",
    }
    assert len(json.dumps(sent, sort_keys=True)) < 1024 < len(data)
    # The staged bytes really did travel: the guest installed what it received.
    assert service.installed["BigUniPad"]["size_bytes"] == len(data)


def test_install_package_accepts_an_install_request_instance(staged_service):
    """Typed callers may pass the dataclass; it is used verbatim."""
    service, data, meta = staged_service
    from unipad.guest.protocol import InstallRequest

    client = _client(FakeTransport(service))
    response = client.install_package(data, InstallRequest.from_dict(meta))

    assert response.stage == "registered"
    assert response.bundle_id == "HelloUniPad"


def test_message_level_install_falls_back_to_the_package_name(staged_service):
    """The wire contract has five fields; the bundle id comes from the file name.

    ``unipad.ipa`` owns the authoritative bundle id; the simulation stand-in is
    the ``.ipa`` stem, and this test pins that fallback so it cannot drift.
    """
    service, data, meta = staged_service
    transport = FakeTransport(service)
    client = _client(transport)

    response = client.install_package(data, _install_meta(data))

    assert response.bundle_id == "HelloUniPad"
    assert "HelloUniPad" in service.installed


# ---------------------------------------------------------------------------
# install_package: every PRD 10 failure mode (PRD 10 table)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fail_at", "expected_code"),
    [
        ("structure", ErrorCode.INVALID_PACKAGE),
        ("transfer", ErrorCode.TRANSFER_FAILED),
        ("signature", ErrorCode.SIGNATURE_OR_POLICY_REJECTED),
        ("install_service", ErrorCode.INSTALL_SERVICE_UNAVAILABLE),
        ("registration", ErrorCode.REGISTRATION_FAILED),
        ("dependency", ErrorCode.MISSING_DEPENDENCY),
        ("dyld", ErrorCode.DYLD_FAILED),
    ],
)
def test_each_injected_failure_surfaces_the_exact_prd_code(tmp_path, fail_at, expected_code):
    """PRD 10: the host must see the taxonomy code for the point that failed."""
    ipa_path = hello_ipa(tmp_path / "HelloUniPad.ipa")
    data = ipa_path.read_bytes()
    meta = _install_meta(data)
    meta["bundle_id"] = "com.unipad.hello"
    service = FakeGuestService(failures={"fail_at": fail_at, "mode": "always"})
    service.stage_package(data, meta)
    client = _client(FakeTransport(service))

    response = client.install_package(data, meta)

    assert response.stage == "failed"
    assert response.error_code == expected_code
    assert response.ok is False
    # A failure never leaves a half-registered app behind for status() to report.
    assert "com.unipad.hello" not in service.installed
    assert service.status().installed == []


def test_guest_error_envelope_becomes_a_failed_response_with_verbatim_code():
    """PRD 10: a guest error envelope is a definitive answer, passed through."""
    transport = ScriptedTransport(always=error_message(
        ErrorCode.SIGNATURE_OR_POLICY_REJECTED, "signature or policy rejected", message_id="corr-1"
    ).to_dict())
    client = _client(transport)
    data = b"PK\x03\x04staged"

    response = client.install_package(data, _install_meta(data))

    assert response.stage == "failed"
    assert response.error_code == ErrorCode.SIGNATURE_OR_POLICY_REJECTED
    assert "signature or policy rejected" in response.error_message


def test_guest_error_envelope_without_a_code_is_not_silent():
    """An error envelope with no code must not look like a success."""
    transport = ScriptedTransport(always={"type": MSG_ERROR, "id": "corr-1", "payload": {"message": "???"}})
    client = _client(transport)

    response = client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert response.stage == "failed"
    assert response.error_code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# a guest that never answers (PRD 10 install_service_unavailable)
# ---------------------------------------------------------------------------


def test_timeout_raises_install_service_unavailable_and_never_succeeds():
    """PRD 10: a peer that never answers is unavailable -- not installed."""
    transport = SilentTransport()
    client = _client(transport, timeout_s=5.0)
    data = b"PK\x03\x04staged"

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(data, _install_meta(data))

    error = excinfo.value
    assert error.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert error.details["detail"] == "timeout"
    assert error.details["attempts"] == 1
    assert error.details["timeout_s"] == 5.0
    assert error.details["request_type"] == MSG_INSTALL_REQUEST
    assert client.last_error is error
    assert isinstance(error.__cause__, TimeoutError)


def test_transport_oserror_maps_to_install_service_unavailable():
    """A dead channel is an unavailable service, with the failure named."""
    transport = SilentTransport(OSError("simulated channel failure"))
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert excinfo.value.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert excinfo.value.details["detail"] == "OSError"


def test_no_valid_answer_is_never_reported_as_installed():
    """The negative half of PRD 9.1: no reply means no stage, at all."""
    transport = SilentTransport()
    client = _client(transport)

    with pytest.raises(UniPadError):
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert client.last_delivery is not None          # evidence of the attempt
    assert client.last_error is not None
    assert client.last_error.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# bounded retries
# ---------------------------------------------------------------------------


def test_retries_are_bounded_by_the_attempt_budget():
    """PRD 7.5-style budget: a dead peer is tried ``attempts`` times, then stops."""
    transport = SilentTransport()
    slept: list[float] = []
    client = GuestClient(transport, attempts=3, backoff_s=0.05, sleeper=slept.append)

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert len(transport.calls) == 3
    assert excinfo.value.details["attempts"] == 3
    assert excinfo.value.details["attempt"] == 3
    assert slept == [0.05, 0.05]          # between attempts only, never after the last


def test_single_attempt_budget_makes_exactly_one_call():
    transport = SilentTransport()
    slept: list[float] = []
    client = GuestClient(transport, attempts=1, backoff_s=0.05, sleeper=slept.append)

    with pytest.raises(UniPadError):
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert len(transport.calls) == 1
    assert slept == []


def test_transport_failure_then_success_recovers_within_the_budget(staged_service):
    """One dropped message is retried; the install still completes."""
    service, data, meta = staged_service
    transport = RetryingTransport(FakeTransport(service), fail_times=1)
    client = GuestClient(transport, attempts=3, backoff_s=0.0)

    response = client.install_package(data, meta)

    assert response.stage == "registered"
    assert len(transport.calls) == 2      # one OSError, one real exchange
    assert transport.inner.calls == [] or len(transport.inner.calls) == 1


def test_a_definitive_guest_answer_is_never_retried(staged_service):
    """A guest-reported failure is an answer, not a reason to re-ask."""
    service, data, meta = staged_service
    service.set_failures({"fail_at": "signature", "mode": "always"})
    transport = FakeTransport(service)
    client = GuestClient(transport, attempts=5, backoff_s=0.0)

    response = client.install_package(data, meta)

    assert response.error_code == ErrorCode.SIGNATURE_OR_POLICY_REJECTED
    assert len(transport.calls) == 1


def test_once_failure_mode_fails_exactly_once_then_succeeds(staged_service):
    """A one-shot guest fault fires once; re-driving the install then succeeds.

    The client itself never retries a definitive answer, so the recovery is
    observable as exactly two exchanges: one failed, one registered.
    """
    service, data, meta = staged_service
    service.set_failures({"fail_at": "signature", "mode": "once"})
    transport = FakeTransport(service)
    client = GuestClient(transport, attempts=3, backoff_s=0.0)

    first = client.install_package(data, meta)
    second = client.install_package(data, meta)

    assert first.stage == "failed"
    assert first.error_code == ErrorCode.SIGNATURE_OR_POLICY_REJECTED
    assert first.ok is False
    assert second.stage == "registered"
    assert second.error_code is None
    assert second.ok is True
    assert len(transport.calls) == 2
    # The retry earned the registration for real, not by assertion.
    assert "HelloUniPad" in service.installed
    # The injected signature fault short-circuits *before* the trust decision is
    # evaluated, so only the successful attempt records one -- and it accepted.
    assert [entry["accepted"] for entry in service.trust_log] == [True]


@pytest.mark.parametrize("fail_at", ["registration", "dyld", "dependency"])
def test_once_failure_mode_recovers_for_every_late_chain_point(staged_service, fail_at):
    """``once`` is honoured at every injection point, including the late ones."""
    service, data, meta = staged_service
    service.set_failures({"fail_at": fail_at, "mode": "once"})
    client = _client(FakeTransport(service), attempts=2)

    first = client.install_package(data, meta)
    second = client.install_package(data, meta)

    assert first.stage == "failed"
    assert second.stage == "registered"
    assert "HelloUniPad" in service.installed


def test_install_of_a_package_that_never_arrived_is_not_a_success(tmp_path):
    """PRD 9.1/10: bytes the guest never received cannot yield a registration.

    The host may believe it delivered the package; the guest only knows what
    actually reached its staging area.  Reporting ``registered`` here would be
    a fabricated success.
    """
    service = FakeGuestService()
    data = hello_ipa(tmp_path / "HelloUniPad.ipa").read_bytes()
    meta = _install_meta(data)
    transport = FakeTransport(service)
    client = _client(transport)

    response = client.install_package(data, meta)

    assert response.stage == "failed"
    assert response.error_code == ErrorCode.TRANSFER_FAILED
    assert response.ok is False
    assert service.installed == {}
    assert service.containers == {}
    assert service.status().installed == []
    assert service.install_log[-1]["stage"] == "failed"


def test_every_attempt_uses_a_fresh_correlation_id():
    """A retry must not reuse a correlation handle (PRD 9.1 message ids)."""
    transport = SilentTransport()
    client = GuestClient(transport, attempts=3, backoff_s=0.0)

    with pytest.raises(UniPadError):
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    ids = [call["id"] for call in transport.calls]
    assert len(set(ids)) == 3
    assert all(call["type"] == MSG_INSTALL_REQUEST for call in transport.calls)


def test_default_attempt_budget_is_three():
    assert DEFAULT_ATTEMPTS == 3


# ---------------------------------------------------------------------------
# reply validation: no fabricated or ambiguous success
# ---------------------------------------------------------------------------


def test_wrong_reply_type_is_rejected():
    """A status reply to an install request is not an install answer."""
    transport = ScriptedTransport(always={"type": MSG_STATUS_RESPONSE, "id": "corr-1", "payload": {}})
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR
    assert excinfo.value.details["expected_type"] == MSG_INSTALL_RESPONSE
    assert excinfo.value.details["actual_type"] == MSG_STATUS_RESPONSE


def test_reply_id_mismatch_is_rejected():
    """A reply must be attributable to the request that triggered it."""
    transport = ScriptedTransport(always={
        "type": MSG_INSTALL_RESPONSE,
        "id": "some-other-exchange",
        "payload": {"stage": "registered", "bundle_id": "com.unipad.hello"},
    }, echo_id=False)
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR
    assert excinfo.value.details["actual_id"] == "some-other-exchange"
    assert excinfo.value.details["expected_id"] == transport.calls[0]["id"]


def test_non_object_reply_is_rejected():
    transport = ScriptedTransport(always=["not", "an", "envelope"])
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_success_without_bundle_id_is_rejected():
    """PRD 9.1: a registered install must name the bundle it registered."""
    transport = ScriptedTransport(always={
        "type": MSG_INSTALL_RESPONSE, "id": "corr-1",
        "payload": {"stage": "registered", "trust_mode": DEFAULT_TRUST_MODE},
    })
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_reply_claiming_success_and_an_error_is_rejected():
    """Ambiguity resolves to failure, never to a quiet success."""
    transport = ScriptedTransport(always={
        "type": MSG_INSTALL_RESPONSE, "id": "corr-1",
        "payload": {
            "stage": "registered",
            "bundle_id": "com.unipad.hello",
            "error_code": ErrorCode.DYLD_FAILED,
        },
    })
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_non_conforming_install_payload_is_rejected():
    transport = ScriptedTransport(always={"type": MSG_INSTALL_RESPONSE, "id": "corr-1", "payload": {"bundle_id": "x"}})
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(b"PK\x03\x04", _install_meta(b"PK\x03\x04"))

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


# ---------------------------------------------------------------------------
# configuration and metadata validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"package_name": "HelloUniPad.ipa"},
        {"package_name": "x.ipa", "package_sha256": "a" * 64, "size_bytes": 1},
        {
            "package_name": "x.ipa",
            "package_sha256": "a" * 64,
            "size_bytes": 1,
            "declared_min_os": "17.0",
        },
        "not-a-mapping",
        None,
    ],
)
def test_malformed_metadata_raises_config_invalid_without_contacting_the_guest(meta):
    """Bad host-side input is a usage error, never a message to the guest."""
    transport = ScriptedTransport()
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.install_package(b"PK\x03\x04", meta)

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert transport.calls == []


@pytest.mark.parametrize("kwargs", [{"attempts": 0}, {"attempts": -1}, {"attempts": True}, {"backoff_s": -0.5}])
def test_invalid_client_configuration_is_rejected(kwargs):
    transport = ScriptedTransport()

    with pytest.raises(UniPadError) as excinfo:
        GuestClient(transport, **kwargs)

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# challenge (PRD 5.2 step 5, M11)
# ---------------------------------------------------------------------------


def test_challenge_returns_the_deterministic_derived_value(staged_service):
    """M11: the host can compare expected against actual for this run."""
    service, data, meta = staged_service
    client = _client(FakeTransport(service))
    client.install_package(data, meta)

    response = client.challenge(NONCE)

    assert response.nonce == NONCE
    assert response.value == derive_challenge_value("HelloUniPad", NONCE)
    assert response.value == service.expected_challenge("HelloUniPad", NONCE)
    assert len(response.value) == 64
    # Deterministic: the same run asks twice and gets the same answer.
    assert client.challenge(NONCE).value == response.value
    assert [entry["nonce"] for entry in service.challenge_log] == [NONCE, NONCE]


def test_challenge_value_depends_on_the_nonce(staged_service):
    """A different run must not be able to replay a previous answer."""
    service, data, meta = staged_service
    client = _client(FakeTransport(service))
    client.install_package(data, meta)

    first = client.challenge(NONCE).value
    second = client.challenge("another-run-nonce").value

    assert first != second


def test_challenge_nonce_mismatch_is_rejected():
    """An answer that cannot be attributed to this run is not evidence."""
    transport = ScriptedTransport(always={
        "type": MSG_CHALLENGE_RESPONSE,
        "id": "corr-1",
        "payload": {"nonce": "some-other-run", "value": "b" * 64},
    })
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.challenge(NONCE)

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


@pytest.mark.parametrize("nonce", ["", None, 7])
def test_challenge_rejects_a_bad_nonce(nonce):
    transport = ScriptedTransport()
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.challenge(nonce)

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert transport.calls == []


def test_challenge_guest_error_passes_the_code_through():
    """PRD 10: a guest-reported challenge failure is raised with its own code."""
    transport = ScriptedTransport(always=error_message(
        ErrorCode.DYLD_FAILED, "entry point did not execute", message_id="corr-1"
    ).to_dict())
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.challenge(NONCE)

    assert excinfo.value.code == ErrorCode.DYLD_FAILED
    assert excinfo.value.details["reply_id"] == transport.calls[0]["id"]


def test_challenge_error_envelope_without_a_code_is_a_protocol_error():
    transport = ScriptedTransport(always={"type": MSG_ERROR, "id": "corr-1", "payload": {"message": "???"}})
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.challenge(NONCE)

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


# ---------------------------------------------------------------------------
# status (M9b / M12)
# ---------------------------------------------------------------------------


def test_status_reflects_the_service(tmp_path):
    """M9b/M12: the host sees exactly what the guest holds, nothing more."""
    service = FakeGuestService()
    client = _client(FakeTransport(service))
    apps = (
        ("HelloUniPad.ipa", "com.unipad.hello", "HelloUniPad", {}),
        (
            "UniPadNotes.ipa",
            "com.unipad.notes",
            "UniPadNotes",
            {"Documents/container-data.txt": b"unipad-notes-container-marker\n"},
        ),
    )
    for name, bundle, app_name, extra in apps:
        ipa_path = build_ipa(
            tmp_path / name,
            app_name=app_name,
            bundle_id=bundle,
            executable=app_name,
            extra_files=extra,
        )
        data = ipa_path.read_bytes()
        meta = _install_meta(data, name=name)
        meta["bundle_id"] = bundle
        service.stage_package(data, meta)
        assert client.install_package(data, meta).stage == "registered"
    service.set_data_marker("UniPadNotes", "Documents/container-data.txt", "hello from the test run")

    status = client.status()

    assert [app["bundle_id"] for app in status.installed] == ["HelloUniPad", "UniPadNotes"]
    assert all(app["icon"] is True for app in status.installed)
    assert {app["bundle_id"]: app["trust_mode"] for app in status.installed} == {
        "HelloUniPad": DEFAULT_TRUST_MODE,
        "UniPadNotes": DEFAULT_TRUST_MODE,
    }
    assert status.data_markers == ["UniPadNotes:Documents/container-data.txt"]


def test_status_is_empty_for_a_fresh_guest():
    client = _client(FakeTransport(FakeGuestService()))

    status = client.status()

    assert status.installed == []
    assert status.data_markers == []


def test_status_rejects_an_entry_without_a_bundle_id():
    transport = ScriptedTransport(always={
        "type": MSG_STATUS_RESPONSE, "id": "corr-1",
        "payload": {"installed": [{"version": "1.0", "icon": True}], "data_markers": []},
    })
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.status()

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_status_guest_error_passes_the_code_through():
    transport = ScriptedTransport(always=error_message(
        ErrorCode.INSTALL_SERVICE_UNAVAILABLE, "guest install service is down", message_id="corr-1"
    ).to_dict())
    client = _client(transport)

    with pytest.raises(UniPadError) as excinfo:
        client.status()

    assert excinfo.value.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE


def test_status_timeout_is_unavailable_not_empty():
    """A guest that cannot be asked is not a guest with no apps."""
    client = _client(SilentTransport())

    with pytest.raises(UniPadError) as excinfo:
        client.status()

    assert excinfo.value.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert excinfo.value.details["request_type"] == MSG_STATUS_REQUEST


def test_challenge_and_status_use_their_own_message_types(staged_service):
    """Each call speaks the message type its reply must match."""
    service, data, meta = staged_service
    transport = FakeTransport(service)
    client = _client(transport)
    client.install_package(data, meta)

    client.challenge(NONCE)
    client.status()

    assert [call["type"] for call in transport.calls] == [
        MSG_INSTALL_REQUEST, MSG_CHALLENGE_REQUEST, MSG_STATUS_REQUEST,
    ]
