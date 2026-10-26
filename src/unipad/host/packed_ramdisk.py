"""The packed Data ramdisk format, as two independent implementations read it.

Why this exists
---------------
The guest's Data volume is not a file on a disk: it is a *packed* ramdisk the
bootloader copies into the kernel's window and the kernel's block driver
expands.  Two pieces of code parse that blob and neither can see the other:

* ``vendor/UniXNU/drivers/DarwinBoot/DarwinBootPkg/Driver/PackedRamdisk.h`` --
  ``RamdiskLayout``, in the UEFI driver, which decides whether DarwinBoot will
  accept the file at all and whether to keep it packed;
* ``vendor/UniXNU/drivers/NativeRamBlock/SparseDisk.h`` -- ``ramPackedLayout``,
  in the kernel driver, which decides whether the pages it was handed are a
  disk it can publish.

They agree on every rule, which is the only reason a seed can cross the
boundary at all -- but nothing checks that they agree, and a blob one accepts
and the other rejects is a guest that boots and then finds no Data volume.
This module is that check on the host side, where a refusal costs a second
instead of a boot.

The format (``PackedRamdisk.h:2-4``, verbatim)::

    Little-endian DBRDISK1: version, page size, expanded bytes, extent count;
    then sorted (destination offset, byte count) pairs and concatenated bytes.
    Omitted ranges are real zeroed RAM, not virtual/simulated disk storage.

so a 1 GiB volume with four dirty regions is a 36 KB file, and every byte the
seed does not carry is a real zero page in the guest.  That is why the file is
small and the disk is not, and why the *logical* size -- not the file size --
is what the guest's storage stack sees.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

from unipad.errors import ErrorCode, UniPadError

#: Both parsers refuse a file larger than this.  ``PackedRamdisk.h`` spells it
#: ``SIZE_256MB`` and ``SparseDisk.h`` spells it ``0x10000000``; they are the
#: same number, so the tighter of the two is not a judgement call.
MAX_PACKED_BYTES = 256 * 1024 * 1024

#: ``SIZE_MAX_LOGICAL`` in the bootloader, ``1ULL << 40`` in the kernel.  A
#: packed disk's logical size is bounded by the disk format rather than by
#: memory, so this is the only ceiling on how large a volume a 36 KB seed can
#: describe.
MAX_LOGICAL_BYTES = 1 << 40

#: ``EFI_PAGE_SIZE`` in the bootloader and the literal ``4096`` in the kernel.
PAGE_SIZE = 4096

#: Both parsers cap the extent count at 65536.
MAX_EXTENTS = 65536

#: The magic both compare against with ``CompareMem``/``memcmp``.
MAGIC = b"DBRDISK1"

#: The version both require.
VERSION = 1

#: The header both read: magic, version, page size, logical bytes, extent count.
HEADER = struct.Struct("<8sIIQQ")
HEADER_SIZE = HEADER.size


@dataclass(frozen=True)
class Extent:
    """One dirty region: ``length`` bytes at ``offset`` in the logical disk."""

    offset: int
    length: int


@dataclass(frozen=True)
class PackedRamdisk:
    """A validated seed, described the way both parsers see it."""

    #: The file's size in bytes.
    size: int
    #: ``sha256`` of the whole file, so a seed can be identified in evidence.
    sha256: str
    #: The disk's logical size -- what the guest's storage stack sees.
    logical: int
    #: The dirty regions, in file order (which is also sorted order).
    extents: tuple[Extent, ...]
    #: The bytes the file actually carries: the sum of the extent lengths.
    resident: int

    @property
    def sparse(self) -> bool:
        """Whether the seed carries less than the whole disk."""
        return self.resident < self.logical


def validate(blob: bytes, *, source: str = "") -> PackedRamdisk:
    """Check ``blob`` against both parsers' rules and describe it.

    Args:
        blob: The candidate seed.
        source: Where it came from, for the error message only.

    Returns:
        The validated seed.

    Raises:
        UniPadError: ``artifact_manifest_invalid`` on the first rule either
            parser would reject.  The message names the rule, because "the
            seed is invalid" is not something anyone can act on.
    """
    where = {"source": source} if source else {}

    def refuse(reason: str) -> None:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            f"the packed Data ramdisk is not a valid seed: {reason}",
            **where,
        )

    if len(blob) > MAX_PACKED_BYTES:
        refuse(f"{len(blob)} bytes exceeds the {MAX_PACKED_BYTES}-byte ceiling "
               "both parsers enforce")

    if len(blob) < HEADER_SIZE:
        refuse(f"{len(blob)} bytes is shorter than the {HEADER_SIZE}-byte header")

    magic, version, page, logical, count = HEADER.unpack_from(blob, 0)
    if magic != MAGIC:
        refuse(f"magic {magic!r} is not {MAGIC!r}")
    if version != VERSION:
        refuse(f"version {version} is not {VERSION}")
    if page != PAGE_SIZE:
        refuse(f"page size {page} is not {PAGE_SIZE}")

    if logical == 0 or logical > MAX_LOGICAL_BYTES or logical % PAGE_SIZE:
        refuse(f"logical size {logical} is zero, over {MAX_LOGICAL_BYTES}, "
               f"or not {PAGE_SIZE}-aligned")
    if count > MAX_EXTENTS or count > (len(blob) - HEADER_SIZE) // 16:
        refuse(f"extent count {count} does not fit a {len(blob)}-byte file")

    extents: list[Extent] = []
    cursor = HEADER_SIZE + count * 16
    end = 0
    for index in range(count):
        offset, length = struct.unpack_from("<QQ", blob, HEADER_SIZE + index * 16)
        if length == 0:
            refuse(f"extent {index} is empty")
        if offset % PAGE_SIZE or length % PAGE_SIZE:
            refuse(f"extent {index} is not {PAGE_SIZE}-aligned")
        if offset < end:
            refuse(f"extent {index} starts at {offset}, before the previous "
                   f"extent ends at {end}")
        if offset > logical or length > logical - offset:
            refuse(f"extent {index} runs past the {logical}-byte logical size")
        if length > len(blob) - cursor:
            refuse(f"extent {index} needs {length} bytes but only "
                   f"{len(blob) - cursor} remain")
        end = offset + length
        cursor += length
        extents.append(Extent(offset=offset, length=length))

    # Both parsers end here, and it is the check that catches a truncated file
    # or a header that lied about the extent count: the table plus the payload
    # has to account for every byte.
    if cursor != len(blob):
        refuse(f"the extent table and payload account for {cursor} bytes but "
               f"the file is {len(blob)}")

    return PackedRamdisk(
        size=len(blob),
        sha256=hashlib.sha256(blob).hexdigest(),
        logical=logical,
        extents=tuple(extents),
        resident=sum(extent.length for extent in extents),
    )


def validate_file(path: Path) -> PackedRamdisk:
    """:func:`validate` for a path, reading it once.

    Raises:
        UniPadError: ``artifact_missing`` when the file is not there, so a
            caller does not have to distinguish "absent" from "invalid".
    """
    path = Path(path)
    if not path.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "the packed Data ramdisk is not at the path given",
            source=str(path),
        )
    return validate(path.read_bytes(), source=str(path))
