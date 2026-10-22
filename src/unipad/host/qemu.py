"""QEMU process launcher for Windows (PRD 2.2, 4.1, 4.2, 6 WP02).

The launcher is the single place where the accelerator guard lives:
:class:`unipad.models.MachineConfig` with anything other than ``tcg`` is
rejected with ``accelerator_not_tcg`` (PRD 2.2-1).  There is no fallback and no
"best effort" mode -- an Intel Windows host can only emulate AArch64 with TCG.

Windows-specific decisions encoded here:

- QMP is **TCP on loopback** (``tcp:127.0.0.1:<port>,server=on,wait=off``);
  Windows has no ``AF_UNIX`` QMP socket (PRD 4.2).
- The guest display is served by ``ramfb`` (PRD 4.2 "reuse ramfb/GOP path");
  ``-display none`` is only emitted for an explicit ``headless=True`` run.
- ``-monitor none`` keeps the human monitor off the serial/stdio path so the
  control and serial channels never contend (config/machine.json).
- Paths are passed to QEMU with forward slashes; ``subprocess`` gets an argv
  list, so no shell quoting is involved.

Readiness is **never** inferred from a sleep: :meth:`QemuLauncher.wait_for_qmp`
only reports ready after a QMP greeting *and* a ``query-status`` reply, and it
records that evidence (PRD 7.4, FR08).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from unipad import models
from unipad.errors import ErrorCode, UniPadError
from unipad.host.qmp import QmpClient

__all__ = ["QemuLauncher", "LaunchResult", "QmpReadiness", "StopResult"]

#: Windows-only: run without a console window / signal handling quirks.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_LAUNCH_FLAGS = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW

#: Path separators QEMU accepts on Windows (never a backslash escape).
_QEMU_PATH_SEP = "/"


def _qemu_path(path: Path | str) -> str:
    """Render ``path`` for a QEMU command line using forward slashes."""
    return Path(path).as_posix()


def _unquote(token: str) -> str:
    """Strip one layer of matching double quotes (Windows command-line style)."""
    if len(token) >= 2 and token[0] == token[-1] == '"':
        return token[1:-1]
    return token


def _split_command_line(text: str) -> list[str]:
    """Split a Windows command line into tokens.

    ``posix=False`` keeps backslashes intact, which is the only correct reading
    of a Windows path (``C:\\Program Files\\...``).  An unbalanced quote yields
    no tokens so the caller reports the attempted path instead of guessing.
    """
    try:
        return [_unquote(token) for token in shlex.split(text, posix=False)]
    except ValueError:
        return []



def _parse_drive_file(spec: str) -> Optional[str]:
    """Extract the ``file=`` component of a ``-drive`` option string."""
    for part in spec.split(","):
        key, _, value = part.partition("=")
        if key.strip() == "file" and value.strip():
            return value.strip()
    return None


@dataclass
class LaunchResult:
    """Structured outcome of :meth:`QemuLauncher.launch`."""

    ok: bool
    binary: str
    args: list[str] = field(default_factory=list)
    pid: Optional[int] = None
    workdir: str = ""
    qmp_port: int = 0
    stdout_path: str = ""
    stderr_path: str = ""
    serial_path: str = ""
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view (no secrets, paths only)."""
        return {
            "ok": self.ok,
            "binary": self.binary,
            "args": list(self.args),
            "pid": self.pid,
            "workdir": self.workdir,
            "qmp_port": self.qmp_port,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "serial_path": self.serial_path,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "evidence": dict(self.evidence),
        }


@dataclass
class QmpReadiness:
    """Structured outcome of :meth:`QemuLauncher.wait_for_qmp`."""

    ready: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    attempts: int = 0
    waited_seconds: float = 0.0

    @property
    def status(self) -> Optional[str]:
        """The guest run state reported by ``query-status``, if it was reached.

        Derived from :attr:`evidence` and never synthesised: ``None`` means QMP
        never answered, which is *not* the same as ``"running"``.
        """
        status = self.evidence.get("status")
        if isinstance(status, dict):
            value = status.get("status")
            return value if isinstance(value, str) else None
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view."""
        return {
            "ready": self.ready,
            "evidence": dict(self.evidence),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "attempts": self.attempts,
            "waited_seconds": self.waited_seconds,
        }


@dataclass
class StopResult:
    """Structured outcome of :meth:`QemuLauncher.stop`."""

    ok: bool
    method: str = "noop"
    exit_code: Optional[int] = None
    already_stopped: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view."""
        return {
            "ok": self.ok,
            "method": self.method,
            "exit_code": self.exit_code,
            "already_stopped": self.already_stopped,
            "evidence": dict(self.evidence),
        }


class QemuLauncher:
    """Build the QEMU command line and own the resulting child process.

    Args:
        machine: The machine definition from ``config/machine.json``.
        qmp_host: QMP bind host (always loopback on Windows).
        qmp_timeout: Default per-QMP-command timeout in seconds.
        poll_interval: Sleep between readiness probes in seconds.
        process_timeout: Default grace period for a graceful stop.

    Example:
        >>> from unipad.models import MachineConfig      # doctest: +SKIP
        >>> launcher = QemuLauncher(MachineConfig())     # doctest: +SKIP
        >>> args = launcher.build_args(fw, sysimg, dataimg, workdir, 4444)
        >>> launcher.launch("qemu-system-aarch64", args) # doctest: +SKIP

    Raises:
        UniPadError: ``accelerator_not_tcg`` from the constructor when
            ``machine.accel`` is not ``tcg`` (case-insensitive).  The guard is
            applied at construction so no code path can build WHPX/HVF args.
    """

    def __init__(
        self,
        machine: models.MachineConfig,
        *,
        qmp_host: str = "127.0.0.1",
        qmp_timeout: float = 10.0,
        poll_interval: float = 0.25,
        process_timeout: float = 30.0,
    ) -> None:
        if not isinstance(machine, models.MachineConfig):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "machine must be a unipad.models.MachineConfig",
                received_type=type(machine).__name__,
            )
        accel = (machine.accel or "").strip().lower()
        if accel != "tcg":
            raise UniPadError(
                ErrorCode.ACCELERATOR_NOT_TCG,
                "accelerator_not_tcg: Intel Windows host must run explicit QEMU TCG; "
                f"config/machine.json requested accel={machine.accel!r}",
                requested_accel=machine.accel,
                required_accel="tcg",
                reason=(
                    "PRD 2.2-1: Intel VT-x/Hyper-V cannot execute ARM64 iPadOS "
                    "instructions. Any non-TCG accelerator is refused rather than "
                    "silently attempted."
                ),
            )
        self.machine = machine
        self.qmp_host = qmp_host
        self.qmp_timeout = qmp_timeout
        self.poll_interval = max(float(poll_interval), 0.01)
        self.process_timeout = float(process_timeout)

        self._last_args: list[str] = []
        self._last_workdir: Optional[Path] = None
        self._last_qmp_port: Optional[int] = None
        self._last_serial_path: Optional[Path] = None

        self._process: Optional[subprocess.Popen] = None
        self._binary: Optional[str] = None
        self._stdout_handle: Any = None
        self._stderr_handle: Any = None
        self._qmp: Optional[QmpClient] = None
        self._readiness: Optional[QmpReadiness] = None
        self._last_stop: Optional[StopResult] = None
        self._wait_started: float = 0.0

    # -- argument construction ------------------------------------------

    def build_args(
        self,
        firmware: Path,
        system_image: Path,
        data_image: Optional[Path],
        workdir: Path,
        qmp_port: int,
        *,
        headless: bool = False,
        serial_log: Optional[Path] = None,
        extra_args: Sequence[str] = (),
    ) -> list[str]:
        """Build the full QEMU argv (without the executable).

        Args:
            firmware: UEFI firmware / bootloader image (``-bios``).
            system_image: Read-only system (RootFS/SystemOS) image.
            data_image: Writable Data image, or ``None`` for a RAM-disk-only
                diagnostic run (PRD 4.2 "Data" row).
            workdir: Run directory; the serial log lands here by default.
            qmp_port: TCP port for the QMP control channel.
            headless: When ``True`` add ``-display none`` (no guest window).
            serial_log: Override for ``-serial file:<path>``.
            extra_args: Caller-supplied extra arguments, appended verbatim.

        Returns:
            The argv list.  The same list is remembered so
            :meth:`launch` can be called with just the binary.

        Raises:
            UniPadError: ``accelerator_not_tcg`` if the machine accelerator is
                not ``tcg`` (also enforced in the constructor);
                ``config_invalid`` for an unusable ``qmp_port``.
        """
        if not isinstance(qmp_port, int) or not (0 < qmp_port < 65536):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "qmp_port must be an integer in 1..65535",
                qmp_port=qmp_port,
            )

        machine = self.machine
        work = Path(workdir)
        serial_target = Path(serial_log) if serial_log else work / "serial.log"

        args: list[str] = [
            "-accel",
            "tcg",                                   # PRD 2.2-1: explicit, never inferred
            "-cpu",
            str(machine.cpu_model),
            "-machine",
            str(machine.machine_type),
            "-smp",
            str(int(machine.cores)),
            "-m",
            str(int(machine.memory_mb)),
            "-bios",
            _qemu_path(firmware),
        ]

        rtc = (machine.clock or {}).get("rtc")
        if rtc:
            args.extend(["-rtc", f"base={rtc}"])

        # System volume: read-only (PRD 4.2 "read-only property correct").
        args.extend(
            [
                "-drive",
                f"if=virtio,format=raw,file={_qemu_path(system_image)},readonly=on,media=disk",
            ]
        )
        if data_image is not None:
            # Data volume: writable, separate from the system volume.
            args.extend(
                [
                    "-drive",
                    f"if=virtio,format=raw,file={_qemu_path(data_image)},readonly=off,media=disk",
                ]
            )

        display = dict(machine.display or {})
        display_format = str(display.get("format", "ramfb")).lower()
        if display_format.startswith("virtio"):
            args.extend(["-device", "virtio-gpu-pci"])
        else:
            # ramfb/GOP path first (PRD 4.2).  The guest keeps its display;
            # -display is only disabled for explicit headless runs.
            args.extend(["-device", "ramfb"])
        if headless:
            args.extend(["-display", "none"])

        # QMP over TCP: Windows has no AF_UNIX QMP socket.
        args.extend(["-qmp", f"tcp:{self.qmp_host}:{qmp_port},server=on,wait=off"])
        # Keep the human monitor off the serial/stdio path.
        args.extend(["-monitor", "none"])
        # Serial to a file so control and serial never contend.
        args.extend(["-serial", f"file:{_qemu_path(serial_target)}"])
        # Window title/pid is the only supported way to find the QEMU window
        # (PRD 4.2 GPU note: never target by adapter).
        args.extend(["-name", f"unipad-{machine.name}"])

        args.extend(str(a) for a in extra_args)

        self._last_args = list(args)
        self._last_workdir = work
        self._last_qmp_port = qmp_port
        self._last_serial_path = serial_target
        return list(args)

    # -- process lifecycle ----------------------------------------------

    def launch(
        self,
        qemu_binary: str | Sequence[str],
        args: Optional[Sequence[str]] = None,
        *,
        workdir: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> LaunchResult:
        """Start the QEMU child process.

        Args:
            qemu_binary: Executable name (resolved on ``PATH``) or absolute path
                -- **or** a complete command: a sequence such as
                ``[sys.executable, script, ...]`` or a spaced command line.
                Tokens after the executable are prepended to ``args``; when
                ``args`` is omitted they *are* the argv, so a command the caller
                already assembled is never duplicated with a remembered
                :meth:`build_args` list.
            args: Argv list; defaults to the list produced by the last
                :meth:`build_args` call.
            workdir: Overrides the remembered work directory.
            env: Environment overrides on top of ``os.environ``.

        Returns:
            A :class:`LaunchResult`.  ``ok`` only means *the process started*;
            readiness is established separately by :meth:`wait_for_qmp`.

        Raises:
            UniPadError: ``qemu_launch_failed`` when the binary cannot be found
                or ``Popen`` fails (the attempted path is in ``details``),
                ``image_not_found`` when ``-bios`` / ``-drive file=`` targets do
                not exist, ``config_invalid`` when no arguments are available.
        """
        command_prefix, binary_name = self._split_command(qemu_binary)
        if args is not None:
            resolved_args = [*command_prefix, *list(args)]
        else:
            # No explicit argv: the command line the caller handed over *is* the
            # argv.  Falling back to a remembered build_args() list here would
            # start QEMU with every argument twice.
            resolved_args = list(command_prefix) if command_prefix else list(self._last_args)
        if not resolved_args:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "no qemu arguments available: call build_args() first or pass args=",
            )
        missing = self._missing_paths(resolved_args)
        if missing:
            raise UniPadError(
                ErrorCode.IMAGE_NOT_FOUND,
                "qemu launch aborted: required image paths do not exist",
                missing=missing,
            )

        binary = self._resolve_binary(binary_name)
        self._adopt_args(resolved_args)
        work = Path(workdir) if workdir is not None else (self._last_workdir or Path.cwd())
        work.mkdir(parents=True, exist_ok=True)

        stdout_path = work / "qemu-stdout.log"
        stderr_path = work / "qemu-stderr.log"
        child_env = dict(os.environ)
        if env:
            child_env.update({str(k): str(v) for k, v in env.items()})

        self._close_log_handles()
        try:
            out_handle = open(stdout_path, "wb")
            err_handle = open(stderr_path, "wb")
        except OSError as exc:
            raise UniPadError(
                ErrorCode.QEMU_LAUNCH_FAILED,
                "could not open qemu log files",
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
                reason=type(exc).__name__,
            ) from exc

        self._stdout_handle, self._stderr_handle = out_handle, err_handle
        try:
            process = subprocess.Popen(
                [binary, *resolved_args],
                cwd=str(work),
                stdin=subprocess.DEVNULL,
                stdout=out_handle,
                stderr=err_handle,
                env=child_env,
                close_fds=True,
                creationflags=_LAUNCH_FLAGS if sys.platform == "win32" else 0,
            )
        except OSError as exc:
            self._close_log_handles()
            raise UniPadError(
                ErrorCode.QEMU_LAUNCH_FAILED,
                "qemu process could not be started",
                attempted_path=binary,
                reason=type(exc).__name__,
            ) from exc

        self._process = process
        self._binary = binary
        self._readiness = None
        self._last_stop = None
        return LaunchResult(
            ok=True,
            binary=binary,
            args=resolved_args,
            pid=process.pid,
            workdir=str(work),
            qmp_port=self._last_qmp_port or 0,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            serial_path=str(self._last_serial_path or work / "serial.log"),
            evidence={
                "accel": "tcg",
                "machine": self.machine.name,
                "started_at_epoch": round(time.time(), 3),
            },
        )

    def wait_for_qmp(self, timeout: Optional[float] = None) -> QmpReadiness:
        """Block until QMP answers, then return the evidence that proves it.

        The QMP port is polled at ``poll_interval``.  A probe only succeeds when
        a **greeting** is received, ``qmp_capabilities`` is accepted and
        ``query-status`` replies; the returned ``evidence`` carries all three.
        There is no fixed countdown and no "process alive therefore ready"
        shortcut (PRD 7.4, FR08).

        Args:
            timeout: Total budget in seconds; defaults to ``qmp_timeout``.

        Returns:
            A :class:`QmpReadiness`.  ``ready`` is ``False`` with a stable
            ``error_code`` when QEMU exited early (``qemu_launch_failed``) or
            the budget expired (``qmp_unavailable``).
        """
        port = self._last_qmp_port or 0
        if port <= 0:
            return self._finish_wait(
                QmpReadiness(
                    ready=False,
                    error_code=ErrorCode.CONFIG_INVALID,
                    error_message=(
                        "qmp port is unknown: call build_args() or launch() with an argv "
                        "containing -qmp tcp:<host>:<port> first"
                    ),
                    attempts=0,
                    evidence={},
                )
            )
        budget = self.qmp_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + budget
        started = time.monotonic()
        self._wait_started = started
        attempts = 0
        last_reason = "qmp was never probed"

        while True:
            attempts += 1
            early_exit = self._early_exit_code()
            if early_exit is not None:
                return self._finish_wait(
                    QmpReadiness(
                        ready=False,
                        error_code=ErrorCode.QEMU_LAUNCH_FAILED,
                        error_message=(
                            f"qemu exited before qmp became ready (exit code {early_exit})"
                        ),
                        attempts=attempts,
                        evidence={"qemu_exit_code": early_exit, "stderr_tail": self._stderr_tail()},
                    )
                )
            client = QmpClient(self.qmp_host, port, timeout=max(budget, 1.0))
            try:
                negotiated = client.connect()
                status = client.query_status()
            except UniPadError as exc:
                last_reason = f"{exc.code}: {exc.message}"
                client.close()
                if time.monotonic() >= deadline:
                    return self._finish_wait(
                        QmpReadiness(
                            ready=False,
                            error_code=ErrorCode.QMP_UNAVAILABLE,
                            error_message=(
                                f"qmp on {self.qmp_host}:{port} never became ready: {last_reason}"
                            ),
                            attempts=attempts,
                            evidence={
                                "last_error": last_reason,
                                "stderr_tail": self._stderr_tail(),
                            },
                        )
                    )
                time.sleep(self.poll_interval)
                continue

            evidence = {
                "host": self.qmp_host,
                "port": port,
                "greeting": negotiated.get("greeting"),
                "capabilities": negotiated.get("capabilities"),
                "status": status,
                "attempts": attempts,
                "checked_at_epoch": round(time.time(), 3),
            }
            self._qmp = client
            return self._finish_wait(
                QmpReadiness(ready=True, evidence=evidence, attempts=attempts)
            )

    def _finish_wait(self, readiness: QmpReadiness) -> QmpReadiness:
        """Stamp the measured elapsed time on a readiness result and remember it."""
        readiness.waited_seconds = round(time.monotonic() - self._wait_started, 3)
        self._readiness = readiness
        return readiness

    def is_running(self) -> bool:
        """``True`` while the QEMU child process exists and has not exited."""
        return self._process is not None and self._process.poll() is None

    def wait_for_exit(self, timeout: Optional[float] = None) -> Optional[int]:
        """Wait for the child to exit.

        Args:
            timeout: Maximum seconds to wait; ``None`` waits forever.

        Returns:
            The process exit code, or ``None`` if it is still running when the
            budget expires (or was never started).
        """
        if self._process is None:
            return None
        budget = self.process_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + budget
        while True:
            code = self._process.poll()
            if code is not None:
                return code
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(self.poll_interval, 0.2))

    def stop(self, graceful: bool = True, timeout: Optional[float] = None) -> StopResult:
        """Stop the VM: QMP ``quit`` first, then terminate, then kill.

        Args:
            graceful: Ask QEMU to exit over QMP before signalling the process.
            timeout: Per-stage grace period; defaults to ``process_timeout``.

        Returns:
            A :class:`StopResult` recording which mechanism actually ended the
            process and the observed exit code.  Never raises for a process
            that is already gone.
        """
        budget = self.process_timeout if timeout is None else float(timeout)
        if self._process is None:
            return self._remember_stop(
                StopResult(ok=True, method="noop", already_stopped=True,
                           evidence={"note": "no qemu process was started"})
            )
        if self._process.poll() is not None:
            return self._remember_stop(
                StopResult(ok=True, method="already-exited", exit_code=self._process.returncode,
                           already_stopped=True,
                           evidence={"note": "qemu had already exited"})
            )

        quit_evidence: dict[str, Any] = {"graceful": True}
        if graceful and self._qmp is not None and self._qmp.is_connected:
            try:
                self._qmp.quit(timeout=max(budget, 5.0))
            except UniPadError as exc:
                quit_evidence["qmp_quit_error"] = {"code": exc.code, "message": exc.message}
            code = self.wait_for_exit(budget)
            if code is not None:
                return self._remember_stop(
                    StopResult(ok=True, method="qmp-quit", exit_code=code,
                               evidence=quit_evidence)
                )

        try:
            self._process.terminate()
        except OSError:
            pass
        code = self.wait_for_exit(budget)
        if code is not None:
            return self._remember_stop(
                StopResult(ok=True, method="terminate", exit_code=code,
                           evidence={"graceful": False})
            )

        try:
            self._process.kill()
        except OSError:
            pass
        code = self.wait_for_exit(max(budget, 5.0))
        result = StopResult(
            ok=code is not None,
            method="kill",
            exit_code=code,
            evidence={"graceful": False, "note": "forced termination after grace period"},
        )
        if code is None:
            result.evidence["error_code"] = ErrorCode.QEMU_TIMEOUT
        return self._remember_stop(result)

    def qmp_client(self) -> Optional[QmpClient]:
        """Return the live QMP client created by :meth:`wait_for_qmp`, if any."""
        return self._qmp

    def readiness(self) -> Optional[QmpReadiness]:
        """Return the last readiness result (``None`` before :meth:`wait_for_qmp`)."""
        return self._readiness

    def close(self) -> None:
        """Release the QMP client and log handles without signalling QEMU."""
        if self._qmp is not None:
            self._qmp.close()
            self._qmp = None
        self._close_log_handles()

    def __enter__(self) -> "QemuLauncher":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Cleanup must never mask the original failure.
        try:
            self.stop(graceful=True)
        finally:
            self.close()

    # -- internals -------------------------------------------------------

    @staticmethod
    def _parse_qmp_spec(spec: str) -> tuple[str, int]:
        """Parse ``tcp:127.0.0.1:4444,server=on,wait=off`` into ``(host, port)``.

        Args:
            spec: The value of the ``-qmp`` argument.

        Returns:
            ``(host, port)``.  An unparseable or non-TCP spec raises
            ``config_invalid``: QMP must be reachable on a Windows host, and a
            ``none``/unix-socket QMP would silently disable control.
        """
        transport = spec.split(",", 1)[0]
        parts = transport.split(":")
        if len(parts) < 3 or parts[0] != "tcp":
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "qmp transport must be tcp:host:port on Windows (no AF_UNIX QMP)",
                qmp_spec=spec,
            )
        host = parts[1]
        try:
            port = int(parts[2])
        except ValueError as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "qmp port in -qmp spec is not an integer",
                qmp_spec=spec,
            ) from exc
        return host, port

    def _adopt_args(self, args: Sequence[str]) -> None:
        """Re-derive QMP port / serial path from an argv list about to be used.

        ``launch()`` accepts either the argv built by :meth:`build_args` or a
        caller-supplied one; either way the launcher must remember *the argv it
        actually started* so :meth:`wait_for_qmp` and :meth:`stop` target the
        right endpoints.
        """
        self._last_args = list(args)
        for index, value in enumerate(args):
            if value == "-qmp" and index + 1 < len(args):
                host, port = self._parse_qmp_spec(args[index + 1])
                self.qmp_host = host
                self._last_qmp_port = port
            elif value == "-serial" and index + 1 < len(args):
                spec = args[index + 1]
                if spec.startswith("file:"):
                    self._last_serial_path = Path(spec[len("file:"):])

    def _remember_stop(self, result: StopResult) -> StopResult:
        """Record the last stop outcome for later evidence collection."""
        self._last_stop = result
        return result

    def _resolve_binary(self, qemu_binary: str) -> str:
        """Resolve the QEMU executable, or fail with the attempted path."""
        candidate = Path(qemu_binary)
        if candidate.exists():
            return str(candidate)
        found = shutil.which(qemu_binary)
        if found:
            return found
        raise UniPadError(
            ErrorCode.QEMU_LAUNCH_FAILED,
            "qemu binary not found",
            attempted_path=str(qemu_binary),
            hint=(
                "Install a native Windows QEMU build and pass its absolute path or "
                "ensure it is on PATH (PRD 2.2 / M0 open item)."
            ),
        )

    @staticmethod
    def _split_command(command: str | Sequence[str]) -> tuple[list[str], str]:
        """Split ``command`` into ``(argv_prefix, executable)``.

        A sequence is taken verbatim, so ``[sys.executable, script, *args]`` --
        what a launcher adapter (or the fake-QEMU command builder) has in hand --
        needs no unpicking by the caller.

        A string that names an existing file or resolves on ``PATH`` is used
        as-is: a Windows path may legitimately contain spaces
        (``C:\\Program Files\\QEMU\\qemu-system-aarch64.exe``), and splitting it
        would look for an executable that does not exist.  Only a spaced string
        whose *head* resolves to a real executable is treated as a command line.

        Anything else is returned unchanged so the caller still gets the
        documented ``qemu_launch_failed`` naming the attempted path.
        """
        if isinstance(command, os.PathLike):
            # A Path is a plain executable name, never a command line to split.
            command = os.fspath(command)
        if isinstance(command, (list, tuple)):
            tokens = [str(token) for token in command]
            if not tokens:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "qemu command is empty",
                )
            return tokens[1:], tokens[0]
        if not isinstance(command, str) or not command.strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "qemu binary must be a path or a command line",
                received_type=type(command).__name__,
            )
        if Path(command).exists() or shutil.which(command):
            return [], command
        tokens = _split_command_line(command)
        if len(tokens) > 1 and (Path(tokens[0]).exists() or shutil.which(tokens[0])):
            return tokens[1:], tokens[0]
        return [], command

    @staticmethod
    def _missing_paths(args: Sequence[str]) -> list[str]:
        """Return the ``-bios`` / ``-drive file=`` targets that do not exist."""
        missing: list[str] = []
        for index, value in enumerate(args):
            if value == "-bios" and index + 1 < len(args):
                target = args[index + 1]
                if not Path(target).exists():
                    missing.append(f"-bios {target}")
            elif value == "-drive" and index + 1 < len(args):
                target = _parse_drive_file(args[index + 1])
                if target and not Path(target).exists():
                    missing.append(f"-drive file={target}")
        return missing

    def _early_exit_code(self) -> Optional[int]:
        """Return the exit code if QEMU already died, else ``None``."""
        if self._process is None:
            return None
        return self._process.poll()

    def _stderr_tail(self, limit: int = 2000) -> str:
        """Return a bounded tail of QEMU's stderr for failure evidence."""
        handle = self._stderr_handle
        if handle is None:
            return ""
        try:
            handle.flush()
            name = handle.name
        except (OSError, ValueError, AttributeError):
            return ""
        try:
            size = os.path.getsize(name)
            with open(name, "rb") as raw:
                if size > limit:
                    raw.seek(size - limit)
                text = raw.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        if len(text) <= limit:
            return text
        return "...(truncated)...\n" + text[-limit:]

    def _close_log_handles(self) -> None:
        """Close any open stdout/stderr file handles."""
        for attribute in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, attribute, None)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, attribute, None)
