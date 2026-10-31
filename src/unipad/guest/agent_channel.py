"""The real channel: a host client for the guest agent's socket (PRD 9.1, 10).

Until now every transport in this package was a stand-in -- ``FakeTransport``
in-process, ``FakeTcpPair`` over loopback with a fake service behind it.  Both
exercise the client correctly and neither proves anything about a guest, because
there is no guest on the other end.  This module is the first transport whose
peer is a QEMU device.

Where the socket comes from
---------------------------
``unipad.host.unixnu_boot._agent_argv`` emits::

    -chardev socket,id=unipad-agent,host=127.0.0.1,port=P,server=on,wait=off,logfile=...
    -device  virtio-serial-device,id=unipad-agent-serial
    -device  virtconsole,chardev=unipad-agent,name=unipad-agent-transport

``server=on`` means **QEMU listens and the host connects** -- the reverse of
``FakeTcpPair``, where the test is the server.  So this is a client, and the
port is only reachable while a boot that asked for the channel is running.

ONE connection per boot, and that is a trap
-------------------------------------------
The socket chardev serves **exactly one** TCP connection for the life of the
QEMU process.  MEASURED (``chardev-connection-count.py``): the first connect is
accepted; every connect after that one is closed -- including one made while the
first is still held -- is **refused** with ``ECONNREFUSED``, immediately and
permanently, not timed out.

That matters because a refusal is what "no boot asked for the channel" also
looks like.  So a client that probed, timed out, closed, and probed again would
read ``refused`` and conclude the boot was gone, when in fact it had spent the
one connection the chardev had.  The measurement behind this note did exactly
that by accident: an earlier version of this transport called ``close()`` in its
timeout handler, which turned one honest timeout into a permanent
``connection_refused`` on the next call.

So this transport holds its socket across a timeout.  A silent peer is not
evidence that the channel is finished, and treating it as such destroys the only
connection available.  The socket is dropped only when the peer has actually
gone away (``OSError``), and :meth:`AgentChannelTransport.close` exists for a
caller that is genuinely done with the boot.

The states, and why they are not one state
------------------------------------------------
A socket that accepts a connection is not a guest that answers.  On this host
today the guest half does not exist: ``NativeVirtioConsole`` is in the device
repository but not in this board's build (``board.toml``'s archives list did not
name it until the fork's ``acf2f86``, and no macOS runner has built it since),
so the kernel enumerates nothing on that transport.  QEMU still accepts the TCP
connection, because accepting TCP is the chardev's job and the virtio-serial
device exists whether or not the guest has a driver.

That makes "connected" worthless as an answer and dangerous as a signal: a
client that treats a successful ``connect()`` as readiness will report the guest
as reachable forever.  So this transport keeps the outcomes distinct and
publishes the latest one on :attr:`AgentChannelTransport.state`:

``refused``
    ``connect()`` failed.  No boot asked for the channel, or it is gone.  The
    caller can conclude the *channel* is absent.
``waiting``
    Connected and the request is on the wire.  The reply has not arrived, so
    nothing about the guest is knowable yet -- and this state is published
    *while it is true*, which is the only way a caller polling ``state`` can
    tell an in-flight request from a finished one.
``silent``
    Connected, sent the request, no line within the budget.  The caller can
    conclude the *guest service* is absent -- which is exactly today's measured
    state -- but must not conclude the socket is broken.
``answered``
    A complete line arrived and parsed.  Only now does anything about the guest
    become knowable, and even then only what the reply says.
``closed``
    The peer hung up underneath us.  The channel existed and stopped.

``state`` is deliberately a plain attribute rather than an exception type: the
caller's question ("can I ask the guest anything?") outlives any single request,
and a retry loop inside :class:`unipad.guest.client.GuestClient` would otherwise
erase it.

Dependency direction: imports :mod:`unipad.errors`, :mod:`unipad.guest.protocol`
and the standard library only.  It must never import :mod:`unipad.ipa` or
:mod:`unipad.host` -- this module is the *seam* the host plugs into, not a
consumer of it.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Optional

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.protocol import MSG_STATUS_REQUEST, Message, new_message_id

#: What :attr:`AgentChannelTransport.state` reads before anything has happened.
STATE_UNCONNECTED = "unconnected"
#: Connected and the request is on the wire, waiting for the reply line.
STATE_WAITING = "waiting"
#: ``connect()`` failed -- the channel itself is absent.
STATE_REFUSED = "refused"
#: Connected, request sent, no line in budget -- the guest service is absent.
STATE_SILENT = "silent"
#: A complete line arrived and parsed as JSON.
STATE_ANSWERED = "answered"
#: The peer closed the connection underneath us.
STATE_CLOSED = "closed"

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_CONNECT_TIMEOUT_S = 5.0


class AgentChannelTransport:
    """A ``GuestClient`` transport over the guest agent's TCP socket.

    Args:
        host: loopback address the chardev listens on.
        port: the ``agent_port`` the boot was given.
        timeout_s: per-request budget for the reply line.
        connect_timeout_s: budget for ``connect()`` alone.
        attempts: reconnects tried when the cached socket has gone away.  A
            silent peer is NOT retried -- retrying a peer that is not there
            turns one honest timeout into three and triples the wait.

    Attributes:
        state: the latest of the six ``STATE_*`` outcomes.  Read it after a
            failed call to learn *which* failure it was.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        attempts: int = 1,
    ) -> None:
        if not 1 <= int(port) <= 65535:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the agent port must be a TCP port in 1..65535",
                port=repr(port),
            )
        if timeout_s <= 0 or connect_timeout_s <= 0:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "timeouts must be positive",
                timeout_s=timeout_s,
                connect_timeout_s=connect_timeout_s,
            )
        if attempts < 1:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID, "attempts must be >= 1", attempts=attempts
            )
        self.host = host
        self.port = int(port)
        self.timeout_s = float(timeout_s)
        self.connect_timeout_s = float(connect_timeout_s)
        self.attempts = int(attempts)
        self.state = STATE_UNCONNECTED
        self._sock: Optional[socket.socket] = None
        self._buffer = b""

    # --- connection ---

    @property
    def connected(self) -> bool:
        """Whether a socket is currently held.  Not a readiness claim."""
        return self._sock is not None

    def connect(self) -> None:
        """Open the socket, or raise ``OSError`` with ``state == 'refused'``.

        Idempotent: an existing socket is kept rather than replaced, because
        replacing it would drop whatever the peer has already sent.
        """
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout_s
            )
        except OSError:
            self.state = STATE_REFUSED
            raise
        sock.settimeout(self.timeout_s)
        self._sock = sock
        self._buffer = b""

    def close(self) -> None:
        """Drop the socket and any buffered line.  Safe to call twice."""
        sock, self._sock = self._sock, None
        self._buffer = b""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "AgentChannelTransport":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- the transport contract ---

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send one message envelope and return the reply envelope.

        Raises:
            OSError: the channel failed -- nothing listening, or the peer
                closed.  ``state`` says which.
            TimeoutError: connected and sent, but no line inside the budget.
                ``state`` is ``'silent'``.
        """
        line = json.dumps(
            message, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        for attempt in range(1, self.attempts + 1):
            if self._sock is None:
                try:
                    self.connect()
                except OSError:
                    raise
            assert self._sock is not None
            self.state = STATE_WAITING
            try:
                self._sock.sendall(line + b"\n")
                raw = self._read_line()
            except TimeoutError:
                # A silent peer is silent on every attempt; retrying only
                # multiplies the wait.  The socket is deliberately KEPT: the
                # chardev serves one connection per boot and a refused connect
                # afterwards is indistinguishable from a dead boot, so closing
                # here would spend the only connection available on a guest
                # that may still answer.  See the module docstring.
                self.state = STATE_SILENT
                raise
            except OSError:
                self.state = STATE_CLOSED
                self.close()
                if attempt >= self.attempts:
                    raise
                continue
            self.state = STATE_ANSWERED
            try:
                reply = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as error:
                raise UniPadError(
                    ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
                    "the agent channel returned a line that is not JSON",
                    detail="malformed_reply",
                    error=str(error),
                ) from error
            if not isinstance(reply, dict):
                raise UniPadError(
                    ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
                    "the agent channel returned a JSON value that is not an object",
                    detail="malformed_reply",
                    reply_type=type(reply).__name__,
                )
            return reply
        raise UniPadError(  # pragma: no cover - attempts >= 1 is enforced
            ErrorCode.INSTALL_SERVICE_UNAVAILABLE, "the agent channel gave up"
        )

    def _read_line(self) -> bytes:
        """Block until one complete line is buffered, and return it."""
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(65536) if self._sock else b""
            if not chunk:
                raise OSError("the agent channel closed the connection")
            self._buffer += chunk
        raw, _, self._buffer = self._buffer.partition(b"\n")
        return raw


def status_probe(
    host: str,
    port: int,
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Ask the agent channel one status question and report how it went.

    This exists so a caller can learn whether the guest service exists *before*
    committing to an install, and so that "no" is reported as a structured
    verdict rather than an exception the caller has to interpret.

    Returns a dict with:

    ``reachable``
        ``True`` only when a reply line arrived and parsed.  Never ``True`` for
        a connection that merely opened.
    ``state``
        one of the ``STATE_*`` values.
    ``detail``
        a short machine-readable reason when ``reachable`` is ``False``.
    ``reply``
        the raw reply envelope when there was one, else ``None``.

    The status message is built through :class:`unipad.guest.protocol.Message`
    -- the same envelope :meth:`unipad.guest.client.GuestClient.status` sends, so
    the probe asks the question the real client asks and not a near-miss of it.
    (``StatusRequest.to_dict()`` is the *payload*, ``{}``; putting that on the
    wire would be an envelope with no ``type`` and no ``id``, which the guest
    would rightly refuse.)
    """
    envelope = Message(type=MSG_STATUS_REQUEST, id=new_message_id(), payload={})
    request = envelope.to_dict()
    try:
        transport = AgentChannelTransport(host, port, timeout_s=timeout_s)
    except UniPadError as error:
        return {"reachable": False, "state": STATE_REFUSED,
                "detail": "invalid_port", "reply": None, "error": str(error)}
    try:
        try:
            transport.connect()
        except OSError as error:
            return {"reachable": False, "state": STATE_REFUSED,
                    "detail": "connection_refused", "reply": None,
                    "error": str(error)}
        try:
            reply = transport.request(request)
        except TimeoutError:
            return {"reachable": False, "state": STATE_SILENT,
                    "detail": "no_reply_in_budget", "reply": None}
        except OSError as error:
            return {"reachable": False, "state": STATE_CLOSED,
                    "detail": "channel_closed", "reply": None, "error": str(error)}
        except UniPadError as error:
            return {"reachable": False, "state": STATE_ANSWERED,
                    "detail": "malformed_reply", "reply": None,
                    "error": str(error)}
        return {"reachable": True, "state": STATE_ANSWERED, "detail": "",
                "reply": reply}
    finally:
        transport.close()


def status_request_line() -> bytes:
    """The exact bytes a status probe puts on the wire, for tests and logs.

    Separate from sending them so a test can assert the wire shape without
    binding a port, and so a log can record what was asked after the fact.
    """
    envelope = Message(type=MSG_STATUS_REQUEST, id=new_message_id(), payload={})
    return envelope.to_json().encode("utf-8") + b"\n"


__all__ = [
    "AgentChannelTransport",
    "STATE_ANSWERED",
    "STATE_CLOSED",
    "STATE_REFUSED",
    "STATE_SILENT",
    "STATE_UNCONNECTED",
    "STATE_WAITING",
    "status_probe",
    "status_request_line",
]
