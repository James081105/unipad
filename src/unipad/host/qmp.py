"""Blocking QMP client over TCP (PRD 4.2 "control", 9.2 evidence set).

QMP is QEMU's JSON control protocol.  On Windows there is no ``AF_UNIX``
socket, so the transport is always ``tcp:127.0.0.1:<port>`` (PRD 4.2: "Windows
transport usable, control and serial must not contend").  Each line on the
socket is one complete JSON object.

The client is deliberately synchronous:

- a background reader thread demultiplexes **replies** (matched by ``id``)
  from **asynchronous events** (``SHUTDOWN``, ``RESET``, ...) into separate
  queues, so a chatty guest never starves a caller waiting for a reply;
- :meth:`QmpClient.execute` blocks until the matching reply arrives, the
  timeout expires, or the connection dies -- there is no polling loop and no
  fixed countdown;
- every failure raises :class:`~unipad.errors.UniPadError` with a stable code
  so the controller can classify it (PRD 9.1).

Taxonomy note: the frozen error taxonomy in :mod:`unipad.errors` has no
``qmp_timeout`` code; a QMP command that exceeds its budget is reported as
``qemu_timeout`` (``QMP_TIMEOUT`` below is an alias kept for readability).
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from collections.abc import Mapping as _Mapping
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from unipad.errors import ErrorCode, UniPadError

__all__ = ["QMP_TIMEOUT", "QmpClient", "keycode_names"]

#: Alias for the code used when a QMP command exceeds its timeout budget.
#: ``unipad.errors`` has no ``qmp_timeout`` entry; ``qemu_timeout`` is the
#: frozen equivalent ("no progress within budget").
QMP_TIMEOUT = ErrorCode.QEMU_TIMEOUT

#: QEMU input-layer absolute axis range (``INPUT_EVENT_ABS_MAX``).
ABS_AXIS_MAX = 0x7FFF

#: QEMU key names that differ from their obvious spelling.  Unknown names are
#: passed through verbatim so QEMU reports the real error instead of us
#: silently substituting a different key.
_QCODE_ALIASES = {
    "space": "spc",
    "spacebar": "spc",
    "enter": "ret",
    "return": "ret",
    "newline": "ret",
    "escape": "esc",
    "del": "delete",
    "pageup": "pgup",
    "pagedown": "pgdn",
    "minus": "minus",
    "plus": "equal",
}

#: Buttons accepted by QMP ``input-send-event`` / ``input-btn`` events.
_VALID_BUTTONS = ("left", "middle", "right", "wheel-up", "wheel-down", "wheel-left", "wheel-right")


def keycode_names(keys: Union[str, Iterable[str]]) -> list[str]:
    """Normalise a key spec into QEMU QCode names.

    Args:
        keys: Either a single chord string such as ``"ctrl-alt-f1"`` (split on
            ``-``) or an iterable of chord strings / single names.

    Returns:
        A list of QCode names, lower-cased, with the friendly aliases in
        ``_QCODE_ALIASES`` applied.  Unknown names are preserved verbatim.

    Note:
        Names are **not** validated here: an unknown QCode is reported by QEMU
        itself so the failure names the exact key.
    """
    if isinstance(keys, str):
        candidates: list[str] = keys.split("-") if "-" in keys else [keys]
    else:
        candidates = []
        for item in keys:
            if not isinstance(item, str):
                raise UniPadError(
                    ErrorCode.INPUT_FAILED,
                    "key spec entries must be strings",
                    received_type=type(item).__name__,
                )
            candidates.extend(item.split("-") if "-" in item else [item])
    names: list[str] = []
    for candidate in candidates:
        token = candidate.strip().lower()
        if not token:
            continue
        names.append(_QCODE_ALIASES.get(token, token))
    if not names:
        raise UniPadError(ErrorCode.INPUT_FAILED, "empty key spec")
    return names


class QmpClient:
    """Synchronous QMP client speaking JSON lines over TCP.

    Args:
        host: QMP host; always loopback on Windows (no ``AF_UNIX``).
        port: QMP TCP port as passed to QEMU's ``-qmp tcp:127.0.0.1:<port>``.
        timeout: Default per-command timeout in seconds.

    Example:
        >>> client = QmpClient(port=4444)          # doctest: +SKIP
        >>> caps = client.connect()                 # doctest: +SKIP
        >>> client.query_status()["status"]         # doctest: +SKIP
        'running'
        >>> client.close()                          # doctest: +SKIP

    Note:
        The socket is put in blocking mode once connected; the reader thread
        owns it.  ``close()`` from any thread unblocks that read.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 4444, timeout: float = 10.0) -> None:
        if not isinstance(port, int) or not (0 < port < 65536):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "qmp port must be an integer in 1..65535",
                port=port,
            )
        self.host = host
        self.port = port
        self.timeout = float(timeout)
        self._socket: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._buffer = ""
        self._closed = threading.Event()
        self._connected = threading.Event()
        self._greeting: Optional[dict[str, Any]] = None
        self._greeting_seen = threading.Event()
        self._eof = threading.Event()
        self._events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._pending: dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self._orphan_replies: list[dict[str, Any]] = []
        self._protocol_errors: list[str] = []
        self._send_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._next_id = 0
        self._command_log: list[dict[str, Any]] = []

    # -- introspection ---------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """``True`` once the greeting has been read and capabilities negotiated."""
        return self._connected.is_set()

    @property
    def greeting(self) -> Optional[dict[str, Any]]:
        """The QMP greeting message, or ``None`` before :meth:`connect`."""
        return self._greeting

    @property
    def protocol_errors(self) -> list[str]:
        """Raw lines that were not valid QMP messages (diagnostics only)."""
        return list(self._protocol_errors)

    def command_log(self) -> list[dict[str, Any]]:
        """Return the commands issued so far (no arguments containing secrets)."""
        return [dict(entry) for entry in self._command_log]

    # -- lifecycle -------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        """Open the TCP connection, read the greeting, negotiate capabilities.

        Returns:
            ``{"greeting": {...}, "capabilities": {...}}`` -- the QMP greeting
            and the reply to ``qmp_capabilities``.  This is the evidence that
            the control channel is live; nothing else counts as readiness.

        Raises:
            UniPadError: ``qmp_unavailable`` when the port is unreachable or no
                greeting arrives within the timeout, ``qmp_protocol_error``
                when the greeting is malformed or the client is already open.
        """
        if self._connected.is_set():
            raise UniPadError(
                ErrorCode.QMP_PROTOCOL_ERROR,
                "qmp client is already connected",
                host=self.host,
                port=self.port,
            )
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise UniPadError(
                ErrorCode.QMP_UNAVAILABLE,
                f"qmp tcp endpoint {self.host}:{self.port} is not reachable",
                host=self.host,
                port=self.port,
                reason=type(exc).__name__,
            ) from exc
        self._socket = sock
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:  # pragma: no cover - best effort
            pass
        sock.settimeout(None)  # the reader thread blocks indefinitely
        self._closed.clear()
        self._reader = threading.Thread(
            target=self._reader_loop, name=f"unipad-qmp-{self.port}", daemon=True
        )
        self._reader.start()

        # Poll instead of one long wait so a peer that answers with something
        # other than a QMP banner is reported immediately rather than after the
        # whole timeout has elapsed.
        deadline = time.monotonic() + max(self.timeout, 0.001)
        while not self._greeting_seen.is_set():
            if self._protocol_errors:
                # The endpoint is reachable and chatty but is not speaking QMP
                # (a wrong port hitting another listener, say).  Saying so beats
                # letting the caller believe nothing answered.
                noise = list(self._protocol_errors)
                self.close()
                raise UniPadError(
                    ErrorCode.QMP_PROTOCOL_ERROR,
                    f"qmp endpoint {self.host}:{self.port} spoke a non-qmp protocol",
                    host=self.host,
                    port=self.port,
                    rejected_lines=len(noise),
                    first_line=noise[0][:120],
                )
            if self._eof.is_set() or self._closed.is_set():
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if not self._greeting_seen.is_set():
            self.close()
            raise UniPadError(
                ErrorCode.QMP_UNAVAILABLE,
                f"no qmp greeting within {self.timeout:g}s",
                host=self.host,
                port=self.port,
            )
        if not isinstance(self._greeting, dict) or "QMP" not in self._greeting:
            received = self._greeting
            self.close()
            raise UniPadError(
                ErrorCode.QMP_PROTOCOL_ERROR,
                "qmp greeting was not a QMP banner",
                received_type=type(received).__name__,
            )
        capabilities = self.execute("qmp_capabilities")
        self._connected.set()
        return {"greeting": self._greeting, "capabilities": capabilities.get("return")}

    def close(self) -> None:
        """Shut the connection down.  Safe to call repeatedly and from any thread."""
        self._closed.set()
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._connected.clear()
        thread = self._reader
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._reader = None
        self._release_waiters()

    def __enter__(self) -> "QmpClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # -- command execution ----------------------------------------------

    def execute(
        self,
        command: str,
        arguments: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Execute one QMP command and return its raw reply.

        Args:
            command: QMP command name, e.g. ``"query-status"``.
            arguments: Optional argument object; omitted when ``None``.
            timeout: Override for the client default timeout.

        Returns:
            The full reply message (``{"return": ..., "id": n}``).  Callers
            usually want ``reply["return"]``.

        Raises:
            UniPadError: ``qmp_unavailable`` when not connected or the send
                fails, ``qemu_timeout`` (``QMP_TIMEOUT``) when no matching reply
                arrives in time, ``qmp_protocol_error`` when QEMU answers with
                an ``error`` object or a reply cannot be matched.
        """
        if not self._greeting_seen.is_set():
            raise UniPadError(
                ErrorCode.QMP_UNAVAILABLE,
                "qmp client is not connected",
                command=command,
            )
        if arguments is not None and not isinstance(arguments, _Mapping):
            raise UniPadError(
                ErrorCode.QMP_PROTOCOL_ERROR,
                "qmp arguments must be a mapping",
                command=command,
                received_type=type(arguments).__name__,
            )

        with self._id_lock:
            self._next_id += 1
            request_id = self._next_id
        payload: dict[str, Any] = {"execute": command, "id": request_id}
        if arguments is not None:
            payload["arguments"] = dict(arguments)

        reply_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._pending[request_id] = reply_queue
        started = time.monotonic()
        try:
            self._send(payload)
        except UniPadError:
            self._pending.pop(request_id, None)
            raise
        budget = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(budget, 0.001)
        message: Optional[dict[str, Any]] = None
        while message is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._pending.pop(request_id, None)
                raise UniPadError(
                    QMP_TIMEOUT,
                    f"qmp command {command!r} produced no reply within {budget:g}s",
                    command=command,
                    request_id=request_id,
                    host=self.host,
                    port=self.port,
                )
            try:
                message = reply_queue.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                if self._eof.is_set() or self._closed.is_set():
                    self._pending.pop(request_id, None)
                    raise UniPadError(
                        ErrorCode.QMP_UNAVAILABLE,
                        "qmp connection closed while awaiting a reply",
                        command=command,
                        request_id=request_id,
                        host=self.host,
                        port=self.port,
                    ) from None
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
        self._command_log.append(
            {"command": command, "request_id": request_id, "elapsed_ms": elapsed_ms}
        )
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict) and error.get("class") == "ClientDisconnected":
                raise UniPadError(
                    ErrorCode.QMP_UNAVAILABLE,
                    "qmp connection closed while awaiting a reply",
                    command=command,
                    request_id=request_id,
                    host=self.host,
                    port=self.port,
                )
            raise UniPadError(
                ErrorCode.QMP_PROTOCOL_ERROR,
                f"qmp command {command!r} was rejected",
                command=command,
                request_id=request_id,
                qmp_error=error if isinstance(error, dict) else {"raw": error},
            )
        return message

    def _send(self, payload: dict[str, Any]) -> None:
        """Serialise and write one JSON line."""
        sock = self._socket
        if sock is None or self._closed.is_set():
            raise UniPadError(
                ErrorCode.QMP_UNAVAILABLE,
                "qmp socket is closed",
                command=payload.get("execute"),
            )
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        with self._send_lock:
            try:
                sock.sendall(data)
            except OSError as exc:
                raise UniPadError(
                    ErrorCode.QMP_UNAVAILABLE,
                    "qmp send failed",
                    command=payload.get("execute"),
                    reason=type(exc).__name__,
                ) from exc

    # -- reader thread ---------------------------------------------------

    def _reader_loop(self) -> None:
        """Read the socket forever, demultiplexing replies from events."""
        try:
            while not self._closed.is_set():
                sock = self._socket
                if sock is None:
                    break
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                self._buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in self._buffer:
                    line, self._buffer = self._buffer.split("\n", 1)
                    self._dispatch(line.strip())
        finally:
            self._eof.set()
            self._release_waiters()

    def _dispatch(self, line: str) -> None:
        """Route one decoded line to the reply queue, event queue, or errors."""
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            self._protocol_errors.append(line)
            return
        if not isinstance(message, dict):
            self._protocol_errors.append(line)
            return
        if "QMP" in message:
            self._greeting = message
            self._greeting_seen.set()
            return
        if "event" in message:
            self._events.put(message)
            return
        if ("return" in message or "error" in message) and "id" in message:
            try:
                request_id = int(message["id"])
            except (KeyError, TypeError, ValueError):
                self._protocol_errors.append(line)
                return
            waiter = self._pending.pop(request_id, None)
            if waiter is None:
                self._orphan_replies.append(message)
            else:
                waiter.put(message)
            return
        self._protocol_errors.append(line)

    def _release_waiters(self) -> None:
        """Fail every blocked ``execute`` immediately after a disconnect."""
        for waiter in list(self._pending.values()):
            waiter.put({"error": {"class": "ClientDisconnected", "desc": "qmp connection closed"}})
        self._pending.clear()

    # -- events ----------------------------------------------------------

    def events(self) -> list[dict[str, Any]]:
        """Drain and return the asynchronous events received so far.

        Returns:
            A list of QMP event messages (``{"event": "SHUTDOWN", ...}``).
            Non-blocking: an empty list means "nothing queued right now", not
            "the guest is idle".
        """
        drained: list[dict[str, Any]] = []
        while True:
            try:
                drained.append(self._events.get_nowait())
            except queue.Empty:
                return drained

    def orphan_replies(self) -> list[dict[str, Any]]:
        """Replies whose ``id`` matched no in-flight command (diagnostics)."""
        return list(self._orphan_replies)

    # -- documented helpers ---------------------------------------------

    def query_status(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """Return the VM run state (``running``, ``paused``, ``shutdown``, ...)."""
        return self.execute("query-status", timeout=timeout).get("return", {})

    def query_name(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """Return the VM name as set by QEMU's ``-name``."""
        return self.execute("query-name", timeout=timeout).get("return", {})

    def query_version(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """Return the QEMU version block (recorded in every experiment)."""
        return self.execute("query-version", timeout=timeout).get("return", {})

    def query_block(self, timeout: Optional[float] = None) -> list[dict[str, Any]]:
        """Return the block-device table (used to evidence disk wiring)."""
        reply = self.execute("query-block", timeout=timeout).get("return", [])
        return reply if isinstance(reply, list) else [reply]

    def screendump(self, filename: Union[str, Path], timeout: Optional[float] = None) -> dict[str, Any]:
        """Ask QEMU to write the current display to ``filename`` as PPM.

        Args:
            filename: Host path in QEMU's filesystem namespace.  Forward
                slashes are used because QEMU parses its own backslash escapes.
            timeout: Per-command timeout; screendump of a large framebuffer can
                be slower than an interactive command.

        Returns:
            ``{"filename": ..., "exists": bool, "bytes": int|None}``.  The file
            is stat'ed after the command so a successful QMP reply with no file
            is reported rather than silently accepted.
        """
        target = Path(filename)
        reply = self.execute(
            "screendump", {"filename": target.as_posix()}, timeout=timeout
        )
        result: dict[str, Any] = {
            "filename": target.as_posix(),
            "exists": False,
            "bytes": None,
            "return": reply.get("return"),
        }
        try:
            result["exists"] = target.exists()
            if result["exists"]:
                result["bytes"] = target.stat().st_size
        except OSError:
            result["exists"] = False
        return result

    def human_monitor(self, command_line: str, timeout: Optional[float] = None) -> str:
        """Run one HMP command and return its textual output.

        HMP shares the QMP socket, so control and serial never contend
        (config/machine.json ``devices.control`` note).
        """
        reply = self.execute(
            "human-monitor-command", {"command-line": command_line}, timeout=timeout
        )
        output = reply.get("return")
        return output if isinstance(output, str) else ""

    def pointer_abs(self, x: int, y: int, timeout: Optional[float] = None) -> dict[str, Any]:
        """Move the absolute pointer to guest coordinates ``(x, y)``.

        Values are QEMU input-layer absolute coordinates (``0..ABS_AXIS_MAX``);
        see :mod:`unipad.host.input` for the pure conversion helpers.

        Note:
            M12 UNVERIFIED on real hardware: whether the guest exposes a
            virtio-tablet (absolute) or virtio-mouse (relative) input device
            decides whether these events move the pointer as expected.
        """
        return self.input_send_event(
            [
                {"type": "abs", "data": {"axis": "x", "value": int(x)}},
                {"type": "abs", "data": {"axis": "y", "value": int(y)}},
            ],
            timeout=timeout,
        )

    def mouse_button(
        self, button: str = "left", down: bool = True, timeout: Optional[float] = None
    ) -> dict[str, Any]:
        """Press or release a pointer button.

        Args:
            button: One of ``left``, ``middle``, ``right``, ``wheel-*``.
            down: ``True`` to press, ``False`` to release.
        """
        if button not in _VALID_BUTTONS:
            raise UniPadError(
                ErrorCode.INPUT_FAILED,
                "unsupported pointer button",
                button=button,
                supported=list(_VALID_BUTTONS),
            )
        return self.input_send_event(
            [{"type": "btn", "data": {"button": button, "down": bool(down)}}],
            timeout=timeout,
        )

    def input_send_event(
        self, events: Sequence[Mapping[str, Any]], timeout: Optional[float] = None
    ) -> dict[str, Any]:
        """Low-level ``input-send-event`` wrapper (QMP input events)."""
        if not events:
            raise UniPadError(ErrorCode.INPUT_FAILED, "input-send-event requires at least one event")
        payload = [dict(event) for event in events]
        return self.execute("input-send-event", {"events": payload}, timeout=timeout).get("return", {})

    def send_key(
        self, keys: Union[str, Sequence[str]], timeout: Optional[float] = None
    ) -> dict[str, Any]:
        """Send one key or a chord (all names pressed, then released together).

        Args:
            keys: A chord string such as ``"ctrl-alt-f1"`` or an iterable of
                names.  See :func:`keycode_names` for alias handling.

        Note:
            QMP ``send-key`` is a press+release pair; it cannot express a held
            key.  M12 UNVERIFIED for real guest behaviour.
        """
        names = keycode_names(keys)
        return self.execute(
            "send-key", {"keys": [{"type": "qcode", "data": name} for name in names]}, timeout=timeout
        ).get("return", {})

    def system_powerdown(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """Request an ACPI power-button event (graceful guest shutdown)."""
        return self.execute("system_powerdown", timeout=timeout).get("return", {})

    def system_reset(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """Reset the VM (evidence-producing, never used as a "success" path)."""
        return self.execute("system_reset", timeout=timeout).get("return", {})

    def quit(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """Ask QEMU to exit.  The connection drops immediately afterwards."""
        try:
            return self.execute("quit", timeout=timeout).get("return", {})
        except UniPadError:
            # QEMU may close the socket before replying; that is expected.
            return {"note": "qmp connection closed during quit"}
