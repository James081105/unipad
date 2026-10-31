"""Unit tests for :mod:`unipad.guest.agent_stub` (ADR-0011).

``GuestAgentStub`` is the first peer in this package that behaves like the
chardev rather than like a function call: it binds a port, the host connects to
it, and it can refuse to answer.  So the properties worth pinning are the ones
that decide whether a test can *believe* a green result:

* **the whole closed loop over one real socket** -- install, launch and persist
  all reuse the single connection the chardev would have served, because a test
  that reconnected per verb would pass on a stub that is more forgiving than the
  device;
* **a guest that is not ready yet** -- ``status`` times out, the socket is
  *kept*, the stub becomes ready, and the same connection answers.  This is the
  path the one-connection rule makes dangerous, and it must pass;
* **a guest that never becomes ready** -- ``install_service_unavailable`` with
  ``detail='timeout'``, and never a fabricated success;
* **the stub never claims a stage it did not reach** -- a failure mid-install
  reports the guest's own code and leaves ``stages_reached`` holding exactly the
  stages before the failure;
* **the reply id is the request id** -- over a real socket, where a minted id
  would surface as ``guest reply id does not match the request id``;
* **answering a stale request after its budget expired is not harmless** -- the
  late reply consumes the next question's slot.  Pinned from the other side of
  the readiness gate, because that is the mistake a guest implementer would
  actually make.

Every test binds a real loopback socket.  No ``socket`` is mocked: the failure
modes under test are socket failure modes, and a mock socket reproduces none of
them.

What is *not* verified here, or anywhere: no guest has ever run on this host.
These tests prove the host side can be driven against a conforming peer, not
that iPadOS contains one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.agent_channel import (
    STATE_ANSWERED,
    STATE_REFUSED,
    STATE_SILENT,
    AgentChannelTransport,
    AgentSession,
)
from unipad.guest.agent_stub import (
    INJECTABLE_CODES,
    INJECTABLE_STAGES,
    STALE_ANSWER,
    STALE_DISCARD,
    GuestAgentStub,
    start_agent_stub,
)
from unipad.guest.client import GuestClient
from unipad.guest.fake import derive_challenge_value
from unipad.guest.protocol import (
    MSG_INSTALL_PROGRESS,
    MSG_STATUS_REQUEST,
    MSG_STATUS_RESPONSE,
    Message,
    new_message_id,
)

# --- the package under test -------------------------------------------------

PACKAGE_NAME = "Hello.ipa"
#: Deliberately not a real IPA: the stub's ``checked`` stage validates the
#: *declared metadata*, and package-structure precheck is the host's
#: ``unipad.ipa`` responsibility (ADR-0003's evidence table lists the precheck
#: report as a host artifact, and it is covered there).
PACKAGE_BYTES = b"PK\x03\x04unipad-agent-stub-scratch-package"
PACKAGE_SHA256 = hashlib.sha256(PACKAGE_BYTES).hexdigest()

META: dict[str, Any] = {
    "package_name": PACKAGE_NAME,
    "package_sha256": PACKAGE_SHA256,
    "size_bytes": len(PACKAGE_BYTES),
    "declared_min_os": "17.0",
    "signature_mode": "self",
}

NONCE = "run-2026-10-31-nonce"

#: Short enough that a not-ready guest costs a fraction of a second, long
#: enough that a ready guest is never mistaken for a slow one.
TIMEOUT_S = 0.3


def _client(transport: Any, *, attempts: int = 2) -> GuestClient:
    """A client that does not sleep between its (few) attempts."""
    return GuestClient(transport, attempts=attempts, backoff_s=0)


def _session(stub: GuestAgentStub, **kwargs: Any) -> AgentSession:
    return AgentSession("127.0.0.1", stub.port, timeout_s=TIMEOUT_S, **kwargs)


# ---------------------------------------------------------------------------
# the closed loop, over one connection
# ---------------------------------------------------------------------------


def test_the_whole_loop_runs_over_one_real_socket(tmp_path: Any) -> None:
    """install -> launch -> persist, all on the connection the chardev served.

    The assertion that matters is ``connections_served == 1``: a stub that
    accepted a fresh connection per verb would let a host that spent the boot's
    only connection look correct.
    """
    with start_agent_stub(ready=True) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            client = _client(session.transport)

            installed = client.install_package(PACKAGE_BYTES, META)
            assert installed.stage == "registered"
            assert installed.error_code is None
            assert installed.bundle_id == "Hello"
            # The trust label is what the guest recorded, not what the host
            # assumed: it is read back off the response, never hard-coded here.
            assert installed.trust_mode == stub.trust_mode_label

            launched = client.launch(installed.bundle_id)
            assert launched.stage == "launched"
            assert launched.bundle_id == "Hello"

            stub.set_data_marker(installed.bundle_id, "notes.txt", "hello")
            persisted = client.persist(str(tmp_path / "data"))
            assert persisted.stage == "persisted"
            assert persisted.markers_written == ["Hello:notes.txt"]
            assert persisted.bytes_written > 0

            status = client.status()
            assert [a["bundle_id"] for a in status.installed] == ["Hello"]
            assert status.data_markers == persisted.markers_written

            # One socket for the whole loop, and it was never dropped.
            assert stub.connections_served == 1
            assert session.transport.connected is True

    # Progress is recorded for the stub's own log and never put on the wire:
    # the client reads exactly one line per request, so an interleaved
    # install.progress would be read as the reply and rejected.
    assert [p["stage"] for p in stub.progress_log] == [
        "received",
        "checked",
        "transferred",
        "installed",
        "registered",
    ]


def test_the_stages_the_stub_claims_are_the_stages_it_reached() -> None:
    """A successful install reaches all five ADR-0003 stages, in order."""
    with start_agent_stub(ready=True) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            response = _client(session.transport).install_package(PACKAGE_BYTES, META)

    record = stub.install_log[-1]
    assert response.stage == "registered"
    assert record["stage"] == "registered"
    assert record["stages_reached"] == ["received", "checked", "transferred", "installed", "registered"]
    assert record["error_code"] is None


def test_the_stub_never_claims_a_stage_the_transfer_did_not_earn() -> None:
    """No staged bytes means the transfer cannot be verified -- so no install.

    PRD 9.1 forbids claiming a stage the transfer did not earn.  The stub's
    ``transferred`` check is therefore real: a request naming a digest the stub
    never received finds nothing and stops there.
    """
    with start_agent_stub(ready=True) as stub:
        with _session(stub) as session:
            response = _client(session.transport).install_package(PACKAGE_BYTES, META)

    assert response.stage == "failed"
    assert response.error_code == ErrorCode.TRANSFER_FAILED
    assert response.bundle_id is not None
    assert stub.install_log[-1]["stages_reached"] == ["received", "checked"]
    # Nothing was installed, so a verifier's status() cannot contradict the
    # failure that was just reported.
    assert stub.installed == {}


def test_a_stage_that_fails_reports_the_guest_code_and_never_the_next_stage() -> None:
    """An injected failure stops the chain where it was injected.

    ``transferred`` is the third stage, so the stub may claim the two before it
    and nothing after -- in particular never ``installed`` or ``registered``,
    which is the lie ADR-0003 decision 3 exists to prevent.
    """
    code = ErrorCode.TRANSFER_FAILED
    with start_agent_stub(ready=True, failures={"transferred": code}) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            response = _client(session.transport).install_package(PACKAGE_BYTES, META)

    assert response.stage == "failed"
    # Verbatim: the code on the wire is the code that was injected, not a
    # re-mapped or generic stand-in.
    assert response.error_code == code
    assert response.error_message
    assert stub.install_log[-1]["stages_reached"] == ["received", "checked"]
    assert stub.installed == {}


def test_a_failure_at_the_last_stage_withdraws_the_registration() -> None:
    """``registered`` failed, so the container that existed is withdrawn.

    ``installed`` really was reached -- the container was created -- and
    leaving it in place would let a verifier's ``status()`` contradict an
    install that reported a failure.  The history keeps what was reached; the
    state does not.
    """
    code = ErrorCode.REGISTRATION_FAILED
    with start_agent_stub(ready=True, failures={"registered": code}) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            response = _client(session.transport).install_package(PACKAGE_BYTES, META)
            status = _client(session.transport).status()

    assert response.stage == "failed"
    assert response.error_code == code
    assert stub.install_log[-1]["stages_reached"] == ["received", "checked", "transferred", "installed"]
    assert stub.install_log[-1]["rolled_back"] is True
    assert stub.installed == {}
    assert status.installed == []


def test_a_package_the_trust_policy_refuses_is_never_installed() -> None:
    """An untrusted package is refused, and the refusal is recorded.

    ``trust_mode`` is recorded, never assumed (ADR-0003 decision 3): when the
    policy refuses, the recorded decision is that refusal -- not a label the
    host would have preferred.  A stub that installed regardless of policy
    would teach the host that any ``signature_mode`` is acceptable, which is
    precisely the confusion PRD 5.1 warns about.
    """
    with start_agent_stub(ready=True, trust_policy={"accept_modes": ["enterprise"]}) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            response = _client(session.transport).install_package(PACKAGE_BYTES, META)
            status = _client(session.transport).status()

    assert response.stage == "failed"
    assert response.error_code == ErrorCode.SIGNATURE_OR_POLICY_REJECTED
    assert stub.install_log[-1]["stages_reached"] == ["received", "checked", "transferred"]
    assert status.installed == []
    assert stub.trust_log[-1]["accepted"] is False
    assert stub.trust_log[-1]["trust_mode"] is None


def test_the_stub_never_reports_a_code_outside_the_taxonomy(tmp_path: Any) -> None:
    """Guard: every code this stub emits is a member of the closed vocabulary.

    Both directions of "never invented": an injected code passes through
    verbatim, and a self-derived code (a digest that does not match, a policy
    that refuses) is a real taxonomy code rather than a fresh string.  A stub
    that could emit anything would teach the host to accept anything.
    """
    with start_agent_stub(ready=True) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            client = _client(session.transport)
            client.install_package(PACKAGE_BYTES, META)
            # An install with nothing staged, for the self-derived transfer code.
            client.install_package(PACKAGE_BYTES, {**META, "package_name": "Other.ipa"})
            client.launch("not.installed.at.all")
            client.persist(str(tmp_path / "data"))
            client.challenge(NONCE)

    for record in stub.install_log:
        assert record["error_code"] is None or record["error_code"] in INJECTABLE_CODES
    for entry in stub.trust_log:
        assert entry["trust_mode"] is None or isinstance(entry["trust_mode"], str)


# ---------------------------------------------------------------------------
# the readiness gate -- the path the one-connection rule makes dangerous
# ---------------------------------------------------------------------------


def test_a_guest_that_is_not_ready_yet_answers_on_the_same_connection() -> None:
    """Not ready: ``status`` times out, the socket is kept, then it answers.

    ``AgentChannelTransport`` deliberately holds its socket across a timeout,
    because the chardev serves one connection per boot and a refused connect
    afterwards is indistinguishable from a dead boot.  So a guest whose service
    comes up late must be answerable on the socket it already has -- and the
    request that arrived too early must not be answered late, or it would
    consume the next question's reply slot.
    """
    with start_agent_stub() as stub:  # not ready
        with _session(stub) as session:
            early = session.status()
            assert early["reachable"] is False
            assert early["state"] == STATE_SILENT
            assert early["detail"] == "no_reply_in_budget"
            # The socket survived the timeout: this is the whole point.
            assert session.transport.connected is True
            assert session.state == STATE_SILENT

            stub.make_ready()
            late = session.status()

    assert late["reachable"] is True
    assert late["state"] == STATE_ANSWERED
    assert late["reply"]["type"] == MSG_STATUS_RESPONSE
    # Still one connection -- the late answer reused the socket it already had.
    assert stub.connections_served == 1
    # The premature request was dropped, not answered late.
    assert [d["type"] for d in stub.discarded] == [MSG_STATUS_REQUEST]
    assert len(stub.discarded[0]["id"]) == 32


def test_a_guest_that_never_becomes_ready_yields_service_unavailable() -> None:
    """Never ready: ``install_service_unavailable``, never a success stage.

    The client retries, and every retry times out.  What must not happen is a
    timeout being read as an answer -- ``detail='timeout'`` is what
    distinguishes "the guest did not say" from "the guest said no".
    """
    with start_agent_stub() as stub:  # never ready
        with _session(stub) as session:
            with pytest.raises(UniPadError) as excinfo:
                _client(session.transport, attempts=2).install_package(PACKAGE_BYTES, META)

            error = excinfo.value
            assert error.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
            assert error.details["detail"] == "timeout"
            assert error.details["request_type"] == "install.request"
            assert error.details["attempts"] == 2
            # The socket is still held: a silent peer is not evidence the
            # channel ended.  Asserted INSIDE the session, because leaving the
            # ``with`` block is what closes it -- asserting after it would be
            # asserting against a teardown the test itself performed.
            assert session.transport.connected is True
            assert session.state == STATE_SILENT
            # And the guest never claimed anything at all.
            assert stub.answered == []
            assert stub.install_log == []


def test_a_definitive_guest_answer_is_never_retried() -> None:
    """A failed install is an answer, so it is sent once and returned once.

    Retrying a definitive answer would double-send a state-changing request
    and could turn one honest refusal into two install attempts.
    """
    with start_agent_stub(ready=True, failures={"installed": ErrorCode.SIGNATURE_OR_POLICY_REJECTED}) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            response = _client(session.transport, attempts=3).install_package(PACKAGE_BYTES, META)

    assert response.stage == "failed"
    assert response.error_code == ErrorCode.SIGNATURE_OR_POLICY_REJECTED
    assert [a["type"] for a in stub.answered] == ["install.request"]


def test_answering_a_stale_request_consumes_the_next_questions_slot() -> None:
    """The other side of the readiness gate, pinned on purpose.

    A guest that answers the question it was asked *before* its service existed
    puts a reply in the stream that no caller is waiting for.  The next caller
    reads it as its own answer, and the only thing that saves the host is the
    id correlation -- which reports a protocol error rather than a success.
    This is why the default policy is :data:`STALE_DISCARD`.
    """
    with start_agent_stub(stale_policy=STALE_ANSWER) as stub:
        with _session(stub) as session:
            client = _client(session.transport, attempts=1)
            early = session.status()
            assert early["reachable"] is False
            assert early["state"] == STATE_SILENT

            stub.make_ready()
            with pytest.raises(UniPadError) as excinfo:
                client.status()

    error = excinfo.value
    assert error.code == ErrorCode.QMP_PROTOCOL_ERROR
    assert "id does not match" in error.message
    # The stub answered both questions, correctly, and the host could use
    # neither of them.
    assert [a["type"] for a in stub.answered] == [MSG_STATUS_REQUEST, MSG_STATUS_REQUEST]
    assert stub.discarded == []


def test_the_stale_policy_is_validated_rather_than_ignored() -> None:
    """A misspelled policy is a config error, not a silent fallback."""
    with pytest.raises(UniPadError) as excinfo:
        GuestAgentStub(stale_policy="queue")
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# correlation, vocabulary and the connection budget
# ---------------------------------------------------------------------------


def test_the_reply_id_is_the_request_id_over_a_real_socket() -> None:
    """A known id goes out and the same id comes back.

    Correlating the id is not politeness: ``GuestClient._validate_reply``
    rejects a reply whose id is not the request's, so a stub that minted a
    fresh id would make every "successful" test fail with ``guest reply id does
    not match the request id``.
    """
    known_id = new_message_id()
    with start_agent_stub(ready=True) as stub:
        transport = AgentChannelTransport("127.0.0.1", stub.port, timeout_s=TIMEOUT_S)
        with transport:
            reply = transport.request(
                Message(type=MSG_STATUS_REQUEST, id=known_id, payload={}).to_dict()
            )
    assert reply["id"] == known_id
    assert reply["type"] == MSG_STATUS_RESPONSE


def test_the_second_connection_is_refused_because_the_chardev_serves_one() -> None:
    """One connection per boot, then the listener is gone.

    Measured (``chardev-connection-count.py``): the first connect is accepted
    and every later one is refused with ECONNREFUSED, immediately and
    permanently.  A stub that happily served a second connection would let a
    host that spent the boot's only connection look correct.
    """
    with start_agent_stub(ready=True) as stub:
        transport = AgentChannelTransport("127.0.0.1", stub.port, timeout_s=TIMEOUT_S)
        transport.connect()
        try:
            reply = transport.request(
                Message(type=MSG_STATUS_REQUEST, id=new_message_id(), payload={}).to_dict()
            )
            assert reply["type"] == MSG_STATUS_RESPONSE
            transport.close()

            with pytest.raises(OSError):
                transport.connect()
            assert transport.state == STATE_REFUSED
        finally:
            transport.close()

    assert stub.connections_served == 1


def test_an_unsupported_message_type_is_refused_with_its_id_intact() -> None:
    """The wire vocabulary is closed in both directions.

    ``install.progress`` is a real message type that is nonetheless
    guest -> host: a host that sent one is asking the guest to do something the
    contract does not define, and the refusal keeps the request's id so the
    host can attribute it.  A type outside ``KNOWN_TYPES`` is refused the same
    way.
    """
    with start_agent_stub(ready=True) as stub:
        transport = AgentChannelTransport("127.0.0.1", stub.port, timeout_s=TIMEOUT_S)
        with transport:
            for mtype in (MSG_INSTALL_PROGRESS, "wombat.request"):
                reply = transport.request(
                    Message(type=mtype, id=new_message_id(), payload={}).to_dict()
                )
                assert reply["type"] == "error"
                assert reply["payload"]["code"] == ErrorCode.UNSUPPORTED_COMMAND
                assert reply["payload"]["message"]


# ---------------------------------------------------------------------------
# launch and persist as their own questions
# ---------------------------------------------------------------------------


def test_a_launch_failure_is_not_an_install_failure() -> None:
    """An app that installed cleanly can still refuse to start (ADR-0010).

    The install result must not change, and the app must stay installed: a
    launch fault belongs to launch, and collapsing it into the install result
    is exactly the error ADR-0007 split open.
    """
    with start_agent_stub(ready=True, failures={"launched": ErrorCode.DYLD_FAILED}) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            client = _client(session.transport)
            installed = client.install_package(PACKAGE_BYTES, META)
            launched = client.launch(installed.bundle_id)
            status = client.status()

    assert installed.stage == "registered"
    assert launched.stage == "failed"
    assert launched.error_code == ErrorCode.DYLD_FAILED
    assert launched.bundle_id == "Hello"
    assert [a["bundle_id"] for a in status.installed] == ["Hello"]


def test_launching_an_app_that_is_not_installed_is_a_registration_fault() -> None:
    """Nothing to start is a registration problem, verbatim from the guest."""
    with start_agent_stub(ready=True) as stub:
        with _session(stub) as session:
            launched = _client(session.transport).launch("com.example.absent")

    assert launched.stage == "failed"
    assert launched.error_code == ErrorCode.REGISTRATION_FAILED
    assert launched.bundle_id == "com.example.absent"


def test_persist_reports_the_markers_the_guest_holds_and_measures_the_bytes(tmp_path: Any) -> None:
    """``markers_written`` is the guest's own inventory; ``bytes_written`` is measured.

    Both are observations of this stub's state.  A marker list echoed from the
    request, or a byte count that was a constant, would be reporting a flush
    nobody performed.
    """
    with start_agent_stub(ready=True) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            client = _client(session.transport)
            client.install_package(PACKAGE_BYTES, META)
            stub.set_data_marker("Hello", "b.txt", "second")
            stub.set_data_marker("Hello", "a.txt", "first")
            persisted = client.persist(str(tmp_path / "data"))

    assert persisted.stage == "persisted"
    assert persisted.error_code is None
    assert persisted.markers_written == ["Hello:a.txt", "Hello:b.txt"]
    assert persisted.bytes_written > 0
    # The measurement is of the file that was actually written.
    assert persisted.bytes_written == stub.last_state_path.stat().st_size
    # Progress is recorded for the stub's own log and never sent on the wire.
    assert [p["stage"] for p in stub.progress_log][-1] == "registered"


def test_a_failed_persist_reports_no_markers(tmp_path: Any) -> None:
    """A failed flush is not "nothing to persist".

    ``markers_written`` is empty because the guest reported no successful
    write, never because the host decided there was nothing there -- and the
    stub really does hold a marker at the time it refuses.
    """
    with start_agent_stub(ready=True, failures={"persisted": ErrorCode.PERSISTENCE_FAILED}) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            client = _client(session.transport)
            client.install_package(PACKAGE_BYTES, META)
            stub.set_data_marker("Hello", "notes.txt", "hello")
            persisted = client.persist(str(tmp_path / "data"))
            status = client.status()

    assert persisted.stage == "failed"
    assert persisted.error_code == ErrorCode.PERSISTENCE_FAILED
    assert persisted.markers_written == []
    assert persisted.bytes_written == 0
    # The data is still there; the flush is what failed.
    assert status.data_markers == ["Hello:notes.txt"]


def test_a_persist_the_guest_cannot_perform_reports_persistence_failed(tmp_path: Any) -> None:
    """A real OS failure, not an injected one, is still the guest's own verdict.

    The path names a directory that cannot exist (it is under a regular file),
    so the guest's write genuinely fails.  The code is a taxonomy code and the
    message carries no path: PRD 9.2 keeps user document paths off the wire.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    with start_agent_stub(ready=True) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            client = _client(session.transport)
            client.install_package(PACKAGE_BYTES, META)
            persisted = client.persist(str(blocker / "sub"))

    assert persisted.stage == "failed"
    assert persisted.error_code == ErrorCode.PERSISTENCE_FAILED
    assert persisted.markers_written == []
    assert str(blocker) not in (persisted.error_message or "")


def test_the_challenge_value_matches_the_derivation_for_the_app_that_answered() -> None:
    """M11 evidence: the value the guest sent is the value the host expects.

    The derivation is shared with :class:`~unipad.guest.fake.FakeGuestService`,
    so an answer from this stub and an answer from the in-memory fake are
    comparable -- and the nonce is echoed, so the answer is attributable to
    this run.
    """
    with start_agent_stub(ready=True) as stub:
        with _session(stub) as session:
            stub.stage_package(PACKAGE_BYTES, META)
            client = _client(session.transport)
            installed = client.install_package(PACKAGE_BYTES, META)
            answered = client.challenge(NONCE)

    assert answered.nonce == NONCE
    assert answered.value == derive_challenge_value(installed.bundle_id, NONCE)
    assert answered.value == stub.expected_challenge("Hello", NONCE)


def test_a_challenge_with_nothing_installed_is_a_registration_fault() -> None:
    """There is no app to answer for, and deriving a phantom value would be a lie."""
    with start_agent_stub(ready=True) as stub:
        with _session(stub) as session:
            with pytest.raises(UniPadError) as excinfo:
                _client(session.transport).challenge(NONCE)

    assert excinfo.value.code == ErrorCode.REGISTRATION_FAILED


# ---------------------------------------------------------------------------
# the injection vocabulary itself
# ---------------------------------------------------------------------------


def test_a_failure_may_be_injected_at_every_stage_in_the_vocabulary() -> None:
    """Each injectable stage stops the chain at the right place.

    The stage names are the ADR-0003 reporting vocabulary plus the terminal
    words of the launch and persist vocabularies -- so ``received`` stops
    immediately and ``registered`` stops last.
    """
    expected_reached = {
        "received": [],
        "checked": ["received"],
        "transferred": ["received", "checked"],
        "installed": ["received", "checked", "transferred"],
        "registered": ["received", "checked", "transferred", "installed"],
    }
    for stage, reached in expected_reached.items():
        with start_agent_stub(ready=True, failures={stage: ErrorCode.INVALID_PACKAGE}) as stub:
            with _session(stub) as session:
                stub.stage_package(PACKAGE_BYTES, META)
                response = _client(session.transport).install_package(PACKAGE_BYTES, META)
        assert response.stage == "failed", stage
        assert stub.install_log[-1]["stages_reached"] == reached, stage


def test_injecting_a_code_outside_the_taxonomy_is_refused() -> None:
    """A stub that accepted any string would teach the host to accept any string."""
    stub = GuestAgentStub()
    try:
        with pytest.raises(UniPadError) as excinfo:
            stub.inject_failure("transferred", "banana")
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID
        assert "banana" not in str(excinfo.value.details.get("known_codes", []))
    finally:
        stub.close()


def test_injecting_at_a_stage_outside_the_vocabulary_is_refused() -> None:
    """An injection that validates cleanly and then never fires is worse than none."""
    stub = GuestAgentStub()
    try:
        with pytest.raises(UniPadError) as excinfo:
            stub.inject_failure("transfer", ErrorCode.TRANSFER_FAILED)
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID
        assert "transfer" not in INJECTABLE_STAGES
    finally:
        stub.close()


def test_a_once_failure_fires_once_and_clears_itself() -> None:
    """``mode='once'`` is consumed by the failure it causes.

    Without the consumption the second install would fail identically, and a
    test modelling a transient guest fault would be modelling a permanent one.
    """
    with start_agent_stub(ready=True, failures={"transferred": ErrorCode.TRANSFER_FAILED}) as stub:
        with _session(stub) as session:
            client = _client(session.transport)
            stub.stage_package(PACKAGE_BYTES, META)
            first = client.install_package(PACKAGE_BYTES, META)
            assert first.stage == "failed"
            # Clear by hand: 'once' is not what this stub was built with.
            stub.inject_failure("transferred", ErrorCode.TRANSFER_FAILED, mode="once")
            second = client.install_package(PACKAGE_BYTES, META)
            assert second.stage == "failed"
            third = client.install_package(PACKAGE_BYTES, META)

    assert third.stage == "registered"
    assert [r["stage"] for r in stub.install_log] == ["failed", "failed", "registered"]


def test_the_stale_policies_are_the_documented_two() -> None:
    """The vocabulary is closed, so a typo cannot silently mean 'discard'."""
    assert {STALE_DISCARD, STALE_ANSWER} == {"discard", "answer"}


def test_a_malformed_line_is_answered_with_an_error_not_silence() -> None:
    """A line that is not JSON gets a coded refusal, not a dropped connection."""
    with start_agent_stub(ready=True) as stub:
        transport = AgentChannelTransport("127.0.0.1", stub.port, timeout_s=TIMEOUT_S)
        with transport:
            sock = transport._sock
            assert sock is not None
            sock.sendall(b"this is not json\n")
            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                assert chunk, "the stub hung up instead of answering"
                buffer += chunk
            reply = json.loads(buffer.decode("utf-8"))

    assert reply["type"] == "error"
    assert reply["payload"]["code"] == ErrorCode.QMP_PROTOCOL_ERROR
    assert stub.connections_served == 1
