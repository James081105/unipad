"""Unit tests for :mod:`unipad.host.qmp` (PRD 4.2 "control", 9.2, 6 WP02).

The QMP client is the Windows control channel: PRD 4.2 requires "Windows
transport 可用，控制与串口互不抢占", which on Windows means
``tcp:127.0.0.1:<port>`` because there is no ``AF_UNIX`` QMP socket.

These tests drive the **real** client against the real fake QEMU process under
``tests/fakes/`` (docs/testing-strategy.md D3, "Fake QMP server" row), plus two
purpose-built chatty/quiet sockets for the failure modes the fake cannot express:

- greeting + capability negotiation are what make the channel live;
- ``query-status`` / ``query-name`` / ``screendump`` return usable values, and a
  screendump is a real, deterministic, non-empty PPM;
- ``human-monitor-command`` and ``send-key`` are recorded in the fake's input
  journal (evidence that control traffic really happened);
- ``quit`` produces a ``SHUTDOWN`` event and the process then exits;
- a command with no reply times out with ``qemu_timeout`` (the frozen taxonomy
  has no separate ``qmp_timeout`` code);
- a garbage endpoint is reported as ``qmp_protocol_error`` instead of being
  mistaken for a slow QEMU.

Nothing here proves a guest exists: a green run proves the host drove the
protocol correctly (PRD 7.4).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.qmp import ABS_AXIS_MAX, QMP_TIMEOUT, QmpClient, keycode_names
from tests.fakes import fake_qemu as fq
from tests.fakes.fake_qemu import (
    DEFAULT_SCREEN_HEIGHT,
    DEFAULT_SCREEN_WIDTH,
    FAKE_NAME,
    QMP_GREETING,
    QMP_VERSION,
)

# ---------------------------------------------------------------------------
# local doubles for the two failure modes the fake QEMU cannot express
# ---------------------------------------------------------------------------


class _ChattyServer:
    """A TCP server that completes the QMP handshake then goes permanently silent.

    A real QEMU that accepted a connection but stopped answering is exactly the
    "no progress within budget" case PRD 7.4 describes; the fake always answers,
    so the timeout path needs this double.  Only ``qmp_capabilities`` is
    answered -- everything after the handshake is left hanging on purpose.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", port))
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            with self._lock:
                self._connections.append(conn)
            worker = threading.Thread(target=self._serve_client, args=(conn,), daemon=True)
            worker.start()

    def _serve_client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(0.2)
            conn.sendall((json.dumps(QMP_GREETING) + "\n").encode("utf-8"))
            buffer = b""
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    reply = self._maybe_reply(line)
                    if reply is not None:
                        try:
                            conn.sendall(reply)
                        except OSError:
                            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _maybe_reply(line: bytes) -> Optional[bytes]:
        """Answer only the capability negotiation; every other command hangs."""
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        if not isinstance(message, dict) or message.get("execute") != "qmp_capabilities":
            return None
        return (json.dumps({"return": {}, "id": message.get("id")}) + "\n").encode("utf-8")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._server.close()
        except OSError:
            pass


class _ReversingServer:
    """A QMP server that answers commands in **reverse** arrival order.

    Replies are matched by ``id``, not by arrival: if the client paired a reply
    with whichever command was waiting, two in-flight commands would swap their
    answers and a gate could be decided on the wrong fact (PRD 7.4).  This double
    holds every request until the connection goes quiet, then replies backwards.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", port))
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            with self._lock:
                self._connections.append(conn)
            threading.Thread(target=self._serve_client, args=(conn,), daemon=True).start()

    def _serve_client(self, conn: socket.socket) -> None:
        pending: list[dict] = []
        try:
            conn.settimeout(0.05)
            conn.sendall((json.dumps(QMP_GREETING) + "\n").encode("utf-8"))
            buffer = b""
            quiet_rounds = 0
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    quiet_rounds += 1
                    if quiet_rounds >= 4 and pending:
                        # Nothing new arrived: flush the queued replies backwards.
                        for message in reversed(pending):
                            conn.sendall(
                                (json.dumps({"return": message["mark"], "id": message["id"]}) + "\n").encode("utf-8")
                            )
                        pending = []
                        quiet_rounds = 0
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                quiet_rounds = 0
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        message = json.loads(line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    if message.get("execute") == "qmp_capabilities":
                        conn.sendall((json.dumps({"return": {}, "id": message.get("id")}) + "\n").encode("utf-8"))
                        continue
                    message["mark"] = f"reply-for-{message.get('execute')}"
                    pending.append(message)
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        with self._lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._server.close()
        except OSError:
            pass


class _GarbageServer:
    """A TCP server that speaks plain text, not QMP."""

    def __init__(self, port: int, payload: bytes = b"I am not a QMP server\n") -> None:
        self.port = port
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", port))
        self._server.listen(4)
        self._stop = threading.Event()
        self._payload = payload
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._server.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.sendall(self._payload)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        try:
            self._server.close()
        except OSError:
            pass


@pytest.fixture
def fake_qemu():
    """A healthy fake QEMU child process with its QMP port ready."""
    with fq.fake_qemu_process() as handle:
        assert handle.wait_for_port(timeout=10.0), f"fake never listened: {handle.stderr_text()}"
        yield handle


@pytest.fixture
def qmp(fake_qemu):
    """A connected :class:`QmpClient` bound to the fake's QMP port."""
    client = QmpClient(port=fake_qemu.port, timeout=5.0)
    client.connect()
    yield client
    client.close()


# ---------------------------------------------------------------------------
# connect: greeting + capabilities
# ---------------------------------------------------------------------------


def test_connect_reads_the_greeting_and_negotiates_capabilities(qmp, fake_qemu):
    """PRD 4.2: greeting + ``qmp_capabilities`` are the only readiness evidence."""
    assert qmp.is_connected is True
    assert qmp.greeting is not None
    assert qmp.greeting == QMP_GREETING
    assert qmp.greeting["QMP"]["version"] == QMP_VERSION
    # The client is talking to the fake we started, on the port we chose.
    assert qmp.port == fake_qemu.port


def test_connect_returns_the_greeting_and_capability_reply(fake_qemu):
    """PRD 4.2: the handshake result carries both halves of the negotiation."""
    client = QmpClient(port=fake_qemu.port, timeout=5.0)

    try:
        negotiated = client.connect()
    finally:
        client.close()

    assert negotiated["greeting"]["QMP"]["version"]["qemu"]["major"] == 9
    assert negotiated["capabilities"] == {}
    assert client.is_connected is False  # closed again


def test_double_connect_is_a_protocol_error(fake_qemu):
    """A second handshake on one socket would corrupt the id/reply pairing."""
    client = QmpClient(port=fake_qemu.port, timeout=5.0)
    try:
        client.connect()
        with pytest.raises(UniPadError) as excinfo:
            client.connect()
    finally:
        client.close()

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_unreachable_port_is_qmp_unavailable():
    """PRD 4.2: a port nobody listens on is ``qmp_unavailable``, not a timeout."""
    client = QmpClient(port=fq.free_tcp_port(), timeout=1.0)

    with pytest.raises(UniPadError) as excinfo:
        client.connect()

    assert excinfo.value.code == ErrorCode.QMP_UNAVAILABLE
    assert excinfo.value.details["port"] == client.port


def test_garbage_endpoint_is_qmp_protocol_error():
    """PRD 4.2: a chatty non-QMP listener is named, not mistaken for slow QEMU."""
    port = fq.free_tcp_port()
    server = _GarbageServer(port)
    try:
        client = QmpClient(port=port, timeout=2.0)
        started = time.monotonic()
        with pytest.raises(UniPadError) as excinfo:
            client.connect()
        elapsed = time.monotonic() - started
    finally:
        server.close()

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR
    assert "non-qmp protocol" in excinfo.value.message
    # Reported immediately rather than after the whole timeout budget.
    assert elapsed < 2.0


def test_fake_bad_greeting_fail_mode_is_qmp_protocol_error(tmp_path):
    """Fault injection (D3): the fake's ``bad-greeting`` mode is classified."""
    with fq.fake_qemu_process(fail_mode="bad-greeting", workdir=tmp_path) as fake:
        assert fake.wait_for_port(timeout=10.0)
        client = QmpClient(port=fake.port, timeout=2.0)
        try:
            with pytest.raises(UniPadError) as excinfo:
                client.connect()
        finally:
            client.close()

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def test_query_status_reports_a_running_vm(qmp):
    """PRD 4.2: the run state is read from QMP, never inferred from the process."""
    status = qmp.query_status()

    assert status["status"] == "running"
    assert status["running"] is True


def test_query_name_returns_the_vm_name(qmp):
    """PRD 4.2: ``-name`` is how the launcher later finds the QEMU window."""
    assert qmp.query_name()["name"] == FAKE_NAME


def test_query_version_returns_the_version_block(qmp):
    """PRD 7.3: the QEMU version is recorded in every experiment journal."""
    assert qmp.query_version() == QMP_VERSION


def test_query_block_returns_a_list(qmp):
    """PRD 4.2: the block table evidences the virtio disk wiring."""
    blocks = qmp.query_block()

    assert isinstance(blocks, list)
    assert blocks
    assert "device" in blocks[0]


def test_unknown_command_is_a_protocol_error_naming_the_command(qmp):
    """A wrong command name must be diagnosable, not swallowed."""
    with pytest.raises(UniPadError) as excinfo:
        qmp.execute("definitely-not-a-qmp-command")

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR
    assert excinfo.value.details["command"] == "definitely-not-a-qmp-command"
    assert excinfo.value.details["qmp_error"]["class"] == "CommandNotFound"


def test_execute_before_connect_is_qmp_unavailable():
    """PRD 9.2: no command may be issued on a channel that was never opened."""
    client = QmpClient(port=4444, timeout=1.0)

    with pytest.raises(UniPadError) as excinfo:
        client.execute("query-status")

    assert excinfo.value.code == ErrorCode.QMP_UNAVAILABLE


def test_non_mapping_arguments_are_rejected(qmp):
    """A malformed argument object is a caller bug, reported as such."""
    with pytest.raises(UniPadError) as excinfo:
        qmp.execute("screendump", ["not", "a", "mapping"])  # type: ignore[arg-type]

    assert excinfo.value.code == ErrorCode.QMP_PROTOCOL_ERROR


def test_command_log_records_commands_without_their_arguments(qmp, tmp_path):
    """PRD 9.2: the control log carries no arguments, so no secret can leak."""
    qmp.screendump(tmp_path / "shot.ppm")
    qmp.send_key("ctrl-alt-f1")
    qmp.human_monitor("mouse_move 5 6")

    entries = qmp.command_log()
    assert [entry["command"] for entry in entries] == [
        "qmp_capabilities",  # issued by connect()
        "screendump",
        "send-key",
        "human-monitor-command",
    ]
    for entry in entries:
        assert set(entry) == {"command", "request_id", "elapsed_ms"}
        assert "shot.ppm" not in json.dumps(entry)


# ---------------------------------------------------------------------------
# screendump
# ---------------------------------------------------------------------------


def test_screendump_writes_a_non_empty_ppm(qmp, tmp_path):
    """PRD 4.2: the display path is evidenced by a real file, not a QMP reply."""
    target = tmp_path / "shots" / "boot.ppm"

    result = qmp.screendump(target)

    assert result["exists"] is True
    assert result["bytes"] > 0
    assert target.read_bytes().startswith(b"P6\n")


def test_screendump_is_deterministic(qmp, tmp_path):
    """D3: the same guest state must produce the same bytes, so runs diff."""
    first = qmp.screendump(tmp_path / "a.ppm")
    second = qmp.screendump(tmp_path / "b.ppm")

    assert first["bytes"] == second["bytes"] > 0
    assert (tmp_path / "a.ppm").read_bytes() == (tmp_path / "b.ppm").read_bytes()


def test_screendump_uses_the_fake_framebuffer_size(qmp, tmp_path):
    """PRD 4.2: the framebuffer geometry reaches the host side intact."""
    target = tmp_path / "shot.ppm"

    qmp.screendump(target)
    header = target.read_bytes().split(b"\n", 3)

    assert header[0] == b"P6"
    assert header[1] == f"{DEFAULT_SCREEN_WIDTH} {DEFAULT_SCREEN_HEIGHT}".encode("ascii")


def test_screendump_honours_a_requested_size(qmp, tmp_path):
    """PRD 4.2: an explicit size is passed through to the display path."""
    target = tmp_path / "small.ppm"

    qmp.screendump(target, timeout=5.0)
    qmp.execute("screendump", {"filename": (tmp_path / "sized.ppm").as_posix(),
                              "width": 64, "height": 48})

    header = (tmp_path / "sized.ppm").read_bytes().split(b"\n", 3)
    assert header[1] == b"64 48"


# ---------------------------------------------------------------------------
# input commands are recorded in the fake journal (evidence of control traffic)
# ---------------------------------------------------------------------------


def test_human_monitor_command_is_recorded_in_the_journal(qmp, fake_qemu):
    """PRD 4.2: HMP shares the QMP socket; the command reaches the guest side."""
    output = qmp.human_monitor("mouse_move 11 22")

    assert isinstance(output, str)
    events = fake_qemu.input_events()
    assert len(events) == 1
    assert events[0]["channel"] == "human-monitor-command"
    assert events[0]["command_line"] == "mouse_move 11 22"
    assert events[0]["x"] == 11 and events[0]["y"] == 22


def test_send_key_is_recorded_in_the_journal(qmp, fake_qemu):
    """PRD 4.2: a key chord is a press+release pair over the control channel."""
    qmp.send_key("ctrl-alt-f1")

    events = fake_qemu.input_events()
    assert [event["channel"] for event in events] == ["send-key"]
    assert events[0]["keys"] == ["ctrl", "alt", "f1"]


def test_pointer_abs_and_mouse_button_are_recorded(qmp, fake_qemu):
    """PRD 4.2: absolute axes use QEMU's input-layer range."""
    qmp.pointer_abs(1234, 4321)
    qmp.mouse_button("left", True)
    qmp.mouse_button("left", False)

    events = fake_qemu.input_events()
    assert [event["channel"] for event in events] == ["input-send-event"] * 3
    axes = events[0]["events"]
    assert [item["data"]["value"] for item in axes] == [1234, 4321]
    assert [item["data"]["axis"] for item in axes] == ["x", "y"]
    assert events[1]["events"][0]["data"] == {"button": "left", "down": True}


def test_mouse_button_rejects_an_unknown_button(qmp):
    """PRD 4.2: an unsupported button is reported, not silently dropped."""
    with pytest.raises(UniPadError) as excinfo:
        qmp.mouse_button("side")

    assert excinfo.value.code == ErrorCode.INPUT_FAILED
    assert "left" in excinfo.value.details["supported"]


def test_input_send_event_requires_at_least_one_event(qmp):
    """An empty event list would look exactly like a dropped input."""
    with pytest.raises(UniPadError) as excinfo:
        qmp.input_send_event([])

    assert excinfo.value.code == ErrorCode.INPUT_FAILED


def test_absolute_axis_max_is_the_qemu_input_range():
    """PRD 4.2: the axis ceiling is QEMU's ``INPUT_EVENT_ABS_MAX``."""
    assert ABS_AXIS_MAX == 0x7FFF


# ---------------------------------------------------------------------------
# events, shutdown, timeouts
# ---------------------------------------------------------------------------


def test_system_reset_emits_a_reset_event_alongside_the_reply(qmp):
    """PRD 4.2: asynchronous events never starve a caller waiting for a reply."""
    reply = qmp.system_reset()

    assert reply == {}
    events = qmp.events()
    assert [event["event"] for event in events] == ["RESET"]
    assert qmp.events() == []  # drained


def test_quit_emits_shutdown_and_the_process_exits(qmp, fake_qemu):
    """PRD 5.2 case 8: a graceful shutdown is a QMP event, then the process goes."""
    qmp.quit(timeout=5.0)

    events = qmp.events()
    assert [event["event"] for event in events] == ["SHUTDOWN"]
    assert events[0]["data"]["reason"] == "guest-shutdown"

    assert fake_qemu.process.wait(timeout=10.0) == 0


def test_quit_tolerates_a_socket_that_closes_first(fake_qemu):
    """PRD 4.2: QEMU may drop the socket before replying; that is not an error."""
    client = QmpClient(port=fake_qemu.port, timeout=5.0)
    client.connect()
    try:
        result = client.quit(timeout=5.0)
    finally:
        client.close()

    assert isinstance(result, dict)


def test_command_timeout_raises_qemu_timeout():
    """PRD 7.4: no reply within the budget is ``qemu_timeout``, not a hang."""
    port = fq.free_tcp_port()
    server = _ChattyServer(port)
    try:
        client = QmpClient(port=port, timeout=5.0)
        client.connect()
        try:
            started = time.monotonic()
            with pytest.raises(UniPadError) as excinfo:
                client.execute("query-status", timeout=0.3)
            elapsed = time.monotonic() - started
        finally:
            client.close()
    finally:
        server.close()

    assert excinfo.value.code == QMP_TIMEOUT
    assert excinfo.value.code == ErrorCode.QEMU_TIMEOUT
    assert excinfo.value.details["command"] == "query-status"
    assert 0.2 < elapsed < 3.0


def test_replies_are_matched_by_id_not_by_arrival_order():
    """PRD 4.2 / 7.4: two in-flight commands never swap their answers.

    The server answers backwards on purpose.  A client that paired a reply with
    whichever command happened to be waiting would hand ``query-name``'s answer
    to ``query-status`` -- and a gate could then be decided on the wrong fact.
    """
    port = fq.free_tcp_port()
    server = _ReversingServer(port)
    try:
        client = QmpClient(port=port, timeout=5.0)
        client.connect()
        try:
            results: dict[str, str] = {}
            errors: list[BaseException] = []

            def issue(command: str) -> None:
                try:
                    results[command] = str(client.execute(command, timeout=5.0).get("return"))
                except BaseException as exc:  # noqa: BLE001 - reported below
                    errors.append(exc)

            first = threading.Thread(target=issue, args=("query-status",))
            second = threading.Thread(target=issue, args=("query-name",))
            first.start()
            second.start()
            first.join(timeout=10.0)
            second.join(timeout=10.0)

            log = [entry for entry in client.command_log() if entry["command"] != "qmp_capabilities"]
        finally:
            client.close()
    finally:
        server.close()

    assert not errors
    assert results == {
        "query-status": "reply-for-query-status",
        "query-name": "reply-for-query-name",
    }
    # Each command got its own id, and the ids are unique per client.
    ids = [entry["request_id"] for entry in log]
    assert sorted(entry["command"] for entry in log) == ["query-name", "query-status"]
    assert len(set(ids)) == len(ids) == 2


def test_a_closed_port_fails_fast_as_qmp_unavailable():
    """PRD 4.2: nothing is listening, so there is nothing to time out on.

    A closed port must be reported as ``qmp_unavailable`` immediately -- never as
    ``qemu_timeout`` after the whole budget, which would read as a hung guest
    instead of a control channel that was never started (the ``start`` command's
    ``qmp_unavailable`` finding depends on that distinction).
    """
    port = fq.free_tcp_port()  # reserved, never bound
    client = QmpClient(port=port, timeout=5.0)
    started = time.monotonic()
    try:
        with pytest.raises(UniPadError) as excinfo:
            client.connect()
    finally:
        client.close()
    elapsed = time.monotonic() - started

    assert excinfo.value.code == ErrorCode.QMP_UNAVAILABLE
    assert excinfo.value.details["port"] == port
    assert elapsed < 3.0


def test_no_qmp_listener_never_becomes_ready_against_the_launcher(tmp_path):
    """PRD 7.4 / FR08: a silent QEMU is not a ready QEMU.

    The fake's ``no-qmp`` fault starts the process with no control listener, so a
    launcher that trusted "the process is alive" would report a guest that cannot
    be driven at all.
    """
    with fq.fake_qemu_process(
        fail_mode="no-qmp", boot_ms=0, workdir=tmp_path, wait_for_port=False
    ) as fake:
        client = QmpClient(port=fake.port, timeout=1.0)
        try:
            with pytest.raises(UniPadError) as excinfo:
                client.connect()
        finally:
            client.close()
        # The process is alive the whole time: readiness is not liveness.
        assert fake.process.poll() is None

    assert excinfo.value.code == ErrorCode.QMP_UNAVAILABLE


def test_connection_loss_while_awaiting_a_reply_is_qmp_unavailable():
    """PRD 9.2: a dead control channel is never reported as a successful command."""
    port = fq.free_tcp_port()
    server = _ChattyServer(port)
    try:
        client = QmpClient(port=port, timeout=5.0)
        client.connect()
        server.close()  # the peer vanishes mid-command
        try:
            with pytest.raises(UniPadError) as excinfo:
                client.execute("query-status", timeout=2.0)
        finally:
            client.close()
    finally:
        server.close()

    assert excinfo.value.code == ErrorCode.QMP_UNAVAILABLE


# ---------------------------------------------------------------------------
# keycode normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("f1", ["f1"]),
        ("ctrl-alt-f1", ["ctrl", "alt", "f1"]),
        ("Enter", ["ret"]),
        ("return", ["ret"]),
        ("space", ["spc"]),
        ("escape", ["esc"]),
        ("del", ["delete"]),
        ("pageup", ["pgup"]),
        (["ctrl", "alt", "delete"], ["ctrl", "alt", "delete"]),
        (["ctrl-alt-f1"], ["ctrl", "alt", "f1"]),
    ],
)
def test_keycode_names_applies_the_documented_aliases(spec, expected):
    """PRD 4.2: friendly names map onto QCode names; unknown names pass through."""
    assert keycode_names(spec) == expected


def test_keycode_names_rejects_an_empty_spec():
    """An empty chord would silently send nothing."""
    with pytest.raises(UniPadError) as excinfo:
        keycode_names("")

    assert excinfo.value.code == ErrorCode.INPUT_FAILED


def test_keycode_names_rejects_non_string_entries():
    """PRD 9.1: bad input is reported with a stable code."""
    with pytest.raises(UniPadError) as excinfo:
        keycode_names(["ctrl", 7])  # type: ignore[list-item]

    assert excinfo.value.code == ErrorCode.INPUT_FAILED


# ---------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port", [0, -1, 65536, "4444", 4.4])
def test_client_rejects_an_unusable_port(port):
    """PRD 4.2: QMP is always an explicit loopback TCP port."""
    with pytest.raises(UniPadError) as excinfo:
        QmpClient(port=port)  # type: ignore[arg-type]

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_close_is_idempotent_and_safe_from_any_thread(fake_qemu):
    """PRD 4.2: teardown must never raise, even when called twice."""
    client = QmpClient(port=fake_qemu.port, timeout=5.0)
    client.connect()

    client.close()
    client.close()

    assert client.is_connected is False


def test_context_manager_closes_the_connection(fake_qemu):
    """PRD 4.2: ``with`` leaves no socket behind."""
    with QmpClient(port=fake_qemu.port, timeout=5.0) as client:
        assert client.is_connected is True

    assert client.is_connected is False
