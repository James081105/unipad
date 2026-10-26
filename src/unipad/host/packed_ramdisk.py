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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Sequence

from unipad.errors import ErrorCode, UniPadError
from unipad.host.paths import safe_location

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


def pack(disk: bytes, *, page: int = PAGE_SIZE) -> bytes:
    """Pack a whole disk image into a seed, keeping only its nonzero pages.

    The inverse of expanding: every maximal run of pages that are not all zero
    becomes one extent, and the payload is those runs concatenated in order.  The
    granularity is the page, so a page holding a single nonzero byte is stored
    whole -- 4096 bytes kept for the one that matters -- and a page that is all
    zero ends the run it sits in.

    A run of 1 TiB of zeroes packs to 32 bytes of header and no extents, which
    is the whole point of the format: a sparse disk is described by its dirty
    pages, not by its size.

    Raises:
        ValueError: when ``page`` is not the page size both parsers require,
            when ``disk`` is not a whole number of pages, or when the packed
            result would exceed the ceiling both parsers enforce.
    """
    _refuse_foreign_page(page)
    if len(disk) % page:
        raise ValueError(
            f"the disk is {len(disk)} bytes, not a multiple of the {page}-byte page"
        )

    extents = list(_dirty_extents(_pages_of(disk, page), page))

    if len(extents) > MAX_EXTENTS:
        raise ValueError(
            f"{len(extents)} extents exceeds the {MAX_EXTENTS} both parsers allow"
        )

    header = struct.pack("<8sIIQQ", MAGIC, VERSION, page, len(disk), len(extents))
    table = b"".join(struct.pack("<QQ", offset, length) for offset, length in extents)
    payload = b"".join(disk[offset:offset + length] for offset, length in extents)
    blob = header + table + payload
    if len(blob) > MAX_PACKED_BYTES:
        raise ValueError(
            f"packing would produce {len(blob)} bytes, over the "
            f"{MAX_PACKED_BYTES}-byte ceiling both parsers enforce"
        )
    return blob


#: How much of the disk image :func:`pack_file` holds in memory at a time.
#: 1 MiB is 256 pages: enough that finding extent boundaries costs one read per
#: megabyte, small enough that a 5.4 GB OS-root image costs 5.4 GB of I/O and
#: this much memory.  It is deliberately unrelated to the 256 MiB *output*
#: ceiling, which is a property of the format rather than of this buffer.
READ_BUFFER_BYTES = 1 << 20


def _refuse_foreign_page(page: int) -> None:
    """Both parsers compare the header's page size against a fixed 4096."""
    if page != PAGE_SIZE:
        raise ValueError(
            f"page size {page} is not {PAGE_SIZE}: both parsers compare the "
            "header's page size against a fixed EFI_PAGE_SIZE"
        )


def _pages_of(disk: bytes, page: int) -> Iterator[bytes]:
    """An image already in memory, as a sequence of pages."""
    for offset in range(0, len(disk), page):
        yield disk[offset:offset + page]


def _pages_of_file(handle: BinaryIO, size: int, page: int, stride: int) -> Iterator[bytes]:
    """An image on disk, as a sequence of pages, ``stride`` bytes at a time.

    The whole point of :func:`pack_file`: the image is never resident, so the
    caller's memory is the ``stride``-sized buffer plus the extent table, and a
    5.4 GB OS-root image is packable on a host that could not hold it.

    ``size`` is what the caller already stat'ed, so the pages yielded are exactly
    the ones that describe that size -- a file that shrank between the two reads
    would otherwise leave the header naming a logical size the file no longer
    has, and nothing downstream could detect it.
    """
    handle.seek(0)
    remaining = size
    while remaining:
        chunk = handle.read(min(stride, remaining))
        if not chunk:
            raise UniPadError(
                ErrorCode.VERIFICATION_FAILED,
                "the disk image is shorter than it was when its size was read, "
                "so it cannot be packed: the header would name a logical size "
                "the file no longer has",
                expected=size,
            )
        for at in range(0, len(chunk), page):
            yield chunk[at:at + page]
        remaining -= len(chunk)


def _dirty_extents(pages: Iterator[bytes], page: int) -> Iterator[tuple[int, int]]:
    """Every maximal run of nonzero pages in ``pages``, as ``(offset, length)``.

    The one definition of "dirty", shared by :func:`pack` and
    :func:`pack_file`.  Two copies of this loop would be two answers to where an
    extent starts and ends, and a seed that unpacks to a different disk depending
    on which function made it is a defect nothing downstream can see: both seeds
    validate, both boot, and only one of them is the disk that was packed.

    The granularity is the whole page: a page with a single nonzero byte anywhere
    in it is dirty and all ``page`` bytes of it are stored, which is why a run of
    pages that are *mostly* zero still costs whole pages.  A page that is all zero
    ends the run it sits in, because that is the entire distinction between RAM
    the guest wrote and RAM it never did.
    """
    # Compared against the zero page rather than scanned with ``any``.  ``any``
    # walks the page through the iterator protocol and costs about 20 s per
    # gibibyte of image -- most of the packing time for a sparse volume -- while
    # ``data != empty`` is one memcmp.  The operands are always the same length,
    # because both packers refuse an image that is not a whole number of pages,
    # so this is a content test and never a length test.
    empty = b"\0" * page
    start: Optional[int] = None
    offset = 0
    for data in pages:
        if data != empty:
            if start is None:
                start = offset
        elif start is not None:
            yield start, offset - start
            start = None
        offset += page
    if start is not None:
        yield start, offset - start


def pack_file(
    source: Path,
    *,
    page: int = PAGE_SIZE,
    buffer_bytes: int = READ_BUFFER_BYTES,
) -> bytes:
    """:func:`pack` for a disk image on disk, without holding the image.

    Same output, byte for byte, as :func:`pack` on the same bytes -- the extent
    boundaries come from the same :func:`_dirty_extents` and the payload is the
    same pages in the same order.  What differs is the memory: the image is read
    twice (once to find the boundaries, once to collect the pages that are kept)
    instead of once into a buffer the size of the disk, because a Data volume is
    1 GiB and an OS root is 5.4 GB, and a host that packs either by reading it
    whole is a host that cannot pack either.

    The two passes are the reason this is not simply ``pack(path.read_bytes())``:
    pass one touches every page but keeps only the extent table, and pass two
    seeks to the extents it found and reads nothing else.  For a sparse disk that
    is most of the I/O saving; for a dense one it is the same bytes read twice,
    which is the price of not needing 5.4 GB of RAM.

    Raises:
        UniPadError: ``artifact_missing`` when the image is not there, the same
            distinction :func:`validate_file` makes; ``verification_failed`` when
            the image shrinks between the size check and the read.
        ValueError: for exactly the conditions :func:`pack` refuses, with the
            same messages, so a caller converts them in one place.
    """
    path = Path(source)
    if not path.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "the disk image is not at the path given",
            # Named the way ``adt.py`` names every path in an error detail: the
            # directory is what PRD 9.2 protects, and two candidates for the same
            # image always differ in their final component anyway.
            source=safe_location(path),
        )
    _refuse_foreign_page(page)
    size = path.stat().st_size
    if size % page:
        raise ValueError(
            f"the disk is {size} bytes, not a multiple of the {page}-byte page"
        )

    # A whole number of pages, so the stride splits into whole pages and the
    # page the reader hands ``_dirty_extents`` is always a full one.
    stride = page * max(1, buffer_bytes // page)
    extents: list[tuple[int, int]] = []
    with path.open("rb") as handle:
        for offset, length in _dirty_extents(_pages_of_file(handle, size, page, stride), page):
            extents.append((offset, length))
            # Checked as the extents arrive rather than after the last one,
            # because the alternative is walking a 1 TiB alternating disk to
            # discover something the 65537th extent already said.  The ceiling
            # on the *file* is checked once, below, where the exact size is
            # known -- the two are not the same bound and conflating them would
            # refuse a disk that fits.
            if len(extents) > MAX_EXTENTS:
                raise ValueError(
                    f"{len(extents)} extents exceeds the {MAX_EXTENTS} both parsers allow"
                )

        # Pass two: only the pages that were kept.
        blob = bytearray(struct.pack("<8sIIQQ", MAGIC, VERSION, page, size, len(extents)))
        blob += b"".join(struct.pack("<QQ", offset, length) for offset, length in extents)
        for offset, length in extents:
            handle.seek(offset)
            payload = handle.read(length)
            if len(payload) != length:
                raise UniPadError(
                    ErrorCode.VERIFICATION_FAILED,
                    "the disk image is shorter than it was when its size was read, "
                    "so it cannot be packed: the header would name a logical size "
                    "the file no longer has",
                    expected=size,
                    offset=offset,
                )
            blob += payload

    if len(blob) > MAX_PACKED_BYTES:
        raise ValueError(
            f"packing would produce {len(blob)} bytes, over the "
            f"{MAX_PACKED_BYTES}-byte ceiling both parsers enforce"
        )
    return bytes(blob)


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command line: ``python -m unipad.host.packed_ramdisk --disk IMG --out SEED.dbr``.

    The packing step as a repo-level operation rather than a scratch script.
    ``scripts/build-os-root.py``'s last step is exactly this, and the macOS
    runner performs it through the device repository's own packer
    (``DarwinBoot/Test/packed_ramdisk.py``); a Windows host that has to pack a
    Data volume by hand had neither.

    ``--disk`` is a *whole disk image*, not a volume, and it is read in bounded
    chunks -- see :func:`pack_file`.  It is never loaded into memory, so a 1 GiB
    Data volume and a 5.4 GB OS root are the same operation at different speeds.

    The seed is validated before a byte of it is written, against the same rules
    both the bootloader and the kernel driver apply, so a seed either of them
    would refuse costs a second here instead of a boot.

    Raises:
        UniPadError: ``config_invalid`` when the image cannot be packed at all
            -- not a whole number of pages, or too dense to fit the 256 MiB seed
            ceiling.  It is ``config_invalid`` rather than
            ``artifact_manifest_invalid`` because the fault is in the *input*,
            not in a seed that failed to parse: retrying can never make a 5.4 GB
            image of live APFS data fit in 256 MiB, and a caller that branched on
            the manifest code and retried would retry forever.  ``build_argv``
            makes the same distinction for an empty root volume
            (``unixnu_boot.py``).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m unipad.host.packed_ramdisk",
        description=(
            "Pack a whole disk image into the packed Data ramdisk (.dbr) that "
            "DarwinBoot copies into the kernel's window and the kernel's ram "
            "block driver expands, keeping only its nonzero pages."
        ),
    )
    parser.add_argument(
        "--disk",
        type=Path,
        required=True,
        help="the whole disk image to pack; read in bounded chunks, so it may be "
             "far larger than memory",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="where to write the .dbr seed",
    )
    args = parser.parse_args(argv)

    try:
        blob = pack_file(args.disk)
    except ValueError as exc:
        # pack()'s contract is a bare ValueError, and it is kept: the module's
        # own tests pin it, and a library caller can catch it without importing
        # the error taxonomy.  The conversion happens here, at the boundary
        # where a ValueError would otherwise reach an operator as a traceback.
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"the disk image cannot be packed: {exc}",
            disk=safe_location(args.disk),
            reason=str(exc),
        ) from exc

    # Before anything is written.  A seed this refuses is a defect in the packer
    # above, and the only place it can be caught is here.
    found = validate(blob, source=safe_location(args.out))
    if args.out.parent and not args.out.parent.exists():
        args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(blob)

    print(f"packed data ramdisk: {found.size} bytes, sha256 {found.sha256}")
    print(
        f"  logical {found.logical} bytes; {len(found.extents)} extents; "
        f"{found.resident} bytes resident"
        + ("" if found.sparse else " (the whole disk)")
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    try:
        raise SystemExit(main())
    except UniPadError as failure:
        # One JSON object on stderr, so stdout stays the summary a caller reads
        # and the process still exits with the code that code maps to (PRD 9.1).
        print(failure.to_json(), file=sys.stderr)
        raise SystemExit(int(failure.exit_code)) from failure
