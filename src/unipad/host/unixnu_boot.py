"""Boot the UniXNU QEMU-virt board on a Windows host and keep a reproducible log.

This module is the Windows counterpart of ``scripts/run-qemu.py`` in the device
repository.  Upstream's script assumes macOS: it finds QEMU and the EDK II
firmware through Homebrew, extracts the ESP with ``mcopy`` (mtools), attaches the
OS disk image through ``hdiutil``, and offers a ``cocoa`` window.  None of that
exists here, so the parts that *do* transfer are reimplemented against the
artifacts the macOS runner actually produced, and the parts that do not are
refused rather than approximated.

What is byte-for-byte the same as upstream, because the guest sees it:

* the machine: ``virt,virtualization=off,secure=off,gic-version=3`` with
  ``-cpu cortex-a710``, ``-smp 1``, ``-accel tcg``;
* the firmware pair: the read-only EDK II code flash plus a *writable* vars
  flash truncated to exactly the code flash's size;
* the ESP as a read-only ``fat:`` directory behind ``virtio-blk-pci`` -- the
  only disk form UniXNU boots;
* ``ramfb`` for the frame buffer, ``qemu-xhci`` + ``usb-kbd`` for input,
  ``-nic none``, ``-no-reboot``.

What is deliberately different:

* QMP is **TCP on loopback**.  Windows has no ``AF_UNIX`` socket, so
  upstream's ``-qmp unix:<path>`` cannot work, and the same is true of the
  ``mcopy`` extraction: the ESP is staged as a directory here instead of being
  read out of ``payload.img``.
* ``-display none`` always.  A guest window is a decision for the operator, not
  for an automated run whose evidence is the serial log and the screendumps.

Readiness is never inferred from a sleep.  :func:`boot` waits for a QMP greeting
and a ``query-status`` reply, and records the reply, exactly as
:meth:`unipad.host.qemu.QemuLauncher.wait_for_qmp` does -- a run that only shows
"QEMU was started" has proved nothing about the guest (PRD 7.4).
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from unipad.errors import ErrorCode, UniPadError
from unipad.host.adt import read_board_adt
from unipad.host.paths import safe_location

__all__ = [
    "BootInputs",
    "BootReport",
    "MEMORY_MIB",
    "PE_SUBSYSTEM_APPLICATION",
    "PE_SUBSYSTEM_NAMES",
    "SCREEN",
    "boot",
    "build_argv",
    "kernel_args_line",
    "pe_subsystem",
    "read_board_payload",
    "stage_esp",
    "unixnu_toml",
]

#: The board's ``[payload] dram_size`` is 6 GiB and ``run-qemu.py:26`` names the
#: same number ``MEMORY_MIB``.  The device tree and the machine must agree, so
#: this is the one place the size is stated for the machine; the tree gets it
#: from board.toml through :mod:`unipad.host.adt`.
MEMORY_MIB = 6144

#: The frame-buffer geometry.  1376x1024, not the iPad's 1366x1024: every row of
#: the picture has to be a multiple of 64 bytes or it lands 2-3 bytes off, and
#: 1376 is the nearest such width (``run-qemu.py:41-45``).  ``QemuRamfb.efi``
#: switches the frame buffer to this size before DarwinBoot starts, which is
#: also why this module does not ship that driver: the runner's payload build
#: puts it on the ESP, and a second copy here would be a second definition of
#: the geometry.
SCREEN = "1376x1024"

#: The QMP port range start.  The port is chosen per run by the caller; this is
#: only the default, and a run that finds it taken tries the next one.
DEFAULT_QMP_PORT = 14444

#: How long to wait for the guest to produce its first serial output before the
#: run is judged stalled.  Generous, because TCG emulating a Cortex-A710 takes
#: minutes to reach userland, not seconds.
DEFAULT_SECONDS = 300

#: Seconds between screendumps.
DEFAULT_EVERY = 20

#: What ``check_args`` in ``unixnu/payload.py:29-37`` accepts, and the 1000-byte
#: limit DarwinBoot's command-line buffer imposes.  Copied rather than imported:
#: the upstream module is not installable, and a local relaxation of either rule
#: would produce a command line the guest refuses.
ARGUMENT_PATTERN = re.compile(r"[A-Za-z0-9_.\-]+(=[A-Za-z0-9_.,\-]+)?")
ARGUMENT_LIMIT = 1000

#: PE/COFF machine types and EFI subsystems, as the firmware spells them.
PE_MACHINE_ARM64 = 0xAA64
PE_SUBSYSTEM_APPLICATION = 10
PE_SUBSYSTEM_NAMES = {
    10: "EFI application",
    11: "EFI boot service driver",
    12: "EFI runtime driver",
    13: "EFI ROM image",
}

#: Where ``UniXNU.inf:13`` declares ``MODULE_TYPE = UEFI_APPLICATION``.  The
#: firmware boots exactly this: ``EFI/BOOT/BOOTAA64.EFI`` is the image BDS
#: *starts*, and a driver it cannot start.
APPLICATION_SUBSYSTEM_REASON = (
    "EFI/BOOT/BOOTAA64.EFI must be a UEFI application (PE subsystem 10). UniXNU "
    "itself is one (package/UniXNUPkg/Application/UniXNU/UniXNU.inf:13, "
    "MODULE_TYPE = UEFI_APPLICATION); DarwinBoot.efi is not "
    "(DarwinBootPkg builds it as UEFI_DRIVER, PE subsystem 11), and the firmware "
    "refuses it with 'The image is not an application.' -- which BDS reports as "
    "'failed to load Boot0001 ... Not Found', an error that names the device and "
    "not the cause."
)


def pe_subsystem(path: Path) -> Optional[int]:
    """The EFI subsystem of a PE32+ image, or ``None`` when it is not one.

    Only the fields the loader reads are touched: the ``MZ`` stub, ``e_lfanew``,
    the ``PE\\0\\0`` signature, the machine type and the optional header's
    subsystem.  A file that is not a PE at all returns ``None`` rather than
    raising, because the caller's message is about what the firmware will do
    with the file and that is the same either way.
    """
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(0x400)
    except OSError:
        return None
    if len(head) < 0x40 or head[:2] != b"MZ":
        return None
    (offset,) = struct.unpack_from("<I", head, 0x3C)
    if offset + 24 > len(head) or head[offset : offset + 4] != b"PE\0\0":
        return None
    optional = offset + 24
    if optional + 70 > len(head):
        return None
    (magic,) = struct.unpack_from("<H", head, optional)
    if magic != 0x20B:  # PE32+ (64-bit); the AArch64 UEFI form, never PE32.
        return None
    (subsystem,) = struct.unpack_from("<H", head, optional + 68)
    return subsystem


def _refuse_non_application(unixnu: Path) -> None:
    """Refuse a ``BOOTAA64.EFI`` the firmware will not start.

    Measured on this host with the pinned EDK II firmware and QEMU 11.1.0: a
    PE32+ AArch64 image whose subsystem is 11 (boot service driver) is found,
    opened, and then rejected -- the UEFI shell prints ``The image is not an
    application.`` and BDS prints ``failed to load Boot0001 ... Not Found``.
    Nothing in either message says the subsystem was wrong, so the run looks
    like a missing file.  Checking it here turns that into a diagnosis.
    """
    subsystem = pe_subsystem(unixnu)
    if subsystem is None:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"{safe_location(unixnu)} is not a PE32+ AArch64 image, so the "
            "firmware cannot start it as EFI/BOOT/BOOTAA64.EFI",
            path=safe_location(unixnu),
        )
    if subsystem != PE_SUBSYSTEM_APPLICATION:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"{safe_location(unixnu)} is a {PE_SUBSYSTEM_NAMES.get(subsystem, subsystem)} "
            f"(PE subsystem {subsystem}), not a UEFI application (subsystem "
            f"{PE_SUBSYSTEM_APPLICATION})",
            path=safe_location(unixnu),
            subsystem=subsystem,
            required_subsystem=PE_SUBSYSTEM_APPLICATION,
            reason=APPLICATION_SUBSYSTEM_REASON,
        )


@dataclass(frozen=True)
class BootInputs:
    """Every file a boot needs, with the role each one plays.

    ``unixnu`` is the EFI application that becomes ``EFI/BOOT/BOOTAA64.EFI`` --
    UniXNU itself, which reads ``unixnu.toml`` and loads the kernel and its
    drivers.  It is *not* DarwinBoot: DarwinBoot is a driver UniXNU loads, and
    the board says so with ``[payload] loader = "driver"``.
    """

    firmware: Path
    kernel: Path
    devicetree: Path
    darwinboot: Path
    unixnu: Path
    #: An existing ``unixnu.toml`` to adopt instead of generating one.  The
    #: runner's payload build writes one with the board's exact arguments; a
    #: locally generated one is the fallback when only the pieces are at hand.
    payload_toml: Optional[Path] = None

    def missing(self) -> list[str]:
        """The paths that do not exist, named by role."""
        absent = []
        for role in ("firmware", "kernel", "devicetree", "darwinboot", "unixnu"):
            path = getattr(self, role)
            if not Path(path).is_file():
                absent.append(f"{role}={safe_location(Path(path))}")
        if self.payload_toml is not None and not Path(self.payload_toml).is_file():
            absent.append(f"payload_toml={safe_location(Path(self.payload_toml))}")
        return absent

    def to_dict(self) -> dict[str, str]:
        record = {role: safe_location(Path(getattr(self, role))) for role in
                  ("firmware", "kernel", "devicetree", "darwinboot", "unixnu")}
        if self.payload_toml is not None:
            record["payload_toml"] = safe_location(Path(self.payload_toml))
        return record


@dataclass
class BootReport:
    """What a boot run produced, and what it proved.

    ``reached_qmp`` is the only readiness claim this module makes, and it means
    exactly what it says: QEMU answered a greeting and a ``query-status``.  It is
    *not* a claim that the firmware started, that UniXNU ran, or that the kernel
    booted -- those are read out of ``serial.log`` by whoever analyses the run,
    and are summarised in :attr:`serial_markers` without being asserted here.
    """

    ok: bool
    workdir: str = ""
    command: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    reached_qmp: bool = False
    qmp_evidence: dict[str, Any] = field(default_factory=dict)
    serial_bytes: int = 0
    serial_markers: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "workdir": self.workdir,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "reached_qmp": self.reached_qmp,
            "qmp_evidence": dict(self.qmp_evidence),
            "serial_bytes": self.serial_bytes,
            "serial_markers": list(self.serial_markers),
            "screenshots": list(self.screenshots),
            "inputs": dict(self.inputs),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


# ---------------------------------------------------------------------------
# the board's declaration
# ---------------------------------------------------------------------------
def read_board_payload(device_dir: Path) -> dict[str, Any]:
    """The ``[payload]`` block of the device's board.toml.

    A board with no ``[payload]`` block is refused: this module exists to boot
    that block's declaration, and inventing one would boot a machine the board
    never asked for.
    """
    import tomllib

    path = Path(device_dir) / "board.toml"
    try:
        board = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"board.toml not found in {safe_location(path.parent)}",
            path=safe_location(path),
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"board.toml is not valid TOML: {safe_location(path)}",
            path=safe_location(path),
            error=str(exc),
        ) from exc
    spec = board.get("payload")
    if not isinstance(spec, dict) or not spec:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"board.toml declares no [payload] block: {safe_location(path)}",
            path=safe_location(path),
        )
    return spec


def kernel_args_line(spec: Mapping[str, Any], extra: Sequence[str] = ()) -> str:
    """The board's kernel command line, with any extra ``NAME=VALUE`` applied.

    The same three rules ``unixnu/payload.py:check_args`` applies, because the
    consumer is the same: an argument matching neither ``NAME`` nor ``NAME=VALUE``
    is refused, a longer-than-1000-byte line is refused (DarwinBoot copies it
    into a 1024-byte buffer), and an extra argument *replaces* the board's
    argument of the same name rather than appending a second one.
    """
    arguments = list(spec.get("kernel_args", []))
    names = {item.split("=", 1)[0] for item in extra}
    merged = [item for item in arguments if item.split("=", 1)[0] not in names] + list(extra)
    for value in merged:
        if not ARGUMENT_PATTERN.fullmatch(value):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unsupported kernel argument",
                argument=value,
            )
    line = " ".join(merged)
    if len(line) > ARGUMENT_LIMIT:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"the kernel command line is {len(line)} bytes; the limit is {ARGUMENT_LIMIT}",
            bytes=len(line),
        )
    return line


def unixnu_toml(spec: Mapping[str, Any], line: str) -> str:
    """The ``EFI/UniXNU/unixnu.toml`` the ESP carries.

    A transcription of ``unixnu/payload.py:166-173``.  The ``[[driver]]`` entry
    is what makes UniXNU load DarwinBoot rather than expect the firmware to have
    run it, and ``run-qemu.py:96-98`` refuses a payload that does not carry it --
    so its absence is a build error here too, not a default.
    """
    loader = spec.get("loader", "firmware")
    if loader != "driver":
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            'board.toml [payload] loader must be "driver" for the QEMU board: '
            "UniXNU loads DarwinBoot itself, the firmware does not",
            loader=repr(loader),
        )
    return (
        "version = 1\n"
        "[boot]\n"
        f"timeout = {int(spec.get('timeout', 3))}\n"
        'default = "xnu"\n'
        "[kits]\n"
        "autoload = false\n"
        '[[driver]]\n'
        'path = "Payloads/DarwinBoot.efi"\n'
        "\n"
        "[[entry]]\n"
        'id = "xnu"\n'
        f'title = "{spec.get("title", "UniXNU")}"\n'
        'type = "xnu"\n'
        'path = "Payloads/kernel.macho"\n'
        f'args = "{line}"\n'
    )


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------
def stage_esp(inputs: BootInputs, esp: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Write the ESP directory QEMU presents as a read-only FAT volume.

    The layout is the one ``unixnu/payload.py`` stages and ``run-qemu.py`` copies
    out of ``payload.img``:

        EFI/BOOT/BOOTAA64.EFI                     UniXNU itself
        EFI/UniXNU/unixnu.toml                    the boot configuration
        EFI/UniXNU/Payloads/kernel.macho
        EFI/UniXNU/Payloads/kernel.macho.adt      the QEMU device tree
        EFI/UniXNU/Payloads/DarwinBoot.efi

    ``QemuRamfb.efi`` is *not* staged: it is a driver the payload build puts on
    the ESP ahead of DarwinBoot to set the frame-buffer geometry
    (``run-qemu.py:105-116``), and a second copy here would be a second
    definition of :data:`SCREEN`.  A boot that needs it must carry it.

    Returns:
        A record of what was written, with each file's size, for the evidence
        bundle.
    """
    esp = Path(esp)
    if esp.exists():
        shutil.rmtree(esp)
    (esp / "EFI/BOOT").mkdir(parents=True)
    (esp / "EFI/UniXNU/Payloads").mkdir(parents=True)

    written: dict[str, Any] = {}
    copies = (
        ("EFI/BOOT/BOOTAA64.EFI", inputs.unixnu),
        ("EFI/UniXNU/Payloads/kernel.macho", inputs.kernel),
        ("EFI/UniXNU/Payloads/kernel.macho.adt", inputs.devicetree),
        ("EFI/UniXNU/Payloads/DarwinBoot.efi", inputs.darwinboot),
    )
    for relative, source in copies:
        target = esp / relative
        shutil.copyfile(source, target)
        written[relative] = target.stat().st_size

    if inputs.payload_toml is not None:
        config = Path(inputs.payload_toml).read_text(encoding="utf-8")
    else:
        config = unixnu_toml(spec, kernel_args_line(spec))
    if "Payloads/DarwinBoot.efi" not in config:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unixnu.toml does not load DarwinBoot as a driver; board.toml "
            '[payload] loader must be "driver"',
        )
    (esp / "EFI/UniXNU/unixnu.toml").write_text(config, encoding="utf-8")
    written["EFI/UniXNU/unixnu.toml"] = len(config.encode("utf-8"))
    written["_esp"] = str(safe_location(esp))
    return written


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def build_argv(
    qemu: Path,
    inputs: BootInputs,
    esp: Path,
    workdir: Path,
    *,
    qmp_port: int = DEFAULT_QMP_PORT,
    memory_mib: int = MEMORY_MIB,
    display: str = "none",
    ramfb_size: Optional[str] = None,
) -> list[str]:
    """The QEMU argv (without the executable).

    The device list is upstream's, in upstream's order, with three changes and
    the reason for each:

    * ``-accel tcg`` is explicit rather than left to QEMU.  On a Windows host an
      accelerator QEMU picks itself can be WHPX, which cannot execute AArch64.
    * ``-qmp`` is TCP.  Windows has no ``AF_UNIX`` socket, so upstream's
      ``unix:<path>`` spelling is unavailable and the port is validated here
      instead of by QEMU's exit.
    * ``-display`` is whatever the caller asks for and defaults to ``none``; a
      window is an operator decision, and an automated run's evidence is the
      serial log.

    ``ramfb_size`` is emitted as ``-fw_cfg name=opt/org.unixnu/ramfb-size`` and is
    only meaningful with ``QemuRamfb.efi`` on the ESP: that driver reads the item
    and switches ramfb to it before DarwinBoot starts (``run-qemu.py:105-116``).
    :func:`boot` passes it only when the ESP actually carries the driver, so a
    locally staged ESP without one does not silently claim a geometry nothing set.
    """
    if not 1 <= int(qmp_port) <= 65535:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "qmp_port must be a TCP port in 1..65535",
            qmp_port=repr(qmp_port),
        )
    firmware = Path(inputs.firmware).resolve()
    code_size = firmware.stat().st_size if firmware.is_file() else 0
    if code_size <= 0:
        raise UniPadError(
            ErrorCode.IMAGE_NOT_FOUND,
            "the EDK II code flash is missing or empty; the vars flash is sized "
            "from it and the machine cannot start without both",
            path=safe_location(firmware),
        )

    def qpath(path: Path | str) -> str:
        # Absolute, and forward slashes.  QEMU is started with this run directory
        # as its working directory, so a *relative* path resolves against the run
        # directory rather than the caller's -- which is how a firmware that
        # exists becomes "Could not open" with a path that looks right.  A
        # backslash is an escape in a QEMU option value, so it is never used.
        return Path(path).resolve().as_posix()

    return [
        "-drive", f"if=pflash,format=raw,readonly=on,file={qpath(firmware)}",
        # The writable vars flash, created by boot() to exactly the code size.
        "-drive", f"if=pflash,format=raw,readonly=off,file={qpath(Path(workdir) / 'vars.fd')}",
        "-accel", "tcg",
        "-cpu", "cortex-a710",
        "-machine", "virt,virtualization=off,secure=off,gic-version=3",
        "-smp", "1",
        "-m", str(int(memory_mib)),
        "-no-reboot",
        # The ESP as a read-only FAT volume behind virtio-blk: the only disk
        # form UniXNU boots.
        "-drive", f"if=none,id=esp,format=raw,readonly=on,file=fat:{qpath(Path(esp))}",
        "-device", "virtio-blk-pci,drive=esp",
        "-device", "ramfb",
        "-device", "qemu-xhci",
        "-device", "usb-kbd",
        "-nic", "none",
        "-display", display,
        "-serial", f"file:{qpath(Path(workdir) / 'serial.log')}",
        "-qmp", f"tcp:127.0.0.1:{int(qmp_port)},server=on,wait=off",
        "-monitor", "none",
        "-name", "unipad-unixnu",
    ] + (
        ["-fw_cfg", f"name=opt/org.unixnu/ramfb-size,string={ramfb_size}"] if ramfb_size else []
    )


# ---------------------------------------------------------------------------
# QMP
# ---------------------------------------------------------------------------
def _qmp(port: int, execute: str, **arguments: Any) -> dict[str, Any]:
    """One QMP round trip over TCP, or an error record.

    A fresh connection per command, because the screendump loop is the only
    other user and a shared connection would serialise the two.  ``arguments``
    is empty for a bare command such as ``quit``.
    """
    # ``reply`` starts as the failure record so the function always has one to
    # return; every successful round trip overwrites it.
    reply: dict[str, Any] = {"error": "qmp closed without a reply"}
    with socket.create_connection(("127.0.0.1", int(port)), timeout=15) as sock:
        sock.settimeout(30)
        stream = sock.makefile("rwb", buffering=0)
        stream.readline()  # the greeting
        for request in ({"execute": "qmp_capabilities"}, {"execute": execute, "arguments": arguments}):
            stream.write((json.dumps(request) + "\n").encode())
            while True:
                candidate = json.loads(stream.readline())
                if "return" in candidate or "error" in candidate:
                    # A ``break``, not a ``return``: returning here would answer
                    # with the *capabilities* reply and never send the command
                    # the caller asked for, which silently turns every
                    # ``screendump`` and ``quit`` into a no-op.
                    reply = candidate
                    break
    return reply


def _write_png(ppm: Path, target: Path) -> None:
    """Convert a QEMU PPM screendump to PNG, with no third-party library.

    QEMU's ``screendump`` writes a binary PPM (``P6``).  The conversion is the
    four-chunk PNG ``run-qemu.py:png`` performs: each row gains a leading zero
    filter byte, and the whole thing is deflated.  A dependency-free conversion
    is the point -- the controller must run on a bare Windows host.
    """
    data = ppm.read_bytes()
    fields: list[bytes] = []
    at = 0
    while len(fields) < 4:
        while data[at : at + 1].isspace():
            at += 1
        end = at
        while not data[end : end + 1].isspace():
            end += 1
        fields.append(data[at:end])
        at = end
    width, height = int(fields[1]), int(fields[2])
    pixels = data[at + 1 :]
    rows = b"".join(b"\0" + pixels[y * width * 3 : (y + 1) * width * 3] for y in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    target.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


#: Lines in the serial log that say something about how far the boot got.  They
#: are *reported*, never asserted: this module does not decide whether a boot
#: succeeded, it records what the guest printed so the analysis can.
SERIAL_MARKERS = (
    "UNIXNU:",
    "DARWINBOOT:",
    "XNU:",
    "AppleUSBHostPort",
    "IOPlatformExpert",
    "SpringBoard",
    "backboardd",
    "launchd",
    "panic",
    "Panic",
)


def _serial_markers(path: Path, limit: int = 40) -> list[str]:
    """The last few marker lines of the serial log, for the report."""
    if not path.is_file():
        return []
    try:
        text = path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [line for line in text.splitlines() if any(marker in line for marker in SERIAL_MARKERS)]
    return lines[-limit:]


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def boot(
    inputs: BootInputs,
    workdir: Path,
    *,
    qemu: Path,
    device_dir: Path,
    seconds: int = DEFAULT_SECONDS,
    every: int = DEFAULT_EVERY,
    qmp_port: int = DEFAULT_QMP_PORT,
    display: str = "none",
) -> BootReport:
    """Stage the ESP, start QEMU, watch it, and keep every log.

    The run is not "successful" because QEMU started.  :attr:`BootReport.ok`
    means the run completed and produced a serial log; :attr:`BootReport.reached_qmp`
    means QEMU answered control commands; and what the *guest* did is in
    ``serial.log`` and the screendumps, summarised but not judged.

    Raises:
        UniPadError: ``image_not_found`` for a missing input (before anything is
            written), ``config_invalid`` for an unusable port or a machine the
            board does not declare, ``qemu_launch_failed`` when QEMU exits
            before QMP answers.
    """
    qemu = Path(qemu)
    if not qemu.is_file():
        raise UniPadError(
            ErrorCode.QEMU_LAUNCH_FAILED,
            "qemu-system-aarch64 not found",
            path=safe_location(qemu),
        )
    absent = inputs.missing()
    if absent:
        raise UniPadError(
            ErrorCode.IMAGE_NOT_FOUND,
            "boot aborted: required inputs do not exist",
            missing=absent,
        )
    # Before anything is written: the firmware starts BOOTAA64.EFI, and a
    # driver it refuses produces an error that names the device and not the
    # cause (measured 2026-10-24, see :func:`pe_subsystem`).
    _refuse_non_application(Path(inputs.unixnu).resolve())
    # Resolved once, here, so every later use -- the ESP staging, the argv, the
    # record -- names the same file.  A relative input path is the caller's
    # spelling, not QEMU's: the child runs with the run directory as its cwd.
    inputs = BootInputs(
        firmware=Path(inputs.firmware).resolve(),
        kernel=Path(inputs.kernel).resolve(),
        devicetree=Path(inputs.devicetree).resolve(),
        darwinboot=Path(inputs.darwinboot).resolve(),
        unixnu=Path(inputs.unixnu).resolve(),
        payload_toml=None if inputs.payload_toml is None else Path(inputs.payload_toml).resolve(),
    )

    spec = read_board_payload(device_dir)
    board_adt = read_board_adt(device_dir)
    memory_mib = board_adt.dram_size >> 20
    if memory_mib != MEMORY_MIB:
        # The machine and the device tree must describe the same memory.  A
        # board that says otherwise is a different board, not a rounding error.
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "board.toml [payload] dram_size does not match the memory this "
            "module gives the machine",
            board_mib=memory_mib,
            machine_mib=MEMORY_MIB,
        )

    # Absolute, because QEMU runs with this directory as its cwd and every path
    # the child is handed has to survive that.  Measured on run 1: a relative
    # workdir made `screendump` fail with "Could not create
    # 'local-private\\boot7\\run1\\screen.ppm': No such file or directory" --
    # QEMU resolved the *relative* filename against the run directory, so it
    # looked for run1/run1/screen.ppm.  The same class of bug as the relative
    # firmware path in the argv.
    work = Path(workdir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    esp = work / "esp"
    staged = stage_esp(inputs, esp, spec)

    # The writable vars flash, sized to the code flash exactly.  A file that is
    # already the right size is left alone: it holds the variables from a
    # previous boot, which is the point of having one.
    vars_path = work / "vars.fd"
    code_size = Path(inputs.firmware).stat().st_size
    if not vars_path.is_file() or vars_path.stat().st_size < code_size:
        with vars_path.open("wb") as handle:
            handle.truncate(code_size)

    command = build_argv(
        qemu, inputs, esp, work, qmp_port=qmp_port, memory_mib=memory_mib, display=display,
        # Only with the driver that reads it: a geometry nothing set is a claim
        # the run cannot support (run-qemu.py:105-116).
        ramfb_size=SCREEN if (esp / "EFI/UniXNU/Payloads/QemuRamfb.efi").is_file() else None,
    )
    (work / "command.txt").write_text(" ".join([str(qemu), *command]) + "\n", encoding="utf-8")

    report = BootReport(ok=False, workdir=str(safe_location(work)), command=[str(qemu), *command], inputs=inputs.to_dict())

    stdout_path = work / "qemu-stdout.log"
    stderr_path = work / "qemu-stderr.log"
    try:
        out_handle = stdout_path.open("wb")
        err_handle = stderr_path.open("wb")
    except OSError as exc:
        raise UniPadError(
            ErrorCode.QEMU_LAUNCH_FAILED,
            "could not open the qemu log files",
            workdir=str(safe_location(work)),
            reason=type(exc).__name__,
        ) from exc

    with out_handle, err_handle:
        try:
            process = subprocess.Popen(
                [str(qemu), *command],
                cwd=str(work),
                stdin=subprocess.DEVNULL,
                stdout=out_handle,
                stderr=err_handle,
                close_fds=True,
            )
        except OSError as exc:
            raise UniPadError(
                ErrorCode.QEMU_LAUNCH_FAILED,
                "qemu could not be started",
                path=str(qemu),
                reason=type(exc).__name__,
            ) from exc

        report = _watch(process, report, work, qmp_port=qmp_port, seconds=seconds, every=every)

    report.serial_bytes = (work / "serial.log").stat().st_size if (work / "serial.log").is_file() else 0
    report.serial_markers = _serial_markers(work / "serial.log")
    (work / "boot-report.json").write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    (work / "esp-manifest.json").write_text(json.dumps(staged, indent=2) + "\n", encoding="utf-8")
    return report


def _watch(
    process: subprocess.Popen,
    report: BootReport,
    work: Path,
    *,
    qmp_port: int,
    seconds: int,
    every: int,
) -> BootReport:
    """Wait for QMP, then take screendumps until the budget or the process ends."""
    started = time.monotonic()
    deadline = started + float(seconds)
    attempts = 0
    last_reason = "qmp was never probed"

    # Readiness: a greeting *and* a query-status reply.  Never "the process is
    # alive", which is true of a QEMU that failed to open its firmware and is
    # spinning on its way out.
    while True:
        attempts += 1
        if process.poll() is not None:
            report.error_code = ErrorCode.QEMU_LAUNCH_FAILED
            report.error_message = f"qemu exited before qmp became ready (exit code {process.returncode})"
            report.exit_code = process.returncode
            report.qmp_evidence = {"qemu_exit_code": process.returncode, "attempts": attempts,
                                   "stderr_tail": _tail(work / "qemu-stderr.log")}
            return report
        try:
            status = _qmp(qmp_port, "query-status")
        except (OSError, json.JSONDecodeError) as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= started + 60:
                report.error_code = ErrorCode.QMP_UNAVAILABLE
                report.error_message = f"qmp on 127.0.0.1:{qmp_port} never became ready: {last_reason}"
                report.exit_code = process.poll()
                report.qmp_evidence = {"last_error": last_reason, "attempts": attempts,
                                       "stderr_tail": _tail(work / "qemu-stderr.log")}
                return report
            time.sleep(0.25)
            continue
        report.reached_qmp = True
        report.qmp_evidence = {
            "port": qmp_port,
            "attempts": attempts,
            "status": status,
            "waited_seconds": round(time.monotonic() - started, 3),
        }
        break

    shots: list[str] = []
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(max(int(every), 1))
        if process.poll() is not None:
            break
        ppm = work / "screen.ppm"
        try:
            # The same spelling the argv uses: absolute and forward-slashed.  A
            # backslash is an escape in a QEMU value and a relative path resolves
            # against the run directory (see :func:`build_argv`'s ``qpath``).
            reply = _qmp(qmp_port, "screendump", filename=ppm.resolve().as_posix())
        except (OSError, json.JSONDecodeError) as exc:
            report.qmp_evidence["screendump_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if "error" in reply or not ppm.is_file():
            report.qmp_evidence["screendump_error"] = reply.get("error", "no screendump file")
            continue
        shot = work / f"screen-{int(time.monotonic() - started):03d}s.png"
        try:
            _write_png(ppm, shot)
        except (OSError, ValueError, IndexError) as exc:
            report.qmp_evidence["screendump_error"] = f"{type(exc).__name__}: {exc}"
            continue
        shots.append(str(safe_location(shot)))
        ppm.unlink(missing_ok=True)

    report.screenshots = shots
    if process.poll() is None:
        try:
            _qmp(qmp_port, "quit")
        except (OSError, json.JSONDecodeError):
            process.terminate()
    try:
        report.exit_code = process.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - a wedged guest
        process.kill()
        report.exit_code = process.wait(timeout=30)
    report.ok = report.reached_qmp
    report.qmp_evidence["screenshots"] = len(shots)
    return report


def _tail(path: Path, limit: int = 2000) -> str:
    """A bounded tail of a log file, for failure evidence."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return text if len(text) <= limit else "...(truncated)...\n" + text[-limit:]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command line: ``python -m unipad.host.unixnu_boot --device DIR --qemu QEMU ...``."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=Path, required=True, help="device repository root (holds board.toml)")
    parser.add_argument("--qemu", type=Path, required=True, help="qemu-system-aarch64 executable")
    parser.add_argument("--firmware", type=Path, required=True, help="edk2-aarch64-code.fd")
    parser.add_argument("--kernel", type=Path, required=True, help="kernel.macho")
    parser.add_argument("--devicetree", type=Path, required=True, help="kernel.macho.adt")
    parser.add_argument("--darwinboot", type=Path, required=True, help="DarwinBoot.efi")
    parser.add_argument("--unixnu", type=Path, required=True, help="UniXNU.efi, staged as EFI/BOOT/BOOTAA64.EFI")
    parser.add_argument("--payload-toml", type=Path, default=None,
                        help="an existing unixnu.toml to adopt instead of generating one")
    parser.add_argument("--workdir", type=Path, required=True, help="run directory (recreated)")
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS)
    parser.add_argument("--every", type=int, default=DEFAULT_EVERY)
    parser.add_argument("--qmp-port", type=int, default=DEFAULT_QMP_PORT)
    parser.add_argument("--display", default="none")
    parser.add_argument("--kernel-arg", action="append", default=[], metavar="NAME=VALUE",
                        help="replace the board's argument of the same name; repeatable")
    args = parser.parse_args(argv)

    inputs = BootInputs(
        firmware=args.firmware,
        kernel=args.kernel,
        devicetree=args.devicetree,
        darwinboot=args.darwinboot,
        unixnu=args.unixnu,
        payload_toml=args.payload_toml,
    )
    if args.kernel_arg:
        # Validated here so a bad extra argument fails before the ESP is staged,
        # not inside a run that has already started QEMU.
        kernel_args_line(read_board_payload(args.device), args.kernel_arg)

    report = boot(
        inputs,
        args.workdir,
        qemu=args.qemu,
        device_dir=args.device,
        seconds=args.seconds,
        every=args.every,
        qmp_port=args.qmp_port,
        display=args.display,
    )
    print(f"qemu exit {report.exit_code}; qmp ready: {report.reached_qmp}; serial {report.serial_bytes} bytes")
    for line in report.serial_markers[-8:]:
        print(f"  {line}")
    if report.screenshots:
        print(f"{len(report.screenshots)} screenshots; last: {report.screenshots[-1]}")
    if report.error_message:
        print(f"error: {report.error_message}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
