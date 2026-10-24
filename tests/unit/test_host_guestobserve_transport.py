"""``guestobserve`` over a real TCP QMP session.

Why this file exists
--------------------
``test_host_guestobserve.py`` exercises the observer against a fake client that
implements one method.  That proves the walk, the ring and the list, and it
proves nothing about the transport -- which is the part that is actually
different on Windows.  UniXNU's ``qemu-ps.py`` speaks QMP over ``AF_UNIX``;
Windows has no AF_UNIX socket for QEMU, so the transport here is
``tcp:127.0.0.1:<port>`` and it has to be *proven*, not assumed.

So this spins up a real TCP server that speaks QMP -- greeting, capability
negotiation, ``pmemsave`` writing a file -- and drives the observer through the
same :class:`~unipad.host.qmp.QmpClient` the controller uses.  The guest it
serves is synthetic, but the socket, the framing, the reply matching by ``id``
and the file-based ``pmemsave`` are all real.

What it does not prove
----------------------
That a live guest answers.  That needs a booted guest, which is the open
question this whole channel exists to answer.
"""

from __future__ import annotations

import hashlib
import json
import socketserver
import struct
import threading
from pathlib import Path
from typing import Any, Optional

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import guestobserve as go
from unipad.host.qmp import QmpClient

# Imported for the fixture's address map, which is the same one the unit tests
# use -- a second spelling of it would be a second thing to keep in step.
from tests.unit.test_host_guestobserve import (  # noqa: E402
    PA_ALLPROC,
    PA_FIRST_PAGE,
    PA_L1,
    PA_L2,
    PA_L3,
    PA_LOG_RING,
    PA_MSGBUF,
    PA_MSGBUF_STRUCT,
    PA_PROC_A,
    PA_PROC_B,
    PA_TTEP,
    TTEP_VALUE,
    _plant_process,
    _plant_tables,
)



class _QmpServer(socketserver.ThreadingTCPServer):
    """A QMP server over synthetic guest RAM."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, ram: bytearray) -> None:
        self.ram = ram
        self.commands: list[str] = []
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _QmpHandler)


class _QmpHandler(socketserver.StreamRequestHandler):
    """One QMP client: greeting, capabilities, then commands until it goes away."""

    def handle(self) -> None:
        assert isinstance(self.server, _QmpServer)
        self.wfile.write(
            (json.dumps({"QMP": {"version": {"QEMU": {"micro": 0, "minor": 1, "major": 8}}}}) + "\n").encode()
        )
        self.wfile.flush()
        for raw in self.rfile:
            if not raw.strip():
                continue
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                continue
            command = str(request.get("execute") or "")
            request_id = request.get("id")
            with self.server._lock:
                self.server.commands.append(command)
            if command == "pmemsave":
                arguments = request.get("arguments") or {}
                val = int(arguments["val"])
                size = int(arguments["size"])
                Path(str(arguments["filename"])).write_bytes(
                    bytes(self.server.ram[val:val + size])
                )
                reply = {"return": {}, "id": request_id}
            elif command == "qmp_capabilities":
                reply = {"return": {}, "id": request_id}
            else:
                reply = {
                    "error": {"class": "CommandNotFound", "desc": command},
                    "id": request_id,
                }
            self.wfile.write((json.dumps(reply) + "\n").encode())
            self.wfile.flush()


@pytest.fixture()
def qmp_server(tmp_path: Path):
    """A running QMP server over a 256 KiB synthetic guest."""
    ram = bytearray(64 * 4096)
    server = _QmpServer(ram)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, ram, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _guest_ram(ram: bytearray) -> None:
    """Plant a guest with tables, one process and a log, straight into RAM.

    The address map is the unit test's, so the two files agree by construction
    rather than by two copies of the same numbers.
    """
    _plant_tables(_RamWriter(ram))
    ram[PA_ALLPROC:PA_ALLPROC + 8] = struct.pack("<Q", PA_PROC_A)
    _plant_process(_RamWriter(ram), PA_PROC_A, 1, "launchd", PA_PROC_B)
    _plant_process(_RamWriter(ram), PA_PROC_B, 99, "kernel_task", 0)
    log = b"XNU: platform expert: this is a synthetic guest"
    size = 128
    ram[PA_LOG_RING:PA_LOG_RING + size] = log + b"\0" * (size - len(log))
    ram[PA_MSGBUF_STRUCT:PA_MSGBUF_STRUCT + 24] = (
        struct.pack("<4I", 0x1234, size, len(log), 0) + struct.pack("<Q", PA_LOG_RING)
    )
    ram[PA_MSGBUF:PA_MSGBUF + 8] = struct.pack("<Q", PA_MSGBUF_STRUCT)


class _RamWriter:
    """The ``write`` half of the unit fixture's guest, over a bytearray."""

    def __init__(self, ram: bytearray) -> None:
        self.ram = ram

    def write(self, physical: int, data: bytes) -> None:
        self.ram[physical:physical + len(data)] = data


def test_the_observer_reads_a_guest_over_a_real_tcp_qmp_session(
    qmp_server, tmp_path: Path
) -> None:
    """End to end: TCP connect, capability negotiation, pmemsave, walk, log.

    This is the test that says the channel works on Windows.  Everything before
    it used a fake client; this one uses the controller's own
    :class:`~unipad.host.qmp.QmpClient` against a real socket, and the transport
    is the thing that differs from UniXNU's ``AF_UNIX`` original.
    """
    server, ram, port = qmp_server
    _guest_ram(ram)

    client = QmpClient(port=port, timeout=15.0)
    try:
        client.connect()
        assert client.is_connected, "the QMP session never came up"

        observer = go.GuestObserver(
            memory=go.GuestMemory(client, tmp_path / "pmem.bin"),
            image=_stub_image(),
            load=go.LoadAddresses(kernel_va=0, kernel_pa=0x30000),
            run_id="run-transport-1",
        )
        sample = observer.sample(
            taken_at="2026-10-25T00:00:00+00:00", log_path=tmp_path / "kernel.log"
        )
    finally:
        client.close()

    # The process list, read through three levels of page tables over TCP.
    assert [row.pid for row in sample.processes] == [1, 99]
    assert [row.name for row in sample.processes] == ["launchd", "kernel_task"]
    # The kernel log, unrolled from its ring.
    assert (tmp_path / "kernel.log").read_bytes() == (
        b"XNU: platform expert: this is a synthetic guest"
    )
    # And the stamp that makes the sample usable as evidence.
    assert sample.probe_version == go.PROBE_VERSION
    assert sample.run_id == "run-transport-1"
    assert sample.kernel_sha256 == "0" * 64
    assert sample.memory_reads > 0

    # The server saw exactly the commands a QMP session should see -- and no
    # monitor command that would have asked the virtual CPU for its state.
    assert "qmp_capabilities" in server.commands
    assert "pmemsave" in server.commands
    assert not [name for name in server.commands if name in ("human-monitor-command", "x")]


def test_a_refusal_from_the_transport_is_not_a_reading(qmp_server, tmp_path: Path) -> None:
    """A guest that answers something else refuses, rather than reading zeros.

    The server is left empty -- no tables, no globals -- so the very first read
    of ``cpu_ttep`` returns a root of 0 and the walk hits unmapped memory.  The
    point is that this is a refusal with a code, not an empty process list.
    """
    _server, _ram, port = qmp_server

    client = QmpClient(port=port, timeout=15.0)
    try:
        client.connect()
        observer = go.GuestObserver(
            memory=go.GuestMemory(client, tmp_path / "pmem.bin"),
            image=_stub_image(),
            load=go.LoadAddresses(kernel_va=0, kernel_pa=0x30000),
        )
        with pytest.raises(UniPadError) as refused:
            observer.processes()
    finally:
        client.close()

    assert refused.value.code in (
        ErrorCode.EVIDENCE_INCOMPLETE,
        ErrorCode.QMP_PROTOCOL_ERROR,
    )


def test_a_closed_session_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """A QMP endpoint that is not there is ``qmp_unavailable``, not a blank list."""
    client = QmpClient(port=1, timeout=1.0)
    with pytest.raises(UniPadError) as refused:
        client.connect()
    assert refused.value.code == ErrorCode.QMP_UNAVAILABLE


class _StubImage:
    """Just enough image for the transport test: addresses and a digest."""

    sha256 = "0" * 64
    size = 1
    path = Path("stub-kernel.macho")
    segments = ()
    symbol_count = 0

    def symbol(self, name: str) -> Optional[int]:
        return {"cpu_ttep": 0x1000, "allproc": 0x2000, "msgbufp": 0x3000}.get(name)

    def require_symbol(self, name: str) -> int:
        found = self.symbol(name)
        if found is None:
            raise UniPadError(ErrorCode.CONFIG_INVALID, "missing", symbol=name)
        return found

    def segment_of(self, vmaddr: int) -> Any:
        return None

    def summary(self) -> dict[str, Any]:
        return {"image": "stub"}


def _stub_image() -> _StubImage:
    return _StubImage()


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def _kernel_for_cli(tmp_path: Path) -> Path:
    """A kernel image good enough for the CLI: the three globals, and nothing else."""
    from tests.unit.test_host_guestobserve import _build_macho

    # The same virtual addresses the fixture's planted tables map, so the CLI's
    # own image and the guest RAM agree.
    return _build_macho(
        tmp_path,
        [
            ("_cpu_ttep", 0x0E, 0x1000),
            ("_allproc", 0x0E, 0x2000),
            ("_msgbufp", 0x0E, 0x3000),
        ],
    )


def _cli_args(port: int, kernel: Path, *extra: str) -> list[str]:
    """The CLI arguments every observation test needs, digest included.

    ``--expected-sha256`` is required, not optional: the symbol addresses the
    walk uses come out of that file, so an unverified image makes every address
    derived from it wrong.  A test that is about the digest gate passes its own.
    """
    digest = hashlib.sha256(kernel.read_bytes()).hexdigest()
    return [
        "--qmp-port", str(port),
        "--kernel", str(kernel),
        "--expected-sha256", digest,
        *extra,
    ]


def test_the_cli_prints_exactly_one_json_object(qmp_server, tmp_path: Path, capsys) -> None:
    """One object on stdout, which is the controller's CLI convention.

    ``tests/acceptance/test_m0_ai_interface_smoke.py`` enforces this for every
    other command by ``json.loads``-ing the whole buffer, so a progress line or
    a second document here would break a contract the rest of the CLI keeps.
    """
    server, ram, port = qmp_server
    _guest_ram(ram)
    kernel = _kernel_for_cli(tmp_path)

    code = go.main(
        _cli_args(
            port, kernel,
            "--kernel-va", "0x0",
            "--kernel-pa", "0x30000",
            "--run-id", "run-cli-1",
            "--log", str(tmp_path / "kernel.log"),
            "--scratch", str(tmp_path / "pmem.bin"),
        )
    )

    out = capsys.readouterr().out
    assert code == 0, out
    document = json.loads(out)  # the whole buffer, exactly as the smoke test does
    assert document["ok"] is True
    assert document["run_id"] == "run-cli-1"
    assert document["probe_version"] == go.PROBE_VERSION
    assert [row["pid"] for row in document["processes"]] == [1, 99]
    assert document["kernel_log"]["written_to"] == "kernel.log"
    assert document["kernel"]["sha256"]


def test_the_cli_refuses_a_kernel_whose_digest_is_not_the_verified_one(
    qmp_server, tmp_path: Path, capsys
) -> None:
    """The digest gate runs before the guest is touched.

    A wrong image means every address derived from it is wrong, so this refuses
    with a code and a non-zero exit -- and, deliberately, without issuing a
    single ``pmemsave``: if the image is wrong there is nothing to read.
    """
    server, _ram, port = qmp_server
    kernel = _kernel_for_cli(tmp_path)

    code = go.main(
        [
            "--qmp-port", str(port),
            "--kernel", str(kernel),
            "--expected-sha256", "f" * 64,
            "--kernel-va", "0x0",
            "--kernel-pa", "0x30000",
        ]
    )

    document = json.loads(capsys.readouterr().out)
    assert code != 0
    assert document["ok"] is False
    assert document["code"] == ErrorCode.IMAGE_HASH_MISMATCH
    assert "pmemsave" not in server.commands


def test_the_cli_refuses_when_the_load_addresses_are_unknown(
    qmp_server, tmp_path: Path, capsys
) -> None:
    """No load line and no explicit addresses is a refusal, not a guess.

    Without the mapping between the kernel's virtual and physical bases there is
    no way to read ``cpu_ttep``, and any address produced anyway would be
    invented.
    """
    _server, _ram, port = qmp_server
    kernel = _kernel_for_cli(tmp_path)

    code = go.main(_cli_args(port, kernel))

    document = json.loads(capsys.readouterr().out)
    assert code != 0
    assert document["ok"] is False
    assert document["code"] == ErrorCode.CONFIG_INVALID
    assert "load addresses" in document["reason"]


def test_the_cli_never_prints_the_kernels_path(qmp_server, tmp_path: Path, capsys) -> None:
    """PRD 9.2: the kernel path is where a user's account name would arrive.

    ``--kernel`` is an absolute path on this host, so it is exactly the field
    that must not reach stdout.  The basename and the digest are printed instead,
    which is enough to identify the image and nothing more.
    """
    server, ram, port = qmp_server
    _guest_ram(ram)
    kernel = _kernel_for_cli(tmp_path)

    go.main(
        _cli_args(
            port, kernel,
            "--kernel-va", "0x0",
            "--kernel-pa", "0x30000",
            "--scratch", str(tmp_path / "pmem.bin"),
        )
    )

    out = capsys.readouterr().out
    assert str(kernel) not in out
    assert str(tmp_path) not in out
    assert kernel.name in out


def test_the_cli_reads_the_load_line_from_a_serial_log(
    qmp_server, tmp_path: Path, capsys
) -> None:
    """``--serial`` is how a real run supplies the load addresses."""
    server, ram, port = qmp_server
    _guest_ram(ram)
    kernel = _kernel_for_cli(tmp_path)
    serial = tmp_path / "serial.log"
    # Full-width addresses, because the load-line pattern requires at least
    # eight hex digits -- a truncated "pa 0x4" used to match and produce a
    # physical base of 4.
    serial.write_text(
        "DARWINBOOT: hand-off to the kernel\n"
        "kernel   va 0x00000000  pa 0x00030000\n"
        "entry    pa 0x00001000\n",
        encoding="utf-8",
    )

    code = go.main(
        _cli_args(
            port, kernel,
            "--serial", str(serial),
            "--scratch", str(tmp_path / "pmem.bin"),
        )
    )

    document = json.loads(capsys.readouterr().out)
    assert code == 0, document
    assert document["kernel"]["va"] == "0x0"
    assert document["kernel"]["pa"] == "0x30000"
    assert document["kernel"]["entry_pa"] == "0x1000"


def test_a_transport_error_cannot_leak_the_account_name(
    qmp_server, tmp_path: Path, capsys
) -> None:
    """PRD 9.2: a QMP error that names the scratch file must not reach stdout.

    Measured on this host before the fix.  QEMU's own text arrives in
    ``details.qmp_error.desc`` when it refuses a ``pmemsave``, and it names the
    file it was asked to open -- which defaults under the user profile.  So a
    transport refusal printed the account name into stdout, and
    ``tests/acceptance/test_m0_ai_interface_smoke.py``'s own assertion would
    have failed on the output.

    The guard is at the emission point, not at the call site: ``cli.py:458-483``
    records six commands leaking the account name and why patching the six call
    sites would have let the seventh ship.
    """
    from unipad.host.paths import account_name

    account = account_name()
    if not account:
        pytest.skip("this host reports no account name, so there is nothing to leak")

    server, _ram, port = qmp_server
    kernel = _kernel_for_cli(tmp_path)

    class Rejecting:
        """A QMP endpoint that connects, then refuses pmemsave the way QEMU does.

        ``connect`` and ``close`` are here because ``main()`` calls them: a stub
        without them fails at ``connect()`` with an ``AttributeError``, which the
        generic refusal path turns into ``{"error": "AttributeError"}`` -- and the
        test would then pass without the leak ever happening.  The first draft of
        this test did exactly that, and the 对质 step (removing the redaction)
        left it green.
        """

        def connect(self):
            return {"greeting": {}, "capabilities": {}}

        def close(self):
            return None

        def execute(self, command, arguments=None, **_):
            raise UniPadError(
                ErrorCode.QMP_PROTOCOL_ERROR,
                "qmp command was rejected",
                command=command,
                qmp_error={
                    "class": "GenericError",
                    "desc": "Could not open 'C:" + chr(92) + "Users" + chr(92)
                             + account + chr(92) + "AppData" + chr(92) + "Local"
                             + chr(92) + "Temp" + chr(92)
                             + "unipad-observe-xyz" + chr(92) + "pmem.bin': "
                             + "No such file or directory",
                },
            )

    import unipad.host.guestobserve as go_module
    import unipad.host.qmp as qmp_module

    # QmpClient is imported inside main(), so patch it where main() looks it up.
    real = qmp_module.QmpClient
    qmp_module.QmpClient = lambda **kwargs: Rejecting()
    try:
        code = go_module.main(
            _cli_args(
                port, kernel,
                "--kernel-va", "0x0",
                "--kernel-pa", "0x30000",
                "--scratch", str(tmp_path / "pmem.bin"),
            )
        )
    finally:
        qmp_module.QmpClient = real

    out = capsys.readouterr().out
    document = json.loads(out)  # still exactly one JSON object
    assert code != 0
    assert document["ok"] is False
    assert account not in out, "the account name reached stdout through a QMP error"
