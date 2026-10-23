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
from unipad.host import httpsvc
from unipad.host.display import decide as decide_display
from unipad.host.paths import safe_location
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


#: Network backends an operator may choose for the guest (PRD 3.2 P1 (1)).
#: ``user`` is QEMU's user-mode (SLIRP) stack -- the only one that works without
#: host administrator rights, and the only one that can be confined to loopback
#: with ``restrict=on``.  ``tap``/``bridge`` need a host bridge device that does
#: not exist on a stock Windows install, so they are deliberately absent: a
#: spelling that would put the guest on a real LAN must fail loudly.
NETWORK_MODES: tuple[str, ...] = ("none", "user")

#: How the firmware is handed to QEMU.  ``bios`` is the single-blob spelling
#: (``-bios <file>``).  ``pflash`` is the code+vars pair the UniXNU board needs:
#: UEFI keeps its variables in a *writable* second flash device, so a firmware
#: that persists settings across boots cannot be expressed with ``-bios`` alone.
#: qemu-virt.c:1907-1916 exits(1) when both forms appear, so the choice is
#: exclusive by construction -- never both, never neither.
FIRMWARE_ATTACHMENTS: tuple[str, ...] = ("bios", "pflash")

#: The form used when a machine configuration says nothing about firmware.
#: ``pflash``, because that is the pair the UniXNU board boots and the one
#: :class:`unipad.models.MachineConfig`'s own default already carries: two
#: different defaults for one decision is a second, silently different machine.
DEFAULT_FIRMWARE_ATTACHMENT = "pflash"

#: The TCP port range every endpoint on a QEMU command line must sit in.  One
#: range, one message, one place.  There were three copies of this check -- the
#: QMP socket here, a ``host_forwards`` rule here, and
#: :func:`unipad.host.httpsvc._validate_port` -- and they had already drifted
#: into two ranges and three messages, which is how ``host_port: 99999`` came
#: back from the fragment builder as an error that could not name the rule it
#: came from (standards-review R-1).
MIN_TCP_PORT = 1
MAX_TCP_PORT = 65535

#: The RTC base values QEMU accepts for ``-rtc base=``.  A closed vocabulary,
#: so a typo is a usage error here rather than a QEMU exit whose message names
#: the option and not the value the operator typed.
RTC_BASES: tuple[str, ...] = ("utc", "localtime")

#: Why a non-ASCII path cannot be used, and what to do about it.  A named
#: constant so the message is testable rather than paraphrased.
NON_ASCII_PATH_REASON = (
    "The Windows QEMU build resolves file paths with the ANSI code page. "
    "Put the run directory on an ASCII-only path; on this host %TEMP% is "
    "under a non-ASCII user directory."
)


def _devices_section(machine: "models.MachineConfig") -> dict[str, Any]:
    """Return the ``devices`` mapping, refusing a section that is not one.

    ``dict(machine.devices or {})`` answered ``{}`` for every falsy value, so
    ``"devices": false``, ``0``, ``""`` and ``[]`` each looked like "no devices
    configured" and fell through to the defaults -- while ``"devices": "net"``
    was refused.  The guard fired for the values an operator is least likely to
    type and was bypassed for the ones they are.
    """
    devices = machine.devices
    if devices is None:
        return {}
    if not isinstance(devices, Mapping):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine devices must be an object",
            devices_type=type(devices).__name__,
        )
    return dict(devices)


def _validate_tcp_port(value: Any, label: str, *, index: Optional[int] = None) -> int:
    """Return ``value`` when it is a fixed TCP port, else refuse it.

    One range and one message for every port this module puts on a QEMU command
    line (``-qmp`` and a ``hostfwd`` rule).  ``index`` is the rule position when
    the port came from a ``host_forwards`` list, so the refusal can name the
    rule the operator has to fix instead of the one the fragment builder
    happened to reach.
    """
    where = f"host_forwards rule {index}: " if index is not None else ""
    details: dict[str, Any] = {label: repr(value)}
    if index is not None:
        details["index"] = index
    if isinstance(value, bool) or not isinstance(value, int):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"{where}{label} must be an integer in {MIN_TCP_PORT}..{MAX_TCP_PORT}",
            **details,
        )
    if not (MIN_TCP_PORT <= value <= MAX_TCP_PORT):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"{where}{label} must be a TCP port in {MIN_TCP_PORT}..{MAX_TCP_PORT}",
            **details,
        )
    return value


def _rtc_base(machine: "models.MachineConfig") -> Optional[str]:
    """Return the RTC base to spell in the argv, or ``None`` for no decision.

    The value is pinned to :data:`RTC_BASES`.  ``(machine.clock or {}).get("rtc")``
    used to forward *any* truthy value verbatim, so a typo became a QEMU exit
    whose message names the option and not the value -- and a ``clock`` section
    that was not an object at all dropped ``-rtc`` from the argv silently,
    because the same ``or {}`` swallowed it.
    """
    clock = machine.clock
    if clock is None:
        return None
    if not isinstance(clock, Mapping):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine clock must be an object",
            clock_type=type(clock).__name__,
        )
    rtc = clock.get("rtc")
    if rtc is None:
        return None
    if not isinstance(rtc, str) or rtc not in RTC_BASES:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown rtc base",
            rtc=repr(rtc),
            known_rtc_bases=list(RTC_BASES),
        )
    return rtc


def _is_substitute_binary(binary: str) -> bool:
    """``True`` when ``binary`` is the interpreter running this process.

    A substitute binary is a script the caller runs in place of QEMU -- the test
    fake is ``[sys.executable, fake_qemu.py, ...]``.  It has no ANSI code-page
    file backend, so refusing a run on its behalf would block a path that works.
    The exemption is decided by *identity*, not by a name: a QEMU build
    installed or wrapped under any other name keeps the limitation (and the
    diagnosis), and a renamed fake keeps the exemption.
    """
    try:
        return Path(binary).resolve() == Path(sys.executable).resolve()
    except OSError:  # pragma: no cover - an unresolvable path is not the fake
        return False


def _argv_option(args: Sequence[str], flag: str) -> Optional[str]:
    """Return the value that follows ``flag`` in an argv list, or ``None``.

    This is the *only* way the launcher is allowed to learn what a run states.
    ``launch()`` takes either the argv :meth:`QemuLauncher.build_args` produced
    or one the caller assembled, so the ``MachineConfig`` the launcher was
    constructed with is not a description of the run -- the argv is.  Reading a
    machine fact back out of the argv is what keeps the recorded evidence and
    the started command line describing the same machine (PRD 9.2).
    """
    for index, value in enumerate(args):
        if value == flag and index + 1 < len(args):
            return args[index + 1]
    return None


def _argv_accel(args: Sequence[str]) -> str:
    """Return the ``-accel`` value an argv states, refusing anything but TCG.

    PRD 2.2-1 is a property of the **command line**, not of the configuration.
    The constructor already refuses a non-TCG ``MachineConfig``, but
    ``launch(args=...)`` accepts an argv the caller assembled and QEMU reads
    that argv, not the config.  An argv that names no accelerator at all leaves
    the choice to QEMU -- on a Windows host that can be WHPX -- which is exactly
    the run the guard exists to prevent; and an argv naming another one would
    start it while the launch record still said ``tcg``.

    Raises:
        UniPadError: ``accelerator_not_tcg`` when the argv does not state
            ``-accel tcg``.
    """
    stated = _argv_option(args, "-accel")
    if stated is None or stated.strip().lower() != "tcg":
        raise UniPadError(
            ErrorCode.ACCELERATOR_NOT_TCG,
            "the qemu command line must state -accel tcg explicitly",
            received_accel=stated,
            required_accel="tcg",
            reason=(
                "PRD 2.2-1: Intel VT-x/Hyper-V cannot execute ARM64 iPadOS "
                "instructions. The command line QEMU reads is the authority, "
                "so an accelerator it does not name is refused rather than "
                "left to QEMU's own default."
            ),
        )
    return stated


def _firmware_attachment(machine: "models.MachineConfig") -> str:
    """Return the validated firmware attachment spelling for ``machine``.

    A section that is *present* but names no usable attachment is refused: it is
    an unfinished configuration, and the two spellings are mutually exclusive,
    so guessing one of them changes which firmware form the guest boots.  A
    section that is absent falls back to the same default
    :class:`unipad.models.MachineConfig` carries, which is ``pflash`` -- the form
    the board we boot needs.  Answering ``bios`` here gave the module a second,
    different definition of "the machine" than the one in ``models.py:199-201``,
    whose comment says exactly that an empty default must not mean ``-bios``.
    """
    devices = _devices_section(machine)
    if "firmware" not in devices:
        return DEFAULT_FIRMWARE_ATTACHMENT
    firmware = devices["firmware"]
    if not isinstance(firmware, Mapping) or "attach" not in firmware:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine devices.firmware must be an object with an attach",
            firmware_type=type(firmware).__name__,
            known_attachments=list(FIRMWARE_ATTACHMENTS),
        )
    attach = firmware["attach"]
    if attach not in FIRMWARE_ATTACHMENTS:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown firmware attachment",
            attach=repr(attach),
            known_attachments=list(FIRMWARE_ATTACHMENTS),
        )
    return attach


def _host_forwards(net: Mapping[str, Any]) -> list[str]:
    """Return the ``hostfwd=`` fragments for the configured guest network.

    ``hostfwd`` maps a *host* port into the guest, so it is the right tool for
    reaching something that lives inside the guest.  It is deliberately **not**
    how the package service is reached: QEMU's user-mode stack relays a guest
    connection to ``10.0.2.2`` straight to the host's own loopback
    (:func:`unipad.host.httpsvc.guest_fetch_url`), which needs no ``-nic``
    change at all.  A forward is therefore an explicit operator request, and an
    absent one emits nothing rather than a default mapping nobody asked for.

    Raises:
        UniPadError: ``config_invalid`` when the rules are not a sequence of
            ``{"host_port": int, "guest_port": int}`` objects, when a rule omits
            either key, or when a port is not a fixed TCP port.  Every rule is
            validated before any fragment is produced, so a half-valid list is
            never split across a rejected and an accepted half.
    """
    raw = net.get("host_forwards", ())
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine devices.net.host_forwards must be a list of rules",
            received_type=type(raw).__name__,
        )

    # Two passes, and the order is deliberate.  A rule that is not an object
    # cannot be asked for its keys, so the shape of the whole list is settled
    # first: ``[{"host_port": 9443}, "oops"]`` reports the rule that is not a
    # rule at all rather than the one that is merely incomplete.  Only then is
    # each rule's content checked.
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "each host_forwards rule must be an object with host_port and guest_port",
                index=index,
                received_type=type(entry).__name__,
            )

    rules: list[tuple[int, int]] = []
    for index, entry in enumerate(raw):
        if "guest_port" not in entry or "host_port" not in entry:
            # A half-written rule used to be completed with the package
            # service's default port, so an operator who typed only host_port
            # got a mapping into the guest they never asked for.  Everything
            # else in this chain refuses an unfinished configuration.
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                f"host_forwards rule {index}: host_port and guest_port are both required",
                index=index,
                required_keys=["host_port", "guest_port"],
            )
        host_port = _validate_tcp_port(entry.get("host_port"), "host_port", index=index)
        guest_port = _validate_tcp_port(entry.get("guest_port"), "guest_port", index=index)
        rules.append((host_port, guest_port))

    return [httpsvc.hostfwd_spec(host_port, guest_port) for host_port, guest_port in rules]


def _network_argument(net: Mapping[str, Any]) -> str:
    """Return the ``-nic`` value for the configured guest network.

    Fail-closed in both directions.  A network that is off is spelled
    ``none`` rather than omitted, because omitting it leaves the guest's
    connectivity to whatever QEMU defaults to for the machine type -- a
    decision recorded nowhere.  A network that is on must say what it is: an
    unfinished configuration is a usage error, never a guess, and the only
    permitted backend defaults to loopback-only.

    Raises:
        UniPadError: ``config_invalid`` for an unknown mode, a missing mode,
            a non-boolean ``enabled``, or a non-boolean ``restrict``.
    """
    if not isinstance(net, Mapping):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine devices.net must be an object",
            net_type=type(net).__name__,
        )

    enabled = net.get("enabled", False)
    if not isinstance(enabled, bool):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine devices.net.enabled must be a boolean",
            enabled=repr(enabled),
        )
    if not enabled:
        return "none"

    mode = net.get("mode")
    if not mode or not isinstance(mode, str):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine devices.net.mode is required when the network is enabled",
            known_modes=list(NETWORK_MODES),
        )
    if mode not in NETWORK_MODES:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown guest network mode",
            mode=mode,
            known_modes=list(NETWORK_MODES),
        )
    if mode == "none":
        return "none"

    model = str(net.get("model") or "virtio-net")
    # ``restrict`` keeps the guest off the public internet through the host.
    # Both a JSON boolean and QEMU's own "on"/"off" spelling are accepted so a
    # configuration can be copied out of a QEMU command line unchanged; the
    # default is on, so a config that omits it is confined, not open.
    restrict = net.get("restrict", True)
    if isinstance(restrict, str):
        normalised = restrict.strip().lower()
        if normalised in ("on", "yes", "true"):
            restrict = True
        elif normalised in ("off", "no", "false"):
            restrict = False
        else:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "machine devices.net.restrict must be a boolean or on/off",
                restrict=repr(restrict),
            )
    if not isinstance(restrict, bool):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine devices.net.restrict must be a boolean or on/off",
            restrict=repr(restrict),
        )
    return (
        f"user,id=unipad-net,model={model},restrict={'on' if restrict else 'off'}"
        + ("".join(f",{fragment}" for fragment in _host_forwards(net)))
    )


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



#: Block-driver prefixes QEMU accepts in a ``-drive file=`` value.  ``fat:``
#: turns a *directory* into a read-only FAT volume, which is the only ESP form
#: that boots UniXNU; ``json:`` is an inline blockdev description; and
#: ``host_device:`` names a host device node whose existence only the host OS
#: can answer for.  All three mean the value is not a plain path.
BLOCK_DRIVER_PREFIXES: tuple[str, ...] = ("fat:", "json:", "host_device:")

#: What each :data:`BLOCK_DRIVER_PREFIXES` entry means and why the existence
#: check treats it the way it does.  The catalogue is only discoverable by
#: reading this module -- ``json:`` in particular appeared in no document and in
#: no configuration file -- so the definitions live beside the tuple and a test
#: asserts the two cannot drift: a prefix added without one fails.
BLOCK_DRIVER_PREFIX_MEANINGS: dict[str, str] = {
    "fat:": (
        "a directory QEMU presents as a read-only FAT volume; the only ESP form "
        "that boots UniXNU, so the directory is what exists on disk"
    ),
    "json:": (
        "an inline blockdev description (``file=json:{\"driver\": ...}``); the "
        "value is JSON text, not a path, so nothing on disk corresponds to it "
        "and QEMU judges it"
    ),
    "host_device:": (
        "a host device node such as /dev/rdiskN, whose existence only the host "
        "OS can answer for; never present on Windows"
    ),
}

#: Prefixes whose value is not a path at all.  ``host_device:`` names a device
#: node and ``json:`` names a JSON document; both are returned with an empty
#: path so the existence check leaves them for QEMU to judge.  Treating the
#: ``json:`` text as a path reported a valid inline blockdev as a missing image.
NON_PATH_PREFIXES: tuple[str, ...] = ("json:", "host_device:")


def _strip_block_driver(value: str) -> tuple[str, str]:
    """Split a leading block-driver prefix off a ``file=`` value.

    Returns ``(prefix, path)`` where ``prefix`` is ``""`` for a plain path and
    ``path`` is always the part to check on disk.  ``host_device:`` and
    ``json:`` are returned with their prefix intact and an empty path: the value
    names a host device node, which only the host OS can answer for, or a JSON
    blockdev description, which is not a path at all.
    """
    for prefix in BLOCK_DRIVER_PREFIXES:
        if value.startswith(prefix):
            remainder = value[len(prefix):]
            return prefix, "" if prefix in NON_PATH_PREFIXES else remainder
    return "", value


def _is_pflash_vars_drive(spec: str) -> bool:
    """``True`` for the writable pflash vars drive in a ``-drive`` spec.

    That file is created by the launcher itself, so it is the one drive target
    whose absence is not a missing image.
    """
    return "if=pflash" in spec and "readonly=off" in spec


def _file_size(path: Path) -> Optional[int]:
    """Return ``path``'s size in bytes, or ``None`` when it does not exist."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def _raw_drive_file(spec: str) -> Optional[str]:
    """Return the ``file=`` value verbatim, block-driver prefix included."""
    for part in spec.split(","):
        key, _, value = part.partition("=")
        if key.strip() == "file" and value.strip():
            return value.strip()
    return None


def _parse_drive_file(spec: str) -> Optional[str]:
    """Extract the ``file=`` component of a ``-drive`` option string.

    The block-driver prefix is stripped: ``file=fat:F:/esp`` describes the
    directory ``F:/esp``, not a path literally called ``fat:F:/esp``.  Keeping
    the prefix made the existence check in :meth:`QemuLauncher._missing_paths`
    report the one disk layout the boot path needs as missing.
    """
    for part in spec.split(","):
        key, _, value = part.partition("=")
        if key.strip() == "file" and value.strip():
            _, path = _strip_block_driver(value.strip())
            return path or None
    return None


@dataclass
class LaunchResult:
    """Structured outcome of :meth:`QemuLauncher.launch`.

    Every field is read back out of the command line the launcher started, not
    out of the ``MachineConfig`` it was constructed with: ``launch()`` accepts an
    argv the caller assembled, so the configuration is not a description of the
    run.  ``serial_path`` is ``""`` when the argv names no serial log -- absent,
    which is not the same as a path.
    """

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
        # Fail at construction, not at launch: an unbuildable firmware decision
        # is a configuration error, and discovering it after QEMU has started
        # means a process that dies immediately with a message that names
        # neither form.
        self.firmware_attachment = _firmware_attachment(machine)
        self.machine = machine
        self.qmp_host = qmp_host
        self.qmp_timeout = qmp_timeout
        self.poll_interval = max(float(poll_interval), 0.01)
        self.process_timeout = float(process_timeout)

        self._last_args: list[str] = []
        self._last_workdir: Optional[Path] = None
        self._last_qmp_port: Optional[int] = None
        self._last_serial_path: Optional[Path] = None
        self._last_pflash_vars: Optional[Path] = None

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
        kernel: Optional[Path] = None,
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
            kernel: An AArch64 image loaded directly at the CPU's reset vector.
                When given, QEMU starts the guest *instead of* any firmware, so
                neither ``-bios`` nor the pflash pair is emitted -- the two are
                not additive, and a run that carried both would be ambiguous
                about which one starts the machine.  ``None`` keeps the
                firmware path.
            extra_args: Caller-supplied extra arguments, appended verbatim.

        Returns:
            The argv list.  The same list is remembered so
            :meth:`launch` can be called with just the binary.

        Raises:
            UniPadError: ``accelerator_not_tcg`` if the machine accelerator is
                not ``tcg`` (also enforced in the constructor);
                ``config_invalid`` for an unusable ``qmp_port``, an unusable
                ``clock``/``rtc``, a ``devices`` section that is not an object,
                a ``devices.net`` value that is not an object, or a firmware
                section that names no usable attachment.
        """
        _validate_tcp_port(qmp_port, "qmp_port")

        machine = self.machine
        work = Path(workdir)
        serial_target = Path(serial_log) if serial_log else work / "serial.log"

        args: list[str] = []

        # Firmware first: the two spellings are mutually exclusive, so the whole
        # decision is made before anything else references the firmware.  A
        # kernel image displaces both -- QEMU starts the guest at the reset
        # vector rather than running a bootloader first.
        if kernel is not None:
            args.extend(["-kernel", _qemu_path(Path(kernel))])
        elif self.firmware_attachment == "pflash":
            firmware_path = Path(firmware)
            args.extend(
                [
                    "-drive",
                    f"if=pflash,format=raw,readonly=on,file={_qemu_path(firmware_path)}",
                ]
            )
            # The vars drive is created and truncated by launch() to exactly the
            # code drive's size (run-qemu.py:151-152).  Its path is remembered so
            # the rest of the class can find it without re-parsing the argv.
            self._last_pflash_vars = work / "vars.fd"
            args.extend(
                [
                    "-drive",
                    f"if=pflash,format=raw,readonly=off,file={_qemu_path(self._last_pflash_vars)}",
                ]
            )
        else:
            args.extend(["-bios", _qemu_path(firmware)])

        args.extend(
            [
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
                # A guest that triple-faults must end the run, not spin the host
                # forever (PRD 4.2).
                "-no-reboot",
            ]
        )

        rtc = _rtc_base(machine)
        if rtc is not None:
            args.extend(["-rtc", f"base={rtc}"])

        # A -kernel boot has no disks: the image is loaded at the reset vector
        # and there is nothing for a block device to back.  Attaching the same
        # file as a virtio disk as well makes QEMU reject the whole argv
        # (exit 1, "could not open ... Invalid argument"), so the drives are
        # suppressed rather than emitted alongside.
        if kernel is None:
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

        # Display, audio and rotation are one decision, not three: a rotated
        # 1024x1366 request is a different framebuffer from an unrotated one,
        # and the fw_cfg item has to carry the *rotated* geometry or every row
        # the guest draws lands in the wrong place.  So the whole block comes
        # from unipad.host.display, which owns that arithmetic, and it
        # *replaces* this block rather than adding to it (emitting ``-device
        # ramfb`` twice is an argv QEMU rejects outright).
        #
        # ``-display none`` stays here, below: it is a window decision the
        # launcher owns, and two modules able to hide the guest display is one
        # too many.
        display_mapping = dict(machine.display or {})
        display_decision = decide_display(
            machine,
            rotation=display_mapping.get("rotation", 0),
            audio=display_mapping.get("audio", "none"),
        )
        args.extend(display_decision.to_qemu_fragments())
        if headless:
            args.extend(["-display", "none"])

        # The guest network decision is explicit in the argv (PRD 3.2 P1 (1)):
        # "off" is spelled ``-nic none`` rather than left to QEMU's default, and
        # "on" must name its backend.  A configuration that cannot be turned
        # into exactly one decision is a usage error, not a default.
        #
        # ``net`` reaches the guard as-is: a trailing ``or {}`` here used to turn
        # every *falsy* non-object (``false``, ``0``, ``""``, ``[]``) into an
        # empty mapping, so the network was silently switched off by a typo
        # while ``"net": "user"`` was correctly refused.
        net = _devices_section(machine).get("net")
        args.extend(["-nic", _network_argument({} if net is None else net)])

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
            UniPadError: ``accelerator_not_tcg`` when the argv about to be
                started does not state ``-accel tcg`` (PRD 2.2-1: the command
                line is the authority, so a caller-supplied argv is judged too);
                ``qemu_launch_failed`` when the binary cannot be found
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
        # The accelerator is judged against the argv, and first: the command
        # line is the authority (PRD 2.2-1), and this is the module's one guard
        # that must never be relaxed, so a refusal here must not be masked by a
        # later, more incidental one.  It costs nothing on disk either.
        _argv_accel(resolved_args)
        # The work directory is resolved first because both pre-flight checks
        # and the pflash vars file live in it.  Everything that can refuse the
        # run runs *before* anything is written: the vars file is 64 MiB on this
        # board, and a launch refused for a missing image or a non-ASCII path
        # used to leave one behind with nothing to clean it up and nothing in
        # the error message saying so.
        work = Path(workdir) if workdir is not None else (self._last_workdir or Path.cwd())
        work.mkdir(parents=True, exist_ok=True)
        missing = self._missing_paths(resolved_args)
        if missing:
            raise UniPadError(
                ErrorCode.IMAGE_NOT_FOUND,
                "qemu launch aborted: required image paths do not exist",
                missing=missing,
            )

        binary = self._resolve_binary(binary_name)
        # Only a real QEMU has this limitation: the ANSI code page is how its
        # file backends resolve paths.  A substitute binary -- the test fake is
        # a Python script -- has no such constraint, so refusing a run on its
        # behalf would block a path that works.  It runs after the binary is
        # resolved so "the binary is missing" stays the more fundamental error,
        # and before anything is written so a refusal costs nothing on disk.
        if not _is_substitute_binary(binary):
            self._check_ascii_paths(resolved_args)
        self._materialise_pflash_vars(resolved_args, work)
        self._adopt_args(resolved_args)

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
            # ``_adopt_args`` derived this from the argv, so an argv that names
            # no serial log records none rather than the path an earlier
            # ``build_args()`` happened to remember.
            serial_path=str(self._last_serial_path) if self._last_serial_path else "",
            evidence=self._launch_evidence(resolved_args),
        )

    @staticmethod
    def _launch_evidence(args: Sequence[str]) -> dict[str, Any]:
        """Return the machine facts the argv about to be started states.

        Every value is transcribed out of the command line, never taken from
        :attr:`machine`: ``launch()`` accepts an argv the caller assembled, so
        the configuration is not a description of the run -- the argv is.  A
        record naming a machine the command line does not describe is worse
        than no record at all, because it looks like evidence for reproducing
        the run while reproducing something else (PRD 9.2).

        A key is ``None`` when the argv does not state it, which is the same
        convention :attr:`QmpReadiness.status` uses: absent is not the same as
        a value.  ``accel`` cannot be ``None`` -- :func:`_argv_accel` has
        already refused an argv that does not state explicit TCG.
        """
        return {
            "accel": _argv_option(args, "-accel"),
            "machine": _argv_option(args, "-machine"),
            "name": _argv_option(args, "-name"),
            "started_at_epoch": round(time.time(), 3),
        }

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

    @property
    def last_args(self) -> list[str]:
        """A copy of the argv the launcher actually started.

        This is evidence, not bookkeeping: PRD 9.2 requires the QEMU command
        line in the evidence set, and a run whose recorded argv could differ
        from the one the child received would make that requirement
        unverifiable.  A copy is returned so a caller cannot rewrite history
        after the fact.
        """
        return list(self._last_args)

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

        Each remembered slot is *replaced*, not merged: a caller-supplied argv
        that carries no ``-qmp`` or no ``-serial`` leaves the slot empty rather
        than keeping whatever an earlier ``build_args()`` put there.  A slot that
        outlived the argv it came from would make ``launch.json`` name endpoints
        the started command line never mentioned -- and would send
        :meth:`wait_for_qmp` probing a port nothing is listening on.
        """
        self._last_args = list(args)
        self._last_qmp_port = None
        self._last_serial_path = None
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
    def _check_ascii_paths(args: Sequence[str]) -> None:
        """Refuse a run whose paths this QEMU build cannot open.

        The Windows QEMU builds resolve file paths with the ANSI code page, so a
        path containing a non-ASCII character -- a user directory such as
        ``C:/Users/<name with CJK>/AppData/Local/Temp``, which is exactly where
        ``tempfile`` lands on this host -- makes QEMU exit 1 with
        ``open <path> failed``.  That message names the file but not the reason,
        so the failure looks like a missing artifact.

        Checking up front turns it into a diagnosis: the same run with an ASCII
        work directory succeeds.  Every path-bearing value is checked, not just
        the ones we generate, because a caller may pass its own.

        Raises:
            UniPadError: ``config_invalid`` naming the offending value.
        """
        #: Options whose following value is a path (or contains one).
        PATH_FLAGS = ("-bios", "-kernel", "-serial")
        for index, value in enumerate(args):
            candidate = None
            if value in PATH_FLAGS and index + 1 < len(args):
                candidate = args[index + 1]
            elif value == "-drive" and index + 1 < len(args):
                target = _parse_drive_file(args[index + 1])
                if target:
                    candidate = target
            elif value == "-qmp" and index + 1 < len(args):
                candidate = args[index + 1]
            if not candidate:
                continue
            try:
                candidate.encode("ascii")
            except UnicodeEncodeError:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "qemu cannot open a path containing non-ASCII characters",
                    option=value,
                    value=candidate,
                    reason=NON_ASCII_PATH_REASON,
                ) from None

    def _materialise_pflash_vars(self, args: Sequence[str], work: Path) -> None:
        """Create any writable pflash vars file the argv asks for.

        UEFI keeps its variables in the second flash device, so a firmware that
        persists settings needs a *writable* file where ``-bios`` alone would
        have none.  Upstream sizes it to the code drive exactly
        (``run-qemu.py:151-152``); a shorter file makes QEMU truncate writes and
        a longer one wastes 64 MiB per run.

        A file that is already the right size is left alone -- it holds the
        variables from a previous run, which is the whole point.  A file that is
        *short* is extended rather than reused: a 0-byte one is the residue of a
        launch that was refused after this file was created, and handing it to
        the next boot gives UEFI a 0-length writable pflash to keep its
        variables in.  Extending keeps the leading bytes, which are the
        variables, and only adds the zeros the device needs.

        A missing *code* drive is not repaired here: that is a real missing
        image and :meth:`_missing_paths` must report it.
        """
        for index, value in enumerate(args):
            if value != "-drive" or index + 1 >= len(args):
                continue
            spec = args[index + 1]
            if not _is_pflash_vars_drive(spec):
                continue
            target = _parse_drive_file(spec)
            if not target:
                continue
            path = Path(target)
            size = self._pflash_code_size(args)
            if size <= 0:
                raise UniPadError(
                    ErrorCode.IMAGE_NOT_FOUND,
                    "could not size the pflash vars drive: the read-only code "
                    "drive is not a file",
                    path=safe_location(path),
                )
            existing = _file_size(path)
            if existing is not None and existing >= size:
                continue
            try:
                # "r+b" extends an existing short file in place, so the
                # variables it already holds survive; "wb" creates a fresh one.
                with open(path, "r+b" if existing is not None else "wb") as handle:
                    handle.truncate(size)
            except OSError as exc:
                raise UniPadError(
                    ErrorCode.IMAGE_NOT_FOUND,
                    "could not create the pflash vars drive",
                    path=safe_location(path),
                    reason=type(exc).__name__,
                ) from exc

    @staticmethod
    def _pflash_code_size(args: Sequence[str]) -> int:
        """Return the byte size of the read-only pflash drive, or 0."""
        for index, value in enumerate(args):
            if value != "-drive" or index + 1 >= len(args):
                continue
            spec = args[index + 1]
            if "if=pflash" not in spec or "readonly=on" not in spec:
                continue
            target = _parse_drive_file(spec)
            if target and Path(target).is_file():
                return Path(target).stat().st_size
        return 0

    @staticmethod
    def _missing_paths(args: Sequence[str]) -> list[str]:
        """Return the ``-bios`` / ``-drive file=`` targets that do not exist.

        A ``host_device:`` or ``json:`` value is deliberately *not* judged:
        ``/dev/rdiskN`` exists on macOS and never on Windows, and an inline
        blockdev description is JSON rather than a path.  In both cases the
        launcher has no way to tell a valid value from a stale one, and
        reporting it missing would block a legitimate run -- leaving it to QEMU
        produces an error that names the value.  Everything else is checked,
        including the directory behind a ``fat:`` prefix and the writable pflash
        vars file the launcher itself creates.
        """
        missing: list[str] = []
        for index, value in enumerate(args):
            if value == "-bios" and index + 1 < len(args):
                target = args[index + 1]
                if not Path(target).exists():
                    missing.append(f"-bios {target}")
            elif value == "-kernel" and index + 1 < len(args):
                target = args[index + 1]
                if not Path(target).exists():
                    missing.append(f"-kernel {target}")
            elif value == "-drive" and index + 1 < len(args):
                spec = args[index + 1]
                if _is_pflash_vars_drive(spec):
                    # The launcher creates this file itself (launch() sizes it
                    # to the code drive), so it is not a missing image.  Judging
                    # it here would refuse a correctly configured run for a file
                    # we were supposed to write -- and would force the write to
                    # happen before the refusal.
                    continue
                prefix, path = _strip_block_driver(_raw_drive_file(spec) or "")
                if not path:
                    continue
                if not Path(path).exists():
                    missing.append(f"-drive file={spec}")
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
