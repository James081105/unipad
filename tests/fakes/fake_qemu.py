"""Fake ``qemu-system-aarch64`` for Windows simulation tests.

The real launcher (:mod:`unipad.host.qemu`) and the real QMP client
(:mod:`unipad.host.qmp`) are exercised against this process, so the host side
of UniPad can be tested end-to-end on a Windows box that has no QEMU, no
firmware and no guest (docs/testing-strategy.md D3, "Fake QEMU" row).

Run it exactly like the binary it stands in for::

    python fake_qemu.py -accel tcg -qmp tcp:127.0.0.1:45999,server=on,wait=off \\
        -serial file:C:/tmp/serial.log -m 3072

What it fakes
-------------
- argv parsing (liberal: unknown arguments are recorded, never fatal), the
  ``-accel`` TCG guard (``kvm``/``whpx``/``hvf`` -> exit 2), ``-qmp`` TCP
  options and ``-serial file:`` redirection;
- a real TCP QMP server speaking the JSON-line protocol (greeting,
  ``qmp_capabilities``, ``query-status``, ``query-name``, ``query-version``,
  ``query-block``, ``screendump``, ``human-monitor-command``, ``send-key``,
  ``input-send-event``, ``stop``/``cont``/``system_reset``/``system_powerdown``,
  ``quit`` and ``CommandNotFound`` for anything else);
- a staged boot serial log (M1..M4 markers, PRD section 6);
- deterministic PPM screendumps and a JSONL input-event journal.

What it does **not** fake
-------------------------
Per docs/testing-strategy.md D3 this process is a *simulation fixture*: it
never proves that a guest, a display, an input device or a disk exists.  A
green run here means "the host side drove the protocol correctly", nothing
more.  Field status stays ``UNVERIFIED`` until real hardware evidence exists.

Determinism
-----------
Same argv -> same bytes on the wire and in the evidence files.  No wall-clock
or random values are written into the journals, so a test can diff them.

Signals
-------
``SIGINT``/``SIGBREAK`` (delivered as a console control event) and ``SIGTERM``
on POSIX make the process exit 0 promptly.  On Windows
:meth:`subprocess.Popen.terminate` is ``TerminateProcess``: no handler runs and
the exit code is 1 -- the same for real QEMU -- so a test that wants the
graceful path must use ``proc.send_signal(signal.CTRL_BREAK_EVENT)`` (the child
is created with ``CREATE_NEW_PROCESS_GROUP`` by :mod:`unipad.host.qemu`).

Evidence written into the working directory (QEMU's ``cwd``)
-----------------------------------------------------------
- ``fake-qemu-args.json`` -- one JSON line with the complete argv plus the
  parsed QMP/serial/accelerator view;
- ``fake-input-events.jsonl`` -- one JSON line per input command;
- the ``-serial file:`` target -- the staged boot markers;
- ``fake-qemu-error.log`` -- traceback of an unexpected internal error inside
  the QMP connection thread (absent when nothing went wrong).  The connection
  itself never dies silently: the client gets a structured QMP error instead.

Environment knobs
-----------------
``UNIPAD_FAKE_BOOT_MS``
    Total time spent emitting the boot markers (default ``1500``).
``UNIPAD_FAKE_FAIL_MODE``
    Injected fault: ``exit-early``, ``exit-after-boot``, ``no-qmp``,
    ``stall``, ``bad-greeting``, ``no-capabilities``, ``ignore-quit``,
    ``partial-boot``, ``no-serial`` (default empty = healthy fake).
``UNIPAD_FAKE_EXIT_CODE``
    Exit code used for a clean stop (default ``0``).
``UNIPAD_FAKE_SCREEN_W`` / ``UNIPAD_FAKE_SCREEN_H``
    Default screendump size when the command carries none.

Importing it from a test
------------------------
A ``tests`` package installed in site-packages shadows this directory on this
host, so ``import tests.fakes.fake_qemu`` is not reliable.  Use one of::

    # (a) put the tests directory on sys.path, then import the subpackage
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fakes.fake_qemu import fake_qemu_command

    # (b) load the file directly (works without any sys.path change)
    spec = importlib.util.spec_from_file_location(
        "unipad_fake_qemu", Path(__file__).resolve().parents[1] / "fakes" / "fake_qemu.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

The subprocess side needs no import at all: :func:`fake_qemu_command` only
builds the argv, and the child runs this file as a script.  When a test would
rather not manage the child itself, :func:`fake_qemu_process` is a context
manager that starts the fake on a free port, waits for QMP and always reaps the
child (including when the ``with`` block raises).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

__all__ = [
    "ARGS_FILENAME",
    "BOOT_MARKERS",
    "DEFAULT_SCREEN_HEIGHT",
    "DEFAULT_SCREEN_WIDTH",
    "ERROR_LOG_FILENAME",
    "FAKE_NAME",
    "FakeQemuProcess",
    "INPUT_EVENTS_FILENAME",
    "PARTIAL_BOOT_MARKERS",
    "QMP_GREETING",
    "QMP_VERSION",
    "fake_qemu_command",
    "fake_qemu_launcher",
    "fake_qemu_process",
    "free_tcp_port",
    "main",
    "parse_argv",
    "read_jsonl",
]

#: Name reported by ``query-name`` (and the fallback for ``-name``).
FAKE_NAME = "unipad-fake"

#: One JSON line with the argv this process was started with.
ARGS_FILENAME = "fake-qemu-args.json"

#: One JSON line per input command seen over QMP.
INPUT_EVENTS_FILENAME = "fake-input-events.jsonl"

#: Traceback of an unexpected internal error inside the QMP connection thread.
ERROR_LOG_FILENAME = "fake-qemu-error.log"

#: Boot stages staged onto the serial log, in order (PRD section 6, M1..M4).
BOOT_MARKERS: tuple[str, ...] = (
    "FakeQEMU 1.0 starting",
    "args validated",
    "M1: UEFI firmware started",
    "M2: UniXNU boot manager running",
    "M3: DarwinBoot handoff prepared",
    "M4: XNU kernel entered",
    "boot complete; idle",
)

#: Subset used by ``UNIPAD_FAKE_FAIL_MODE=partial-boot`` (never reaches M3).
PARTIAL_BOOT_MARKERS: tuple[str, ...] = BOOT_MARKERS[:4]

#: iPad-class landscape framebuffer used when a screendump carries no size.
DEFAULT_SCREEN_WIDTH = 1112
DEFAULT_SCREEN_HEIGHT = 834

#: Version block echoed by the greeting and ``query-version``.
QMP_VERSION: dict[str, Any] = {
    "qemu": {"micro": 0, "minor": 9, "major": 9},
    "package": "fake-qemu",
}

#: The QMP banner sent the moment a client connects.
QMP_GREETING: dict[str, Any] = {"QMP": {"version": QMP_VERSION, "capabilities": []}}

#: Accelerators this host refuses outright (PRD 2.2-1: Intel Windows + AArch64).
_NON_TCG_ACCELERATORS = frozenset({"kvm", "whpx", "hvf", "hax", "xen", "nvmm"})

#: Options this fake understands as ``flag value`` pairs.
_VALUE_OPTIONS = ("-qmp", "-serial", "-chardev", "-accel", "-machine", "-name")

#: How long a stop request waits for the socket to drain before the process goes.
_SOCKET_DRAIN_SECONDS = 0.05

#: Note recorded by :func:`parse_argv` when the argv asks for usage output.
#: ``run()`` looks for exactly this string, so the two must stay in sync.
_HELP_NOTE = "help requested"


# ---------------------------------------------------------------------------
# argv parsing
# ---------------------------------------------------------------------------


class _ParsedArgs:
    """Liberal view of the QEMU command line (never raises on unknown args).

    Deliberately a plain class rather than a dataclass: tests load this module
    by file path with :func:`importlib.util.spec_from_file_location`, and a
    dataclass under ``from __future__ import annotations`` needs its module to
    be registered in ``sys.modules`` to resolve its string annotations.
    """

    def __init__(self, argv: Optional[Sequence[str]] = None) -> None:
        self.argv: list[str] = list(argv or [])
        self.qmp_spec: Optional[str] = None
        self.qmp_transport: Optional[str] = None
        self.qmp_host: Optional[str] = None
        self.qmp_port: Optional[int] = None
        self.qmp_options: dict[str, str] = {}
        self.serial_value: Optional[str] = None
        self.serial_path: Optional[Path] = None
        self.serial_mode: str = "absent"
        self.accel: Optional[str] = None
        self.accels: list[str] = []
        self.machine: Optional[str] = None
        self.name: Optional[str] = None
        self.chardevs: dict[str, str] = {}
        self.kvm_flag: bool = False
        self.other_args: list[str] = []
        self.notes: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable evidence view (paths only, never secrets)."""
        return {
            "argv": list(self.argv),
            "qmp": {
                "spec": self.qmp_spec,
                "transport": self.qmp_transport,
                "host": self.qmp_host,
                "port": self.qmp_port,
                "options": dict(self.qmp_options),
                "listening": self.qmp_port is not None and self.qmp_transport == "tcp",
            },
            "serial": {
                "value": self.serial_value,
                "mode": self.serial_mode,
                "path": self.serial_path.as_posix() if self.serial_path else None,
            },
            "accel": self.accel,
            "accels": list(self.accels),
            "machine": self.machine,
            "name": self.name,
            "chardevs": dict(self.chardevs),
            "kvm_flag": self.kvm_flag,
            "other_args": list(self.other_args),
            "notes": list(self.notes),
        }


def _script_path() -> Path:
    """Absolute path of this file (used to build the launch command)."""
    return Path(__file__).resolve()


def _strip_launcher_tokens(argv: Sequence[str]) -> list[str]:
    """Drop interpreter / script tokens a caller may have prepended.

    A launcher is allowed to hand us ``[sys.executable, script, *qemu_args]``
    (or ``[script, *qemu_args]``).  Either way the tokens that belong to the
    interpreter are removed so the rest parses as pure QEMU arguments.
    """
    cleaned = list(argv)
    script = _script_path().as_posix().lower()
    while cleaned:
        head = cleaned[0]
        base = os.path.basename(head).lower()
        if (
            head == sys.executable
            or Path(head).as_posix().lower() == script
            or base.startswith("python")
            or base == "py.exe"
        ):
            cleaned.pop(0)
            continue
        break
    return cleaned


def _split_attached(argv: Sequence[str]) -> list[str]:
    """Expand ``-qmpfoo`` / ``-qmp=foo`` into ``["-qmp", "foo"]``."""
    attached_flags = ("-qmp", "-serial", "-accel", "-machine", "-name", "-chardev")
    expanded: list[str] = []
    for token in argv:
        for flag in attached_flags:
            if token == flag or not token.startswith(flag):
                continue
            remainder = token[len(flag) :]
            if remainder.startswith("="):
                expanded.extend([flag, remainder[1:]])
                break
            if not remainder.startswith("-"):
                expanded.extend([flag, remainder])
                break
        else:
            expanded.append(token)
    return expanded


def _next_value(argv: Sequence[str], index: int) -> tuple[Optional[str], int]:
    """Return ``(value, next_index)`` for the option at ``index``.

    A missing value, or a value that is really the next option, yields
    ``(None, index + 1)`` so the caller keeps scanning.
    """
    if index + 1 < len(argv):
        candidate = argv[index + 1]
        if not candidate.startswith("-"):
            return candidate, index + 2
    return None, index + 1


def _parse_qmp_spec(spec: str, parsed: _ParsedArgs) -> None:
    """Parse ``tcp:127.0.0.1:4444,server=on,wait=off`` into host/port/options."""
    parsed.qmp_spec = spec
    transport, _, option_text = spec.partition(",")
    scheme = transport.split(":", 1)[0].strip().lower()
    parsed.qmp_transport = scheme
    for option in option_text.split(","):
        if not option:
            continue
        key, _, value = option.partition("=")
        parsed.qmp_options[key.strip()] = value.strip() or "on"
    if scheme != "tcp":
        # Non-TCP transports (unix:, fd:, none) are recorded and ignored: the
        # Windows host has no AF_UNIX QMP socket (PRD 4.2).
        parsed.notes.append(f"qmp transport {transport!r} ignored: not tcp")
        return
    parts = transport.split(":")
    if len(parts) < 3:
        parsed.notes.append(f"qmp spec {spec!r} has no host:port")
        return
    parsed.qmp_host = parts[1]
    try:
        parsed.qmp_port = int(parts[2])
    except ValueError:
        parsed.notes.append(f"qmp port in {spec!r} is not an integer")
        parsed.qmp_host = None


def _parse_chardev(value: str, parsed: _ParsedArgs) -> None:
    """Remember ``-chardev file,id=x,path=y`` so ``-serial chardev:x`` resolves.

    QEMU accepts the backend either positionally (``file,id=x,path=y``) or as an
    option (``backend=file,id=x,path=y``); both spellings must resolve, otherwise
    a ``-serial chardev:x`` that follows would silently lose its log file.
    """
    parts = value.split(",")
    fields: dict[str, str] = {}
    for part in parts:
        key, _, item = part.partition("=")
        fields[key.strip()] = item.strip()
    positional_backend = parts[0].strip() if "=" not in parts[0] else ""
    backend = fields.get("backend", "") or positional_backend
    identifier = fields.get("id")
    path = fields.get("path")
    if backend.startswith("file") and path:
        if identifier:
            parsed.chardevs[identifier] = path
        elif parsed.serial_path is None:
            parsed.serial_value = value
            parsed.serial_mode = "file"
            parsed.serial_path = Path(path)


def _parse_serial(value: str, parsed: _ParsedArgs) -> None:
    """Resolve the ``-serial`` value to a host file we can write markers to."""
    parsed.serial_value = value
    head, _, _rest = value.partition(",")
    if head.startswith("file:"):
        target = head[len("file:") :]
        if target and target != "-":
            parsed.serial_mode = "file"
            parsed.serial_path = Path(target)
        else:
            parsed.serial_mode = "stdio"
        return
    if head.startswith("chardev:"):
        identifier = head[len("chardev:") :]
        path = parsed.chardevs.get(identifier)
        if path:
            parsed.serial_mode = "file"
            parsed.serial_path = Path(path)
        else:
            parsed.serial_mode = "unresolved-chardev"
            parsed.notes.append(f"serial chardev {identifier!r} has no file backend")
        return
    parsed.serial_mode = head or "stdio"
    if parsed.serial_mode not in ("stdio", "none", "mon:stdio", "vc", "null"):
        parsed.notes.append(f"serial backend {head!r} recorded, no log file opened")


def parse_argv(argv: Sequence[str]) -> _ParsedArgs:
    """Parse a QEMU command line without ever raising.

    Unknown arguments are kept verbatim in :attr:`_ParsedArgs.other_args` and
    in the recorded argv, so a caller can assert on what was really passed.
    """
    tokens = _split_attached(list(argv))
    parsed = _ParsedArgs(argv=list(argv))
    consumed: set[int] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        flag_index = index
        if token not in _VALUE_OPTIONS:
            if token in ("-enable-kvm", "-enable-hax", "-enable-haxm"):
                parsed.kvm_flag = True
                consumed.add(flag_index)
            elif token in ("-help", "--help", "-h"):
                parsed.notes.append(_HELP_NOTE)
                consumed.add(flag_index)
            index += 1
            continue
        value, index = _next_value(tokens, index)
        if value is None:
            parsed.notes.append(f"{token} without a value")
            consumed.add(flag_index)
            if token == "-serial":
                # QEMU treats a bare ``-serial`` as stdio: no log file.
                parsed.serial_mode = "stdio"
            continue
        consumed.add(flag_index)
        consumed.add(index - 1)
        if token == "-qmp":
            _parse_qmp_spec(value, parsed)
        elif token == "-serial":
            _parse_serial(value, parsed)
        elif token == "-chardev":
            _parse_chardev(value, parsed)
        elif token == "-accel":
            parsed.accel = value
            for part in value.split(","):
                parsed.accels.append(part.strip())
        elif token == "-machine":
            parsed.machine = value
            _check_machine_accel(value, parsed)
        elif token == "-name":
            parsed.name = value
    for position, token in enumerate(tokens):
        if position not in consumed:
            parsed.other_args.append(token)
    return parsed


def _check_machine_accel(value: str, parsed: _ParsedArgs) -> None:
    """Flag ``-machine accel=kvm`` style requests as non-TCG."""
    for part in value.split(","):
        key, _, item = part.partition("=")
        if key.strip() == "accel" and item.strip():
            parsed.accel = parsed.accel or item.strip()
            parsed.accels.append(item.strip())
            if item.strip().lower() not in ("tcg",):
                parsed.notes.append(f"machine accel={item.strip()!r} is not tcg")


def _requested_accel(parsed: _ParsedArgs) -> Optional[str]:
    """Return the non-TCG accelerator the argv asked for, if any.

    ``None`` means "TCG or nothing was requested", which is the only state the
    fake accepts.  Any other value is reported so the caller can refuse it
    exactly like :mod:`unipad.host.qemu` does (PRD 2.2-1).

    Every ``-accel`` token is inspected, not just the last one: real QEMU
    accumulates them, so ``-accel tcg -accel kvm`` is a request to run KVM and
    must be refused just like a bare ``-accel kvm``.
    """
    if parsed.kvm_flag:
        return "kvm"
    for requested in parsed.accels:
        primary = requested.split(",", 1)[0].strip().lower()
        if primary and primary not in ("tcg",):
            return primary
    if not parsed.accel:
        return None
    primary = parsed.accel.split(",", 1)[0].strip().lower()
    if primary in ("tcg", ""):
        return None
    return primary


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def free_tcp_port() -> int:
    """Reserve and release a loopback TCP port (best effort, for tests)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _env_int(name: str, default: int) -> int:
    """Read a non-negative integer environment knob."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(int(raw.strip()), 0)
    except ValueError:
        return default


def _env_mode() -> str:
    """Normalised ``UNIPAD_FAKE_FAIL_MODE`` value."""
    return (os.environ.get("UNIPAD_FAKE_FAIL_MODE") or "").strip().lower()


def _ppm_bytes(width: int, height: int) -> bytes:
    """A minimal, valid, deterministic binary PPM (``P6``) image."""
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    count = width * height * 3
    body = (bytes(range(256)) * (count // 256 + 1))[:count]
    return header + body


# ---------------------------------------------------------------------------
# the fake itself
# ---------------------------------------------------------------------------


class _FakeQemu:
    """One fake QEMU process: argv, serial log, QMP server, stop handling."""

    def __init__(self, argv: Sequence[str]) -> None:
        self.parsed = parse_argv(argv)
        self.workdir = Path.cwd()
        self.mode = _env_mode()
        self.boot_ms = _env_int("UNIPAD_FAKE_BOOT_MS", 1500)
        self.exit_code = _env_int("UNIPAD_FAKE_EXIT_CODE", 0)
        self.screen_width = _env_int("UNIPAD_FAKE_SCREEN_W", DEFAULT_SCREEN_WIDTH)
        self.screen_height = _env_int("UNIPAD_FAKE_SCREEN_H", DEFAULT_SCREEN_HEIGHT)
        self._stop = threading.Event()
        self._stop_reason = "not-stopped"
        self._serial_handle: Any = None
        self._server_socket: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []
        self._input_lock = threading.Lock()
        self._input_seq = 0

    # -- lifecycle --------------------------------------------------------

    def run(self) -> int:
        """Start the fake and block until it is asked to stop."""
        rejected = _requested_accel(self.parsed)
        if rejected is not None:
            # Mirror of the real guard: PRD 2.2-1 refuses anything but TCG.
            self._write_args(rejected_accel=rejected)
            sys.stderr.write(
                f"fake-qemu: accelerator_not_tcg: refusing accel={rejected!r} "
                "(Intel Windows host must run explicit QEMU TCG, PRD 2.2-1)\n"
            )
            sys.stderr.flush()
            return 2

        if self.mode == "exit-early":
            # Record the argv before bailing out so the launcher's stderr tail
            # has matching evidence on disk.
            self._write_args()
            sys.stderr.write(
                "fake-qemu: exit-early fault: bailing out before qmp/serial setup\n"
            )
            sys.stderr.flush()
            return 3
        if _HELP_NOTE in self.parsed.notes:
            self._write_args()
            sys.stdout.write(
                "fake-qemu 1.0 (simulation fixture; not a real QEMU build)\n"
                "usage: fake_qemu.py <qemu args...>\n"
            )
            sys.stdout.flush()
            return 0

        self._open_serial()
        qmp_ok = self._start_qmp_server()
        # Written *after* the QMP decision so ``parsed.notes`` in the evidence
        # includes "qmp listener suppressed by fail mode" when it applies.
        self._write_args()
        if not qmp_ok:
            return 1
        self._start_serial_writer()
        self._install_signal_handlers()

        sys.stdout.write(
            f"fake-qemu 1.0 pid={os.getpid()} argv_tokens={len(self.parsed.argv)} "
            f"qmp={self.parsed.qmp_spec or 'none'} mode={self.mode or 'healthy'}\n"
        )
        sys.stdout.flush()

        try:
            while not self._stop.wait(0.05):
                pass
        except KeyboardInterrupt:
            self._request_stop("sigint")
        finally:
            self._teardown()
        return self.exit_code

    def _request_stop(self, reason: str, exit_code: Optional[int] = None) -> None:
        """Ask the main loop to finish (idempotent, thread-safe)."""
        if exit_code is not None:
            self.exit_code = exit_code
        self._stop_reason = reason
        self._stop.set()

    def _teardown(self) -> None:
        """Flush and close everything; never raises."""
        handle = self._serial_handle
        self._serial_handle = None
        if handle is not None:
            try:
                handle.flush()
                handle.close()
            except (OSError, ValueError):
                pass
        server = self._server_socket
        self._server_socket = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)

    # -- evidence ---------------------------------------------------------

    def _write_args(self, *, rejected_accel: Optional[str] = None) -> None:
        """Record the full argv (one JSON line) for evidence assertions."""
        record: dict[str, Any] = {
            "fake_qemu": "1.0",
            "pid": os.getpid(),
            "cwd": self.workdir.as_posix(),
            "parsed": self.parsed.to_dict(),
            "fail_mode": self.mode or None,
            "boot_ms": self.boot_ms,
        }
        if rejected_accel is not None:
            record["rejected"] = {
                "reason": "accelerator_not_tcg",
                "requested_accel": rejected_accel,
                "required_accel": "tcg",
                "exit_code": 2,
            }
        target = self.workdir / ARGS_FILENAME
        try:
            target.write_text(
                json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - unwritable cwd
            sys.stderr.write(f"fake-qemu: could not record argv: {exc}\n")

    def _record_input(self, channel: str, **fields: Any) -> None:
        """Append one JSON line to the input-event journal."""
        with self._input_lock:
            self._input_seq += 1
            seq = self._input_seq
        record = {"seq": seq, "channel": channel}
        record.update(fields)
        target = self.workdir / INPUT_EVENTS_FILENAME
        try:
            with self._input_lock:
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        except OSError:  # pragma: no cover - unwritable cwd
            pass

    # -- serial -----------------------------------------------------------

    def _open_serial(self) -> None:
        """Open the ``-serial file:`` target for writing, if one was given."""
        if self.mode == "no-serial":
            return
        path = self.parsed.serial_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._serial_handle = open(path, "wb")
        except OSError as exc:
            sys.stderr.write(f"fake-qemu: could not open serial log {path}: {exc}\n")
            sys.stderr.flush()
            self._serial_handle = None

    def _serial_markers(self) -> tuple[str, ...]:
        if self.mode == "partial-boot":
            return PARTIAL_BOOT_MARKERS
        return BOOT_MARKERS

    def _write_serial(self) -> None:
        """Stage the boot markers onto the serial log, flushing each line."""
        markers = self._serial_markers()
        handle = self._serial_handle
        if handle is None or not markers:
            if handle is not None and self.mode == "exit-after-boot":
                self._request_stop("exit-after-boot", exit_code=4)
            return
        interval = (self.boot_ms / 1000.0) / max(len(markers) - 1, 1)
        try:
            for position, marker in enumerate(markers):
                if position and interval > 0:
                    time.sleep(interval)
                handle.write((marker + "\n").encode("utf-8"))
                handle.flush()
        except (OSError, ValueError):  # pragma: no cover - log closed under us
            return
        if self.mode == "exit-after-boot":
            self._request_stop("exit-after-boot", exit_code=4)

    def _start_serial_writer(self) -> None:
        thread = threading.Thread(
            target=self._write_serial, name="fake-qemu-serial", daemon=True
        )
        thread.start()
        self._threads.append(thread)

    # -- qmp --------------------------------------------------------------

    def _start_qmp_server(self) -> bool:
        """Bind the QMP TCP port and serve it in a background thread.

        Returns ``False`` when this process cannot own the port.  Ownership is
        *proved*, not assumed: Windows permits a second ``bind()`` on a port
        another socket is already listening to when ``SO_REUSEADDR`` is set, so
        a stale fake could silently steal the port and answer QMP from a
        different working directory.  A self-connect that our own ``accept()``
        never sees means somebody else owns the port, which is a hard failure
        (exit 1) rather than a fake that quietly serves nothing.
        """
        host, port = self.parsed.qmp_host, self.parsed.qmp_port
        if host is None or port is None or self.parsed.qmp_transport != "tcp":
            if self.mode in ("no-qmp", "stall"):
                return True
            return True
        if self.mode in ("no-qmp", "stall"):
            self.parsed.notes.append("qmp listener suppressed by fail mode")
            return True
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((host, port))
            server.listen(8)
            server.settimeout(0.2)
        except OSError as exc:
            sys.stderr.write(
                f"fake-qemu: could not bind qmp {host}:{port}: {exc}\n"
            )
            sys.stderr.flush()
            return False
        if not self._owns_port(server, host, port):
            try:
                server.close()
            except OSError:
                pass
            return False
        self._server_socket = server
        thread = threading.Thread(
            target=self._accept_loop, name="fake-qemu-qmp", daemon=True
        )
        thread.start()
        self._threads.append(thread)
        return True

    def _owns_port(self, server: socket.socket, host: str, port: int) -> bool:
        """Prove that *this* process answers on ``host:port``.

        Connects to our own listener and requires our own ``accept()`` to see
        the probe within a short deadline.  Returns ``False`` (with a message
        on stderr) when another process holds the port.
        """
        prober: Optional[socket.socket] = None
        try:
            prober = socket.create_connection((host, port), timeout=0.5)
        except OSError as exc:
            sys.stderr.write(
                f"fake-qemu: qmp {host}:{port} is not reachable after bind: {exc}\n"
            )
            sys.stderr.flush()
            return False
        finally:
            if prober is not None:
                try:
                    prober.close()
                except OSError:
                    pass
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return False
            try:
                conn.close()
            except OSError:
                pass
            return True
        sys.stderr.write(
            f"fake-qemu: qmp {host}:{port} is already served by another process; "
            "this fake cannot answer QMP for this working directory\n"
        )
        sys.stderr.flush()
        return False

    def _accept_loop(self) -> None:
        """Accept QMP clients until the process is asked to stop."""
        server = self._server_socket
        if server is None:
            return
        while not self._stop.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            worker = threading.Thread(
                target=self._serve_connection,
                args=(conn,),
                name="fake-qemu-qmp-client",
                daemon=True,
            )
            worker.start()
            self._threads.append(worker)

    def _serve_connection(self, conn: socket.socket) -> None:
        """Speak the QMP JSON-line protocol on one client connection."""
        conn.settimeout(0.2)
        buffer = b""
        try:
            if self.mode == "bad-greeting":
                # A chatty endpoint that is not speaking QMP at all.
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n")
                return
            self._send(conn, QMP_GREETING)
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
                    reply, events, stop = self._safe_handle(line)
                    for event in events:
                        self._send(conn, event)
                    if reply is not None:
                        self._send(conn, reply)
                    if stop:
                        time.sleep(_SOCKET_DRAIN_SECONDS)
                        self._request_stop("qmp-quit")
                        return
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send(conn: socket.socket, message: dict[str, Any]) -> None:
        """Write one JSON line, ignoring a peer that already hung up."""
        try:
            conn.sendall(
                (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
                    "utf-8"
                )
            )
        except OSError:
            pass

    def _safe_handle(
        self, raw_line: bytes
    ) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], bool]:
        """Run :meth:`_handle_message` without ever killing the connection.

        The QMP connection thread owns the client socket.  If a command
        handler raised, the ``finally`` in :meth:`_serve_connection` would close
        the socket and the real client would see EOF ("connection closed while
        awaiting a reply") instead of a diagnosable answer.  A fake must never
        fail silently, so the traceback is written to stderr and to
        ``fake-qemu-error.log`` in the working directory and the client gets a
        structured ``GenericError`` reply carrying the ``id`` it sent.
        """
        try:
            return self._handle_message(raw_line)
        except Exception as exc:  # noqa: BLE001 - defensive: never lose the link
            detail = "".join(traceback.format_exc())
            try:
                request_id = json.loads(
                    raw_line.decode("utf-8", errors="replace")
                ).get("id")
            except Exception:  # noqa: BLE001 - unparsable input has no id
                request_id = None
            message = (
                f"fake-qemu: unhandled {type(exc).__name__} in QMP handler: {exc}\n"
                f"{detail}"
            )
            sys.stderr.write(message)
            sys.stderr.flush()
            try:
                with open(ERROR_LOG_FILENAME, "a", encoding="utf-8") as handle:
                    handle.write(message)
            except OSError:
                pass
            error = {
                "error": {
                    "class": "GenericError",
                    "desc": f"fake-qemu internal error: {type(exc).__name__}: {exc}",
                }
            }
            if request_id is not None:
                error["id"] = request_id
            return error, [], False

    def _handle_message(
        self, raw_line: bytes
    ) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], bool]:
        """Turn one request line into ``(reply, events, stop)``."""
        try:
            message = json.loads(raw_line.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, [], False
        if not isinstance(message, dict):
            return None, [], False
        command = message.get("execute")
        request_id = message.get("id")
        arguments = message.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}

        def reply(payload: dict[str, Any]) -> dict[str, Any]:
            if request_id is not None:
                payload["id"] = request_id
            return payload

        if not isinstance(command, str) or not command:
            return reply({"error": {"class": "GenericError", "desc": "missing command"}}), [], False

        if command == "qmp_capabilities":
            if self.mode == "no-capabilities":
                # The banner was delivered but negotiation never completes, so
                # QmpClient.connect() must fail instead of reporting readiness.
                return None, [], False
            return reply({"return": {}}), [], False
        if command == "query-status":
            return (
                reply(
                    {
                        "return": {
                            "status": "running",
                            "running": True,
                            "singlestep": False,
                        }
                    }
                ),
                [],
                False,
            )
        if command == "query-name":
            return reply({"return": {"name": FAKE_NAME}}), [], False
        if command == "query-version":
            return reply({"return": dict(QMP_VERSION)}), [], False
        if command == "query-block":
            return reply({"return": [{"device": "virtio0", "inserted": {}}]}), [], False
        if command == "screendump":
            self._screendump(arguments)
            return reply({"return": {}}), [], False
        if command == "human-monitor-command":
            output = self._human_monitor(arguments)
            return reply({"return": output}), [], False
        if command == "send-key":
            self._record_send_key(arguments)
            return reply({"return": {}}), [], False
        if command == "input-send-event":
            self._record_input_send_event(arguments)
            return reply({"return": {}}), [], False
        if command in ("stop", "cont", "system_powerdown"):
            return reply({"return": {}}), [], False
        if command == "system_reset":
            event = {"event": "RESET", "data": {"guest": False, "reason": "guest-reset"}}
            return reply({"return": {}}), [event], False
        if command == "quit":
            if self.mode == "ignore-quit":
                # Graceful shutdown is ignored: the launcher must escalate.
                return reply({"return": {}}), [], False
            event = {
                "event": "SHUTDOWN",
                "data": {"guest": False, "reason": "guest-shutdown"},
            }
            return reply({"return": {}}), [event], True
        return (
            reply(
                {
                    "error": {
                        "class": "CommandNotFound",
                        "desc": f"The command {command} has not been found",
                    }
                }
            ),
            [],
            False,
        )

    # -- qmp command bodies ----------------------------------------------

    def _screendump(self, arguments: dict[str, Any]) -> None:
        """Write a minimal valid PPM where the client asked for it."""
        filename = arguments.get("filename")
        if not isinstance(filename, str) or not filename:
            return
        width = _coerce_size(arguments.get("width"), self.screen_width)
        height = _coerce_size(arguments.get("height"), self.screen_height)
        target = Path(filename)
        try:
            if target.parent and not target.parent.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_ppm_bytes(width, height))
        except OSError as exc:
            sys.stderr.write(f"fake-qemu: screendump failed for {filename}: {exc}\n")

    def _human_monitor(self, arguments: dict[str, Any]) -> str:
        """Answer one HMP command and journal the input it represents."""
        command_line = arguments.get("command-line")
        if not isinstance(command_line, str) or not command_line.strip():
            return ""
        tokens = command_line.split()
        head = tokens[0].lower()
        if head == "mouse_move":
            fields: dict[str, Any] = {"kind": "mouse_move"}
            if len(tokens) >= 3:
                fields["x"] = _coerce_int(tokens[1])
                fields["y"] = _coerce_int(tokens[2])
            self._record_input("human-monitor-command", command_line=command_line, **fields)
            return ""
        if head in ("mouse_button", "mouse_set"):
            fields = {"kind": "mouse_button"}
            if len(tokens) >= 2:
                fields["button"] = _coerce_int(tokens[1])
            self._record_input("human-monitor-command", command_line=command_line, **fields)
            return ""
        if head == "send_key" or head == "sendkey":
            fields = {"kind": "send_key", "keys": tokens[1:]}
            self._record_input("human-monitor-command", command_line=command_line, **fields)
            return ""
        self._record_input(
            "human-monitor-command", command_line=command_line, kind="hmp-other"
        )
        return f"unknown command: '{command_line}'"

    def _record_send_key(self, arguments: dict[str, Any]) -> None:
        """Journal a QMP ``send-key`` (press+release) request."""
        keys: list[str] = []
        raw_keys = arguments.get("keys")
        if isinstance(raw_keys, list):
            for entry in raw_keys:
                if isinstance(entry, dict) and isinstance(entry.get("data"), str):
                    keys.append(entry["data"])
                elif isinstance(entry, str):
                    keys.append(entry)
        self._record_input("send-key", kind="send_key", keys=keys)

    def _record_input_send_event(self, arguments: dict[str, Any]) -> None:
        """Journal a QMP ``input-send-event`` request."""
        events = arguments.get("events")
        self._record_input(
            "input-send-event",
            kind="input_send_event",
            events=list(events) if isinstance(events, list) else [],
        )

    # -- signals ----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Exit 0 promptly on SIGTERM/SIGINT (Windows: also SIGBREAK).

        Windows caveat, recorded because it changes what a test can assert:
        :meth:`subprocess.Popen.terminate` calls ``TerminateProcess``, which the
        OS applies without ever running a handler -- every process, real QEMU
        included, dies with exit code 1 and no Python code runs.  To exercise
        the graceful-signal path on Windows, deliver a console control event
        with ``proc.send_signal(signal.CTRL_BREAK_EVENT)``; that requires the
        child to be in its own process group, which is what
        :mod:`unipad.host.qemu` does with ``CREATE_NEW_PROCESS_GROUP``.
        """
        candidates = [
            signal.SIGTERM,
            signal.SIGINT,
            getattr(signal, "SIGBREAK", None),
        ]
        for sig in candidates:
            if sig is None:
                continue
            try:
                signal.signal(sig, self._on_signal)
            except (OSError, ValueError, RuntimeError):  # pragma: no cover
                pass

    def _on_signal(self, signum: int, _frame: Any) -> None:
        names = {
            int(signal.SIGTERM): "sigterm",
            int(signal.SIGINT): "sigint",
        }
        name = names.get(int(signum), f"signal-{signum}")
        self._request_stop(name)


def _coerce_size(value: Any, default: int) -> int:
    """Clamp a requested screendump dimension to something writable."""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(size, 4096))


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# subprocess driver
#
# Three host-side test modules (QMP client, QEMU launcher, evidence bundle and
# input sink) all need the *same* thing: a real child process speaking the real
# protocol.  Spawning it three times over would be duplication (CLAUDE.md 15),
# so the spawn/wait/teardown dance lives here next to the fake itself.
# ---------------------------------------------------------------------------

#: Windows flag that keeps the fake from flashing a console window.
_CREATE_NO_WINDOW = 0x08000000


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """Parse a JSONL file into a list of objects.

    Returns ``[]`` when the file is absent, unreadable, or holds a line that is
    not a JSON object.  Evidence readers must never raise on a half-written
    journal: a missing journal means "no input was recorded", which is itself
    the assertion most callers want to make.
    """
    target = Path(path)
    if not target.is_file():
        return []
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


class FakeQemuProcess:
    """One running fake QEMU child process plus its evidence files.

    Deliberately a plain class rather than a dataclass: tests may load
    :mod:`fake_qemu` by file path with :func:`importlib.util.spec_from_file_location`,
    and a dataclass under ``from __future__ import annotations`` needs its module
    registered in ``sys.modules`` to resolve its string annotations (same
    rationale as :class:`_ParsedArgs`).
    """

    def __init__(
        self,
        process: Any,
        argv: Sequence[str],
        port: int,
        workdir: Path | str,
        serial_path: Optional[Path | str] = None,
        stderr_path: Optional[Path | str] = None,
    ) -> None:
        self.process = process
        self.argv = list(argv)
        self.port = int(port)
        self.workdir = Path(workdir)
        self.serial_path = Path(serial_path) if serial_path is not None else None
        self.stderr_path = Path(stderr_path) if stderr_path is not None else None

    # -- evidence locations ---------------------------------------------

    @property
    def args_path(self) -> Path:
        """``fake-qemu-args.json`` written by the child into its cwd."""
        return self.workdir / ARGS_FILENAME

    @property
    def input_journal(self) -> Path:
        """``fake-input-events.jsonl`` written by the child into its cwd."""
        return self.workdir / INPUT_EVENTS_FILENAME

    def args_record(self) -> Optional[dict[str, Any]]:
        """The child's recorded argv view, or ``None`` before it is written."""
        records = read_jsonl(self.args_path)
        return records[0] if records else None

    def input_events(self) -> list[dict[str, Any]]:
        """Every input command the fake journaled over QMP, in order."""
        return read_jsonl(self.input_journal)

    def serial_text(self) -> str:
        """The staged boot serial log (``""`` when ``-serial`` was not given)."""
        if self.serial_path is None or not self.serial_path.is_file():
            return ""
        try:
            return self.serial_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stderr_text(self) -> str:
        """The child's stderr, used as failure evidence."""
        if self.stderr_path is None or not self.stderr_path.is_file():
            return ""
        try:
            return self.stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # -- lifecycle ------------------------------------------------------

    def wait_for_port(self, timeout: float = 15.0) -> bool:
        """Block until the QMP port accepts a TCP connection.

        Returns ``False`` as soon as the child dies (an ``exit-early`` fault) or
        when the budget expires.  A caller that needs "never became ready"
        semantics should pass ``wait_for_port=False`` to
        :func:`fake_qemu_process` and drive :class:`QmpClient` directly.
        """
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            if self.process.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                    return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.02)

    def stop(self, timeout: float = 5.0) -> Optional[int]:
        """Terminate the child (kill if it refuses) and return its exit code."""
        process = self.process
        if process.poll() is not None:
            return process.returncode
        try:
            process.terminate()
        except OSError:  # pragma: no cover - already gone
            pass
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            try:
                process.kill()
            except OSError:
                pass
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return None


@contextmanager
def fake_qemu_process(
    extra_args: Optional[Sequence[str]] = None,
    *,
    fail_mode: Optional[str] = None,
    boot_ms: int = 0,
    exit_code: Optional[int] = None,
    screen: Optional[tuple[int, int]] = None,
    workdir: Optional[Path | str] = None,
    qmp_port: Optional[int] = None,
    serial_path: Optional[Path | str] = None,
    env: Optional[dict[str, str]] = None,
    wait_for_port: bool = True,
    wait_timeout: float = 15.0,
) -> Iterator[FakeQemuProcess]:
    """Run the fake as a child process for the duration of a ``with`` block.

    The child is started exactly like the binary it stands in for, so the real
    :class:`unipad.host.qmp.QmpClient` and
    :class:`unipad.host.qemu.QemuLauncher` can be driven against it
    (docs/testing-strategy.md D3, "Fake QEMU" row).

    Args:
        extra_args: Additional QEMU arguments, appended verbatim.
        fail_mode: Injected fault (``exit-early``, ``no-qmp``, ``bad-greeting``,
            ``ignore-quit``, ``partial-boot``, ``no-serial``, ``stall``,
            ``exit-after-boot``).
        boot_ms: Total time spent emitting the boot markers.  ``0`` writes them
            back to back, which keeps the suite fast and deterministic.
        exit_code: Exit code used for a clean stop.
        screen: ``(width, height)`` used when a screendump carries no size.
        workdir: Child cwd; all evidence files land here.  Defaults to a
            per-port temporary directory that is removed on exit.
        qmp_port: Reuse a specific port instead of reserving a free one.
        serial_path: Add ``-serial file:<path>`` so the staged log is written.
        env: Extra child environment variables (applied last).
        wait_for_port: Wait for the QMP port before yielding.
        wait_timeout: Budget for :meth:`FakeQemuProcess.wait_for_port`.

    Yields:
        A :class:`FakeQemuProcess` handle.  The child is always terminated (and
        killed if it refuses) on exit, including on test failure.
    """
    created_workdir = workdir is None
    if created_workdir:
        port_for_dir = int(qmp_port) if qmp_port is not None else free_tcp_port()
        work = Path(tempfile.gettempdir()) / f"unipad-fake-qemu-{port_for_dir}"
    else:
        work = Path(workdir)  # type: ignore[arg-type]
    port = int(qmp_port) if qmp_port is not None else free_tcp_port()

    args = [
        "-accel",
        "tcg",  # the fake mirrors the launcher's explicit-TCG guard (PRD 2.2-1)
        "-monitor",
        "none",
        "-display",
        "none",
        "-qmp",
        f"tcp:127.0.0.1:{port},server=on,wait=off",
    ]
    if serial_path is not None:
        args.extend(["-serial", f"file:{Path(serial_path).as_posix()}"])
    args.extend(str(arg) for arg in (extra_args or ()))
    argv = [sys.executable, _script_path().as_posix(), *args]

    child_env = dict(os.environ)
    child_env["UNIPAD_FAKE_BOOT_MS"] = str(int(boot_ms))
    if fail_mode:
        child_env["UNIPAD_FAKE_FAIL_MODE"] = str(fail_mode)
    if exit_code is not None:
        child_env["UNIPAD_FAKE_EXIT_CODE"] = str(int(exit_code))
    if screen is not None:
        child_env["UNIPAD_FAKE_SCREEN_W"] = str(int(screen[0]))
        child_env["UNIPAD_FAKE_SCREEN_H"] = str(int(screen[1]))
    if env:
        child_env.update({str(k): str(v) for k, v in env.items()})

    work.mkdir(parents=True, exist_ok=True)
    stderr_path = work / "fake-stderr.log"
    with open(stderr_path, "wb") as stderr_handle:
        process = subprocess.Popen(
            argv,
            cwd=str(work),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle,
            close_fds=True,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    handle = FakeQemuProcess(process, argv, port, work, serial_path, stderr_path)
    try:
        if wait_for_port:
            handle.wait_for_port(timeout=wait_timeout)
        yield handle
    finally:
        try:
            handle.stop()
        finally:
            if created_workdir:
                shutil.rmtree(work, ignore_errors=True)


def fake_qemu_launcher() -> tuple[str, list[str]]:
    """Return ``(binary, argv_prefix)`` for :meth:`QemuLauncher.launch`.

    The launcher takes one executable path plus an argv list, so the fake is
    started as ``[sys.executable, <this file>, *qemu_args]``::

        binary, prefix = fake_qemu_launcher()
        launcher.launch(binary, [*prefix, *args], workdir=tmp_path)

    Nothing is spawned and no port is bound.
    """
    return sys.executable, [_script_path().as_posix()]


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def fake_qemu_command(
    extra_args: Optional[Sequence[str]] = None,
    *,
    qmp_port: Optional[int] = None,
    serial_path: Optional[Path] = None,
) -> list[str]:
    """Return the argv that launches this fake through the real launcher.

    The result is exactly ``[sys.executable, <this file>, *args]`` -- no flags
    are injected, so ``command[1:]`` is verbatim what the caller asked for and
    the recorded ``fake-qemu-args.json`` evidence is not polluted with
    duplicated ``-accel`` tokens when a caller passes the argv produced by
    :meth:`unipad.host.qemu.QemuLauncher.build_args`.

    Hand it to the launcher as ``command[0]`` plus ``command[1:]`` (the launcher
    takes one binary string and an argv list, not a single joined command).

    Args:
        extra_args: QEMU arguments, appended verbatim.  Pass ``["-accel", "tcg"]``
            yourself when a test needs the explicit-TCG guard to be exercised.
        qmp_port: When given, append the standard loopback QMP spec.
        serial_path: When given, append ``-serial file:<path>``.

    Returns:
        The argv list.  Nothing is spawned and no port is bound.
    """
    args: list[str] = []
    if qmp_port is not None:
        args.extend(["-qmp", f"tcp:127.0.0.1:{int(qmp_port)},server=on,wait=off"])
    if serial_path is not None:
        args.extend(["-serial", f"file:{Path(serial_path).as_posix()}"])
    args.extend(str(arg) for arg in (extra_args or ()))
    return [sys.executable, _script_path().as_posix(), *args]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Script entry point: run one fake QEMU process."""
    raw = list(sys.argv[1:] if argv is None else argv)
    return _FakeQemu(_strip_launcher_tokens(raw)).run()


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
