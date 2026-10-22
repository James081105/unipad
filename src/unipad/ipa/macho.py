"""Pure-Python Mach-O and universal ("fat") binary parser.

PRD 5.1 requires the host-side package precheck to inspect ``Payload/*.app``,
the Info.plist, the main executable, the Mach-O CPU slices, the platform
identifier, the minimum OS version, the linked dylibs and the encryption
information of an IPA.  This module provides that inspection with the
standard library only (:mod:`struct`), from bytes the caller already holds:
a file on disk, an in-memory buffer, or a member of the IPA zip archive.

Design notes
------------
* Fat/universal headers are always big-endian (``FAT_MAGIC``).  Thin headers
  are little-endian on every platform Apple still ships; the byte-swapped
  magics (``MH_CIGAM`` / ``MH_CIGAM_64``) are accepted so a big-endian image
  still decodes instead of being rejected as garbage.
* Command decoding is *tolerant* about the tail of the load-command region.
  A bogus ``ncmds`` / ``sizeofcmds`` pair (hand-built images, truncated
  files) stops decoding with a note recorded on the slice instead of
  raising, because the header facts a precheck needs are already known by
  then.  A wrong *magic* is always fatal (``invalid_package``).
* ``LC_BUILD_VERSION`` platform numbers follow Apple's ``loader.h`` /
  ``ld.hpp`` contract: ``1 == macOS``, ``2 == iOS``, ``7 == iOS
  simulator``.  They are *not* the order in which the platforms were
  introduced, and swapping them would misclassify every real Device IPA.
* Nothing here executes, trusts or validates the binary: encryption,
  platform and architecture are reported as facts for
  :mod:`unipad.ipa.precheck` to judge.

Every failure path raises :class:`unipad.errors.UniPadError` with a stable
code (``invalid_package`` for anything that is not a readable Mach-O).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from ..errors import ErrorCode, UniPadError

__all__ = [
    "FAT_MAGIC",
    "FAT_MAGIC_64",
    "FAT_CIGAM",
    "FAT_CIGAM_64",
    "MH_MAGIC",
    "MH_MAGIC_64",
    "MH_CIGAM",
    "MH_CIGAM_64",
    "CPU_TYPE_ARM64",
    "CPU_TYPE_X86_64",
    "PLATFORM_MACOS",
    "PLATFORM_IOS",
    "PLATFORM_IOSSIMULATOR",
    "PLATFORM_NAMES",
    "SIMULATOR_PLATFORMS",
    "FILETYPE_NAMES",
    "SegmentInfo",
    "SliceInfo",
    "MachOInfo",
    "cputype_name",
    "parse_macho",
    "parse_macho_bytes",
    "parse_macho_stream",
]

# --- Universal (fat) container magics.  Fat headers are always big-endian. ---
FAT_MAGIC = 0xCAFEBABE          # 32-bit fat header, read big-endian
FAT_CIGAM = 0xBEBAFECA          # the same bytes read little-endian (legacy hosts)
FAT_MAGIC_64 = 0xCAFEBABF       # 64-bit fat header, read big-endian
FAT_CIGAM_64 = 0xBFBAFECA       # the same bytes read little-endian (legacy hosts)

# --- Thin Mach-O magics ---
MH_MAGIC = 0xFEEDFACE           # 32-bit, host-endian (little on modern platforms)
MH_CIGAM = 0xCEFAEDFE           # 32-bit, byte-swapped
MH_MAGIC_64 = 0xFEEDFACF        # 64-bit, host-endian
MH_CIGAM_64 = 0xCFFAEDFE        # 64-bit, byte-swapped

# --- CPU types ---
CPU_ARCH_ABI64 = 0x01000000
CPU_ARCH_ABI64_32 = 0x02000000
CPU_TYPE_X86 = 0x00000007
CPU_TYPE_X86_64 = 0x01000007
CPU_TYPE_ARM = 0x0000000C
CPU_TYPE_ARM64 = 0x0100000C
CPU_TYPE_ARM64_32 = 0x0200000C
CPU_SUBTYPE_MASK = 0xFF000000   # capability bits live in the high byte
CPU_SUBTYPE_ARM64E = 2

_CPU_TYPE_NAMES: dict[int, str] = {
    CPU_TYPE_X86: "i386",
    CPU_TYPE_X86_64: "x86_64",
    CPU_TYPE_ARM: "arm",
    CPU_TYPE_ARM64: "arm64",
    CPU_TYPE_ARM64_32: "arm64_32",
}

# --- Load commands (``loader.h``).  ``LC_REQ_DYLD`` = 0x80000000 is OR-ed into
# --- a few of them, so comparisons go through ``_cmd_matches``. ---
LC_REQ_DYLD = 0x80000000
LC_SEGMENT = 0x00000001
LC_SYMTAB = 0x00000002
LC_ID_DYLIB = 0x0000000D
LC_LOAD_DYLIB = 0x0000000C
LC_LOAD_WEAK_DYLIB = 0x00000018
LC_REEXPORT_DYLIB = 0x0000001F
LC_LOAD_UPWARD_DYLIB = 0x00000023
LC_RPATH = 0x0000001C
LC_UUID = 0x0000001B
LC_CODE_SIGNATURE = 0x0000001D
LC_SEGMENT_64 = 0x00000019
LC_UNIXTHREAD = 0x00000005
LC_ENCRYPTION_INFO = 0x00000021
LC_VERSION_MIN_MACOSX = 0x00000024
LC_VERSION_MIN_IPHONEOS = 0x00000025
LC_VERSION_MIN_WATCHOS = 0x00000026
LC_VERSION_MIN_TVOS = 0x0000002F
LC_BUILD_VERSION = 0x00000032
LC_ENCRYPTION_INFO_64 = 0x0000002C

# --- ``LC_BUILD_VERSION`` platform identifiers (``ld.hpp``) ---
PLATFORM_UNKNOWN = 0
PLATFORM_MACOS = 1
PLATFORM_IOS = 2
PLATFORM_TVOS = 3
PLATFORM_WATCHOS = 4
PLATFORM_BRIDGEOS = 5
PLATFORM_MACCATALYST = 6
PLATFORM_IOSSIMULATOR = 7
PLATFORM_TVOSSIMULATOR = 8
PLATFORM_WATCHOSSIMULATOR = 9
PLATFORM_DRIVERKIT = 10
PLATFORM_VISIONOS = 11
PLATFORM_VISIONOSSIMULATOR = 12
PLATFORM_FIRMWARE = 13
PLATFORM_SEPOS = 14

PLATFORM_NAMES: dict[int, str] = {
    PLATFORM_UNKNOWN: "unknown",
    PLATFORM_MACOS: "macOS",
    PLATFORM_IOS: "iOS",
    PLATFORM_TVOS: "tvOS",
    PLATFORM_WATCHOS: "watchOS",
    PLATFORM_BRIDGEOS: "bridgeOS",
    PLATFORM_MACCATALYST: "MacCatalyst",
    PLATFORM_IOSSIMULATOR: "iPhoneSimulator",
    PLATFORM_TVOSSIMULATOR: "AppleTVSimulator",
    PLATFORM_WATCHOSSIMULATOR: "WatchSimulator",
    PLATFORM_DRIVERKIT: "DriverKit",
    PLATFORM_VISIONOS: "visionOS",
    PLATFORM_VISIONOSSIMULATOR: "visionOSSimulator",
    PLATFORM_FIRMWARE: "Firmware",
    PLATFORM_SEPOS: "SEPOS",
}

#: Platforms that only ever run inside the Simulator (never on a device).
SIMULATOR_PLATFORMS = frozenset(
    {
        PLATFORM_IOSSIMULATOR,
        PLATFORM_TVOSSIMULATOR,
        PLATFORM_WATCHOSSIMULATOR,
        PLATFORM_VISIONOSSIMULATOR,
    }
)

# --- File types (``loader.h`` ``MH_*``) ---
MH_OBJECT = 1
MH_EXECUTE = 2
MH_CORE = 4
MH_PRELOAD = 5
MH_DYLIB = 6
MH_DYLINKER = 7
MH_BUNDLE = 8
MH_DYLIB_STUB = 9
MH_DSYM = 10
MH_KEXT_BUNDLE = 11
MH_FILESET = 12

FILETYPE_NAMES: dict[int, str] = {
    MH_OBJECT: "object",
    MH_EXECUTE: "executable",
    MH_CORE: "core",
    MH_PRELOAD: "preload",
    MH_DYLIB: "dylib",
    MH_DYLINKER: "dyld",
    MH_BUNDLE: "bundle",
    MH_DYLIB_STUB: "dylib_stub",
    MH_DSYM: "dsym",
    MH_KEXT_BUNDLE: "kext",
    MH_FILESET: "fileset",
}

#: Bytes pulled from a stream to decode one image.  Real load-command regions
#: are a few KiB; the ceiling only exists so a corrupt/huge image cannot
#: exhaust host memory.  Truncation is recorded, never silently accepted.
_MAX_REGION_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class SegmentInfo:
    """One ``LC_SEGMENT(_64)`` record (PRD 5.1: Mach-O layout facts)."""

    name: str
    vmaddr: int
    vmsize: int
    fileoff: int
    filesize: int

    def to_dict(self) -> dict[str, Any]:
        """Return the segment as a JSON-serialisable mapping."""
        return {
            "name": self.name,
            "vmaddr": self.vmaddr,
            "vmsize": self.vmsize,
            "fileoff": self.fileoff,
            "filesize": self.filesize,
        }


@dataclass
class SliceInfo:
    """One architecture slice of a thin or fat Mach-O image.

    ``minos``/``sdk`` are ``(major, minor, patch)`` tuples decoded from the
    ``x.y.z`` packed version used by ``LC_BUILD_VERSION`` and
    ``LC_VERSION_MIN_*``.  ``encrypted`` is true when a
    ``LC_ENCRYPTION_INFO(_64)`` command reports ``cryptid != 0`` -- i.e. a
    FairPlay/DRM protected binary, which is out of scope for UniPad (PRD
    5.1) and must be rejected by the precheck.
    """

    cputype: int
    cpusubtype: int
    offset: int
    size: int
    filetype: int = MH_EXECUTE
    filetype_name: str = "executable"
    platform: int = PLATFORM_UNKNOWN
    platform_name: str = "unknown"
    minos: tuple[int, int, int] = (0, 0, 0)
    sdk: tuple[int, int, int] = (0, 0, 0)
    encrypted: bool = False
    cryptid: int = 0
    cryptoff: int = 0
    cryptsize: int = 0
    dylibs: list[str] = field(default_factory=list)
    rpaths: list[str] = field(default_factory=list)
    install_name: str | None = None
    has_code_signature: bool = False
    code_signature_offset: int | None = None
    code_signature_size: int | None = None
    uuid: str | None = None
    segments: list[SegmentInfo] = field(default_factory=list)
    is_64: bool = True
    endian: str = "<"
    parse_notes: list[str] = field(default_factory=list)

    @property
    def cputype_name(self) -> str:
        """Human-readable architecture name (``arm64``, ``arm64e``, ...)."""
        return cputype_name(self.cputype, self.cpusubtype)

    @property
    def is_arm64(self) -> bool:
        """True for any ARM64 device slice, ``arm64e`` included."""
        return self.cputype == CPU_TYPE_ARM64

    @property
    def is_arm64e(self) -> bool:
        """True only for the ``arm64e`` subtype of ``arm64``."""
        return self.cputype == CPU_TYPE_ARM64 and self._subtype_low() == CPU_SUBTYPE_ARM64E

    @property
    def is_simulator(self) -> bool:
        """True when the slice targets a Simulator platform."""
        return self.platform in SIMULATOR_PLATFORMS

    @property
    def minos_string(self) -> str:
        """``minos`` rendered as ``major.minor.patch``."""
        return ".".join(str(part) for part in self.minos)

    def _subtype_low(self) -> int:
        return self.cpusubtype & ~CPU_SUBTYPE_MASK & 0xFF

    def to_dict(self) -> dict[str, Any]:
        """Return the slice as a JSON-serialisable mapping."""
        return {
            "cputype": self.cputype,
            "cputype_name": self.cputype_name,
            "cpusubtype": self.cpusubtype,
            "offset": self.offset,
            "size": self.size,
            "is_arm64": self.is_arm64,
            "is_arm64e": self.is_arm64e,
            "filetype": self.filetype,
            "filetype_name": self.filetype_name,
            "platform": self.platform,
            "platform_name": self.platform_name,
            "is_simulator": self.is_simulator,
            "minos": list(self.minos),
            "minos_string": self.minos_string,
            "sdk": list(self.sdk),
            "encrypted": self.encrypted,
            "cryptid": self.cryptid,
            "cryptoff": self.cryptoff,
            "cryptsize": self.cryptsize,
            "dylibs": list(self.dylibs),
            "rpaths": list(self.rpaths),
            "install_name": self.install_name,
            "has_code_signature": self.has_code_signature,
            "code_signature_offset": self.code_signature_offset,
            "code_signature_size": self.code_signature_size,
            "uuid": self.uuid,
            "segments": [seg.to_dict() for seg in self.segments],
            "is_64": self.is_64,
            "endian": "little" if self.endian == "<" else "big",
            "parse_notes": list(self.parse_notes),
        }


@dataclass
class MachOInfo:
    """Result of parsing a thin or fat Mach-O image.

    ``primary`` is the slice the precheck should judge: the first ARM64
    device slice when one exists, otherwise the first slice.  It is ``None``
    only when the image carries no slice at all.
    """

    fat: bool
    slices: list[SliceInfo] = field(default_factory=list)
    primary: SliceInfo | None = None
    source: str | None = None
    size_bytes: int = 0
    is_64_fat: bool = False

    @property
    def arm64_slices(self) -> list[SliceInfo]:
        """Every ARM64 device slice (``arm64`` and ``arm64e``)."""
        return [s for s in self.slices if s.is_arm64]

    @property
    def has_arm64(self) -> bool:
        """True when at least one ARM64 slice is present."""
        return bool(self.arm64_slices)

    @property
    def slice_cputypes(self) -> list[str]:
        """Distinct architecture names in file order (e.g. ``['arm64']``)."""
        seen: list[str] = []
        for s in self.slices:
            name = s.cputype_name
            if name not in seen:
                seen.append(name)
        return seen

    @property
    def platforms(self) -> list[int]:
        """Distinct ``LC_BUILD_VERSION`` platform numbers in file order."""
        seen: list[int] = []
        for s in self.slices:
            if s.platform not in seen:
                seen.append(s.platform)
        return seen

    @property
    def encrypted(self) -> bool:
        """True when *any* slice reports ``cryptid != 0``."""
        return any(s.encrypted for s in self.slices)

    @property
    def cryptid(self) -> int:
        """Highest ``cryptid`` across slices (0 == not encrypted)."""
        return max((s.cryptid for s in self.slices), default=0)

    @property
    def dylibs(self) -> list[str]:
        """Distinct linked dylib names across slices, in file order."""
        seen: list[str] = []
        for s in self.slices:
            for name in s.dylibs:
                if name not in seen:
                    seen.append(name)
        return seen

    @property
    def rpaths(self) -> list[str]:
        """Distinct ``LC_RPATH`` entries across slices, in file order."""
        seen: list[str] = []
        for s in self.slices:
            for path in s.rpaths:
                if path not in seen:
                    seen.append(path)
        return seen

    def to_dict(self) -> dict[str, Any]:
        """Return the parsed image as a JSON-serialisable mapping."""
        return {
            "fat": self.fat,
            "is_64_fat": self.is_64_fat,
            "source": self.source,
            "size_bytes": self.size_bytes,
            "slice_count": len(self.slices),
            "slice_cputypes": self.slice_cputypes,
            "has_arm64": self.has_arm64,
            "platforms": self.platforms,
            "encrypted": self.encrypted,
            "cryptid": self.cryptid,
            "dylibs": self.dylibs,
            "rpaths": self.rpaths,
            "primary": self.primary.to_dict() if self.primary is not None else None,
            "slices": [s.to_dict() for s in self.slices],
        }


def cputype_name(cputype: int, cpusubtype: int = 0) -> str:
    """Return the architecture name for a Mach-O ``cputype``/``cpusubtype``.

    ``arm64`` and ``arm64e`` share ``cputype`` 0x0100000C and differ only in
    the low subtype bits, so both values are required (PRD 5.1).
    """
    base = cputype & 0xFFFFFFFF
    if base == CPU_TYPE_ARM64:
        if (cpusubtype & ~CPU_SUBTYPE_MASK) & 0xFF == CPU_SUBTYPE_ARM64E:
            return "arm64e"
        return "arm64"
    name = _CPU_TYPE_NAMES.get(base)
    if name is not None:
        return name
    if base & CPU_ARCH_ABI64_32:
        return f"cpu_0x{base:08x}"
    if base & CPU_ARCH_ABI64:
        return f"cpu64_0x{base & ~CPU_ARCH_ABI64:08x}"
    return f"cpu_0x{base:08x}"


def platform_name(platform: int) -> str:
    """Return the platform name for a ``LC_BUILD_VERSION`` platform number."""
    return PLATFORM_NAMES.get(platform, f"platform_{platform}")


def parse_macho(path: str | Path) -> MachOInfo:
    """Parse a Mach-O or universal binary from disk.

    Args:
        path: File to read.  Never executed, only opened for reading.

    Returns:
        MachOInfo: decoded slices with the primary slice selected.

    Raises:
        UniPadError: ``invalid_package`` when the file is missing, unreadable,
            too small, or does not start with a Mach-O/fat magic.
    """
    p = Path(path)
    try:
        size = p.stat().st_size
        with p.open("rb") as handle:
            return parse_macho_stream(handle, size=size, source=str(p))
    except UniPadError:
        raise
    except OSError as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"cannot read Mach-O file: {p}",
            reason="unreadable_file",
            path=str(p),
        ) from exc


def parse_macho_bytes(data: bytes, source: str = "<memory>") -> MachOInfo:
    """Parse a Mach-O or universal binary held in memory.

    Args:
        data: Complete image bytes (fat container or thin image).
        source: Label used in error details and ``MachOInfo.source``.

    Returns:
        MachOInfo: decoded slices with the primary slice selected.

    Raises:
        UniPadError: ``invalid_package`` when the bytes are not a Mach-O.
    """
    if len(data) < 4:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "image too small to contain a Mach-O magic",
            reason="truncated",
            source=source,
            size_bytes=len(data),
        )
    return _parse_buffer(data, source)


def parse_macho_stream(
    stream: BinaryIO,
    size: int | None = None,
    source: str = "<stream>",
) -> MachOInfo:
    """Parse a Mach-O image from a seekable binary stream.

    Only the fat header and one bounded region per slice are read, so a large
    executable inside an IPA zip can be inspected without materialising the
    whole file.  The stream position is saved and restored, so the caller may
    keep using the stream afterwards.

    Args:
        stream: Seekable binary stream positioned anywhere.
        size: Total byte length of the underlying image, when known.
        source: Label used in error details and ``MachOInfo.source``.

    Returns:
        MachOInfo: decoded slices with the primary slice selected.

    Raises:
        UniPadError: ``invalid_package`` for unreadable or non-Mach-O input.
    """
    saved_position = _tell(stream)
    try:
        stream.seek(0)
    except (OSError, ValueError) as exc:  # pragma: no cover - unusual streams
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "Mach-O stream is not seekable",
            reason="unseekable_stream",
            source=source,
        ) from exc

    try:
        return _parse_stream_body(stream, size, source)
    finally:
        if saved_position is not None:
            try:
                stream.seek(saved_position)
            except (OSError, ValueError):  # pragma: no cover - unusual streams
                pass


def _parse_stream_body(stream: BinaryIO, size: int | None, source: str) -> MachOInfo:
    """Read a seekable stream from offset 0 and decode the image."""
    head = stream.read(4)
    if len(head) < 4:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "image too small to contain a Mach-O magic",
            reason="truncated",
            source=source,
        )
    big_endian_magic = struct.unpack(">I", head)[0]
    if big_endian_magic in (FAT_MAGIC, FAT_MAGIC_64):
        return _parse_fat_stream(stream, big_endian_magic == FAT_MAGIC_64, size, source)
    if big_endian_magic in (FAT_CIGAM, FAT_CIGAM_64):
        # Byte-swapped fat header (a fat image written by a little-endian
        # producer).  The entries are read with the matching endianness.
        return _parse_fat_stream(
            stream, big_endian_magic == FAT_CIGAM_64, size, source, entry_endian="<"
        )

    region = head + stream.read(_MAX_REGION_BYTES)
    total = size if size is not None else len(region)
    slice_info = _parse_thin(
        region,
        offset=0,
        size=total,
        source=source,
    )
    return MachOInfo(
        fat=False,
        slices=[slice_info],
        primary=slice_info,
        source=source,
        size_bytes=total,
        is_64_fat=False,
    )


# --------------------------------------------------------------------------
# internal helpers
# --------------------------------------------------------------------------


def _tell(stream: BinaryIO) -> int | None:
    """Return the current stream offset, or None when it cannot be queried."""
    try:
        return int(stream.tell())
    except (OSError, ValueError):  # pragma: no cover - unusual streams
        return None


def _parse_buffer(data: bytes, source: str) -> MachOInfo:
    """Dispatch an in-memory buffer to the fat or thin parser."""
    big_endian_magic = struct.unpack(">I", data[:4])[0]
    if big_endian_magic in (FAT_MAGIC, FAT_MAGIC_64):
        return _parse_fat_buffer(data, big_endian_magic == FAT_MAGIC_64, source)
    if big_endian_magic in (FAT_CIGAM, FAT_CIGAM_64):
        return _parse_fat_buffer(
            data, big_endian_magic == FAT_CIGAM_64, source, entry_endian="<"
        )
    slice_info = _parse_thin(data, offset=0, size=len(data), source=source)
    return MachOInfo(
        fat=False,
        slices=[slice_info],
        primary=slice_info,
        source=source,
        size_bytes=len(data),
        is_64_fat=False,
    )


def _parse_fat_buffer(
    data: bytes,
    is_64: bool,
    source: str,
    entry_endian: str = ">",
) -> MachOInfo:
    """Parse a fat container that is already fully in memory."""
    archs = _read_fat_archs(data, is_64, entry_endian, source)
    slices: list[SliceInfo] = []
    for arch in archs:
        start = arch["offset"]
        declared = arch["size"]
        if start < 0 or declared < 0 or start + declared > len(data):
            raise UniPadError(
                ErrorCode.INVALID_PACKAGE,
                "fat slice lies outside the image",
                reason="slice_out_of_bounds",
                source=source,
                slice_offset=start,
                slice_size=declared,
                image_size=len(data),
            )
        slices.append(
            _parse_thin(
                data[start : start + declared],
                offset=start,
                size=declared,
                source=source,
                cputype=arch["cputype"],
                cpusubtype=arch["cpusubtype"],
            )
        )
    return _finish_fat(slices, is_64, source, len(data))


def _parse_fat_stream(
    stream: BinaryIO,
    is_64: bool,
    size: int | None,
    source: str,
    entry_endian: str = ">",
) -> MachOInfo:
    """Parse a fat container by seeking to each declared slice."""
    entry_size = 32 if is_64 else 20
    stream.seek(0)
    header = stream.read(8)
    if len(header) < 8:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "truncated fat header",
            reason="truncated_fat_header",
            source=source,
        )
    count = struct.unpack(entry_endian + "I", header[4:8])[0]
    if count > 64 or count == 0:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "fat header declares an implausible slice count",
            reason="implausible_fat_arch_count",
            source=source,
            nfat_arch=count,
        )
    entries = stream.read(count * entry_size)
    archs = _read_fat_archs(header + entries, is_64, entry_endian, source)

    total = size if size is not None else 0
    slices: list[SliceInfo] = []
    for arch in archs:
        start = arch["offset"]
        declared = arch["size"]
        if start < 0 or declared < 0 or (total and start + declared > total):
            raise UniPadError(
                ErrorCode.INVALID_PACKAGE,
                "fat slice lies outside the image",
                reason="slice_out_of_bounds",
                source=source,
                slice_offset=start,
                slice_size=declared,
                image_size=total,
            )
        stream.seek(start)
        chunk = stream.read(min(declared, _MAX_REGION_BYTES))
        if len(chunk) < 28:
            raise UniPadError(
                ErrorCode.INVALID_PACKAGE,
                "fat slice is too small to contain a Mach-O header",
                reason="truncated_slice",
                source=source,
                slice_offset=start,
            )
        slices.append(
            _parse_thin(
                chunk,
                offset=start,
                size=declared,
                source=source,
                cputype=arch["cputype"],
                cpusubtype=arch["cpusubtype"],
            )
        )
    return _finish_fat(slices, is_64, source, total)


def _read_fat_archs(
    raw: bytes,
    is_64: bool,
    entry_endian: str,
    source: str,
) -> list[dict[str, int]]:
    """Validate and decode ``fat_arch`` / ``fat_arch_64`` records from ``raw``.

    ``raw`` must start with the 8-byte fat header (magic + ``nfat_arch``).
    """
    if len(raw) < 8:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "truncated fat header",
            reason="truncated_fat_header",
            source=source,
            available_bytes=len(raw),
        )
    entry_size = 32 if is_64 else 20
    count = struct.unpack(entry_endian + "I", raw[4:8])[0]
    if count == 0:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "fat container declares no slices",
            reason="empty_fat_container",
            source=source,
        )
    if count > 64:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "fat header declares an implausible slice count",
            reason="implausible_fat_arch_count",
            source=source,
            nfat_arch=count,
        )
    if 8 + count * entry_size > len(raw):
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "fat header extends past the end of the image",
            reason="truncated_fat_header",
            source=source,
            nfat_arch=count,
            available_bytes=len(raw),
        )
    archs: list[dict[str, int]] = []
    for index in range(count):
        base = 8 + index * entry_size
        fields = struct.unpack_from(entry_endian + "IIIII", raw, base)
        archs.append(
            {
                "cputype": fields[0],
                "cpusubtype": fields[1],
                "offset": fields[2],
                "size": fields[3],
                "align": fields[4],
            }
        )
    return archs


def _finish_fat(slices: list[SliceInfo], is_64: bool, source: str, size: int) -> MachOInfo:
    """Select the primary slice and build the :class:`MachOInfo`."""
    if not slices:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "fat container declares no slices",
            reason="empty_fat_container",
            source=source,
        )
    primary = next((s for s in slices if s.is_arm64), slices[0])
    return MachOInfo(
        fat=True,
        slices=slices,
        primary=primary,
        source=source,
        size_bytes=size,
        is_64_fat=is_64,
    )


def _parse_thin(
    buf: bytes,
    *,
    offset: int,
    size: int,
    source: str,
    cputype: int | None = None,
    cpusubtype: int | None = None,
) -> SliceInfo:
    """Decode one thin Mach-O image from ``buf``.

    ``cputype``/``cpusubtype`` are supplied by the fat container when present
    (the authoritative values for a slice); they are read from the thin
    header otherwise.
    """
    if len(buf) < 4:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "image too small to contain a Mach-O magic",
            reason="truncated",
            source=source,
            size_bytes=len(buf),
        )
    magic = struct.unpack_from("<I", buf, 0)[0]
    if magic == MH_MAGIC_64:
        endian, is_64 = "<", True
    elif magic == MH_CIGAM_64:
        endian, is_64 = ">", True
    elif magic == MH_MAGIC:
        endian, is_64 = "<", False
    elif magic == MH_CIGAM:
        endian, is_64 = ">", False
    else:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "not a Mach-O image (unrecognised magic)",
            reason="bad_magic",
            source=source,
            magic=f"0x{magic:08x}",
        )

    header_size = 32 if is_64 else 28
    if len(buf) < header_size:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "image too small to contain a Mach-O header",
            reason="truncated",
            source=source,
            size_bytes=len(buf),
            required_bytes=header_size,
        )

    if is_64:
        (
            _magic,
            header_cputype,
            header_cpusubtype,
            filetype,
            ncmds,
            sizeofcmds,
            _flags,
            _reserved,
        ) = struct.unpack_from(endian + "8I", buf, 0)
    else:
        (
            _magic,
            header_cputype,
            header_cpusubtype,
            filetype,
            ncmds,
            sizeofcmds,
            _flags,
        ) = struct.unpack_from(endian + "7I", buf, 0)

    resolved_cputype = header_cputype if cputype is None else cputype
    resolved_cpusubtype = header_cpusubtype if cpusubtype is None else cpusubtype

    info = SliceInfo(
        cputype=resolved_cputype,
        cpusubtype=resolved_cpusubtype,
        offset=offset,
        size=size,
        filetype=filetype,
        filetype_name=FILETYPE_NAMES.get(filetype, f"filetype_{filetype}"),
        is_64=is_64,
        endian=endian,
    )

    # Trust ``sizeofcmds`` only while it fits in the bytes we actually hold;
    # a corrupt value must not stop the header facts from being reported.
    bound = len(buf)
    declared_end = header_size + sizeofcmds
    if declared_end <= bound:
        bound = declared_end

    pos = header_size
    seen = 0
    while pos + 8 <= bound:
        cmd, cmdsize = struct.unpack_from(endian + "II", buf, pos)
        if cmdsize < 8 or pos + cmdsize > len(buf):
            info.parse_notes.append(
                f"malformed load command at image+{offset + pos} (cmd=0x{cmd:08x}, cmdsize={cmdsize})"
            )
            break
        payload = buf[pos : pos + cmdsize]
        _apply_command(info, cmd, payload, endian)
        pos += cmdsize
        seen += 1
    if seen == 0:
        info.parse_notes.append("no load commands decoded")
    elif ncmds and seen < ncmds:
        info.parse_notes.append(f"decoded {seen} of {ncmds} declared load commands")
    return info


def _apply_command(
    info: SliceInfo,
    cmd: int,
    payload: bytes,
    endian: str,
) -> None:
    """Fold one load command into ``info``.

    Unknown commands are ignored (Mach-O images legitimately carry commands
    this precheck does not care about); malformed *known* commands are noted.
    """
    body = payload[8:]  # skip cmd + cmdsize

    if _cmd_matches(cmd, LC_BUILD_VERSION):
        if len(body) >= 12:
            platform, minos, sdk = struct.unpack_from(endian + "III", body, 0)
            info.platform = platform
            info.platform_name = platform_name(platform)
            info.minos = _version_tuple(minos)
            info.sdk = _version_tuple(sdk)
        else:
            info.parse_notes.append("short LC_BUILD_VERSION payload")
        return

    if _cmd_matches(cmd, LC_VERSION_MIN_MACOSX):
        _apply_version_min(info, body, endian, PLATFORM_MACOS)
        return
    if _cmd_matches(cmd, LC_VERSION_MIN_IPHONEOS):
        _apply_version_min(info, body, endian, PLATFORM_IOS)
        return
    if _cmd_matches(cmd, LC_VERSION_MIN_TVOS):
        _apply_version_min(info, body, endian, PLATFORM_TVOS)
        return
    if _cmd_matches(cmd, LC_VERSION_MIN_WATCHOS):
        _apply_version_min(info, body, endian, PLATFORM_WATCHOS)
        return

    if _cmd_matches(cmd, LC_SEGMENT_64):
        if len(body) >= 56:
            (
                segname,
                vmaddr,
                vmsize,
                fileoff,
                filesize,
                _maxprot,
                _initprot,
                _nsects,
                _segflags,
            ) = struct.unpack_from(endian + "16sQQQQIIII", body, 0)
            info.segments.append(
                SegmentInfo(
                    name=_decode_segname(segname),
                    vmaddr=vmaddr,
                    vmsize=vmsize,
                    fileoff=fileoff,
                    filesize=filesize,
                )
            )
        else:
            info.parse_notes.append("short LC_SEGMENT_64 payload")
        return

    if _cmd_matches(cmd, LC_SEGMENT):
        if len(body) >= 44:
            (
                segname,
                vmaddr,
                vmsize,
                fileoff,
                filesize,
                _maxprot,
                _initprot,
                _nsects,
                _segflags,
            ) = struct.unpack_from(endian + "16sIIIIIIII", body, 0)
            info.segments.append(
                SegmentInfo(
                    name=_decode_segname(segname),
                    vmaddr=vmaddr,
                    vmsize=vmsize,
                    fileoff=fileoff,
                    filesize=filesize,
                )
            )
        else:
            info.parse_notes.append("short LC_SEGMENT payload")
        return

    if _cmd_matches(cmd, LC_ENCRYPTION_INFO_64):
        if len(body) >= 16:
            cryptoff, cryptsize, cryptid, _pad = struct.unpack_from(endian + "IIII", body, 0)
            _apply_encryption(info, cryptoff, cryptsize, cryptid)
        else:
            info.parse_notes.append("short LC_ENCRYPTION_INFO_64 payload")
        return

    if _cmd_matches(cmd, LC_ENCRYPTION_INFO):
        if len(body) >= 12:
            cryptoff, cryptsize, cryptid = struct.unpack_from(endian + "III", body, 0)
            _apply_encryption(info, cryptoff, cryptsize, cryptid)
        else:
            info.parse_notes.append("short LC_ENCRYPTION_INFO payload")
        return

    if _cmd_matches(cmd, LC_LOAD_DYLIB) or _cmd_matches(cmd, LC_LOAD_WEAK_DYLIB):
        name = _read_dylib_name(payload, endian)
        if name:
            info.dylibs.append(name)
        return
    if _cmd_matches(cmd, LC_REEXPORT_DYLIB) or _cmd_matches(cmd, LC_LOAD_UPWARD_DYLIB):
        name = _read_dylib_name(payload, endian)
        if name:
            info.dylibs.append(name)
        return
    if _cmd_matches(cmd, LC_ID_DYLIB):
        name = _read_dylib_name(payload, endian)
        if name:
            info.install_name = name
        return

    if _cmd_matches(cmd, LC_RPATH):
        if len(body) >= 4:
            path_off = struct.unpack_from(endian + "I", body, 0)[0]
            path = _read_cstring_at(payload, path_off)
            if path:
                info.rpaths.append(path)
        return

    if _cmd_matches(cmd, LC_CODE_SIGNATURE):
        if len(body) >= 8:
            dataoff, datasize = struct.unpack_from(endian + "II", body, 0)
            info.has_code_signature = True
            info.code_signature_offset = dataoff
            info.code_signature_size = datasize
        else:
            info.parse_notes.append("short LC_CODE_SIGNATURE payload")
        return

    if _cmd_matches(cmd, LC_UUID):
        if len(body) >= 16:
            info.uuid = body[:16].hex()
        return

    # LC_SYMTAB / LC_UNIXTHREAD / LC_DYSYMTAB / anything else: not needed by
    # the precheck, and silently ignored on purpose.


def _cmd_matches(cmd: int, base: int) -> bool:
    """Compare a load command against its base value, tolerating LC_REQ_DYLD."""
    return cmd == base or cmd == (base | LC_REQ_DYLD)


def _apply_version_min(
    info: SliceInfo,
    body: bytes,
    endian: str,
    platform: int,
) -> None:
    """Fold ``LC_VERSION_MIN_*`` into the slice (only when no LC_BUILD_VERSION)."""
    if info.platform != PLATFORM_UNKNOWN:
        return  # LC_BUILD_VERSION is authoritative and wins
    if len(body) < 8:
        info.parse_notes.append("short LC_VERSION_MIN payload")
        return
    version, sdk = struct.unpack_from(endian + "II", body, 0)
    info.platform = platform
    info.platform_name = platform_name(platform)
    info.minos = _version_tuple(version)
    info.sdk = _version_tuple(sdk)


def _apply_encryption(info: SliceInfo, cryptoff: int, cryptsize: int, cryptid: int) -> None:
    """Record ``LC_ENCRYPTION_INFO(_64)`` facts (PRD 5.1 / 10)."""
    info.cryptoff = cryptoff
    info.cryptsize = cryptsize
    info.cryptid = cryptid
    info.encrypted = cryptid != 0


def _version_tuple(value: int) -> tuple[int, int, int]:
    """Decode a packed ``x.y.z`` Mach-O version into a tuple."""
    return ((value >> 16) & 0xFFFF, (value >> 8) & 0xFF, value & 0xFF)


def _decode_segname(raw: bytes) -> str:
    """Decode a fixed 16-byte segment name field."""
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _read_dylib_name(payload: bytes, endian: str) -> str | None:
    """Read the install name out of a ``dylib`` load command payload."""
    if len(payload) < 12:
        return None
    name_off = struct.unpack_from(endian + "I", payload, 8)[0]
    return _read_cstring_at(payload, name_off)


def _read_cstring_at(payload: bytes, offset: int) -> str | None:
    """Read a NUL-terminated string whose offset is relative to ``payload``."""
    if offset < 0 or offset >= len(payload):
        return None
    end = payload.find(b"\x00", offset)
    if end == -1:
        end = len(payload)
    try:
        return payload[offset:end].decode("utf-8")
    except UnicodeDecodeError:
        return payload[offset:end].decode("utf-8", errors="replace")
