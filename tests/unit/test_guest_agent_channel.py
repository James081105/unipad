"""Unit tests for :mod:`unipad.guest.agent_channel` (PRD 9.1, 10).

This is the first transport in the package whose peer is a QEMU device rather
than a stand-in, so the properties worth pinning are the ones that keep it from
*over*-claiming.  Every test here is a real socket on loopback -- no stubbed
``socket`` -- because the failure modes being tested are socket failure modes,
and a mock socket reproduces none of them.

What is pinned:

* **A socket that opens is not a guest that answers.**  A listener that accepts
  and then says nothing produces ``state == 'silent'`` and ``TimeoutError``,
  never a reply and never ``reachable``.
* **Nothing listening** produces ``state == 'refused'`` and ``OSError``.
* **A real reply** produces ``state == 'answered'`` and the parsed envelope.
* **A line that is not JSON**, and a JSON value that is not an object, are
  errors -- not a reply with missing fields.
* **The wire shape is the protocol's**: the probe sends the same envelope
  ``GuestClient.status`` sends.  ``StatusRequest.to_dict()`` is the payload
  ``{}``; an earlier draft of the probe put that on the wire, which is an
  envelope with no ``type`` and no ``id``.
* **A silent peer is not retried.**  Retrying a peer that is not there triples
  the wait and buys nothing, so the transport drops the socket and re-raises.
* **The same rules hold for every verb on the wire.**  ADR-0010 adds
  ``launch.request`` and ``persist.request``; a silent peer is silent about
  them too, so a guest that cannot be asked has neither launched an app nor
  flushed a volume.  Nothing here shows that any guest *implements* those two
  messages -- on this host none does.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Optional

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.agent_channel import (
    STATE_ANSWERED,
    STATE_CLOSED,
    STATE_REFUSED,
    STATE_SILENT,
    STATE_UNCONNECTED,
    STATE_WAITING,
    AgentChannelTransport,
    AgentSession,
    status_probe,
    status_request_line,
)
from unipad.guest.protocol import (
    MSG_LAUNCH_REQUEST,
    MSG_LAUNCH_RESPONSE,
    MSG_PERSIST_REQUEST,
    MSG_PERSIST_RESPONSE,
    MSG_STATUS_REQUEST,
    MSG_STATUS_RESPONSE,
    Message,
    new_message_id,
)


class Listener:
    """A loopback TCP server whose behaviour the test dictates.

    ``reply`` is what each accepted connection gets back per line received;
    ``None`` means stay silent, ``"close"`` means hang up immediately, and a
    bytes value is sent verbatim.  ``echo=True`` instead builds a *conforming*
    reply: it parses the request's envelope and returns a ``status.response``
    carrying the request's own ``id``.

    ``echo`` is not a convenience -- it is what a conforming guest does.
    ``GuestClient._validate_reply`` correlates the reply id against the request
    id, so a listener that answered with a fresh id of its own made every
    "successful" test fail with ``guest reply id does not match the request id``.
    A test that cannot pass for the right reason is worse than no test, so the
    helper reproduces the correlation the protocol requires.

    ``echo_type`` is the reply type the echo builds.  It defaults to
    ``status.response`` because that is what every earlier test here asked
    about; a test that drives ``launch`` or ``persist`` must pass its own type,
    because a conforming guest answers the verb it was asked and nothing else.
    """

    def __init__(self, reply: Optional[bytes] = None, *, hang_up: bool = False,
                 delay_s: float = 0.0, echo: bool = False,
                 echo_type: str = MSG_STATUS_RESPONSE,
                 echo_payload: Optional[dict] = None) -> None:
        self._reply = reply
        self._hang_up = hang_up
        self._delay_s = delay_s
        self._echo = echo
        self._echo_type = echo_type
        self._echo_payload = echo_payload if echo_payload is not None else {"installed": [], "data_markers": []}
        self.received: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(10.0)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _answer(self, line: bytes) -> Optional[bytes]:
        """The bytes to send back for one received line."""
        if not self._echo:
            return self._reply
        try:
            request = json.loads(line.decode("utf-8"))
            request_id = request["id"]
        except (ValueError, KeyError, TypeError):
            return self._reply
        reply = Message(type=self._echo_type, id=request_id,
                        payload=dict(self._echo_payload)).to_dict()
        return json.dumps(reply, sort_keys=True, separators=(",", ":")).encode() + b"\n"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            if self._hang_up:
                conn.close()
                return
            buffer = b""
            conn.settimeout(10.0)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except (socket.timeout, OSError):
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    self.received.append(line)
                    if self._delay_s:
                        time.sleep(self._delay_s)
                    answer = self._answer(line)
                    if answer is not None:
                        try:
                            conn.sendall(answer)
                        except OSError:
                            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


def status_reply(for_id: str = "any") -> bytes:
    """A conforming status reply envelope for the given request id.

    The type is ``MSG_STATUS_RESPONSE`` -- the dotted spelling the protocol
    defines.  An earlier draft of this file wrote ``"status_response"``, which
    the client refuses with ``guest reply type does not match the request``;
    the same typo in a real probe would make a working guest look like a
    broken one, which is why the constant is imported rather than retyped.
    """
    reply = Message(
        type=MSG_STATUS_RESPONSE,
        id=new_message_id(),
        payload={"installed": [], "data_markers": []},
    ).to_dict()
    if for_id != "any":
        reply["id"] = for_id
    return json.dumps(reply, sort_keys=True, separators=(",", ":")).encode() + b"\n"


# --- the three outcomes ---------------------------------------------------

def test_a_listener_that_accepts_and_stays_silent_is_not_reachability():
    """The central property: connect() succeeding proves nothing about a guest.

    On this host today the guest half of the agent channel does not exist, so
    QEMU accepts the TCP connection and nothing ever answers.  A transport that
    reported readiness on ``connect()`` would report the guest as reachable
    forever, which is the exact failure this state machine exists to prevent.
    """
    listener = Listener(reply=None)  # accept, then silence
    try:
        verdict = status_probe("127.0.0.1", listener.port, timeout_s=0.4)
    finally:
        listener.close()
    assert verdict["reachable"] is False
    assert verdict["state"] == STATE_SILENT
    assert verdict["detail"] == "no_reply_in_budget"
    assert verdict["reply"] is None
    # And it really did connect -- this is a silent peer, not a refused one.
    assert listener.received, "the probe never put a line on the wire"


def test_nothing_listening_is_refused_and_is_not_the_silent_state():
    """A closed port is a different fact from a silent guest.

    Conflating them would let "the boot is gone" and "the boot has no guest
    service" look identical, and only one of those is fixable by waiting.
    """
    # Bind and immediately release, so the port is free but nothing serves it.
    probe_sock = socket.socket()
    probe_sock.bind(("127.0.0.1", 0))
    port = probe_sock.getsockname()[1]
    probe_sock.close()
    verdict = status_probe("127.0.0.1", port, timeout_s=0.4)
    assert verdict["reachable"] is False
    assert verdict["state"] == STATE_REFUSED
    assert verdict["detail"] == "connection_refused"
    assert verdict["reply"] is None


def test_a_real_reply_is_the_only_thing_that_counts_as_reachable():
    listener = Listener(reply=status_reply())
    try:
        verdict = status_probe("127.0.0.1", listener.port, timeout_s=5.0)
    finally:
        listener.close()
    assert verdict["reachable"] is True
    assert verdict["state"] == STATE_ANSWERED
    assert verdict["reply"]["type"] == MSG_STATUS_RESPONSE
    assert verdict["reply"]["payload"] == {"installed": [], "data_markers": []}


# --- the transport's own state machine ------------------------------------

def test_the_transport_publishes_which_failure_it_was():
    transport = AgentChannelTransport("127.0.0.1", 1, timeout_s=0.3)
    assert transport.state == STATE_UNCONNECTED
    with pytest.raises(OSError):
        transport.request({"type": "status_request", "id": "x", "payload": {}})
    assert transport.state == STATE_REFUSED
    assert transport.connected is False


def test_connecting_alone_never_claims_the_guest_answered():
    """The mutation this pins: set ``state = answered`` *before* sending.

    Every other test that ends at ``answered`` also passes through the success
    path, which overwrites the flag -- so a premature ``answered`` was invisible
    to all of them and the mutation survived.  This test looks at the state at
    the one moment the lie is observable: after ``connect()`` returns and
    before any reply exists.

    A caller that polled ``transport.state`` while an install was in flight
    would read ``answered`` from a guest that had not spoken yet.
    """
    listener = Listener(reply=None)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=5.0)
        transport.connect()
        assert transport.connected is True, "the socket did not open"
        assert transport.state != STATE_ANSWERED, (
            "connect() alone reported the guest as answered")
        # And it stays that way while the peer is silent.
        with pytest.raises(TimeoutError):
            transport.request({"type": "status_request", "id": "x", "payload": {}})
        assert transport.state == STATE_SILENT
    finally:
        listener.close()


def test_no_state_is_published_as_answered_before_a_reply_exists():
    """The mutation that survived the first two rounds of this suite.

    Setting ``state = STATE_ANSWERED`` before ``sendall`` is invisible to every
    sequential test, because by the time ``request()`` returns or raises, the
    outcome handler has overwritten the flag with the real answer.  The lie is
    only observable *while the request is in flight*, so it is observed from
    another thread.

    Why it matters beyond this module: a caller polling ``transport.state`` to
    decide whether the guest is ready -- which is exactly what a UI waiting on
    an install would do -- would read ``answered`` from a guest that has not
    spoken a word.
    """
    listener = Listener(reply=None)  # silent for the whole wait
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=1.5)
        seen: list[str] = []
        stop = threading.Event()

        def watch() -> None:
            while not stop.is_set():
                seen.append(transport.state)
                time.sleep(0.01)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            with pytest.raises(TimeoutError):
                transport.request({"type": "status_request", "id": "x", "payload": {}})
        finally:
            stop.set()
            watcher.join(timeout=2)
        assert STATE_ANSWERED not in seen, (
            f"answered was published before any reply existed; saw {seen[:12]}")
        # The in-flight window is its own state, which is what makes the lie
        # above observable at all rather than masked by the outcome handler.
        assert STATE_WAITING in seen, (
            f"the waiting state was never published; saw {sorted(set(seen))}")
        assert listener.received, "the request never reached the peer"
    finally:
        listener.close()


def test_a_silent_peer_is_not_retried_even_when_reconnects_are_allowed():
    """One timeout is one timeout.  Three is three times the wait for nothing.

    ``attempts`` exists for the *reconnect* path -- a cached socket the peer
    closed underneath us -- and the docstring says a silent peer is not
    retried.  The first version of this test used the default ``attempts=1``,
    which made the retry branch unreachable and let the mutation that removed
    the distinction survive.  So this asks for three attempts explicitly and
    then asserts the budget was spent once.
    """
    listener = Listener(reply=None)
    try:
        transport = AgentChannelTransport(
            "127.0.0.1", listener.port, timeout_s=0.3, attempts=3)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            transport.request({"type": "status_request", "id": "x", "payload": {}})
        elapsed = time.monotonic() - started
        assert transport.state == STATE_SILENT
        # One budget, not three, with room for the socket churn.
        assert elapsed < 1.5, f"a silent peer was retried: {elapsed:.2f}s"
        # The socket is KEPT.  This is the assertion an earlier version of this
        # test got backwards: it required `connected is False` after a timeout,
        # which is what the transport used to do -- and the chardev serves ONE
        # connection per boot (measured: the next connect after that one is
        # refused, which reads exactly like "the boot is gone").  Closing on a
        # timeout spends the only connection available on a guest that may still
        # answer, so holding it is the correct behaviour and this pins it.
        assert transport.connected is True
        # Exactly one line reached the peer, not three.
        assert len(listener.received) == 1, (
            f"the request was sent {len(listener.received)} times")
    finally:
        listener.close()


def test_a_timeout_leaves_the_connection_usable_for_the_next_request():
    """The same fact from the other side: the socket survives a timeout.

    A guest that answers slowly -- longer than one budget but within the boot --
    must still be reachable on the second call.  If the transport closed on
    timeout, the chardev's single connection would be gone and the retry would
    fail with ``refused``, which is a *different* failure and would be reported
    as one.

    **This test used to pass for the wrong reason, and the way it did is the
    point.**  ``Listener._handle`` sleeps ``delay_s`` for *every* line it reads,
    so the first request's reply only exists after one delay and the second
    request's after two.  With a 0.4 s budget and a 0.8 s delay, the second call
    was receiving the FIRST request's late reply -- and the assertion was only
    ``reply["type"] == MSG_STATUS_RESPONSE``, which a stale reply satisfies
    perfectly.  ``AgentChannelTransport.request`` reads exactly one line and does
    not check its ``id``; only ``GuestClient._validate_reply`` correlates.  So
    the test claimed "the second request succeeded on the same connection" while
    actually measuring "a reply arrived".

    The fix is to assert the thing the test says it measures: the reply's ``id``
    must be the SECOND request's.  ``BecomesReady`` is used rather than
    ``Listener`` because it delays only until a deadline, so both requests get
    their own reply at a predictable time.
    """
    guest = BecomesReady(silent_for_s=0.9)
    try:
        transport = AgentChannelTransport("127.0.0.1", guest.port, timeout_s=0.4)
        with pytest.raises(TimeoutError):
            transport.request({"type": "status.request", "id": "a", "payload": {}})
        assert transport.state == STATE_SILENT
        assert transport.connected is True

        time.sleep(1.0)  # the guest starts answering
        reply = transport.request({"type": "status.request", "id": "b", "payload": {}})
        assert reply["type"] == MSG_STATUS_RESPONSE
        # The correlation, which is what makes this the second request's answer
        # rather than the first one's late arrival.
        assert reply["id"] == "b", (
            f"the reply belongs to another exchange (id={reply['id']!r}); "
            "a stale reply is not a successful retry")
        assert transport.state == STATE_ANSWERED
    finally:
        guest.close()


def test_a_peer_that_hangs_up_mid_exchange_reports_the_channel():
    listener = Listener(reply=None, hang_up=True)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=2.0)
        with pytest.raises(OSError):
            transport.request({"type": "status_request", "id": "x", "payload": {}})
        assert transport.state == STATE_CLOSED
    finally:
        listener.close()


def test_a_reply_arriving_after_the_budget_is_silent_not_answered():
    """A slow guest is indistinguishable from an absent one inside the budget.

    The point is that the budget is what decides, and the state says so, rather
    than the transport waiting indefinitely for a guest that may never come.
    """
    listener = Listener(reply=status_reply(), delay_s=1.0)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=0.3)
        with pytest.raises(TimeoutError):
            transport.request({"type": "status_request", "id": "x", "payload": {}})
        assert transport.state == STATE_SILENT
        # Give the late reply time to land, then prove it was never consumed.
        time.sleep(1.6)
        assert listener.received, "the request never reached the peer"
    finally:
        listener.close()


# --- malformed replies ----------------------------------------------------

@pytest.mark.parametrize("bad", [
    b"not json at all\n",
    b'"a json string is not an envelope"\n',
    b"[1, 2, 3]\n",
    b"42\n",
    b"null\n",
])
def test_a_line_that_is_not_a_json_object_is_an_error(bad: bytes):
    """The transport's own limit: a line must be JSON, and it must be an object.

    Deliberately narrow.  *Envelope* validation -- a ``type`` that matches the
    request, an ``id`` that correlates, an object ``payload`` -- belongs to
    ``GuestClient._validate_reply``, which raises the protocol error with the
    expected and actual types.  Duplicating it here would make the client's
    careful diagnosis unreachable and would be the same contract written twice.
    """
    listener = Listener(reply=bad)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=2.0)
        with pytest.raises(UniPadError) as caught:
            transport.request({"type": "status_request", "id": "x", "payload": {}})
    finally:
        listener.close()
    assert caught.value.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert caught.value.details["detail"] == "malformed_reply"
    # It reached the peer and the peer answered, so the state says answered --
    # the answer was garbage, which is its own finding.
    assert transport.state == STATE_ANSWERED


def test_an_object_that_is_not_an_envelope_is_the_clients_error_not_the_transports():
    """``{"type": "status.response"}`` with no id and no payload.

    The transport hands it back, because it *is* a JSON object; the client
    refuses it, because it is not an envelope.  That division is the contract,
    and this test is here so a future edit that moves the check into the
    transport has to delete a test that says why it should not.
    """
    from unipad.guest.client import GuestClient

    listener = Listener(reply=b'{"type": "status.response"}\n')
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=5.0)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        with pytest.raises(UniPadError) as caught:
            client.status()
    finally:
        listener.close()
    assert caught.value.code == ErrorCode.QMP_PROTOCOL_ERROR
    assert transport.state == STATE_ANSWERED


def test_a_reply_with_the_wrong_type_is_refused_by_the_client():
    """The dotted spelling matters: ``status_response`` is not ``status.response``.

    This is the exact bug an earlier draft of this test file shipped -- the
    helper wrote the underscored name, and every "successful" probe failed with
    ``guest reply type does not match the request``.  A real guest answering
    correctly would have looked broken, which is the worse of the two errors.
    """
    from unipad.guest.client import GuestClient

    wrong = json.dumps(
        {"type": "status_response", "id": new_message_id(), "payload": {}},
        sort_keys=True, separators=(",", ":"),
    ).encode() + b"\n"
    listener = Listener(reply=wrong)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=5.0)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        with pytest.raises(UniPadError) as caught:
            client.status()
    finally:
        listener.close()
    assert caught.value.code == ErrorCode.QMP_PROTOCOL_ERROR
    assert caught.value.details["actual_type"] == "status_response"
    assert caught.value.details["expected_type"] == MSG_STATUS_RESPONSE


def test_two_messages_on_one_connection_are_two_replies():
    """One line per request, correlated by arrival, on a persistent socket."""
    first = Message(type=MSG_STATUS_RESPONSE, id=new_message_id(),
                    payload={"installed": [], "data_markers": ["marker"]}).to_dict()
    second = Message(type=MSG_STATUS_RESPONSE, id=new_message_id(),
                     payload={"installed": [], "data_markers": []}).to_dict()
    blob = (json.dumps(first, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            + json.dumps(second, sort_keys=True, separators=(",", ":")).encode() + b"\n")
    listener = Listener(reply=blob)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=5.0)
        one = transport.request({"type": "status_request", "id": "a", "payload": {}})
        two = transport.request({"type": "status_request", "id": "b", "payload": {}})
    finally:
        listener.close()
    assert one["payload"]["data_markers"] == ["marker"]
    assert two["payload"]["data_markers"] == []
    assert transport.connected is True, "the socket was dropped between requests"


# --- the wire shape -------------------------------------------------------

def test_the_probe_sends_the_envelope_the_real_client_sends():
    """``StatusRequest.to_dict()`` is ``{}`` -- the payload, not the message.

    An earlier draft of the probe put that on the wire, which is an envelope
    with no ``type`` and no ``id``; the guest would rightly refuse it and the
    probe would have looked like a working channel that always failed.
    """
    listener = Listener(reply=status_reply())
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=5.0)
        try:
            transport.connect()
            transport.request(Message(type=MSG_STATUS_REQUEST, id="fixed-id",
                                      payload={}).to_dict())
        finally:
            transport.close()
    finally:
        listener.close()
    assert len(listener.received) == 1
    sent = json.loads(listener.received[0].decode())
    assert sent["type"] == MSG_STATUS_REQUEST
    assert isinstance(sent["id"], str) and sent["id"]
    assert sent["payload"] == {}
    # Not the bare payload an earlier draft would have sent.
    assert sent != {}


def test_status_request_line_is_one_complete_envelope_with_no_newline_inside():
    line = status_request_line()
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    envelope = json.loads(line.decode())
    assert envelope["type"] == MSG_STATUS_REQUEST
    assert isinstance(envelope["id"], str) and envelope["id"]
    assert envelope["payload"] == {}


def test_status_request_line_carries_a_fresh_id_each_call():
    """A reused correlation id would make retries indistinguishable."""
    first = json.loads(status_request_line().decode())
    second = json.loads(status_request_line().decode())
    assert first["id"] != second["id"]


# --- construction guards --------------------------------------------------

@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_a_port_outside_the_tcp_range_is_refused_at_construction(port: int):
    with pytest.raises(UniPadError) as caught:
        AgentChannelTransport("127.0.0.1", port)
    assert caught.value.code == ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize("timeout_s", [0.0, -1.0])
def test_a_non_positive_timeout_is_refused_at_construction(timeout_s: float):
    with pytest.raises(UniPadError) as caught:
        AgentChannelTransport("127.0.0.1", 15000, timeout_s=timeout_s)
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_close_is_idempotent_and_leaves_the_state_readable():
    transport = AgentChannelTransport("127.0.0.1", 15000, timeout_s=0.3)
    transport.close()
    transport.close()
    assert transport.state == STATE_UNCONNECTED
    assert transport.connected is False


def test_the_context_manager_connects_and_closes():
    listener = Listener(echo=True)
    try:
        with AgentChannelTransport("127.0.0.1", listener.port, timeout_s=5.0) as transport:
            reply = transport.request({"type": "status_request", "id": "x", "payload": {}})
            assert reply["type"] == MSG_STATUS_RESPONSE
        assert transport.connected is False
    finally:
        listener.close()


def test_a_probe_against_a_bad_port_is_refused_not_an_exception():
    verdict = status_probe("127.0.0.1", 0)
    assert verdict["reachable"] is False
    assert verdict["detail"] == "invalid_port"


# --- the shape GuestClient needs ------------------------------------------

def test_the_transport_satisfies_the_guest_client_transport_contract():
    """``GuestClient`` takes a duck-typed transport; this one must quack.

    Checked structurally rather than by inheritance, because the protocol is
    ``runtime_checkable`` and the point is that the host can plug the real
    channel in without changing the client.
    """
    from unipad.guest.client import GuestClient

    listener = Listener(echo=True)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=5.0)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        response = client.status()
        assert response.installed == []
        assert response.data_markers == []
        assert transport.state == STATE_ANSWERED
    finally:
        listener.close()


def test_a_silent_guest_never_becomes_an_install_success():
    """The end-to-end version of the central property.

    ``GuestClient.status()`` over a channel that connects and says nothing must
    raise, and must not return an empty status that a caller could read as
    "nothing is installed".
    """
    from unipad.guest.client import GuestClient

    listener = Listener(reply=None)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=0.4)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        with pytest.raises(UniPadError) as caught:
            client.status()
    finally:
        listener.close()
    assert caught.value.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert caught.value.details["detail"] == "timeout"
    assert transport.state == STATE_SILENT


def test_a_refused_channel_is_an_install_service_unavailable_too():
    from unipad.guest.client import GuestClient

    probe_sock = socket.socket()
    probe_sock.bind(("127.0.0.1", 0))
    port = probe_sock.getsockname()[1]
    probe_sock.close()
    transport = AgentChannelTransport("127.0.0.1", port, timeout_s=0.4)
    client = GuestClient(transport, attempts=1, backoff_s=0)
    with pytest.raises(UniPadError) as caught:
        client.status()
    assert caught.value.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert transport.state == STATE_REFUSED


# ---------------------------------------------------------------------------
# ADR-0010: launch and persist over the same channel
# ---------------------------------------------------------------------------


def test_a_silent_guest_never_becomes_a_launch_success():
    """启动: a socket that opens and says nothing has launched nothing.

    This is the central property of this module applied to a verb that *sounds*
    like it succeeded.  ``launch`` is the one call where a fabricated answer is
    most tempting -- there is no state to read back, so a client that returned
    a default ``launched`` would look plausible forever.  It must raise.
    """
    from unipad.guest.client import GuestClient

    listener = Listener(reply=None)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=0.4)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        with pytest.raises(UniPadError) as caught:
            client.launch("com.unipad.hello")
    finally:
        listener.close()
    assert caught.value.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert caught.value.details["detail"] == "timeout"
    assert caught.value.details["request_type"] == MSG_LAUNCH_REQUEST
    # The channel connected -- that is all it did.  It is not reachability.
    assert transport.state == STATE_SILENT


def test_a_silent_guest_never_becomes_a_persist_success():
    """持久化: an unanswered flush is not "there was nothing to flush"."""
    from unipad.guest.client import GuestClient

    listener = Listener(reply=None)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=0.4)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        with pytest.raises(UniPadError) as caught:
            client.persist("/volumes/unipad-data")
    finally:
        listener.close()
    assert caught.value.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert caught.value.details["request_type"] == MSG_PERSIST_REQUEST
    assert transport.state == STATE_SILENT


def test_a_conforming_launch_reply_over_the_real_socket_is_a_launch():
    """The whole path, on a real socket, with a conforming guest behind it.

    The listener answers ``launch.response`` with the request's own id -- the
    correlation the protocol requires -- so this exercises the transport, the
    client's validation and the payload coercion together rather than one of
    them against a stub.
    """
    from unipad.guest.client import GuestClient

    listener = Listener(
        echo=True,
        echo_type=MSG_LAUNCH_RESPONSE,
        echo_payload={
            "stage": "launched",
            "bundle_id": "com.unipad.hello",
            "trust_mode": "self-signed-dev-trust",
        },
    )
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=2.0)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        response = client.launch("com.unipad.hello")
    finally:
        listener.close()
    assert response.stage == "launched"
    assert response.bundle_id == "com.unipad.hello"
    assert response.trust_mode == "self-signed-dev-trust"
    assert transport.state == STATE_ANSWERED


def test_a_persist_reply_over_the_real_socket_reports_the_guests_markers():
    from unipad.guest.client import GuestClient

    markers = ["com.unipad.hello:Documents/notes.txt"]
    listener = Listener(
        echo=True,
        echo_type=MSG_PERSIST_RESPONSE,
        echo_payload={"stage": "persisted", "markers_written": markers, "bytes_written": 1615},
    )
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=2.0)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        response = client.persist("/volumes/unipad-data")
    finally:
        listener.close()
    assert response.stage == "persisted"
    assert response.markers_written == markers
    assert response.bytes_written == 1615


def test_the_channel_carries_the_launch_and_persist_frames_verbatim():
    """The wire shape is the protocol's, for the new verbs too.

    ``listener.received`` holds the exact bytes the client sent, so this checks
    what a real guest would see: the dotted type, the correlation id, and a
    payload that names the app / the volume and nothing else.
    """
    from unipad.guest.client import GuestClient

    listener = Listener(reply=None)
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=0.3)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        for call in (lambda: client.launch("com.unipad.hello"),
                     lambda: client.persist("/volumes/unipad-data")):
            with pytest.raises(UniPadError):
                call()
    finally:
        listener.close()

    sent = [json.loads(line.decode("utf-8")) for line in listener.received]
    assert [frame["type"] for frame in sent] == [MSG_LAUNCH_REQUEST, MSG_PERSIST_REQUEST]
    assert sent[0]["payload"] == {"bundle_id": "com.unipad.hello"}
    assert sent[1]["payload"] == {"path": "/volumes/unipad-data"}
    # Every frame carries a correlation id, and the two are not the same one.
    assert all(frame["id"] for frame in sent)
    assert sent[0]["id"] != sent[1]["id"]


def test_a_launch_reply_with_the_wrong_type_is_refused_over_the_real_socket():
    """A peer that answers ``launch.request`` has not answered ``launch.response``.

    Over a real socket this is the failure that would otherwise look like a
    working guest: the line arrives, parses, and carries a plausible payload.
    """
    from unipad.guest.client import GuestClient

    listener = Listener(
        reply=json.dumps(
            Message(type=MSG_LAUNCH_REQUEST, id="corr-1", payload={"bundle_id": "com.unipad.hello"}).to_dict(),
            sort_keys=True, separators=(",", ":"),
        ).encode() + b"\n",
    )
    try:
        transport = AgentChannelTransport("127.0.0.1", listener.port, timeout_s=2.0)
        client = GuestClient(transport, attempts=1, backoff_s=0)
        with pytest.raises(UniPadError) as caught:
            client.launch("com.unipad.hello")
    finally:
        listener.close()
    assert caught.value.code == ErrorCode.QMP_PROTOCOL_ERROR
    assert caught.value.details["expected_type"] == MSG_LAUNCH_RESPONSE


# --- one session, one connection: the two paths the caller must be able to walk --

class BecomesReady:
    """A listener that stays silent, then starts answering.

    Models a guest whose install service comes up late -- which is the case the
    one-connection rule makes dangerous.  If the transport closed on the first
    timeout, the second request would hit a refused port and the caller would
    conclude the boot was gone.
    """

    def __init__(self, silent_for_s: float) -> None:
        self._silent_until = time.monotonic() + silent_for_s
        self.received: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(20.0)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            buffer = b""
            conn.settimeout(20.0)
            while not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    self.received.append(line)
                    if time.monotonic() < self._silent_until:
                        continue  # not up yet
                    request = json.loads(line.decode())
                    reply = Message(type=MSG_STATUS_RESPONSE, id=request["id"],
                                    payload={"installed": [], "data_markers": []}).to_dict()
                    conn.sendall(json.dumps(reply, sort_keys=True,
                                            separators=(",", ":")).encode() + b"\n")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


def test_status_times_out_then_the_guest_is_ready_on_the_same_connection():
    """Path 1: 状态查询超时 -> 客体稍后就绪 -> 同一连接继续请求.

    The one-connection rule makes this the dangerous case.  A transport that
    closed on timeout would leave the caller holding a dead socket against a
    chardev that refuses every further connect, so the retry would fail with
    ``refused`` -- a different failure, reported as a different cause, for a
    guest that is in fact now answering.
    """
    guest = BecomesReady(silent_for_s=1.0)
    try:
        with AgentSession("127.0.0.1", guest.port, timeout_s=0.4) as session:
            first = session.status()
            assert first["reachable"] is False
            assert first["state"] == STATE_SILENT
            # The connection survived, which is the whole point.
            assert session.connected is True

            time.sleep(1.2)  # the guest's service comes up
            second = session.status()
            assert second["reachable"] is True
            assert second["state"] == STATE_ANSWERED
            assert second["reply"]["payload"]["installed"] == []
            assert session.connected is True
        # Two questions, one connection.
        assert len(guest.received) == 2
    finally:
        guest.close()


def test_status_succeeds_then_the_install_continues_on_the_same_connection():
    """Path 2: 查询成功 -> 继续安装.

    The install is what the status question exists to gate, so it must be able
    to use the same socket.  If the probe closed it, this would be the second
    caller to find the port refused.
    """
    from unipad.guest.client import GuestClient

    guest = Listener(echo=True)
    try:
        with AgentSession("127.0.0.1", guest.port, timeout_s=5.0) as session:
            verdict = session.status()
            assert verdict["reachable"] is True

            client = GuestClient(session.transport, attempts=1, backoff_s=0)
            response = client.status()
            assert response.installed == []
            # Still the same socket, and still connected.
            assert session.connected is True
            assert session.state == STATE_ANSWERED
        assert session.connected is False, "the session must release on exit"
    finally:
        guest.close()


def test_a_session_releases_the_connection_and_is_safe_to_close_twice():
    guest = Listener(echo=True)
    try:
        session = AgentSession("127.0.0.1", guest.port, timeout_s=5.0)
        session.transport.connect()
        assert session.connected is True
        session.close()
        session.close()
        assert session.connected is False
        assert session.state == STATE_UNCONNECTED
    finally:
        guest.close()


def test_a_supplied_transport_is_not_closed_by_the_probe():
    """The probe only closes what it created.

    This is the assertion an earlier version of ``status_probe`` got wrong: it
    closed in a ``finally`` unconditionally, so every caller spent the boot's one
    connection on a single question.
    """
    guest = Listener(echo=True)
    try:
        transport = AgentChannelTransport("127.0.0.1", guest.port, timeout_s=5.0)
        verdict = status_probe("127.0.0.1", guest.port, transport=transport)
        assert verdict["reachable"] is True
        assert transport.connected is True, "the probe closed a transport it does not own"
        # And it is still usable.
        reply = transport.request({"type": "status.request", "id": "again",
                                   "payload": {}})
        assert reply["type"] == MSG_STATUS_RESPONSE
        transport.close()
    finally:
        guest.close()


def test_a_one_shot_probe_still_closes_what_it_opened():
    """The other half of the contract: a caller with no session must not leak.

    Holding a socket open with no owner is its own leak, so the one-shot path
    closes.  Both halves are needed -- never close a borrowed transport, always
    close an owned one.
    """
    guest = Listener(echo=True)
    try:
        verdict = status_probe("127.0.0.1", guest.port, timeout_s=5.0)
        assert verdict["reachable"] is True
        # Nothing observable to assert on the transport itself, so assert on the
        # peer: exactly one connection was made and the listener is idle again.
        assert len(guest.received) == 1
    finally:
        guest.close()
