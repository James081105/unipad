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

import hashlib
import json
import re
import shutil
import socket
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from unipad.errors import ErrorCode, UniPadError
from unipad.host import adt, packed_ramdisk
from unipad.host.adt import read_board_adt
from unipad.host.paths import safe_location, scrub_account

__all__ = [
    "BootInputs",
    "BootReport",
    "DATA_MOUNT_MARKER",
    "MAX_BOOT_ATTEMPTS",
    "MEMORY_MIB",
    "PE_SUBSYSTEM_APPLICATION",
    "PE_SUBSYSTEM_NAMES",
    "SCREEN",
    "boot",
    "boot_until_mounted",
    "build_argv",
    "guest_stopped_progressing",
    "kernel_args_line",
    "pe_subsystem",
    "read_board_payload",
    "stage_esp",
    "tree_ramdisk_placeholder",
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
#: why :attr:`BootInputs.ramfb_driver` exists: without the driver nothing
#: applies this value, ``build_argv`` declines to emit it, and QEMU's ``ramfb``
#: falls back to 800x600 -- measured, in a run whose Data volume had mounted,
#: as 13 byte-identical screendumps of a 800x600 framebuffer.  The geometry is
#: still defined in exactly one place: the driver receives it, it does not
#: define it.
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


def _ends_in_udif_trailer(path: Path) -> bool:
    """Whether a file ends in a UDIF ``koly`` trailer.

    Sniffed rather than inferred from the suffix, because ``.dmg`` does not mean
    UDIF in an IPSW: ``044-76034-038.dmg`` is 3,303,014,400 bytes of raw APFS
    with no trailer at all, while ``044-76040-038.dmg`` is a container.  Reading
    the last 512 bytes is one small read and it is the only thing that separates
    them -- a suffix heuristic would refuse the image that needs no decoding.
    """
    try:
        size = path.stat().st_size
        if size <= 512:
            return False
        with path.open("rb") as handle:
            handle.seek(size - 512)
            return handle.read(4) == b"koly"
    except OSError:
        # An unreadable file is not a UDIF container, and saying so would be a
        # lie: QEMU will refuse it with its own error, which is more informative
        # than a guess made here.
        return False


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
    #: The trust cache, staged as ``kernel.macho.trustcache`` beside the kernel.
    #: Optional because a boot can be run without one -- but not because it is
    #: unimportant: ``vendor/UniXNU-qemu/scripts/build-os-root.py:26-27`` records
    #: that without it AMFI refuses to run launchd (namespace 9, EXEC, code 8,
    #: SECURITY_POLICY), and ``unixnu/payload.py:101`` guards the whole trust
    #: cache block behind ``if trust:`` with no else, so a payload built without
    #: one succeeds and produces a guest that cannot start a userland.  A boot
    #: input set that carries it can say so; one that does not must say that too.
    trust_cache: Optional[Path] = None
    #: The packed Data ramdisk, staged beside the kernel and named by
    #: ``unixnu.toml``'s ``initrd`` key.
    #:
    #: This and the device tree are two halves of one hand-off, and neither
    #: works alone.  DarwinBoot refuses a boot that supplies one without the
    #: other (``DarwinBoot.c:563-569``): with ``Request->Ramdisk != NULL`` it
    #: requires ``/chosen/memory-map`` to carry a 16-byte **zeroed**
    #: ``PackedRAMDisk`` (or ``RAMDisk``) property, and with ``Request->Ramdisk
    #: == NULL`` it requires the tree *not* to carry one.  Measured
    #: 2026-10-26: a tree generated without ``--packed-data-ramdisk`` plus an
    #: ``initrd`` line is refused with ``EFI_COMPROMISED_DATA`` before the
    #: kernel starts, and a tree carrying the placeholder with no ``initrd``
    #: is refused with ``EFI_UNSUPPORTED``.  :func:`stage_esp` checks the pair
    #: instead of trusting the caller to have got both halves right.
    data_ramdisk: Optional[Path] = None
    #: ``QemuRamfb.efi``, staged as ``EFI/UniXNU/Payloads/QemuRamfb.efi``.
    #:
    #: This field exists because of a check that could never pass.
    #: :func:`build_argv` emits ``-fw_cfg name=opt/org.unixnu/ramfb-size`` only
    #: when that path exists on the ESP, and :func:`boot` therefore decided
    #: :data:`SCREEN` the same way -- but nothing in this module could *put*
    #: the file there, so on the official path the test was always false and
    #: ``ramfb_size`` was dead code.  The consequence was measured, not
    #: theorised: 13 screendumps from t=61s to t=783s of a run whose Data volume
    #: had mounted were byte-identical (sha256 ``549bfa61991c``), and the
    #: framebuffer they showed was 800x600 rather than 1376x1024 -- QEMU's
    #: ``ramfb`` default, because nothing had set the geometry.
    #:
    #: :func:`stage_esp` used to refuse to stage it on purpose, on the grounds
    #: that a second copy would be a second definition of :data:`SCREEN`.  That
    #: reasoning is what produced the dead check: the geometry is still defined
    #: exactly once, here, and the driver only *receives* it.  What was missing
    #: was a way for a caller to say which driver, which is what this is.
    ramfb_driver: Optional[Path] = None

    def missing(self) -> list[str]:
        """The paths that do not exist, named by role."""
        absent = []
        for role in ("firmware", "kernel", "devicetree", "darwinboot", "unixnu"):
            path = getattr(self, role)
            if not Path(path).is_file():
                absent.append(f"{role}={safe_location(Path(path))}")
        if self.payload_toml is not None and not Path(self.payload_toml).is_file():
            absent.append(f"payload_toml={safe_location(Path(self.payload_toml))}")
        if self.trust_cache is not None and not Path(self.trust_cache).is_file():
            absent.append(f"trust_cache={safe_location(Path(self.trust_cache))}")
        if self.data_ramdisk is not None and not Path(self.data_ramdisk).is_file():
            absent.append(f"data_ramdisk={safe_location(Path(self.data_ramdisk))}")
        if self.ramfb_driver is not None and not Path(self.ramfb_driver).is_file():
            absent.append(f"ramfb_driver={safe_location(Path(self.ramfb_driver))}")
        return absent

    def to_dict(self) -> dict[str, str]:
        record = {role: safe_location(Path(getattr(self, role))) for role in
                  ("firmware", "kernel", "devicetree", "darwinboot", "unixnu")}
        if self.payload_toml is not None:
            record["payload_toml"] = safe_location(Path(self.payload_toml))
        if self.trust_cache is not None:
            record["trust_cache"] = safe_location(Path(self.trust_cache))
        if self.data_ramdisk is not None:
            record["data_ramdisk"] = safe_location(Path(self.data_ramdisk))
        if self.ramfb_driver is not None:
            record["ramfb_driver"] = safe_location(Path(self.ramfb_driver))
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
    #: The root volume this run attached, or ``None``.  Recorded because a run
    #: without one *looks* identical in the serial log until the kernel blocks
    #: in ``vfs_mountroot``; the report has to say which kind of run it was.
    root_image: Optional[str] = None
    #: The SystemOS cryptex this run attached, or ``None``.  The same argument as
    #: :attr:`root_image`, for a failure that is *further* along the boot and
    #: therefore easier to misread: a run with a root volume and no cryptex gets
    #: past ``vfs_mountroot``, passes the root checks, and panics with
    #: ``offline owner SystemOS mount failed: 80`` -- so "the kernel started" is
    #: true of both machines and only this field separates them.
    systemos_image: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    #: Whether the guest's own kernel log reported the Data volume mounted.  This
    #: is the only field in this report that describes something the *guest* did,
    #: and it is read out of guest memory over QMP rather than inferred from the
    #: serial log -- the kernel console is not on the serial port (measured, see
    #: :mod:`unipad.host.guestobserve`), so ``serial.log`` cannot answer it.
    #:
    #: It exists because of a measured failure that nothing else in this report
    #: would have caught: in 1 of 8 boots of the matched build the guest panics
    #: inside ``load_init_program`` with ``offline scratch Data mount failed``
    #: because the Data volume's ``IOMedia`` was not published yet, and **QEMU
    #: does not exit** -- the panic parks the CPU in a ``wfe; b wfe`` loop, so the
    #: process stays alive, ``query-status`` keeps answering, and screendumps keep
    #: succeeding.  Every other field here reports a healthy run.
    data_volume_mounted: bool = False
    #: The marker that was searched for, so a report that says ``False`` also says
    #: what it failed to find.
    data_marker: str = ""
    #: Bytes of guest kernel log recovered by the last successful probe.
    guest_log_bytes: int = 0
    #: sha256 of the last recovered guest kernel log.  Two probes with the same
    #: fingerprint and no marker mean the guest stopped writing, which is how a
    #: panic is told apart from a slow boot without knowing anything about panics.
    guest_log_fingerprint: Optional[str] = None
    #: One entry per probe: the wall clock, the bytes, the fingerprint, and either
    #: the marker hit or the reason the probe could not run.
    guest_probes: list[dict[str, Any]] = field(default_factory=list)
    #: Boot attempts this report represents.  Always 1 from :func:`boot`; more
    #: only from :func:`boot_until_mounted`, and recorded there because a report
    #: that says "mounted on the third try" without saying there were three tries
    #: reads as a flake rather than as the 12.5% race it is.
    attempts: int = 1
    #: Which attempt this report is, counting from 1, or 0 when the boot was not
    #: part of a chain.  Distinct from :attr:`attempts` on purpose: ``attempts``
    #: is a property of the report a caller receives, ``attempt`` is a property
    #: of the directory it was written to.  Reading ``attempt-3/boot-report.json``
    #: and seeing ``attempts: 1`` would be a contradiction with nothing to
    #: contradict it, so the two are separate fields and neither is derived from
    #: the other.
    attempt: int = 0

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
            "root_image": self.root_image,
            "systemos_image": self.systemos_image,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "data_volume_mounted": self.data_volume_mounted,
            "data_marker": self.data_marker,
            "guest_log_bytes": self.guest_log_bytes,
            "guest_log_fingerprint": self.guest_log_fingerprint,
            "guest_probes": [dict(entry) for entry in self.guest_probes],
            "attempts": self.attempts,
            "attempt": self.attempt,
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


def tree_ramdisk_placeholder(devicetree: Path) -> Optional[str]:
    """Which ramdisk placeholder the device tree carries, if any.

    ``AdtNode`` (``DarwinBoot.c``) accepts **either** ``RAMDisk`` or
    ``PackedRAMDisk`` inside ``/chosen/memory-map`` as the property it will
    patch with the ramdisk's address and size, requires it to be exactly 16
    bytes, requires those 16 bytes to be zero before it patches them, and
    refuses a tree that carries both.  This reads the tree back and reports
    which of the three states it is in.

    Returns:
        ``"PackedRAMDisk"``, ``"RAMDisk"``, or ``None``.

    Raises:
        UniPadError: ``config_invalid`` when the tree does not parse, carries
            both properties, or carries one that is not a zeroed 16 bytes.
            Those are not states a boot can recover from, and the alternative
            -- returning ``None`` for "no placeholder" -- would let a
            malformed tree pass as a tree without one.
    """
    tree = Path(devicetree)
    if not tree.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "the device tree is not at the path given",
            tree=safe_location(tree),
        )
    blob = tree.read_bytes()
    try:
        root, end = adt.parse_adt(blob)
    except Exception as exc:  # a tree that does not parse is a config error
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the device tree does not parse as an Apple device tree",
            tree=safe_location(tree),
            reason=type(exc).__name__,
        ) from exc
    if end != len(blob):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the device tree has trailing bytes after the root node",
            tree=safe_location(tree),
            bytes=len(blob) - end,
        )
    chosen = adt.find_node(root, "chosen")
    memory_map = adt.find_node(chosen, "memory-map") if chosen else None
    if memory_map is None:
        return None

    found: list[str] = []
    for key in ("PackedRAMDisk", "RAMDisk"):
        value = adt.node_property(memory_map, key)
        if value is None:
            continue
        if len(value) != 16:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                f"/chosen/memory-map/{key} is {len(value)} bytes; DarwinBoot "
                "requires exactly 16 and refuses the tree otherwise",
                tree=safe_location(tree),
                property=key,
            )
        if value != bytes(16):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                f"/chosen/memory-map/{key} is not zeroed; DarwinBoot treats a "
                "nonzero placeholder as compromised data",
                tree=safe_location(tree),
                property=key,
            )
        found.append(key)
    if len(found) > 1:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "/chosen/memory-map carries both PackedRAMDisk and RAMDisk; "
            "AdtNode refuses a tree that does",
            tree=safe_location(tree),
        )
    return found[0] if found else None


def unixnu_toml(
    spec: Mapping[str, Any],
    line: str,
    initrd: Optional[str] = None,
    ramfb: bool = False,
) -> str:
    """The ``EFI/UniXNU/unixnu.toml`` the ESP carries.

    A transcription of ``unixnu/payload.py:166-173``.  The ``[[driver]]`` entry
    is what makes UniXNU load DarwinBoot rather than expect the firmware to have
    run it, and ``run-qemu.py:96-98`` refuses a payload that does not carry it --
    so its absence is a build error here too, not a default.

    ``initrd`` is the path, relative to ``\\EFI\\UniXNU``, of the packed Data
    ramdisk.  ``Config.c:1189`` turns the entry's ``initrd`` key into
    ``Entry->Initrd``, ``Boot.c:147`` reads that file off the ESP, and
    ``Boot.c:165`` hands it to ``RunXnu`` as ``Request.Ramdisk`` -- so the line
    and the staged file are the same thing seen from two ends.  Upstream writes
    it immediately before ``args`` (``run-qemu.py:141``); :func:`stage_esp`
    refuses a tree whose placeholder and this line do not agree, so a caller
    cannot supply one without the other.

    ``ramfb`` adds a ``[[driver]]`` entry for the frame-buffer driver, **ahead
    of** DarwinBoot's.  The order is the point and not a formatting choice:
    ``QemuRamfbPkg/QemuRamfb.c:1-7`` says the driver exists to size ramfb
    "before DarwinBoot takes the frame buffer for the kernel", so a driver
    listed after it would configure a framebuffer that has already been claimed.

    And without this line the file is inert, which is what a measurement showed
    rather than what was assumed: staging ``QemuRamfb.efi`` on the ESP while the
    config named only ``Payloads/DarwinBoot.efi`` left the framebuffer at
    800x600, because this manifest is the *only* thing that makes UniXNU load a
    driver -- the firmware does not enumerate ``Payloads/``.  A staged file with
    no ``[[driver]]`` entry is a file nothing opens.
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
        + ('[[driver]]\n'
           f'path = "{RAMFB_NAME.removeprefix("EFI/UniXNU/")}"\n'
           "\n" if ramfb else "")
        + '[[driver]]\n'
        'path = "Payloads/DarwinBoot.efi"\n'
        "\n"
        "[[entry]]\n"
        'id = "xnu"\n'
        f'title = "{spec.get("title", "UniXNU")}"\n'
        'type = "xnu"\n'
        'path = "Payloads/kernel.macho"\n'
        + (f'initrd = "{initrd}"\n' if initrd else "")
        + f'args = "{line}"\n'
    )


#: The name the packed Data ramdisk is staged under, beside the kernel.  It is
#: the name ``unixnu.toml``'s ``initrd`` key has to spell, and it is the name the
#: device repository's payload build uses: ``unixnu/scripts/unixnu/payload.py:148``
#: copies the ramdisk to ``payloads / 'scratch-data.img'`` and :172 writes
#: ``initrd = "Payloads/scratch-data.img"``.  This is a *lookup*, exactly like
#: :data:`TRUSTCACHE_NAME` below -- DarwinBoot opens the file the config names --
#: so a second spelling stages a file nothing opens.
#:
#: It used to be ``owner-data-1g.dbr``, which is the name the seed has *on the
#: host* and in the delivery, not on the ESP.  Measured on 2026-10-27: booting the
#: first verified delivery was refused by ``_check_ramdisk_pair`` with "unixnu.toml's
#: initrd does not name the file that was staged" -- the delivery's own
#: ``unixnu.toml`` (which must come from the matched build) named
#: ``Payloads/scratch-data.img`` and this module staged ``owner-data-1g.dbr``
#: beside it.  The refusal was correct and the constant was wrong.
DATA_RAMDISK_NAME = "scratch-data.img"

#: The name the trust cache is staged under, beside the kernel.  One spelling,
#: because it is a *lookup* rather than a label: ``unixnu/payload.py:143-144``
#: writes exactly this and UniXNU reads exactly this, so a second spelling would
#: stage a file nothing opens.
TRUSTCACHE_NAME = "EFI/UniXNU/Payloads/kernel.macho.trustcache"

#: The name the frame-buffer driver is staged under, and the path
#: :func:`build_argv` tests for before emitting ``ramfb_size``.  It has to be
#: exactly this because it is also a *lookup*: ``run-qemu.py:105-116`` runs it
#: by this path, so a driver staged under any other name would be present on
#: the ESP and never executed -- which is the failure this constant makes
#: impossible, and the reason the two spellings live in one place.
RAMFB_NAME = "EFI/UniXNU/Payloads/QemuRamfb.efi"

#: The ``initrd =`` line, as a compiled pattern.  Config.c reads the key with
#: ``TomlGetString`` so whitespace is not significant, but this is only used to
#: detect presence in a config this module did not write, so the simple form is
#: enough -- and a config that spells it some other way is a config to read,
#: not to guess at.
_INITRD_LINE = re.compile(r'^\s*initrd\s*=\s*"([^"]+)"', re.MULTILINE)


def _check_ramdisk_pair(
    inputs: "BootInputs", config: str, written: dict[str, Any]
) -> None:
    """Refuse a boot whose ramdisk halves do not agree.

    DarwinBoot's gate (``DarwinBoot.c:563-569``) is an exclusive-or::

        if (Request->Ramdisk != NULL) {
          if ((RamdiskOffset == MAX_UINTN) || (U64 (Tree + RamdiskOffset) != 0) ||
              (U64 (Tree + RamdiskOffset + 8) != 0)) { Status = EFI_COMPROMISED_DATA; goto Fail; }
        } else if (RamdiskOffset != MAX_UINTN) {
          Status = EFI_UNSUPPORTED;
          goto Fail;
        }

    so exactly one of {an initrd, a placeholder} is a boot that never reaches
    the kernel, and both are needed for a Data volume to exist at all.  Neither
    failure is visible in the serial log -- the firmware refuses before
    DarwinBoot prints its hand-off banner -- which is why it is checked here,
    where the cost is an exception instead of a seven-minute boot that looks
    like a regression.
    """
    staged = f"EFI/UniXNU/Payloads/{DATA_RAMDISK_NAME}" in written
    declared = _INITRD_LINE.search(config)
    placeholder = tree_ramdisk_placeholder(inputs.devicetree)

    if staged != (declared is not None):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the packed Data ramdisk and unixnu.toml disagree: one names an "
            "initrd and the other does not carry the file",
            staged=staged,
            initrd_line=declared is not None,
        )
    if declared is not None and declared.group(1) != f"Payloads/{DATA_RAMDISK_NAME}":
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unixnu.toml's initrd does not name the file that was staged",
            initrd=declared.group(1),
            expected=f"Payloads/{DATA_RAMDISK_NAME}",
        )
    if staged != (placeholder is not None):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the packed Data ramdisk and the device tree disagree: DarwinBoot "
            "accepts both or neither, never one",
            data_ramdisk=staged,
            tree_placeholder=placeholder,
            tree=safe_location(Path(inputs.devicetree)),
        )


def _check_ramfb_pair(inputs: "BootInputs", config: str) -> None:
    """Refuse a boot that stages the frame-buffer driver nothing will load.

    Measured, not reasoned: a 24-minute run with ``QemuRamfb.efi`` staged on the
    ESP took 29 screendumps, every one of them 800x600 and every one carrying
    the same sha256.  The driver had no effect at all, and the reason is in the
    config -- the delivery's ``unixnu.toml`` lists exactly one driver,
    ``Payloads/DarwinBoot.efi``.  UniXNU loads what that manifest names; the
    firmware does not enumerate ``Payloads/``.  A staged file with no
    ``[[driver]]`` entry is a file nothing opens.

    So this refuses rather than staging a file the guest will ignore.  It is the
    same trade :func:`_check_ramdisk_pair` makes: the failure is invisible in
    the serial log and in the screendumps -- the boot succeeds, the Data volume
    mounts, and the only symptom is a framebuffer at the wrong size.  A caller
    that hits this needs a payload build whose ``unixnu.toml`` names the driver,
    which is a build change and not something this module should fake by editing
    a config that has to come from the matched build.
    """
    if inputs.ramfb_driver is None:
        return
    wanted = RAMFB_NAME.removeprefix("EFI/UniXNU/")
    if f'path = "{wanted}"' not in config:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the frame-buffer driver is staged but unixnu.toml does not load it: "
            "UniXNU loads only the drivers the manifest names, so this file "
            "would be ignored and the geometry would not be applied",
            driver=wanted,
            config_names=[
                line.split('"')[1] for line in config.splitlines()
                if line.strip().startswith("path = ")
            ],
        )


#: Where each optional input lands on the ESP, keyed by the role
#: :class:`BootInputs` names it.  ``stage_esp`` writes exactly these keys, so
#: this is the whole contract between "the caller asked for it" and "the guest
#: has it" -- which is what :func:`_check_carried` compares.
_OPTIONAL_STAGED = {
    "trust_cache": TRUSTCACHE_NAME,
    "data_ramdisk": f"EFI/UniXNU/Payloads/{DATA_RAMDISK_NAME}",
    "ramfb_driver": RAMFB_NAME,
}


def _check_carried(
    requested: "BootInputs", carried: Mapping[str, bool], where: str
) -> None:
    """Refuse a run that has lost one of the optional inputs it was given.

    ``requested`` is what the caller handed :func:`boot`.  ``carried`` says, role
    by role, whether that input is still in hand at the point ``where`` names, and
    the two call sites are the whole point of the ``where``: resolving the paths
    and staging the ESP fail in different places and would otherwise be told apart
    only by which one raised.

    ``BootInputs`` gained ``trust_cache`` and ``data_ramdisk`` after ``boot()``'s
    path-resolution block was written, and that block listed the five paths by
    hand.  A caller that passed both therefore had them checked by ``missing()``,
    resolved, and then dropped on the floor: the ESP carried five files,
    ``report.inputs`` recorded five, and the guest booted without either.  Both
    consequences are invisible in the serial log -- AMFI refusing to exec launchd
    (namespace 9, EXEC, code 8, SECURITY_POLICY, ``build-os-root.py:26-27``) and
    DarwinBoot refusing the ramdisk half of the pair with ``EFI_COMPROMISED_DATA``
    before the kernel starts (``DarwinBoot.c:563-569``) -- so the run reads as a
    guest bug, and the run report agrees with the lie.

    Comparing two *records* is what makes this a guard rather than a second copy
    of ``missing()``: ``missing()`` says the file was on disk when the run began,
    and this says the file is still in hand afterwards.  The inputs compared are
    the caller's, taken before anything is resolved, so a future edit that drops a
    field again fails here -- naming the role -- instead of producing a boot
    report that describes a payload that was never staged.

    ``where`` is carried in the details as well as in the message, so a caller (or
    a test) can tell the two call sites apart without matching prose.
    """
    for role, esp_name in _OPTIONAL_STAGED.items():
        asked = getattr(requested, role) is not None
        present = carried[role]
        if asked != present:
            raise UniPadError(
                ErrorCode.INVALID_PACKAGE,
                f"{where} does not carry the {role} this run asked for; the guest "
                "would boot without it and the run report would not say so",
                role=role,
                asked=asked,
                carried=present,
                where=where,
                esp_name=esp_name,
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
        EFI/UniXNU/Payloads/kernel.macho.trustcache      when the run has one
        EFI/UniXNU/Payloads/owner-data-1g.dbr             when the run has a
            packed Data ramdisk, which is also what puts an ``initrd`` line in
            unixnu.toml and what requires the tree to carry the placeholder
        EFI/UniXNU/Payloads/QemuRamfb.efi                when the run carries
            the frame-buffer driver, which is also what makes ``ramfb_size``
            reachable at all (see :attr:`BootInputs.ramfb_driver`)

    This used to refuse to stage ``QemuRamfb.efi``, on the grounds that the
    runner's payload build already puts one on the ESP and a second copy here
    would be a second definition of :data:`SCREEN`.  That was the wrong
    trade: the geometry stayed defined once, but the file became something no
    caller could supply, and :func:`build_argv`'s check for it was dead code on
    the official path -- with the 800x600 framebuffer described under
    :attr:`BootInputs.ramfb_driver` as the result.  A caller that wants the
    driver supplies it and this stages it; a caller that does not gets the
    behaviour it had before.

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

    # The trust cache, at the name the payload step uses
    # (``unixnu/payload.py:143-144``).  Recorded only when there is one, so a
    # caller that does not use a trust cache sees exactly the record it saw
    # before this field existed -- and so the record itself says which kind of
    # run it was: the key's presence is the answer to "did this boot carry a
    # trust cache", which is the question that decides whether AMFI will let the
    # kernel exec launchd (``build-os-root.py:26-27``).
    if inputs.trust_cache is not None:
        target = esp / TRUSTCACHE_NAME
        shutil.copyfile(inputs.trust_cache, target)
        written[TRUSTCACHE_NAME] = target.stat().st_size

    # The packed Data ramdisk.  Validated before it is copied, against the
    # format both the bootloader and the kernel driver parse, so a seed one of
    # them would reject costs a second here rather than a boot.  The name is
    # fixed because unixnu.toml's initrd is a path, not a lookup: whatever is
    # staged has to be spelled exactly by the config.
    if inputs.data_ramdisk is not None:
        packed_ramdisk.validate_file(inputs.data_ramdisk)
        target = esp / "EFI/UniXNU/Payloads" / DATA_RAMDISK_NAME
        shutil.copyfile(inputs.data_ramdisk, target)
        written[f"EFI/UniXNU/Payloads/{DATA_RAMDISK_NAME}"] = target.stat().st_size

    # The frame-buffer driver.  Not validated: it is a PE image this module has
    # no parser for, and the one thing that matters about it -- that it is the
    # build's own driver and not some other EFI binary -- is the caller's
    # decision, made by choosing the path.  What this records is *that it was
    # staged*, because that is the fact ``build_argv`` then acts on and a report
    # that says the geometry was 800x600 with no mention of why is not
    # diagnosable.
    if inputs.ramfb_driver is not None:
        target = esp / RAMFB_NAME
        shutil.copyfile(inputs.ramfb_driver, target)
        written[RAMFB_NAME] = target.stat().st_size

    initrd = f"Payloads/{DATA_RAMDISK_NAME}" if inputs.data_ramdisk is not None else None
    if inputs.payload_toml is not None:
        config = Path(inputs.payload_toml).read_text(encoding="utf-8")
    else:
        config = unixnu_toml(spec, kernel_args_line(spec), initrd=initrd,
                             ramfb=inputs.ramfb_driver is not None)
    if "Payloads/DarwinBoot.efi" not in config:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unixnu.toml does not load DarwinBoot as a driver; board.toml "
            '[payload] loader must be "driver"',
        )
    _check_ramdisk_pair(inputs, config, written)
    _check_ramfb_pair(inputs, config)
    (esp / "EFI/UniXNU/unixnu.toml").write_text(config, encoding="utf-8")
    written["EFI/UniXNU/unixnu.toml"] = len(config.encode("utf-8"))
    written["_esp"] = str(safe_location(esp))
    return written


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def qpath(path: Path | str) -> str:
    """A path as a QEMU option value: absolute, and forward slashes.

    QEMU is started with the run directory as its working directory, so a
    *relative* path resolves against the run directory rather than the caller's
    -- which is how a firmware that exists becomes "Could not open" with a path
    that looks right.  A backslash is an escape in a QEMU option value, so it is
    never used.

    Module level rather than nested in :func:`build_argv`, because
    :func:`_serial_argv` needs the same conversion and a second copy of a
    two-line function that decides how a path is spelled is exactly the kind of
    duplication that lets the two spellings drift apart.
    """
    return Path(path).resolve().as_posix()


def _serial_argv(port: Optional[int], log: Path) -> list[str]:
    """The UART's argv: a file by default, a TCP chardev when a port is given.

    Both spellings produce the same ``serial.log`` -- QEMU writes the log itself
    in the chardev case too -- and they differ in exactly one respect: the
    chardev can be *written to*.

    ``logappend=on`` rather than QEMU's default truncate, because a workdir
    reused across boots is documented to be a different machine and the earlier
    log is evidence of it; truncating would make the second run look like the
    first.  ``wait=off`` so a run with no client attached still starts -- the
    guest's serial output is simply dropped, exactly as ``file:`` would drop a
    host's input.

    The port is validated here rather than left to QEMU's exit, the same way
    ``qmp_port`` is: ``build_argv`` already refuses a port outside the ephemeral
    range and this keeps the two consistent.
    """
    if port is None:
        return ["-serial", f"file:{qpath(log)}"]
    port = int(port)
    if not 1024 <= port <= 65535:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the serial port must be a TCP port the host can bind",
            port=port,
        )
    return [
        "-chardev",
        f"socket,id=unixnu-serial,host=127.0.0.1,port={port},server=on,wait=off,"
        f"logfile={qpath(log)},logappend=on",
        "-serial", "chardev:unixnu-serial",
    ]


def _raw_volume(path: Path, role: str, consequence: str) -> Path:
    """A raw block-device image, resolved, and refused if it cannot be one.

    The three refusals are the three ways a path can look right and be useless,
    and each of them is measured rather than imagined:

    * **absent.**  QEMU would refuse the path too, but *after* it started -- and
      a run that started is a run that produced a serial log to read, one that
      stops at ``Waiting on IOMedia`` and looks exactly like a run that had no
      volume at all.
    * **empty.**  A zero-length file is a valid raw disk to QEMU and a useless
      volume; the run would spend its whole window waiting for a UUID nothing
      can publish.  ``CONFIG_INVALID`` rather than ``IMAGE_NOT_FOUND`` because
      retrying can never change a file's length from zero, and a caller that
      branches on the code and retries would retry forever.
    * **a UDIF container.**  ``044-76040-038.dmg`` is 5,157,406,069 bytes, so it
      passes both checks above and QEMU opens it happily -- ``format=raw``
      accepts any bytes -- and hands the guest a compressed container as a raw
      disk.  There is then no GPT, no APFS and no volume UUID, so the boot
      blocks on the same ``Waiting on IOMedia`` line as a run with no volume at
      all, while the caller reads the report field set and ``main()`` prints
      that the volume was attached.  ``unipad.host.udif`` is what turns one of
      these into a raw image -- and for the OS member that is 3415 of its 3814
      chunks, which is why the decoder exists.

    ``role`` names the attachment ("root volume", "SystemOS cryptex") and
    ``consequence`` is the clause that says what the guest loses without it,
    because that is the part of the message an operator acts on.  Both volumes
    go through here so they cannot drift into different answers about the same
    bytes.
    """
    volume = Path(path).resolve()
    if not volume.is_file():
        raise UniPadError(
            ErrorCode.IMAGE_NOT_FOUND,
            f"the {role} image does not exist; {consequence}",
            path=safe_location(volume),
        )
    if volume.stat().st_size <= 0:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"the {role} image is empty; no IOMedia can be published from it, so "
            "the volume the guest looks for cannot be found.  This is "
            "CONFIG_INVALID rather than IMAGE_NOT_FOUND because retrying can "
            "never change a file's length from zero, and a caller that branches "
            "on the code and retries would retry forever",
            path=safe_location(volume),
        )
    if _ends_in_udif_trailer(volume):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"the {role} image is a UDIF container, not a raw disk; QEMU's "
            "format=raw would hand the guest the compressed container, which "
            "publishes no IOMedia. Decode it first with unipad.host.udif",
            path=safe_location(volume),
        )
    return volume


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
    root_image: Optional[Path] = None,
    systemos_image: Optional[Path] = None,
    serial_port: Optional[int] = None,
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

    ``root_image`` is the OS volume as a *second*, read-only block device.
    Upstream's ``qemu_root_defconfig`` run attaches the OS image and the
    SystemOS image exactly this way (``run-qemu.py --os-root``), and the kernel
    finds the root by the volume UUID the board's ``[payload.adt]`` publishes.
    **That is the mechanism, not a promise this function can keep.**  It needs
    a kernel carrying ``CONFIG_XNU_NATIVE_VIRTIO_BLOCK``; without one the
    device is attached and never enumerated at all, which is the state of
    every kernel on this host (measured 2026-10-25: attaching a 3.3 GB raw
    APFS image produced zero block-device enumeration in the kernel log,
    and none of the four ``kernel.macho`` files here carries
    ``NativeVirtioBlock``).  So a successful :func:`build_argv` means the
    argv was built and the device was created -- nothing about whether the
    guest can read it.  ``scripts/windows/probe-root-volume.py`` is what
    answers that, and the report cannot: ``SERIAL_MARKERS`` carries no
    ``Waiting on``/``IOMedia``/``mountroot``/``bsd_autoconf``, so the
    ``vfs_mountroot`` block line is filtered out of ``serial_markers``
    entirely and a run that blocked is indistinguishable from one that did
    not except through ``root_image``.
    Without it the guest has the ESP and nothing else, so ``vfs_mountroot``
    blocks forever on ``Waiting on IOMedia with UUID ...`` -- measured on this
    host on 2026-10-24, which is why the parameter exists rather than the
    operator adding a ``-drive`` by hand.  It is read-only for the same reason
    upstream's is: this is the shipped system volume, and a boot that writes to
    it is a different experiment with different consequences (PRD 4.2).

    The root volume is **MMIO**, not PCI, and that is not a preference.  The
    driver that would read it matches on the transport:
    ``NativeVirtioBlock::probe`` (``vendor/UniXNU/drivers/NativeVirtioBlock/
    NativeVirtioBlock.cpp:148-154``) returns ``nullptr`` unless the provider's
    ``compatible`` is exactly the twelve bytes ``"virtio,mmio"``, so a
    ``virtio-blk-pci`` device can never be bound by it however the kernel is
    configured.  That is why upstream's ``--os-root`` spells
    ``-device virtio-blk-device,drive=os`` and sets
    ``-global virtio-mmio.force-legacy=false`` (``run-qemu.py:203-208``).  The
    ESP *stays* on PCI, because it is EDK II that has to find and mount it
    (``BdsDxe: loading Boot0001 ... from PciRoot(0x0)/Pci(0x1,0x0)``), and the
    two are therefore different transports by necessity rather than by
    oversight.

    A first version of this parameter emitted the root volume on PCI as well.
    It was wrong for the reason above and the mistake was only visible by
    reading the driver's ``probe`` -- the argv was accepted by QEMU either way.

    ``systemos_image`` is the SystemOS cryptex as a *third*, read-only block
    device, and it is the attachment the official path was missing: the OS root
    volume and the SystemOS image are the two VirtIO block devices the device
    repository's README says to hand QEMU, and the guest's own checker names
    what each one is for.  ``native_owner_cache_prepare()``
    (``vendor/UniXNU/patches/xnu/offline-owner-systemos-mount.patch``) looks for
    the volume ``XNU_OWNER_CACHE_VOLUME_UUID``
    (``8846FE78-2910-4297-AF97-D59BB6F8FC16``) and sets ``error = EAUTH`` when
    no device carries it.

    Measured on this host, 2026-10-26, on the machine that had an ESP and an OS
    root volume and nothing else: ``_native_owner_cache_stage`` stayed 1,
    ``_native_owner_cache_status`` 80 (``EAUTH``),
    ``_native_virtio_verified_devices`` 1, and the kernel panicked with
    ``offline owner SystemOS mount failed: 80 @kern_exec.c:8032``.  Attaching
    the cryptex as a third read-only ``virtio-blk-device`` took the same globals
    to stage 6, status 0, mount_status 0, readonly 1, authorized 1, selected ==
    mounted == ``0x01000004`` and ``_native_virtio_verified_devices`` 2 -- nine
    of the device repository's eleven success criteria field for field, the two
    that differ being I/O counters four blocks ahead.  The console line that
    separates the two machines is ``disk1s1 mounting volume ...SystemCryptex,
    requested by: init (pid 1)`` followed by ``mount-complete``.

    So it is not decoration, and its absence is *not* the same failure as a
    missing root volume: without a cryptex the guest gets past
    ``vfs_mountroot``, passes the root checks, and panics.  A run that "reached
    the kernel" is therefore true of both, and only
    :attr:`BootReport.systemos_image` says which one it was.

    **Position: last, after ``-name``.**  That is where the verified run puts it
    -- ``local-private/boot24/command.txt`` is the argv of the boot that
    measured the numbers above, and its last two tokens are exactly this pair.
    The order is ``-drive`` then ``-device`` and the drive id is ``systemos``;
    both are pinned by test against that file rather than by taste.

    MMIO, not PCI, for the reason the OS root volume is: the driver that reads
    it matches on ``compatible == "virtio,mmio"``.  ``readonly=on`` because this
    is a signed, shipped image and a boot that writes to it is a different
    experiment with different consequences (PRD 4.2).

    One operational consequence the verified run had to pay for: a third block
    device changes UEFI's automatic enumeration order, and the boot fell through
    to the UEFI Shell with the serial log stopped at ``Shell>`` -- DarwinBoot
    never ran.  The verified command carries ``bootindex=0`` on the ESP's
    ``virtio-blk-pci``, and that is what restored it, so :func:`build_argv` emits
    it too -- unconditionally, for the reason spelled out at the device below.
    The probe scripts call this function, so they get it without being edited.
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
    # Both block-device images are checked here, before the argv exists, for the
    # same reason the firmware is: QEMU would reject the whole command line at
    # exit 1 with a path that looks right, and the failure would be
    # indistinguishable from a boot that never started.  The three ways each can
    # be unusable, and why each is a refusal rather than a QEMU error, are in
    # :func:`_raw_volume`.
    root: Optional[Path] = None
    if root_image is not None:
        root = _raw_volume(
            root_image,
            "root volume",
            "the guest would boot with the ESP alone and block in vfs_mountroot",
        )
    # The cryptex goes through the same three checks, and the trap is the same
    # one wearing a different suffix: ``044-76034-038.dmg`` is 3,303,014,400
    # bytes of *raw* APFS with no trailer at all, while ``044-76040-038.dmg`` is
    # a container -- so the image that needs no decoding and the one that does
    # are told apart by their last 512 bytes, never by their names.
    systemos: Optional[Path] = None
    if systemos_image is not None:
        systemos = _raw_volume(
            systemos_image,
            "SystemOS cryptex",
            "native_owner_cache_prepare() would find no volume to mount and the "
            "kernel would panic with 'offline owner SystemOS mount failed'",
        )

    # The SystemOS cryptex, built last because that is where the verified run
    # puts it.  It carries the MMIO global with it when there is no OS root
    # volume to carry it: the cryptex is ``-device virtio-blk-device`` too, and
    # legacy mode would hand it the wrong register layout.
    systemos_argv: list[str] = []
    if systemos is not None:
        if root is None:
            systemos_argv += ["-global", "virtio-mmio.force-legacy=false"]
        systemos_argv += [
            "-drive",
            f"if=none,id=systemos,format=raw,readonly=on,file={qpath(systemos)}",
            "-device", "virtio-blk-device,drive=systemos",
        ]

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
        # form UniXNU boots.  PCI, because EDK II enumerates the virtual PCI bus
        # to find it (BdsDxe loads Boot0001 from PciRoot(0x0)/Pci(0x1,0x0)).
        #
        # ``bootindex=0`` is not decoration and it is not conditional on how many
        # other disks happen to be attached.  It says the ESP is the disk to boot
        # from, which is true of every machine this function builds: it is the
        # only disk carrying EFI/BOOT/BOOTAA64.EFI.  The measured reason it has to
        # be *said* rather than left to the firmware's guess is the third block
        # device -- with the ESP, an OS root volume and a SystemOS cryptex, UEFI's
        # automatic enumeration order flipped and the boot fell through to the
        # UEFI Shell with the serial log stopped at ``Shell>``, so DarwinBoot
        # never ran.  Every recorded run of that three-disk machine carries it
        # (``local-private/boot23`` through ``boot25``), and the earlier runs
        # without it are the ones from before the problem was hit, not evidence
        # that omitting it is ever better.
        #
        # It is unconditional rather than "only when a third disk is attached"
        # because that threshold is not a property of the machine, it is a
        # property of one afternoon's discoveries -- and the combination it would
        # get wrong is reachable from this very module: a SystemOS cryptex with no
        # OS root volume is a two-disk machine that has never been booted, and it
        # would silently lose the one hint that keeps the firmware off the Shell.
        "-drive", f"if=none,id=esp,format=raw,readonly=on,file=fat:{qpath(Path(esp))}",
        "-device", "virtio-blk-pci,drive=esp,bootindex=0",
        # The OS root volume, when one is supplied.  Its own drive id, because
        # two block devices sharing one would be one device; and it is emitted
        # *after* the ESP so the ESP keeps the device number the boot log and
        # every earlier run already recorded.  MMIO, not PCI -- see the
        # docstring: NativeVirtioBlock::probe matches only "virtio,mmio".
    ] + ([
        # Upstream's own spelling (run-qemu.py:203-208).  Without this global the
        # MMIO transport stays in legacy mode, which the driver's register
        # offsets (mapDeviceMemoryWithIndex(0) must be at least 0x108) assume is
        # not the case.
        "-global", "virtio-mmio.force-legacy=false",
        "-drive", f"if=none,id=osroot,format=raw,readonly=on,file={qpath(root)}",
        "-device", "virtio-blk-device,drive=osroot",
    ] if root is not None else []) + [
        "-device", "ramfb",
        "-device", "qemu-xhci",
        "-device", "usb-kbd",
        "-nic", "none",
        "-display", display,
        # The UART, and the only route by which the guest can ever receive a
        # keystroke.
        #
        # ``-serial file:`` is **one-way**: QEMU writes what the guest emits and
        # discards anything a host might send.  That is not a detail -- it is why
        # no input has ever reached this guest.  Measured: the guest's own kernel
        # log says ``Serial keyboard started`` at t=43.75s, and XNU's
        # ``serial_keyboard_init`` (``osfmk/console/serial_general.c:51-68``)
        # only prints that when ``serialmode & SERIALMODE_INPUT``, which
        # ``serial=3`` sets (``SERIALMODE_OUTPUT|SERIALMODE_INPUT``,
        # ``serial_protos.h:52,55``).  The thread it starts polls
        # ``_serial_getc(false)`` every 16 ms and feeds each byte to
        # ``cons_cinput`` (``serial_general.c:79-97``).  So the guest *is*
        # listening -- to a port the host cannot write to.
        #
        # With ``serial_port`` the UART becomes a TCP chardev instead.  QEMU
        # still writes the log itself (``logfile=``/``logappend=``), so
        # ``serial.log`` is byte-for-byte what it was, and a host that connects
        # to the port can send bytes the guest will read.  Without it the
        # behaviour is exactly what it always was.
        *(_serial_argv(serial_port, Path(workdir) / "serial.log")),
        "-qmp", f"tcp:127.0.0.1:{int(qmp_port)},server=on,wait=off",
        "-monitor", "none",
        "-name", "unipad-unixnu",
    ] + (
        ["-fw_cfg", f"name=opt/org.unixnu/ramfb-size,string={ramfb_size}"] if ramfb_size else []
    ) + systemos_argv


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
    serial_port: Optional[int] = None,
    display: str = "none",
    root_image: Optional[Path] = None,
    systemos_image: Optional[Path] = None,
) -> BootReport:
    """Stage the ESP, start QEMU, watch it, and keep every log.

    The run is not "successful" because QEMU started.  :attr:`BootReport.ok`
    means the run completed and produced a serial log; :attr:`BootReport.reached_qmp`
    means QEMU answered control commands; and what the *guest* did is in
    ``serial.log`` and the screendumps, summarised but not judged.

    Raises:
        UniPadError: ``image_not_found`` for a missing *boot input* -- the five
            :class:`BootInputs` paths and the QEMU binary, all checked before
            anything is written.  The two block-device images are **not** in that
            set: they are checked inside :func:`build_argv`, which runs after the
            work directory, the ESP and ``vars.fd`` have been written, so a bad
            ``root_image`` or ``systemos_image`` leaves a staged ESP behind.  The
            asymmetry is deliberate -- each is an attachment, not an input the
            boot cannot proceed without -- and it is stated here because the
            earlier wording claimed both happened "before anything is written",
            which was not true of the root image and was not tested for it.
            ``config_invalid`` for an unusable port, a machine the board does
            not declare, or an attachment that is empty or a UDIF container;
            ``invalid_package`` when an optional input the caller asked for is
            lost -- either resolving the paths or staging the ESP
            (:func:`_check_carried`);
            ``qemu_launch_failed`` when QEMU exits before QMP answers.
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
    # What the caller asked for, kept aside before the paths are resolved.
    # :func:`_check_carried` compares *this* against the resolved inputs and then
    # against the ESP that was written, so it can tell "the caller never asked"
    # from "the caller asked and the file is not on the disk the guest gets".
    requested = inputs
    # Resolved once, here, so every later use -- the ESP staging, the argv, the
    # record -- names the same file.  A relative input path is the caller's
    # spelling, not QEMU's: the child runs with the run directory as its cwd.
    #
    # :func:`dataclasses.replace`, not a hand-written ``BootInputs(...)``: this
    # block used to list the five paths by name, and when ``trust_cache`` and
    # ``data_ramdisk`` were added to the dataclass the list was not extended.
    # A caller that passed both had them checked by ``missing()``, resolved, and
    # then dropped -- the ESP carried five files, the report recorded five, and
    # the guest booted without a trust cache AMFI needs to exec launchd
    # (``build-os-root.py:26-27``) and without the ramdisk DarwinBoot's
    # exclusive-or gate refuses (``DarwinBoot.c:563-569``).  Deriving the new
    # record from the old one means a field added later survives by
    # construction rather than by somebody remembering this block.
    inputs = replace(
        inputs,
        firmware=Path(inputs.firmware).resolve(),
        kernel=Path(inputs.kernel).resolve(),
        devicetree=Path(inputs.devicetree).resolve(),
        darwinboot=Path(inputs.darwinboot).resolve(),
        unixnu=Path(inputs.unixnu).resolve(),
        payload_toml=None if inputs.payload_toml is None else Path(inputs.payload_toml).resolve(),
    )
    # ...and the resolution kept every optional input.  Checked here rather than
    # only against the staged ESP because this is where a hand-written list would
    # drop one, and the pair check inside ``stage_esp`` would otherwise fire
    # first and blame the device tree for a field the resolution lost.
    _check_carried(
        requested,
        {role: getattr(inputs, role) is not None for role in _OPTIONAL_STAGED},
        "resolving the input paths",
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
    # ...and what was asked for is what the guest is handed.  After staging
    # rather than before, because this is the first moment both halves of the
    # comparison exist; it costs a dict lookup and it is the only thing standing
    # between a dropped field and a boot report that describes a payload which
    # was never written to disk.
    _check_carried(
        requested,
        {role: esp_name in staged for role, esp_name in _OPTIONAL_STAGED.items()},
        "the staged ESP",
    )

    # The writable vars flash, sized to the code flash exactly.  A file that is
    # already the right size is left alone: it holds the variables from a
    # previous boot, which is the point of having one.
    #
    # **Two boots into the same workdir are therefore not the same machine**:
    # the second carries UEFI NVRAM the first wrote, and nothing in
    # boot-report.json says so, so two reports from one workdir look
    # comparable when they are not.  Recorded rather than changed --
    # preserving the variables is the documented intent -- and a caller that
    # wants a reproducible machine uses a fresh workdir, which is what
    # scripts/windows/probe-root-volume.py does.
    vars_path = work / "vars.fd"
    code_size = Path(inputs.firmware).stat().st_size
    if not vars_path.is_file() or vars_path.stat().st_size < code_size:
        with vars_path.open("wb") as handle:
            handle.truncate(code_size)

    command = build_argv(
        qemu, inputs, esp, work, qmp_port=qmp_port, serial_port=serial_port,
        memory_mib=memory_mib, display=display,
        # Only with the driver that reads it: a geometry nothing set is a claim
        # the run cannot support (run-qemu.py:105-116).
        ramfb_size=SCREEN if (esp / RAMFB_NAME).is_file() else None,
        root_image=root_image,
        systemos_image=systemos_image,
    )
    (work / "command.txt").write_text(" ".join([str(qemu), *command]) + "\n", encoding="utf-8")

    report = BootReport(
        ok=False, workdir=str(safe_location(work)), command=[str(qemu), *command],
        inputs=inputs.to_dict(),
        # Named here rather than in every later assignment, so a run that fails
        # before the report is built still says whether it had a root volume.
        root_image=safe_location(root_image.resolve()) if root_image is not None else None,
        # ...and the same for the cryptex, for the same reason and with a
        # further-out failure to distinguish (see :attr:`BootReport.systemos_image`).
        systemos_image=(
            safe_location(systemos_image.resolve()) if systemos_image is not None else None
        ),
    )

    stdout_path = work / "qemu-stdout.log"
    stderr_path = work / "qemu-stderr.log"
    # The kernel's digest, computed once and handed to the guest-log probe.  It
    # is not a check on the boot -- the inputs are matched by the delivery
    # manifest -- it is the price of reading the kernel at all:
    # :class:`~unipad.host.guestobserve.KernelImage` refuses an image without a
    # digest it was verified against, because the symbol addresses the probe
    # walks with come out of that file.
    try:
        kernel_sha256 = hashlib.sha256(Path(inputs.kernel).read_bytes()).hexdigest()
    except OSError as exc:
        raise UniPadError(
            ErrorCode.IMAGE_NOT_FOUND,
            "the kernel image could not be read to verify it",
            path=safe_location(inputs.kernel),
            reason=type(exc).__name__,
        ) from exc
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

        report = _watch(
            process, report, work, qmp_port=qmp_port, seconds=seconds, every=every,
            kernel=Path(inputs.kernel), kernel_sha256=kernel_sha256,
        )

    report.serial_bytes = (work / "serial.log").stat().st_size if (work / "serial.log").is_file() else 0
    report.serial_markers = _serial_markers(work / "serial.log")
    (work / "boot-report.json").write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    (work / "esp-manifest.json").write_text(json.dumps(staged, indent=2) + "\n", encoding="utf-8")
    return report


#: How many boots :func:`boot_until_mounted` will spend before giving up.  The
#: measured failure rate is 1 in 8, so three attempts take it to 1 in 512 --
#: below anything this project has measured, and small enough that the caller
#: still learns about a real regression inside a few minutes rather than never.
MAX_BOOT_ATTEMPTS = 3


def boot_until_mounted(
    inputs: BootInputs,
    workdir: Path,
    *,
    qemu: Path,
    device_dir: Path,
    seconds: int = DEFAULT_SECONDS,
    every: int = DEFAULT_EVERY,
    qmp_port: int = DEFAULT_QMP_PORT,
    serial_port: Optional[int] = None,
    display: str = "none",
    root_image: Optional[Path] = None,
    systemos_image: Optional[Path] = None,
    attempts: int = MAX_BOOT_ATTEMPTS,
) -> BootReport:
    """Boot until the guest reports the Data volume mounted, or the budget ends.

    This is the workaround for a measured race, not a wrapper for convenience.
    ``selectVolumeSeed`` (NativeRamBlock.cpp:368-417) issues **one**
    ``IOService::getMatchingServices`` for the Data volume's ``IOMedia`` and
    returns 0 if that single match does not come back; ``load_init_program``
    panics on the 0.  There is no retry, sleep or wait anywhere in it, so a guest
    that loses the race is dead -- and QEMU does not exit, because an XNU panic
    parks the CPU in ``wfe; b wfe``.  A caller watching the process sees nothing.

    So the retry lives here, on the host, where it can be done without touching
    the kernel.  Each attempt gets a **fresh** work directory, because
    :func:`boot` leaves a ``vars.fd`` behind and two boots into one directory are
    two different machines (documented at :func:`boot`).

    Args:
        attempts: How many boots to spend.  The last report is returned even
            when it failed, with ``attempts`` set to how many were actually
            spent, so "the Data volume never mounted" carries the number of
            tries that produced it.

    Returns:
        The report of the **last** attempt.  ``data_volume_mounted`` is the
        success condition; ``attempts`` is how many boots produced it.  A report
        with ``attempts > 1`` and ``data_volume_mounted`` is a race that was
        retried, and a report with ``attempts == 1`` is a boot that either
        worked first time or was never retried -- the two are told apart by
        ``data_volume_mounted`` and by nothing else in the report.

    Note:
        Only the returned attempt's ``boot-report.json`` carries the chain's
        true length.  An earlier attempt's file says ``attempts: 1``, because
        that is what was known when it was written, and rewriting it afterwards
        would falsify the record of what that attempt actually reported.  Read
        those files as per-attempt records keyed by ``attempt``; read the
        returned object as the chain's verdict.
    """
    workdir = Path(workdir)
    report: Optional[BootReport] = None
    for attempt in range(1, max(int(attempts), 1) + 1):
        # A fresh directory per attempt: ``boot`` seeds ``vars.fd`` only when it
        # is missing or short, so reusing one directory would carry the previous
        # attempt's UEFI variables into this one and make two reports that look
        # comparable describe different machines.
        run = workdir / f"attempt-{attempt}"
        report = boot(
            inputs, run,
            qemu=qemu, device_dir=device_dir, seconds=seconds, every=every,
            qmp_port=qmp_port, serial_port=serial_port, display=display,
            root_image=root_image, systemos_image=systemos_image,
        )
        report.attempt = attempt
        if report.data_volume_mounted:
            break
    if report is None:  # pragma: no cover - the range above is never empty
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "no boot was attempted",
            attempts=attempts,
        )
    report.attempts = attempt
    # The returned attempt's report is rewritten so the file and the object say
    # the same thing.  ``attempts`` is the chain's length, which is only known
    # once the chain ends, and a file that says 1 next to an object that says 3
    # is a discrepancy nothing downstream can resolve.
    (workdir / f"attempt-{attempt}" / "boot-report.json").write_text(
        json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    return report


# ---------------------------------------------------------------------------
# reading what the guest did
# ---------------------------------------------------------------------------
#: The line the kernel prints when IOKit has published and mounted a volume.
#: This is the guest's own report of the exact point at which
#: ``selectVolumeSeed`` either finds the Data volume's ``IOMedia`` or returns 0
#: -- and a 0 is what ``load_init_program`` panics on.
#:
#: It is searched for in the msgbuf ring and not in ``serial.log`` because the
#: kernel console is not on the serial port (measured negative, see
#: :mod:`unipad.host.guestobserve`): the serial log stops at the firmware and
#: cannot answer this question at all.
DATA_MOUNT_MARKER = "mount-complete volume OwnerData"

#: How many consecutive probes with an identical fingerprint and no marker are
#: read as "the guest stopped writing its log".  Two, not one: a single repeat
#: is also exactly what a slow boot looks like between two screendumps, and the
#: point of the fingerprint is to tell those apart without knowing anything
#: about panics.
STALLED_LOG_READS = 2


def guest_stopped_progressing(probes: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the guest stopped writing its kernel log without mounting Data.

    This is the detector, and it is the only thing standing between a panicked
    guest and a boot that is merely slow.  Every other signal is identical for
    the two: QEMU does not exit when XNU panics (the panic parks the CPU in a
    ``wfe; b wfe`` loop), ``query-status`` keeps answering ``running``, and
    screendumps keep succeeding while the guest is dead.  What differs is the
    log -- a guest that is still booting keeps writing to ``msgbuf``, and one
    that has panicked writes nothing more, ever.

    So the rule is "the last :data:`STALLED_LOG_READS` probes that produced a
    log produced the same bytes".  It deliberately does **not** look at what the
    log says: the two panic strings in this build carry a ``@%s:%d`` suffix that
    appears nowhere in the vendored tree, so a detector matching on panic text
    would be matching a string this project cannot yet explain, and would stop
    working the moment the suffix changes.

    A probe that could not read the log is skipped rather than counted.  An
    unreadable log is not a stalled one -- before the kernel publishes its load
    line there is no verified address mapping, and counting that as a stall
    would call the first twenty seconds of every good boot a failure.

    Args:
        probes: The report's probe entries, in order.  Only entries with
            ``ready`` and a ``fingerprint`` are considered.

    Returns:
        ``True`` when the tail of the readable probes is a run of identical
        fingerprints *and* no probe ever found the marker.  The second half
        matters: a guest that mounted the Data volume and then went idle has
        also stopped writing, and reporting that as a failure would turn every
        success into a retry.
    """
    if any(entry.get("marker") for entry in probes):
        return False
    fingerprints = [
        entry["fingerprint"]
        for entry in probes
        if entry.get("ready") and entry.get("fingerprint")
    ]
    # Fewer readable probes than the threshold is not a stall, and the check is
    # on the *count* rather than on the size of the tail.  A tail of one
    # fingerprint is trivially "all identical", so without this a boot that has
    # just started writing its log is reported as stalled -- measured by
    # ``test_the_detector_ignores_a_probe_that_could_not_read_the_log``, which
    # failed the first version of this function for exactly that reason.
    if len(fingerprints) < STALLED_LOG_READS:
        return False
    return len(set(fingerprints[-STALLED_LOG_READS:])) == 1


def _kernel_image(kernel: Path, digest: str) -> tuple[Optional[Any], Optional[str]]:
    """A verified :class:`~unipad.host.guestobserve.KernelImage`, or a reason.

    Parsing the kernel is expensive and it is done once per boot, here rather
    than once per probe.  A kernel that cannot be parsed is *reported*, not
    raised: :func:`boot` already checked the file exists, and a caller booting a
    kernel this probe cannot read must still get a boot report rather than an
    exception from a module whose job is to record what happened.
    """
    from unipad.host.guestobserve import KernelImage

    try:
        return KernelImage(kernel, expected_sha256=digest), None
    except (UniPadError, OSError, struct.error) as exc:
        return None, f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"


def _guest_log_probe(
    qmp_port: int,
    *,
    kernel: Any,
    serial: Path,
    scratch: Path,
) -> dict[str, Any]:
    """One read of the guest's kernel log, or the reason it could not be taken.

    This is the only channel through which anything the *guest* did can be seen
    during a boot.  It exists because of a measured failure that nothing else
    reports: in 1 of 8 boots of the matched build the guest panics inside
    ``load_init_program`` with ``offline scratch Data mount failed``, and QEMU
    does **not** exit -- the panic parks the CPU in a ``wfe; b wfe`` loop, so
    ``query-status`` keeps answering ``running`` and screendumps keep succeeding
    while the guest is dead.  A frozen log fingerprint is what distinguishes the
    two, and it needs no knowledge of what the panic says.

    Every failure mode returns a record instead of raising.  A probe that raised
    would abort a boot that is merely slow: before the kernel's load line exists
    in the serial log there is no verified mapping between the kernel's virtual
    and physical addresses, and that is the normal state of the first twenty
    seconds.

    Returns:
        ``{"ready": True, "bytes": int, "fingerprint": str, "marker": bool,
        "log": Path}`` when the ring was read, or ``{"ready": False,
        "reason": str}`` when it was not.  ``marker`` is whether
        :data:`DATA_MOUNT_MARKER` appears anywhere in it.
    """
    from unipad.host.guestobserve import (
        GuestMemory,
        GuestObserver,
        load_addresses_from_serial,
    )
    from unipad.host.qmp import QmpClient

    if kernel is None:
        return {"ready": False, "reason": "the kernel image could not be parsed"}
    try:
        blob = serial.read_bytes() if serial.is_file() else b""
    except OSError as exc:
        return {"ready": False, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        load = load_addresses_from_serial(blob)
    except UniPadError as exc:
        # Not an error: the firmware prints this line, and until it does there
        # is nothing to translate with.  Said in the reason so a report of
        # "not ready" can be told apart from "the guest refused".
        return {"ready": False, "reason": f"no kernel load line in the serial log yet ({exc.code})"}

    # One connection for the whole probe, and it is opened *after* the serial log
    # has been read: until the load line exists there is nothing to read, and a
    # connection opened for a probe that cannot run is a connection QEMU has to
    # drop and take again.  ``timeout`` is the greeting budget, and it is 60 s
    # because this is the only command in the loop that can legitimately take
    # that long -- a 16 MiB ring is four thousand ``pmemsave`` round trips.
    #
    # The constructor is inside the ``try`` on purpose: it raises
    # ``UniPadError(CONFIG_INVALID)`` for a port outside 1..65535, and a probe
    # that raised would abort a boot that is merely misconfigured -- which is the
    # one thing this function's contract says it must not do.  Measured by
    # ``test_the_probe_reports_a_port_it_cannot_even_construct_a_client_for``.
    try:
        client = QmpClient(port=qmp_port, timeout=60.0)
    except (UniPadError, OSError, ValueError) as exc:
        return {
            "ready": False,
            "reason": f"{getattr(exc, 'code', type(exc).__name__)}: {exc}",
        }
    try:
        client.connect()
        # ``scratch`` is a plain ``Path``, so on Windows it stringifies with
        # backslashes -- unlike the screendump filename, which has to be
        # forward-slashed because QEMU reads a backslash in an argv *value* as
        # an escape.  This is a QMP argument, not an argv value, and it is the
        # spelling ``guestobserve``'s own CLI passes and the diagnostics used
        # successfully.  Changing it here "for consistency" would change the one
        # thing that has been measured to work.
        observer = GuestObserver(
            memory=GuestMemory(client, scratch), image=kernel, load=load
        )
        data = observer.kernel_log()
    except (UniPadError, OSError) as exc:
        return {
            "ready": False,
            "reason": f"{getattr(exc, 'code', type(exc).__name__)}: {exc}",
        }
    finally:
        client.close()

    # The whole ring is kept, not just a tail: the ring wraps, so the Data
    # volume's mount line can be anywhere in it, and a probe that only kept the
    # last few kilobytes would report ``marker: False`` for a guest that mounted
    # the volume four hundred lines ago.
    #
    # Scrubbed before it is written, for the same reason ``guestobserve``'s CLI
    # scrubs its own stdout: this is evidence, and PRD 9.2 forbids the account
    # name in any of it.  The fingerprint is taken from the *unscrubbed* bytes,
    # because it has to be comparable across probes and scrubbing is not
    # idempotent in general -- a run whose log contained the account name once
    # would fingerprint differently after the first pass.
    #
    # Written as bytes, not text: Python's text mode translates ``\n`` to
    # ``\r\n`` on Windows, and a copy of the guest's ring that has had its line
    # endings rewritten is not a copy of the guest's ring.
    text = data.decode("utf-8", "replace")
    try:
        scratch.write_bytes(scrub_account(text).encode("utf-8"))
    except OSError:
        # The bytes were already read; losing the copy on disk costs the
        # evidence file and nothing else, so it must not fail the probe.
        pass
    return {
        "ready": True,
        "bytes": len(data),
        "fingerprint": hashlib.sha256(data).hexdigest(),
        "marker": DATA_MOUNT_MARKER in text,
        "log": str(safe_location(scratch)),
    }


def _take_screenshot(
    qmp_port: int,
    work: Path,
    started: float,
    shots: list[str],
    report: BootReport,
) -> Optional[Path]:
    """One screendump, or ``None`` -- and never an exception.

    This exists so that a failed screendump cannot suppress the guest-log probe.
    It was written inline in :func:`_watch` with three ``continue`` statements,
    and each of them skipped the probe as well: a run whose screendumps broke
    would report no ``guest_probes`` at all, so ``data_volume_mounted`` stayed
    ``False`` and :func:`boot_until_mounted` spent every attempt re-booting a
    guest that may have mounted its Data volume on the first try.  The two
    observations are independent and must fail independently.

    Returns the PNG's path when one was written, else ``None``.  Every failure is
    recorded in ``report.qmp_evidence["screendump_error"]``.
    """
    ppm = work / "screen.ppm"
    try:
        # The same spelling the argv uses: absolute and forward-slashed.  A
        # backslash is an escape in a QEMU value and a relative path resolves
        # against the run directory (see :func:`build_argv`'s ``qpath``).
        reply = _qmp(qmp_port, "screendump", filename=ppm.resolve().as_posix())
    except (OSError, json.JSONDecodeError) as exc:
        report.qmp_evidence["screendump_error"] = f"{type(exc).__name__}: {exc}"
        return None
    if "error" in reply or not ppm.is_file():
        report.qmp_evidence["screendump_error"] = reply.get("error", "no screendump file")
        return None
    shot = work / f"screen-{int(time.monotonic() - started):03d}s.png"
    try:
        _write_png(ppm, shot)
    except (OSError, ValueError, IndexError) as exc:
        report.qmp_evidence["screendump_error"] = f"{type(exc).__name__}: {exc}"
        # Removed here too, not only on the success path.  It is one filename
        # reused every round rather than an accumulating pile, so leaving it
        # costs a stale 1-pixel file and nothing else -- but a run directory
        # that reports a screendump error should not also contain the PPM that
        # caused it, and the next round's dump has to overwrite it anyway.
        ppm.unlink(missing_ok=True)
        return None
    shots.append(str(safe_location(shot)))
    ppm.unlink(missing_ok=True)
    return shot


def _watch(
    process: subprocess.Popen,
    report: BootReport,
    work: Path,
    *,
    qmp_port: int,
    seconds: int,
    every: int,
    kernel: Optional[Path] = None,
    kernel_sha256: str = "",
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
    # The kernel image is parsed lazily and at most once, because parsing it
    # walks a whole symbol table and because a kernel that cannot be parsed must
    # not cost the screendumps.
    image: Any = None
    image_reason: Optional[str] = None
    if kernel is not None:
        image, image_reason = _kernel_image(Path(kernel), kernel_sha256)

    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(max(int(every), 1))
        if process.poll() is not None:
            break
        # Interleaved with the probe, never concurrent with it: QEMU serves one
        # QMP client at a time, so a probe running alongside a screendump would
        # be refused rather than queued.  It comes first, for the same reason --
        # the screendump has always worked, and a probe that fails must not cost
        # it.  A screendump that fails costs *itself* and nothing else: see
        # :func:`_take_screenshot` for why that used to be a bug.
        _take_screenshot(qmp_port, work, started, shots, report)
        if image is None:
            # Not a probe that failed: there is no image to read symbols from,
            # and every later round would report the same thing.  One entry
            # says so and the screendumps carry on.
            if not report.guest_probes:
                report.guest_probes.append(
                    {"ready": False, "at_seconds": round(time.monotonic() - started, 3),
                     "reason": f"no kernel image to read: {image_reason or 'none was given'}"}
                )
            # Set here too, not only after a successful probe: a report from a
            # run whose kernel could not be parsed must still answer the
            # question, and an absent key reads as "never asked" rather than as
            # "nothing was advancing".  Measured -- this branch used to skip it,
            # and ``test_watch_with_no_kernel_at_all_still_watches`` caught the
            # report coming back with no ``guest_log_stalled`` at all.
            report.qmp_evidence["guest_log_stalled"] = guest_stopped_progressing(
                report.guest_probes
            )
            continue
        entry = _guest_log_probe(
            qmp_port,
            kernel=image,
            serial=work / "serial.log",
            scratch=work / "guest-kernel.log",
        )
        entry["at_seconds"] = round(time.monotonic() - started, 3)
        report.guest_probes.append(entry)
        if entry.get("ready"):
            report.guest_log_bytes = int(entry["bytes"])
            report.guest_log_fingerprint = entry["fingerprint"]
            if entry["marker"]:
                report.data_volume_mounted = True
                report.data_marker = DATA_MOUNT_MARKER
        # Recomputed from the whole list rather than kept as a counter, so the
        # rule exists once -- in :func:`guest_stopped_progressing`, which the
        # tests exercise directly.  A counter here would be a second
        # implementation of the same rule and the two would drift.
        report.qmp_evidence["guest_log_stalled"] = guest_stopped_progressing(
            report.guest_probes
        )

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
    # Every optional BootInputs field is reachable from here, and that is the
    # whole point of the flag.  ``--data-ramdisk`` and ``--systemos-image`` were
    # added by the commits that fixed the official path dropping them; this one
    # was not, so the unified path's own CLI could not express a trust cache even
    # though BootInputs, stage_esp, missing() and _check_carried all handle one.
    # The diagnostics could (probe-root-volume.py passes it), which is how the
    # gap survived: the config the diagnostics verified on 2026-10-26 was only
    # reachable from the diagnostics.  Absent here means "boot without one", which
    # build-os-root.py:26-27 records as a guest AMFI will not let run launchd.
    parser.add_argument("--trust-cache", type=Path, default=None,
                        help="the chain's trust bundle, staged as "
                             "EFI/UniXNU/Payloads/kernel.macho.trustcache beside the "
                             "kernel; without it AMFI refuses to exec launchd "
                             "(build-os-root.py:26-27)")
    parser.add_argument("--data-ramdisk", type=Path, default=None,
                        help="the packed Data ramdisk (.dbr) to stage beside the kernel; "
                             "the device tree must also carry the PackedRAMDisk "
                             "placeholder, which unipad.host.adt --data-ramdisk writes")
    parser.add_argument("--ramfb-driver", type=Path, default=None,
                        help="QemuRamfb.efi to stage at EFI/UniXNU/Payloads; without it "
                             "nothing applies SCREEN, build_argv emits no ramfb-size "
                             "fw_cfg item, and the framebuffer stays at QEMU's 800x600 "
                             "default (see BootInputs.ramfb_driver)")
    parser.add_argument("--workdir", type=Path, required=True, help="run directory (recreated)")
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS)
    parser.add_argument("--every", type=int, default=DEFAULT_EVERY)
    parser.add_argument("--qmp-port", type=int, default=DEFAULT_QMP_PORT)
    # Without this the UART is ``-serial file:``, which QEMU writes and never
    # reads: the guest's serial keyboard polls the port every 16 ms and gets
    # nothing, so no keystroke a host sends can ever arrive.  With it the UART is
    # a TCP chardev on loopback and ``unipad.host.input.SerialInputSink`` can
    # type into the kernel console.  The log is still written to serial.log by
    # the chardev's own ``logfile=``, so observation is unchanged either way --
    # which is exactly why the one-wayness was invisible for so long.
    parser.add_argument("--serial-port", type=int, default=None,
                        help="bind the guest's serial UART to this TCP port on "
                             "127.0.0.1 as well as logging it to serial.log; "
                             "without it the UART is a write-only file and no "
                             "host keystroke can reach the guest")
    parser.add_argument("--display", default="none")
    parser.add_argument("--root-image", type=Path, default=None,
                        help="raw OS volume to attach as a second, read-only virtio-blk disk; "
                             "without it the guest has the ESP alone and blocks in vfs_mountroot")
    parser.add_argument("--systemos-image", type=Path, default=None,
                        help="raw SystemOS cryptex to attach as a third, read-only virtio-blk "
                             "disk; without it native_owner_cache_prepare() finds no volume to "
                             "mount and the kernel panics, which is a different failure from "
                             "the one --root-image prevents")
    parser.add_argument("--kernel-arg", action="append", default=[], metavar="NAME=VALUE",
                        help="replace the board's argument of the same name; repeatable")
    parser.add_argument("--attempts", type=int, default=1,
                        help="boot this many times, each in a fresh attempt-N "
                             "directory, until the guest reports the Data volume "
                             "mounted (see boot_until_mounted); 1 is the old "
                             "single-boot behaviour")
    args = parser.parse_args(argv)

    inputs = BootInputs(
        firmware=args.firmware,
        kernel=args.kernel,
        devicetree=args.devicetree,
        darwinboot=args.darwinboot,
        unixnu=args.unixnu,
        payload_toml=args.payload_toml,
        trust_cache=args.trust_cache,
        data_ramdisk=args.data_ramdisk,
        ramfb_driver=args.ramfb_driver,
    )
    if args.kernel_arg:
        # Validated here so a bad extra argument fails before the ESP is staged,
        # not inside a run that has already started QEMU.
        kernel_args_line(read_board_payload(args.device), args.kernel_arg)

    runner = boot_until_mounted if args.attempts > 1 else boot
    report = runner(
        inputs,
        args.workdir,
        qemu=args.qemu,
        device_dir=args.device,
        seconds=args.seconds,
        every=args.every,
        qmp_port=args.qmp_port,
        serial_port=args.serial_port,
        display=args.display,
        root_image=args.root_image,
        systemos_image=args.systemos_image,
        **({"attempts": args.attempts} if args.attempts > 1 else {}),
    )
    print(f"qemu exit {report.exit_code}; qmp ready: {report.reached_qmp}; serial {report.serial_bytes} bytes")
    # The guest's own answer, and the only line in this output that describes
    # something the guest did rather than something the host did.  A run whose
    # Data volume never mounted is otherwise indistinguishable from a good one:
    # QEMU is alive, QMP answers, and the screendumps keep coming.
    print(f"data volume mounted: {report.data_volume_mounted}"
          + (f" (found {report.data_marker!r})" if report.data_volume_mounted else ""))
    if report.attempts > 1:
        print(f"boot attempts: {report.attempts} (this was attempt {report.attempt})")
    if report.guest_probes:
        stalled = report.qmp_evidence.get("guest_log_stalled")
        last = report.guest_probes[-1]
        print(f"guest kernel log: {report.guest_log_bytes} bytes from "
              f"{len(report.guest_probes)} probes"
              + (f"; stalled: {stalled}" if stalled else "")
              + (f"; last probe: {last.get('reason')}" if not last.get("ready") else ""))
    # From ``inputs`` and not from ``report``: BootReport deliberately does not
    # carry the trust cache, because a report that names one unconditionally
    # cannot tell a run that had one from a run that did not, and that difference
    # is invisible in the serial log.  ``inputs.to_dict()`` omits the key when
    # there is none, which is the same rule.
    if inputs.trust_cache is not None:
        print(f"trust cache staged: {inputs.trust_cache.name}")
    else:
        print("no trust cache staged: AMFI will refuse to exec launchd")
    # The geometry line, printed because it is the one configuration whose
    # absence is invisible: a run at 800x600 and a run at 1376x1024 both produce
    # screendumps, both mount the Data volume, and only the framebuffer contents
    # tell them apart -- and those are identical in the broken case.
    if inputs.ramfb_driver is not None:
        print(f"ramfb driver staged: {inputs.ramfb_driver.name}; "
              f"frame buffer will be {SCREEN}")
    else:
        print(f"no ramfb driver staged: frame buffer stays at QEMU's ramfb default, "
              f"not {SCREEN}")
    # Printed for the same reason as the geometry line: a write-only UART and a
    # bidirectional one both produce a serial.log full of guest output, so the
    # only way to tell them apart from a run's own output is to say which one
    # this was.
    if args.serial_port is None:
        print("serial UART is a write-only file: no host keystroke can reach "
              "the guest (pass --serial-port to change that)")
    else:
        print(f"serial UART is a TCP chardev on 127.0.0.1:{args.serial_port}; "
              f"SerialInputSink(port={args.serial_port}) can type into it")
    if report.root_image:
        print(f"root volume attached: {report.root_image}")
    else:
        print("no root volume attached: the guest has the ESP alone")
    if report.systemos_image:
        print(f"SystemOS cryptex attached: {report.systemos_image}")
    else:
        print("no SystemOS cryptex attached: native_owner_cache_prepare() will refuse")
    for line in report.serial_markers[-8:]:
        print(f"  {line}")
    if report.screenshots:
        print(f"{len(report.screenshots)} screenshots; last: {report.screenshots[-1]}")
    if report.error_message:
        print(f"error: {report.error_message}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
