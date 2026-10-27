"""Reassemble the Data volume out of a guest-memory dump and pack it back.

Why this exists
---------------
Task #28 asks whether the Data volume's changes can be persisted host-side, and
the measured answer to "read it back by physical address" was no: the volume is
a *packed ramdisk* in guest RAM whose kernel driver keeps it as an array of
individually allocated 4 KiB pages (``SparseDisk.h``), so the pages are
scattered and a range read returns garbage.  What is left is the whole-memory
dump, and this module is the other end of it: find the one struct that describes
the volume, follow its pointers, and write the result back as a seed the guest
will accept.

The struct is found by signature, not by symbol, because neither it nor its page
array has one.  ``RamSparseDisk sparse`` is a *member* of the ``NativeRamBlock``
IOService object (``NativeRamBlock.cpp:55``) and ``sparse.pages`` is a separate
``IOMallocZero`` allocation, so nothing in the symbol table reaches either.  The
signature is the struct's shape, verbatim from ``SparseDisk.h``::

    struct RamSparseDisk {
      uint8_t **pages;         // offset 0
      uint64_t bytes, resident;// offsets 8 and 16
    };

so a candidate is 24 bytes whose field at offset 8 is the volume's logical size,
whose field at offset 0 is an address the dump actually maps, and whose field at
offset 16 is a sane resident count.  A second candidate is a refusal, not a
choice: two structs matching that shape means the signature is not specific
enough, and picking one is a guess that would produce a plausible-looking Data
volume built from the wrong pages.

The control is the seed's own identity.  ``NativeRamBlock.cpp:236-240`` hashes
the logical image page by page -- ``sparse.pages[i] ? pages[i] : zero`` -- into
``native_ram_block_seed_sha256``, so ``sha256`` of the reassembled image is
directly comparable to the 32 bytes the guest itself computed.  A reassembly
that hashes to anything else is a wrong reassembly, and no amount of it booting
makes it right.  :func:`verify_against_seed` is that comparison against the
seed on this host; the 32 bytes in the guest are the same comparison against the
guest.

The repacker is :func:`unipad.host.packed_ramdisk.pack`, not a second
implementation.  The extent rule -- a maximal run of nonzero pages -- is the one
thing two implementations cannot disagree about, because both seeds validate and
only one of them is the disk that was packed.  That module's docstring says so at
length; this one only has to not repeat the mistake.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence

from unipad.errors import ErrorCode, UniPadError
from unipad.host import packed_ramdisk
from unipad.host import ramdisk_dump as rd
from unipad.host.guestobserve import LoadAddresses, load_addresses_from_serial
from unipad.host.packed_ramdisk import PAGE_SIZE
from unipad.host.paths import safe_location

__all__ = [
    "DELIVERED_BYTES",
    "FIELD_BYTES",
    "FIELD_PAGES",
    "FIELD_RESIDENT",
    "STRUCT_SIZE",
    "SeedIdentity",
    "SparseDisk",
    "find_sparse_disk",
    "reassemble",
    "repack_packed",
    "unpack_packed",
    "verify_against_seed",
]


#: The delivered Data volume's logical size: 1 GiB.  This is the signature's
#: *search key*, not an assumption about what a Data volume is -- a volume of
#: another size is found by passing another value, and a caller that hardcodes
#: this one is a caller that cannot find a 2 GiB volume.  It is a default because
#: the delivered seed is 1 GiB, and nothing more than that.
DELIVERED_BYTES = 1 << 30

#: ``RamSparseDisk::pages`` -- an array of ``bytes / 4096`` pointers, each either
#: null or a separately allocated 4 KiB buffer.
FIELD_PAGES = 0

#: ``RamSparseDisk::bytes`` -- the volume's logical size.  The search key.
FIELD_BYTES = 8

#: ``RamSparseDisk::resident`` -- 4096 per allocated page, and never more than
#: ``bytes``.  Nonzero, because a volume with no resident pages is a volume the
#: driver never published.
FIELD_RESIDENT = 16

#: Three 8-byte fields, and no padding: the struct is exactly this large.
STRUCT_SIZE = 24


@dataclass(frozen=True)
class SparseDisk:
    """The ``RamSparseDisk`` as the guest holds it, plus the pointers it names.

    ``struct_at`` is where the struct was found, in the guest *virtual* address
    space -- the same space :meth:`GuestMemoryDump.read` takes, so a caller can
    go back and look at it.  ``pages_ptr`` is the value of the ``pages`` field,
    which is a virtual address too, because that is what the kernel stores.

    ``pages`` is the pointer array read out: one entry per page of the volume, in
    page order, zero where the guest never allocated.  Keeping the zeros matters,
    because page *index* is what places a page in the volume and a list of only
    the resident pages cannot say where any of them goes.
    """

    struct_at: int
    pages_ptr: int
    bytes: int
    resident: int
    pages: list[int]

    @property
    def page_count(self) -> int:
        """How many pages the volume has, allocated or not."""
        return self.bytes // PAGE_SIZE

    @property
    def resident_pages(self) -> int:
        """How many pages the guest actually allocated."""
        return sum(1 for address in self.pages if address)


@dataclass(frozen=True)
class SeedIdentity:
    """What a reassembled volume is, named the way the guest names it.

    ``logical_sha256`` is the digest ``NativeRamBlock.cpp:236-240`` computes, so
    it is the number to hold a reassembly against -- whether the other side is
    the seed on this host or the guest's own ``native_ram_block_seed_sha256``.
    """

    #: Where the seed came from, for the report only.
    source: str
    #: ``sha256`` of the packed seed file itself.
    seed_sha256: str
    #: ``sha256`` of the logical image the seed expands to -- the guest's number.
    logical_sha256: str
    #: The volume's logical size.
    logical_bytes: int
    #: The bytes the seed carries: the sum of its extent lengths.
    resident_bytes: int
    #: How many extents describe them.
    extent_count: int

    @property
    def guest_digest(self) -> bytes:
        """``logical_sha256`` as the 32 raw bytes a global would hold."""
        return bytes.fromhex(self.logical_sha256)


def find_sparse_disk(
    dump: GuestMemoryDump,
    *,
    logical_bytes: int = DELIVERED_BYTES,
) -> SparseDisk:
    """Find the one ``RamSparseDisk`` in ``dump`` and read its page array.

    The signature is three fields, and all three are checked: the logical size
    at offset 8 (which is what the scan keys on), a ``pages`` pointer the dump
    maps at offset 0, and a ``resident`` count at offset 16 that is nonzero,
    page-aligned and no larger than the volume.  A candidate that fails any of
    them is not a match, and the count of matches is reported either way.

    Args:
        dump: The guest-memory dump to search.
        logical_bytes: The volume's logical size, the signature's search key.

    Returns:
        The struct, with its page array read out.

    Raises:
        UniPadError: ``evidence_incomplete`` when the signature matches nothing
            or more than one thing, when the page array is not wholly mapped,
            or when ``resident`` disagrees with the array.  Every one of those
            is a refusal to guess: a wrong struct yields a plausible-looking
            volume assembled from the wrong pages, which is worse than no
            volume, because it would be believed.
    """
    matches = list(
        dump.find_struct(
            FIELD_BYTES,
            logical_bytes,
            validate=lambda at: _looks_like_sparse_disk(dump, at, logical_bytes),
        )
    )
    if len(matches) != 1:
        raise UniPadError(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "the guest memory dump does not hold exactly one RamSparseDisk of "
            f"{logical_bytes} bytes",
            logical_bytes=logical_bytes,
            matches=len(matches),
            candidates=[f"{address:#x}" for address in matches],
        )
    struct_at = matches[0]
    head = dump.read(struct_at, STRUCT_SIZE)
    # The struct's own field order, which is also the order ``SparseDisk.h``
    # declares them in: ``pages``, ``bytes``, ``resident``.
    pages_ptr = struct.unpack_from("<Q", head, FIELD_PAGES)[0]
    size = struct.unpack_from("<Q", head, FIELD_BYTES)[0]
    resident = struct.unpack_from("<Q", head, FIELD_RESIDENT)[0]

    count = size // PAGE_SIZE
    array = dump.read(pages_ptr, count * 8)
    pages = list(struct.unpack_from(f"<{count}Q", array))

    # ``ramSparseImport`` and ``ramSparseIO`` both store the pointer and then add
    # 4096 to ``resident``, and neither ever frees a page, so the two counts are
    # the same number computed twice.  A disagreement means either the struct
    # found is not the one, or the dump caught the driver mid-update -- and both
    # are reasons to stop rather than to publish a volume.
    allocated = sum(1 for page in pages if page)
    if resident != allocated * PAGE_SIZE:
        raise UniPadError(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "the RamSparseDisk's resident count does not match the pages it "
            "points at, so the struct found is not the volume's",
            vaddr=struct_at,
            resident=resident,
            allocated=allocated * PAGE_SIZE,
        )

    return SparseDisk(
        struct_at=struct_at,
        pages_ptr=pages_ptr,
        bytes=size,
        resident=resident,
        pages=pages,
    )


def reassemble(dump: GuestMemoryDump, disk: SparseDisk) -> bytearray:
    """The volume's logical image: every resident page, zeros everywhere else.

    The zeros are not a shortcut.  A page the guest never allocated is real
    zeroed RAM to the storage stack -- ``ramSparseIO`` serves the static zero
    page for it -- so a volume reassembled with the absent pages left as
    whatever was in the buffer would be a different disk, and the digest
    comparison would catch it only after the fact.

    Pages are read in ascending address order, in runs, because the dump's
    segments are forward-only byte sources (a zlib stream cannot be entered in
    the middle): ascending order means each segment is inflated once from its
    start to its end, and a scattered read order would inflate the whole segment
    again for every page behind it.

    Raises:
        UniPadError: ``evidence_incomplete`` when a resident page is not inside
            any mapped segment.  The struct promised it; the dump does not have
            it; that is incomplete evidence and not a zero page.
    """
    image = bytearray(disk.bytes)
    for index, address, count in _readable_runs(dump, disk.pages):
        image[index * PAGE_SIZE:(index + count) * PAGE_SIZE] = dump.read(
            address, count * PAGE_SIZE
        )
    return image


def unpack_packed(blob: bytes) -> bytes:
    """Expand a packed seed into its logical image, zeros restored.

    The inverse of the guest's own import: every extent's bytes land at the
    offset it names, and everything the seed does not carry is a real zero page.
    A zero page *inside* an extent is stored in the file and skipped by
    ``ramSparseImport`` when it builds the volume, but it is still a zero page
    here -- the logical image is the same either way, which is the point.

    Raises:
        UniPadError: ``artifact_manifest_invalid`` for anything either parser
            would refuse, with the rule it broke -- the same validation
            :func:`unipad.host.packed_ramdisk.validate` applies, because a seed
            this module expands and the guest refuses is a seed that was never
            going to boot.
    """
    image = bytearray(packed_ramdisk.validate(blob).logical)
    for index, page in _logical_pages(blob):
        start = index * PAGE_SIZE
        image[start:start + PAGE_SIZE] = page
    return bytes(image)


def repack_packed(logical: bytes) -> bytes:
    """Pack a logical image back into a seed, keeping only its nonzero pages.

    :func:`unipad.host.packed_ramdisk.pack` does the work.  It is not
    reimplemented here on purpose: the extent rule is where an extent starts and
    ends, and two copies of that loop are two answers, both of which produce a
    seed that validates and only one of which is the disk that was packed.  The
    round trip -- ``repack_packed(unpack_packed(seed)) == seed`` -- is therefore
    also a check on that module, and it is the control this one is written
    against.

    Raises:
        ValueError: for what :func:`~unipad.host.packed_ramdisk.pack` refuses:
            an image that is not a whole number of pages, or one too dense to
            fit the 256 MiB seed ceiling both parsers enforce.
    """
    return packed_ramdisk.pack(logical)


def verify_against_seed(logical: bytes, seed_path: Path | str) -> SeedIdentity:
    """Check a reassembled image against the seed it came from.

    The seed's identity is computed the way the guest computes it -- page by page
    over the logical image, with the zero page where the seed carries none --
    rather than by expanding the seed and hashing the result, because expanding
    allocates the whole volume a second time to answer a question a streaming
    hash answers in one pass.

    Args:
        logical: The reassembled logical image.
        seed_path: The packed seed to check it against.

    Returns:
        The identity, so a caller can report both digests and compare the
        logical one against the guest's own global.

    Raises:
        UniPadError: ``artifact_missing`` when the seed is not there;
            ``artifact_manifest_invalid`` when it is not a valid seed;
            ``evidence_incomplete`` when the image's length or digest does not
            match it.  The last one is the whole point: the guest will refuse a
            volume whose digest is not its own, and finding that out here costs
            a second instead of a boot.
    """
    path = Path(seed_path)
    if not path.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "the packed Data ramdisk is not at the path given",
            source=safe_location(path),
        )
    blob = path.read_bytes()
    found = packed_ramdisk.validate(blob, source=safe_location(path))
    logical_sha256 = _logical_digest(blob)

    if len(logical) != found.logical:
        raise UniPadError(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "the reassembled image is not the size the seed describes",
            source=safe_location(path),
            image_bytes=len(logical),
            seed_logical_bytes=found.logical,
        )
    actual = hashlib.sha256(logical).hexdigest()
    if actual != logical_sha256:
        raise UniPadError(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "the reassembled image does not hash to what the seed describes, "
            "so it is not the volume the guest published",
            source=safe_location(path),
            reassembled=actual,
            expected=logical_sha256,
        )

    return SeedIdentity(
        source=safe_location(path),
        seed_sha256=found.sha256,
        logical_sha256=logical_sha256,
        logical_bytes=found.logical,
        resident_bytes=found.resident,
        extent_count=len(found.extents),
    )


# ---------------------------------------------------------------------------
# the signature
# ---------------------------------------------------------------------------
def _looks_like_sparse_disk(
    dump: GuestMemoryDump, at: int, logical_bytes: int
) -> bool:
    """Whether the 24 bytes at ``at`` are a ``RamSparseDisk`` of this size.

    A predicate, not a check: :meth:`GuestMemoryDump.find_struct` calls it for
    every candidate the size field matched, and a candidate the dump cannot read
    at all is simply not one.  The three rules are the three things that are true
    of the real struct and false of a random 24 bytes that happen to hold the
    volume's size -- which, in a 6 GiB dump full of volume-sized runs, is not a
    hypothetical.
    """
    try:
        head = dump.read(at, STRUCT_SIZE)
    except UniPadError:
        return False
    # Named offsets rather than a tuple unpack, so each rule below says which
    # field of the struct it is about -- the three offsets *are* the signature,
    # and a reader who has to count to eight to find out which is which is a
    # reader who cannot check it.
    pages_ptr = struct.unpack_from("<Q", head, FIELD_PAGES)[0]
    size = struct.unpack_from("<Q", head, FIELD_BYTES)[0]
    resident = struct.unpack_from("<Q", head, FIELD_RESIDENT)[0]
    if size != logical_bytes or size % PAGE_SIZE:
        return False
    if not resident or resident % PAGE_SIZE or resident > size:
        return False
    # The pointer array is ``size / 4096`` entries of 8 bytes and the guest
    # allocated it in one piece, so its first entry is mapped.  Reading one
    # pointer is the cheapest way to ask, and it is the difference between a
    # candidate and a coincidence.
    try:
        dump.read(pages_ptr, 8)
    except UniPadError:
        return False
    return True


# ---------------------------------------------------------------------------
# the logical image, one page at a time
# ---------------------------------------------------------------------------
def _logical_pages(blob: bytes) -> Iterator[tuple[int, bytes]]:
    """Every page of the logical image, as ``(index, page)``.

    The one definition of "what the seed means", shared by
    :func:`unpack_packed` and :func:`_logical_digest`.  It walks the extent table
    rather than the payload, so a zero page inside an extent and a page the seed
    never carried come out the same -- which is what ``ramSparseImport`` does
    too, and what makes the two digests comparable.
    """
    found = packed_ramdisk.validate(blob)
    zero = b"\0" * PAGE_SIZE
    cursor = packed_ramdisk.HEADER_SIZE + len(found.extents) * 16
    index = 0
    for extent in found.extents:
        while index < extent.offset // PAGE_SIZE:
            yield index, zero
            index += 1
        for _ in range(extent.length // PAGE_SIZE):
            yield index, blob[cursor:cursor + PAGE_SIZE]
            cursor += PAGE_SIZE
            index += 1
    while index < found.logical // PAGE_SIZE:
        yield index, zero
        index += 1


def _logical_digest(blob: bytes) -> str:
    """``sha256`` of the logical image, as ``NativeRamBlock.cpp:236-240`` hashes it.

    Page by page over the same pages :func:`_logical_pages` yields, so the two
    cannot disagree about what the seed means.  The per-page update is not
    pedantry: it is the loop the guest runs, and a digest computed any other way
    is a number that matches a different image.
    """
    digest = hashlib.sha256()
    for _index, page in _logical_pages(blob):
        digest.update(page)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# reading the pages in the order the dump can serve them
# ---------------------------------------------------------------------------
def _contiguous_runs(pages: Sequence[int]) -> Iterator[tuple[int, int, int]]:
    """Maximal runs of resident pages, as ``(index, address, count)``.

    A run is pages that are adjacent in the volume *and* adjacent in memory, so
    one read serves all of them.  Both conditions are needed: adjacent in the
    volume but not in memory cannot be one read, and adjacent in memory but not
    in the volume would put a page in the wrong place.
    """
    start_index: Optional[int] = None
    start_address = 0
    previous_index = -1
    previous_address = 0
    for index, address in enumerate(pages):
        if not address:
            continue
        if start_index is None:
            start_index, start_address = index, address
        elif index != previous_index + 1 or address != previous_address + PAGE_SIZE:
            yield start_index, start_address, previous_index - start_index + 1
            start_index, start_address = index, address
        previous_index, previous_address = index, address
    if start_index is not None:
        yield start_index, start_address, previous_index - start_index + 1


def _readable_runs(
    dump: GuestMemoryDump, pages: Sequence[int]
) -> Iterator[tuple[int, int, int]]:
    """:func:`_contiguous_runs` clamped to what one segment can answer.

    A run that straddles two mapped segments cannot be one read -- the dump
    refuses a range that is not inside a single segment rather than stitching
    two, because the two are not necessarily adjacent in the guest -- so each run
    is split at the segment boundary.  For a contiguous kernel heap that is
    almost never a split; for a dump whose regions were cut by QEMU it is the
    difference between reading the volume and refusing it.
    """
    for index, address, count in _contiguous_runs(pages):
        while count:
            segment = dump.segment_for(address, PAGE_SIZE)
            if segment is None:
                raise UniPadError(
                    ErrorCode.EVIDENCE_INCOMPLETE,
                    "the RamSparseDisk points at a page the guest memory dump "
                    "does not map",
                    vaddr=address,
                    page_index=index,
                )
            room = (segment.vaddr + segment.size - address) // PAGE_SIZE
            take = min(count, max(room, 1))
            yield index, address, take
            index += take
            address += take * PAGE_SIZE
            count -= take


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def _load_addresses(args: argparse.Namespace) -> Optional[LoadAddresses]:
    """The kernel load addresses, from the serial log or from the command line.

    ``--serial`` wins when it is given, and it is the one to prefer: the guest
    prints ``kernel va 0x... pa 0x...`` itself, so the mapping is measured rather
    than typed in.  ``--kernel-va``/``--kernel-pa`` are for a run whose serial
    log is gone, and they need each other.

    The translation itself is :func:`unipad.host.guestobserve.load_addresses_from_serial`
    and :meth:`unipad.host.guestobserve.LoadAddresses.physical`.  Neither is
    reimplemented here: the mapping between the kernel's virtual and physical
    addresses is one constant this whole task turns on, and a second copy of it
    is a second thing to get wrong.

    Raises:
        UniPadError: ``config_invalid`` when only one half of the pair is given,
            or when an address is not a number.
    """
    if args.serial is not None:
        if not args.serial.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "the serial log is not at the path given",
                source=safe_location(args.serial),
            )
        return load_addresses_from_serial(args.serial.read_bytes())

    if (args.kernel_va is None) != (args.kernel_pa is None):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the kernel's load addresses are a pair: giving one without the "
            "other leaves the mapping between virtual and physical unknown",
        )
    if args.kernel_va is None:
        return None
    try:
        return LoadAddresses(
            kernel_va=int(args.kernel_va, 0), kernel_pa=int(args.kernel_pa, 0)
        )
    except ValueError as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"a kernel load address is not a number: {exc}",
        ) from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command line: extract the Data volume from a dump and pack it back.

    The whole of task #28 as a repo-level operation rather than a scratch script:
    ``--dump`` is a ``dump-guest-memory`` file, ``--seed`` is the packed seed the
    volume started from, ``--out`` is where the repacked seed goes, and
    ``--sha-global`` is the address of the guest's own
    ``native_ram_block_seed_sha256`` when the dump is taken with the kernel
    mapped.  Every step reports what it found, because a run that only says
    "done" has proved nothing about which pages it read (PRD 7.4).

    Raises:
        UniPadError: whatever the steps above raise, with the code that names
            the fault -- a dump that is not a dump, a signature that matches
            twice, an image that does not hash to the guest's number.
    """
    parser = argparse.ArgumentParser(
        prog="python -m unipad.host.ramdisk_extract",
        description=(
            "Reassemble the Data volume out of a QEMU guest-memory dump by "
            "following its RamSparseDisk, and pack the result back into a seed "
            "the guest will accept."
        ),
    )
    parser.add_argument(
        "--dump",
        type=Path,
        required=True,
        help="the guest-memory dump (elf, or kdump-zlib); sniffed, not assumed",
    )
    parser.add_argument(
        "--seed",
        type=Path,
        help="the packed seed to check the reassembled volume against",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="where to write the repacked seed",
    )
    parser.add_argument(
        "--bytes",
        type=int,
        default=DELIVERED_BYTES,
        help="the Data volume's logical size: the signature's search key, not an "
             f"assumption (default {DELIVERED_BYTES})",
    )
    parser.add_argument(
        "--serial",
        type=Path,
        help="the guest's serial log, to read the kernel load line and translate "
             "its virtual addresses; needed for any physical dump",
    )
    parser.add_argument(
        "--kernel-va",
        metavar="ADDRESS",
        help="the kernel's virtual load address, when there is no serial log to "
             "read it from",
    )
    parser.add_argument(
        "--kernel-pa",
        metavar="ADDRESS",
        help="the kernel's physical load address, when there is no serial log to "
             "read it from",
    )
    parser.add_argument(
        "--keying",
        choices=[rd.DumpKeying.PHYSICAL, rd.DumpKeying.VIRTUAL],
        default=rd.DumpKeying.PHYSICAL,
        help="which address space the dump is keyed by; every dump this project "
             "takes is physical, because QEMU refuses paging for the compressed "
             "formats (default physical)",
    )
    parser.add_argument(
        "--sha-global",
        metavar="ADDRESS",
        help="address of the guest's native_ram_block_seed_sha256, to compare "
             "the reassembled digest against the guest's own",
    )
    args = parser.parse_args(argv)

    load = _load_addresses(args)
    with rd.GuestMemoryDump.open(args.dump, keying=args.keying, load=load) as dump:
        print(
            f"dump: {safe_location(args.dump)} {dump.format}, keyed by "
            f"{dump.keying}, {len(dump.regions)} segments, notes {list(dump.notes)}"
        )
        if load is not None:
            print(
                f"load: kernel va {load.kernel_va:#x} pa {load.kernel_pa:#x} "
                f"(va - pa = {load.kernel_va - load.kernel_pa:#x})"
            )
        disk = find_sparse_disk(dump, logical_bytes=args.bytes)
        print(
            f"RamSparseDisk at {disk.struct_at:#x}: {disk.bytes} bytes, "
            f"{disk.resident_pages} of {disk.page_count} pages resident "
            f"({disk.resident} bytes)"
        )
        logical = reassemble(dump, disk)
        digest = hashlib.sha256(logical).hexdigest()
        print(f"reassembled {len(logical)} bytes, sha256 {digest}")

        if args.sha_global is not None:
            address = int(args.sha_global, 0)
            guest = dump.read(address, 32)
            if guest != bytes.fromhex(digest):
                raise UniPadError(
                    ErrorCode.EVIDENCE_INCOMPLETE,
                    "the reassembled volume does not hash to the digest the "
                    "guest itself computed, so it is not the volume the guest "
                    "published",
                    sha_global=f"{address:#x}",
                    guest=guest.hex(),
                    reassembled=digest,
                )
            print(f"the guest's own digest at {address:#x} matches")

        if args.seed is not None:
            identity = verify_against_seed(logical, args.seed)
            print(
                f"seed {identity.source}: sha256 {identity.seed_sha256}, "
                f"logical sha256 {identity.logical_sha256} "
                f"({identity.extent_count} extents, {identity.resident_bytes} "
                "bytes resident)"
            )

        if args.out is not None:
            blob = repack_packed(logical)
            # Before anything is written, for the same reason
            # ``packed_ramdisk.main`` validates first: a seed this refuses is a
            # defect in the repacker above, and the only place it can be caught
            # is here.
            packed_ramdisk.validate(blob, source=safe_location(args.out))
            if args.out.parent and not args.out.parent.exists():
                args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(blob)
            print(f"wrote {len(blob)} bytes to {safe_location(args.out)}")

        stats = dump.cache_stats
        print(
            f"cache: {stats.hits} hits, {stats.misses} misses, "
            f"{stats.evictions} evictions, {stats.cached_bytes} of "
            f"{stats.budget_bytes} bytes resident"
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
