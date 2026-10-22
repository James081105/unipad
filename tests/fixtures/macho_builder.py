"""Deterministic Mach-O / universal-binary fixture builders (PRD 5.1 / 10).

The IPA precheck (:mod:`unipad.ipa.macho`, :mod:`unipad.ipa.package`,
:mod:`unipad.ipa.precheck`) has to be exercised with images that are
*structurally valid* -- a garbage blob only proves that the parser rejects
garbage.  These builders emit real little-endian Mach-O headers with real
load commands, so a fixture that the precheck accepts is accepted for the
right reasons (a genuine ``LC_BUILD_VERSION`` platform, a genuine
``LC_ENCRYPTION_INFO_64`` cryptid, genuine ``LC_LOAD_DYLIB`` names).

Everything is produced from :mod:`unipad.ipa.macho` constants rather than
re-declared literals: if the parser's view of the format is wrong, the
fixture must be wrong in the *same* way so the unit tests catch it instead
of silently papering over it (CLAUDE.md "Failing Test Before Fixing Code").

.. warning::

   ``LC_BUILD_VERSION`` platform numbers are **not** 1-based: ``1`` is
   ``macOS``, ``2`` is ``iOS``, ``7`` is ``iPhoneSimulator``
   (``loader.h`` / ``ld.hpp``, and ``unipad.ipa.macho`` follows that
   contract).  Passing ``platform=1`` builds a *macOS* image that the
   precheck correctly rejects with ``unsupported_platform``.  Always import
   :data:`~unipad.ipa.macho.PLATFORM_IOS` instead of hard-coding a number.

Determinism (testing-strategy D3): no clock, no randomness, no host state.
``build_thin_macho()`` called twice returns identical bytes, so fixture
hashes are reproducible.
"""

from __future__ import annotations

import struct
from typing import Sequence

from unipad.ipa.macho import (
    CPU_SUBTYPE_ARM64E,
    CPU_TYPE_ARM,
    CPU_TYPE_ARM64,
    CPU_TYPE_X86_64,
    FAT_MAGIC,
    LC_BUILD_VERSION,
    LC_CODE_SIGNATURE,
    LC_ENCRYPTION_INFO,
    LC_ENCRYPTION_INFO_64,
    LC_ID_DYLIB,
    LC_LOAD_DYLIB,
    LC_RPATH,
    MH_DYLIB,
    MH_EXECUTE,
    MH_MAGIC,
    MH_MAGIC_64,
    PLATFORM_IOS,
    PLATFORM_IOSSIMULATOR,
    PLATFORM_MACOS,
)

__all__ = [
    # re-exported so tests never hard-code a magic/arch/platform number
    "ARM64",
    "ARM32",
    "ARM64E_SUBTYPE",
    "X86_64",
    "FAT_MAGIC",
    "MH_MAGIC",
    "MH_MAGIC_64",
    "MH_EXECUTE",
    "MH_DYLIB",
    "PLATFORM_MACOS",
    "PLATFORM_IOS",
    "PLATFORM_IOSSIMULATOR",
    # builders
    "build_thin_macho",
    "build_fat",
    "pack_version",
    "align_up",
    # ready-made slices
    "default_device_slice",
    "simulator_slice",
    "x86_64_ios_slice",
    "arm64e_slice",
    "arm32_ios_slice",
    "dylib_slice",
    "device_fat",
    "mixed_platform_fat",
]

#: Architecture / platform aliases used by the fixtures.  These are the
#: authoritative values from :mod:`unipad.ipa.macho`; do not re-derive them.
ARM64 = CPU_TYPE_ARM64                  # 0x0100000C
ARM32 = CPU_TYPE_ARM                    # 0x0000000C (32-bit armv7/armv7s)
ARM64E_SUBTYPE = CPU_SUBTYPE_ARM64E     # 2 (low subtype bits of arm64e)
X86_64 = CPU_TYPE_X86_64                # 0x01000007

#: Realistic dyld dependencies of a minimal UIKit app (PRD 5.1: the linked
#: dylibs must be reported, so a fixture needs some).
DEFAULT_DEVICE_DYLIBS: tuple[str, ...] = (
    "/System/Library/Frameworks/UIKit.framework/UIKit",
    "/System/Library/Frameworks/Foundation.framework/Foundation",
    "/usr/lib/libobjc.A.dylib",
)

#: ``@rpath`` an embedded framework declares as its install name.
FRAMEWORK_INSTALL_NAME = "@rpath/F.framework/F"

#: ``MH_NOUNDEFS | MH_DYLDLINK | MH_TWOLEVEL | MH_PIE`` -- what a modern
#: Xcode-produced iOS executable carries.  The parser ignores flags; they
#: are here so the image is not obviously fake to a human reader.
_DEFAULT_FLAGS = 0x1 | 0x4 | 0x80 | 0x200000

#: Trailing bytes appended after the load commands so the image has a body
#: (and so ``LC_CODE_SIGNATURE`` points at something real).
_DEFAULT_BODY_SIZE = 512

#: Size of the placeholder code signature blob appended to the image.
_SIGNATURE_SIZE = 32


def pack_version(version: int | Sequence[int]) -> int:
    """Encode ``(major, minor, patch)`` as Mach-O's packed ``x.y.z`` value.

    An ``int`` is passed through unchanged so a caller that already has the
    packed form (e.g. ``0x00110000``) does not have to unpack it.
    """
    if isinstance(version, int):
        return version
    parts = tuple(version) + (0, 0, 0)
    major, minor, patch = parts[0], parts[1], parts[2]
    return ((int(major) & 0xFFFF) << 16) | ((int(minor) & 0xFF) << 8) | (int(patch) & 0xFF)


def align_up(value: int, alignment: int) -> int:
    """Round ``value`` up to the next multiple of ``alignment`` (>= 1)."""
    if alignment <= 1:
        return value
    remainder = value % alignment
    return value if remainder == 0 else value + (alignment - remainder)


def _lc_build_version(
    platform: int,
    minos: int | Sequence[int],
    sdk: int | Sequence[int],
    endian: str = "<",
) -> bytes:
    """``LC_BUILD_VERSION`` (0x32): platform + minos + sdk + ntools."""
    body = struct.pack(endian + "IIII", platform, pack_version(minos), pack_version(sdk), 0)
    return struct.pack(endian + "II", LC_BUILD_VERSION, 8 + len(body)) + body


def _lc_encryption_info_64(cryptoff: int, cryptsize: int, cryptid: int, endian: str = "<") -> bytes:
    """``LC_ENCRYPTION_INFO_64`` (0x2C): cryptoff/cryptsize/cryptid/pad."""
    body = struct.pack(endian + "IIII", cryptoff, cryptsize, cryptid, 0)
    return struct.pack(endian + "II", LC_ENCRYPTION_INFO_64, 8 + len(body)) + body


def _lc_encryption_info_32(cryptoff: int, cryptsize: int, cryptid: int, endian: str = "<") -> bytes:
    """``LC_ENCRYPTION_INFO`` (0x21): the 32-bit variant (no ``pad`` field)."""
    body = struct.pack(endian + "III", cryptoff, cryptsize, cryptid)
    return struct.pack(endian + "II", LC_ENCRYPTION_INFO, 8 + len(body)) + body


def _lc_dylib(
    name: str,
    command: int,
    *,
    timestamp: int = 2,
    current_version: int = 0x00010000,
    compatibility_version: int = 0x00010000,
    endian: str = "<",
) -> bytes:
    """``LC_LOAD_DYLIB`` / ``LC_ID_DYLIB`` with a NUL-terminated install name.

    ``dylib_command`` is ``cmd, cmdsize, lc_str name, timestamp,
    current_version, compatibility_version``; the string offset is relative
    to the *start of the command*, i.e. 24 for a 64-bit image.
    """
    name_offset = 24
    body = struct.pack(
        endian + "IIII", name_offset, timestamp, current_version, compatibility_version
    ) + name.encode("utf-8") + b"\x00"
    body += b"\x00" * ((-len(body)) % 8)  # 64-bit commands are 8-byte aligned
    return struct.pack(endian + "II", command, 8 + len(body)) + body


def _lc_rpath(path: str, endian: str = "<") -> bytes:
    """``LC_RPATH`` (0x1C) with a NUL-terminated path string."""
    path_offset = 12
    body = struct.pack(endian + "I", path_offset) + path.encode("utf-8") + b"\x00"
    body += b"\x00" * ((-len(body)) % 8)
    return struct.pack(endian + "II", LC_RPATH, 8 + len(body)) + body


def _lc_code_signature(dataoff: int, datasize: int, endian: str = "<") -> bytes:
    """``LC_CODE_SIGNATURE`` (0x1D): dataoff/datasize placeholder."""
    body = struct.pack(endian + "II", dataoff, datasize)
    return struct.pack(endian + "II", LC_CODE_SIGNATURE, 8 + len(body)) + body


def build_thin_macho(
    cputype: int = ARM64,
    cpusubtype: int = 0,
    platform: int = PLATFORM_IOS,
    minos: Sequence[int] = (17, 0),
    encrypted: bool = False,
    cryptid: int = 0,
    dylibs: Sequence[str] = (),
    has_code_signature: bool = True,
    *,
    sdk: Sequence[int] = (17, 0),
    rpaths: Sequence[str] = (),
    install_name: str | None = None,
    filetype: int = MH_EXECUTE,
    flags: int = _DEFAULT_FLAGS,
    body_size: int = _DEFAULT_BODY_SIZE,
    is_64: bool = True,
    endian: str = "<",
) -> bytes:
    """Build a structurally valid thin 64-bit Mach-O image.

    Args:
        cputype: CPU type (``ARM64`` or ``X86_64``; see the module warning
            about platforms).
        cpusubtype: CPU subtype; ``ARM64E_SUBTYPE`` yields ``arm64e``.
        platform: ``LC_BUILD_VERSION`` platform number.  **iOS is 2.**
        minos: Minimum OS as ``(major, minor[, patch])``.
        encrypted: Set true for a FairPlay/DRM image (``cryptid`` becomes 1).
        cryptid: Explicit ``cryptid``.  A non-zero value with
            ``encrypted=False`` is a contradiction and raises ``ValueError``
            rather than silently producing an image that disagrees with the
            caller's stated intent.
        dylibs: Install names for one ``LC_LOAD_DYLIB`` each, in order.
        has_code_signature: Emit an ``LC_CODE_SIGNATURE`` command plus a
            placeholder blob at the declared offset.
        sdk: SDK version packed the same way as ``minos``.
        rpaths: Paths for one ``LC_RPATH`` each.
        install_name: When set, emit ``LC_ID_DYLIB`` (a dylib/framework).
        filetype: ``MH_EXECUTE`` (default) or ``MH_DYLIB``.
        flags: Mach-O header flags.
        body_size: Trailing body bytes after the load commands.
        is_64: Emit a 64-bit image (``MH_MAGIC_64``, 32-byte header) or a
            32-bit one (``MH_MAGIC``, 28-byte header, ``LC_ENCRYPTION_INFO``).
        endian: ``"<"`` (little-endian, every platform Apple still ships) or
            ``">"`` for the byte-swapped ``MH_CIGAM_64`` spelling the parser
            must still decode instead of rejecting as garbage.

    Returns:
        The complete image bytes: header, load commands with consistent
        ``ncmds``/``sizeofcmds``, body, optional signature blob.
    """
    if encrypted and int(cryptid) == 0:
        resolved_cryptid = 1
    elif not encrypted and int(cryptid) != 0:
        raise ValueError(
            f"contradictory arguments: encrypted={encrypted} but cryptid={cryptid}; "
            "pass encrypted=True for a FairPlay image, or cryptid=0 for a clear one"
        )
    else:
        resolved_cryptid = int(cryptid)

    commands: list[bytes] = [_lc_build_version(platform, minos, sdk, endian)]
    # The encryption command is always emitted: an unencrypted image then
    # carries an explicit ``cryptid == 0`` instead of merely omitting the
    # evidence, which is what the precheck reports on either way.
    if is_64:
        commands.append(_lc_encryption_info_64(0, 0, resolved_cryptid, endian))
    else:
        commands.append(_lc_encryption_info_32(0, 0, resolved_cryptid, endian))
    if install_name:
        commands.append(_lc_dylib(install_name, LC_ID_DYLIB, endian=endian))
    commands.extend(_lc_dylib(name, LC_LOAD_DYLIB, endian=endian) for name in dylibs)
    commands.extend(_lc_rpath(path, endian) for path in rpaths)

    sizeofcmds = sum(len(cmd) for cmd in commands)
    # ``LC_CODE_SIGNATURE`` is a fixed 16 bytes, so the signature offset can
    # be computed before the command itself is serialised.
    header_size = 32 if is_64 else 28
    signature_offset = header_size + sizeofcmds + 16 + body_size
    if has_code_signature:
        commands.append(_lc_code_signature(signature_offset, _SIGNATURE_SIZE, endian))
        sizeofcmds += len(commands[-1])

    magic = (MH_MAGIC_64 if is_64 else MH_MAGIC)
    if is_64:
        header = struct.pack(
            endian + "8I",
            magic,
            cputype,
            cpusubtype,
            filetype,
            len(commands),
            sizeofcmds,
            flags,
            0,  # reserved
        )
    else:
        header = struct.pack(
            endian + "7I",
            magic,
            cputype,
            cpusubtype,
            filetype,
            len(commands),
            sizeofcmds,
            flags,
        )
    prefix = header + b"".join(commands)
    if len(prefix) != header_size + sizeofcmds:  # pragma: no cover - builder self-check
        raise AssertionError("sizeofcmds must match the serialised load commands")
    image = bytearray(prefix + b"\x00" * body_size)
    if has_code_signature:
        if len(image) != signature_offset:  # pragma: no cover - builder self-check
            raise AssertionError("code signature offset must match the image body")
        image += b"\x00" * _SIGNATURE_SIZE
    return bytes(image)


def build_fat(slices: Sequence[bytes], align: int = 12) -> bytes:
    """Wrap thin images in a big-endian FAT_MAGIC universal container.

    Args:
        slices: Thin Mach-O images, in the order they should appear.  Each
            one's ``cputype``/``cpusubtype`` is read back from its own
            header so the container can never disagree with the slice.
        align: log2 of the slice alignment (``12`` == 4096 bytes, what
            ``lipo`` uses for iOS).

    Returns:
        The container bytes: ``FAT_MAGIC`` + ``nfat_arch`` + one
        ``fat_arch`` per slice, each slice padded to ``2**align``.

    Raises:
        ValueError: when ``slices`` is empty or an entry is not a thin
            64-bit Mach-O image (fail early rather than emit a container
            the parser would reject for a reason the caller cannot see).
    """
    if not slices:
        raise ValueError("build_fat needs at least one slice")
    alignment = 1 << int(align)
    header_size = 8 + 20 * len(slices)

    offsets: list[int] = []
    cursor = align_up(header_size, alignment)
    for slice_bytes in slices:
        offsets.append(cursor)
        cursor = align_up(cursor + len(slice_bytes), alignment)
    total = cursor

    container = bytearray(total)
    container[0:8] = struct.pack(">II", FAT_MAGIC, len(slices))
    for index, (slice_bytes, offset) in enumerate(zip(slices, offsets)):
        cputype, cpusubtype = _slice_arch(slice_bytes, index)
        entry = 8 + 20 * index
        container[entry : entry + 20] = struct.pack(
            ">IIIII", cputype, cpusubtype, offset, len(slice_bytes), align
        )
        container[offset : offset + len(slice_bytes)] = slice_bytes
    return bytes(container)


def _slice_arch(slice_bytes: bytes, index: int) -> tuple[int, int]:
    """Read ``(cputype, cpusubtype)`` from a thin image's own header."""
    if len(slice_bytes) < 16:
        raise ValueError(
            f"fat slice {index} is only {len(slice_bytes)} bytes; "
            "a thin Mach-O header needs at least 32"
        )
    magic = struct.unpack_from("<I", slice_bytes, 0)[0]
    if magic != MH_MAGIC_64:
        raise ValueError(
            f"fat slice {index} does not start with MH_MAGIC_64 "
            f"(found 0x{magic:08x}); build_fat only wraps 64-bit thin images"
        )
    cputype, cpusubtype = struct.unpack_from("<II", slice_bytes, 4)
    return cputype, cpusubtype


# --------------------------------------------------------------------------
# ready-made slices
# --------------------------------------------------------------------------


def default_device_slice() -> bytes:
    """A minimal iOS arm64 Device executable slice (the default fixture).

    Links :data:`DEFAULT_DEVICE_DYLIBS` (UIKit/Foundation/libobjc) so that
    package-level dylib reporting has real facts to report -- PRD 5.1 lists
    the linked dylibs among the things a precheck must inspect.  Use
    ``build_thin_macho()`` directly for a bare image with no dependencies.
    """
    return build_thin_macho(dylibs=DEFAULT_DEVICE_DYLIBS)


def simulator_slice() -> bytes:
    """An x86_64 iPhoneSimulator slice (must be rejected by the precheck)."""
    return build_thin_macho(cputype=X86_64, platform=PLATFORM_IOSSIMULATOR)


def x86_64_ios_slice() -> bytes:
    """An x86_64 slice that still declares the **iOS** platform.

    This is not something Apple ships; it exists so a test can isolate the
    *primary-slice selection* rule (arm64 must win) from the *platform* rule
    (a simulator slice is rejected).  Use :func:`simulator_slice` when the
    point of the test is the platform rejection.
    """
    return build_thin_macho(cputype=X86_64, platform=PLATFORM_IOS)


def arm64e_slice() -> bytes:
    """An arm64e Device slice (still ARM64, still acceptable)."""
    return build_thin_macho(cpusubtype=ARM64E_SUBTYPE)


def arm32_ios_slice() -> bytes:
    """A 32-bit ``armv7`` Device slice (``MH_MAGIC``, not ``MH_MAGIC_64``)."""
    return build_thin_macho(cputype=ARM32, cpusubtype=9, is_64=False)


def dylib_slice(name: str = FRAMEWORK_INSTALL_NAME) -> bytes:
    """A dylib/framework image (``MH_DYLIB`` + ``LC_ID_DYLIB``)."""
    return build_thin_macho(
        filetype=MH_DYLIB,
        install_name=name,
        dylibs=("/System/Library/Frameworks/Foundation.framework/Foundation",),
        has_code_signature=False,
    )


def device_fat() -> bytes:
    """An arm64 + x86_64 universal image where arm64 is the primary slice.

    Both slices declare the iOS platform so the precheck's *accept* path is
    exercised (see :func:`x86_64_ios_slice`).
    """
    return build_fat([default_device_slice(), x86_64_ios_slice()])


def mixed_platform_fat() -> bytes:
    """arm64 Device + x86_64 Simulator: accepted arch, rejected platform."""
    return build_fat([default_device_slice(), simulator_slice()])
