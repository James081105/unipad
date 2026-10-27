"""``unipad.host.ramdisk_dump`` and ``ramdisk_extract`` against a synthetic guest.

Why this file exists
--------------------
The Data volume is not a file: it is a packed ramdisk in guest RAM, kept by the
kernel driver as an array of individually allocated 4 KiB pages
(``SparseDisk.h``), and neither the ``RamSparseDisk`` struct nor its page array
has a symbol -- ``sparse`` is a member of the ``NativeRamBlock`` IOService object
and ``sparse.pages`` is a separate allocation (``NativeRamBlock.cpp:55``).  So
host-side persistence finds them by signature in a whole-memory dump, and this
file is the proof that the signature and the reader agree.

Every dump here is built on this host and written to disk as a real file: a
``PT_NOTE`` with ``VMCOREINFO``, then ``PT_LOAD`` segments whose data is
zlib-compressed (or, for one test, verbatim), and a fake ``RamSparseDisk``, a
fake page array and a handful of separately-addressed pages laid into it.  The
extractor then runs on a real file rather than on a mock, which is the only way
a bug in the ELF or zlib layer can show up at all.

The dumps are **physical**, and the guest's pointers are **virtual**
-------------------------------------------------------------------
That is not a detail, it is the reason the reader has a translation layer at all.
QEMU refuses ``paging`` for every compressed format -- ``kdump-compressed format
doesn't support paging or filter`` -- so a compressed dump is keyed by physical
address, while ``sparse.pages`` holds kernel virtual ones.  The gap is closed by
:meth:`unipad.host.guestobserve.LoadAddresses.physical`, from the ``kernel va ...
pa ...`` line the guest prints on its own serial port; on this board that line
makes the delta ``0xFFFFFFF000000000`` for the whole kernel map.

So the fake guest below lays its struct, its pointer array and its pages out at
*kernel-map virtual* addresses, and the dumps are written keyed by the physical
addresses those translate to.  The extractor is handed a synthetic
``LoadAddresses`` and has to do the translation itself, which is the part a
layout that pretended the two were the same would never exercise.

The defects these tests are written after
-----------------------------------------
* **A short read.**  :meth:`GuestMemoryDump.read` must refuse a range no segment
  holds rather than return what it has.  A short read is a Data volume that is
  silently wrong -- the pages it did get, in the places it did get them -- and
  nothing downstream can tell.  It is pinned here as a raise, in both of its
  shapes: past the end of a segment, and straddling two.
* **A virtual address read as a physical one.**  A physical dump opened without
  the load addresses must refuse, because every pointer in it would be off by
  the kernel's load delta and the volume reassembled from them would be wrong in
  a way nothing detects.  That is the ninth test, and it is the one this whole
  design turns on.
* **A signature that is not specific enough.**  Two 24-byte runs holding the
  volume's size at offset 8 is not a volume, it is a coin flip, and the extractor
  refuses instead of choosing.  So is a dump with no match at all, and so is a
  struct whose ``resident`` count disagrees with the array it points at.
* **A hardcoded volume size.**  The 1 GiB is the signature's *search key*, so a
  volume of another size must be findable by passing another number, and the
  same dump must not match when the wrong number is asked for.
* **Loading the dump.**  A 6 GiB guest does not fit on this host, so the reader
  streams: reading a 2 MiB pointer array out of a much larger dump must not
  materialise the dump, and the window cache must stay inside its budget.

The control
-----------
``repack_packed(unpack_packed(seed)) == seed``, byte for byte, on the delivered
seed.  That control can only hold for a seed that carries no all-zero page
*inside* an extent -- ``ramSparseImport`` skips such a page when it builds the
volume, so a repacker that drops zero pages would not reproduce it -- and the
delivered seed carries none (measured: 366 pages in its 7 extents, 0 of them all
zero).  A synthetic seed with a zero page inside an extent is therefore *not*
asserted to round-trip; it is asserted to expand to the same zeros instead, which
is what the guest does with it.

What is *not* proven here
------------------------
That a real guest's memory looks like this.  No QEMU, no kernel, no dump on this
host: the layout is synthetic, chosen so the extractor has to follow pointers
rather than read a contiguous range.  The delivered seed is used where it is
present, and the tests that need it skip where it is absent -- it is Apple-derived
material that does not belong in the repository (PRD 12).

Nor is QEMU's own container format proven here.  Measured on this host against
the real dumps under ``local-private/tmp/dump28``: QEMU 11 writes
``format=kdump-zlib`` as a *makedumpfile* container, not as the ELF this reader
parses, so the reader refuses one with a message naming ``format=elf`` instead.
The dumps below are the ELF form, written by hand.
"""

from __future__ import annotations

import hashlib
import struct
import tracemalloc
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import packed_ramdisk as pr
from unipad.host import ramdisk_dump as rd
from unipad.host import ramdisk_extract as rx
from unipad.host.guestobserve import LoadAddresses

ROOT = Path(__file__).resolve().parents[2]
REAL_PRIVATE = ROOT / "local-private"

#: The delivered seed.  Not committed (PRD 12), so every test that needs it goes
#: through the :func:`seed_path` fixture and skips when it is absent.
SEED_PATH = REAL_PRIVATE / "firmware" / "owner-data-delivery" / "owner-data-1g.dbr"

PAGE = pr.PAGE_SIZE
ZERO = b"\0" * PAGE

#: The three fields of ``RamSparseDisk``.  Named so a test can say which one it
#: is aiming at rather than counting to eight.
FIELD_BYTES = rx.FIELD_BYTES

#: Where the fake kernel was loaded, as the guest's own serial line reports it.
#: The two together make the delta ``0xFFFFFFF000000000`` for the whole kernel
#: map, which is the constant every virtual address in the dump is translated
#: by -- and which is why the struct and its pages can live anywhere in the
#: kernel's map and still be reachable.
KERNEL_VA = 0xFFFFFFF004000000
KERNEL_PA = 0x40000000
LOAD = LoadAddresses(kernel_va=KERNEL_VA, kernel_pa=KERNEL_PA)

#: Where the fake guest's memory starts: a kernel-map virtual address, because
#: that is the address space ``sparse.pages`` holds pointers into.
GUEST_BASE = 0xFFFFFFF020000000

#: The same address, physical -- what the dump's ``PT_LOAD`` entries are keyed
#: by.  Everything the extractor is handed is virtual; everything the file holds
#: is this.
GUEST_PA = LOAD.physical(GUEST_BASE)

#: The fake ``RamSparseDisk``, the page array, and the first resident page.  The
#: page array of a 1 GiB volume is 2 MiB, so it runs from ``0x200000`` to
#: ``0x400000`` and the pages start right after it.
STRUCT_OFFSET = 0x00100000
PAGES_OFFSET = 0x00200000
PAGE_OFFSET = 0x00400000

#: How far apart the resident pages are placed.  They are *separately allocated*
#: in the guest (``ramSparseImport`` calls ``ops.allocate()`` per page), so an
#: extractor that read a contiguous range instead of following the pointers would
#: get zeros -- which is exactly the defect the whole design exists to avoid.
PAGE_STRIDE = 0x8000

#: Where a test puts a struct or a global it wrote by hand, and a region big
#: enough to hold it.  Past everything the layout places, so the two cannot
#: collide -- and stated as a constant because a test that wrote past the end of
#: the segment would be testing the reader's refusal instead of its own subject.
DECOY_OFFSET = 0x500000
SHA_OFFSET = 0x600000
HANDWRITTEN_BYTES = 8 << 20

#: The ELF64 pieces, so the writer below does not have to import them.
_EHDR = struct.Struct("<16sHHIQQQIHHHHHH")
_PHDR = struct.Struct("<IIQQQQQQ")
_ELF_IDENT = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8


# ---------------------------------------------------------------------------
# a synthetic guest memory dump
# ---------------------------------------------------------------------------
def write_dump(
    path: Path,
    segments: list[tuple[int, bytes]],
    *,
    fmt: str = rd.DumpFormat.KDUMP_ZLIB,
    terminate: bool = True,
    notes: bool = True,
) -> Path:
    """Write ``segments`` as a real QEMU-format dump.

    ``terminate=False`` writes the zlib stream without a final block, the way
    QEMU flushes it: a reader that calls :func:`zlib.decompress` on the whole
    segment raises ``incomplete or truncated stream`` and refuses a dump it
    should have read.  Both flavours are produced here so that difference is
    tested rather than assumed.
    """
    phnum = len(segments) + (1 if notes else 0)
    header = _EHDR.pack(
        _ELF_IDENT, 4, 183, 1, 0, 64, 0, 0, 64, _PHDR.size, phnum, 0, 0, 0
    )
    body = bytearray()
    table = b""
    offset = 64 + _PHDR.size * phnum
    if notes:
        name = b"VMCOREINFO\x00"
        desc = b"OSRELEASE=6.1.0\nPAGESIZE=4096\n"
        note = struct.pack("<III", len(name), len(desc), 0) + name + desc
        note += b"\x00" * (-len(note) % 4)
        table += _PHDR.pack(rd.PT_NOTE, 0, offset, 0, 0, len(note), len(note), 4)
        body += note
        offset += len(note)
    for vaddr, data in segments:
        if fmt == rd.DumpFormat.KDUMP_ZLIB:
            if terminate:
                stored = zlib.compress(data, 6)
            else:
                compressor = zlib.compressobj(6)
                stored = compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH)
        else:
            stored = data
        table += _PHDR.pack(rd.PT_LOAD, 7, offset, vaddr, vaddr, len(stored), len(data), 4096)
        body += stored
        offset += len(stored)
    path.write_bytes(header + table + bytes(body))
    return path


@dataclass
class FakeGuest:
    """A synthetic guest memory holding a ``RamSparseDisk`` and its pages.

    ``resident`` maps a page *index* to the bytes the guest allocated for it, and
    each one is placed at its own address ``page_stride`` apart -- so the only way
    to reassemble the volume is to read the pointer array and follow it, which is
    the same thing the extractor has to do against a real dump.
    """

    logical: int
    resident: dict[int, bytes] = field(default_factory=dict)
    #: How big the one mapped region is.  ``None`` sizes it to hold the last
    #: resident page, because a segment that does not is a dump whose pointer
    #: array points outside itself -- which is a different failure from the one
    #: any given test is about.
    segment_bytes: Optional[int] = None
    struct_offset: int = STRUCT_OFFSET
    pages_offset: int = PAGES_OFFSET
    page_stride: int = PAGE_STRIDE
    #: Whether to lay the struct at all.  ``False`` is how a test gets a dump whose
    #: only signature-shaped bytes are the ones it wrote by hand.
    lay_struct: bool = True
    #: Extra structs to lay into the memory, as ``(offset, fields)``.
    extra_structs: tuple[tuple[int, tuple[int, int, int]], ...] = ()

    @property
    def struct_at(self) -> int:
        return GUEST_BASE + self.struct_offset

    @property
    def pages_at(self) -> int:
        return GUEST_BASE + self.pages_offset

    def page_at(self, index: int) -> int:
        return GUEST_BASE + PAGE_OFFSET + index * self.page_stride

    @property
    def resident_bytes(self) -> int:
        return len(self.resident) * PAGE

    def span(self) -> int:
        """How much of the region the layout actually needs."""
        highest = max(self.resident, default=0)
        needed = max(
            PAGE_OFFSET + (highest + 1) * self.page_stride,
            self.pages_offset + (self.logical // PAGE) * 8,
        )
        for offset, _fields in self.extra_structs:
            needed = max(needed, offset + 24)
        return max(needed, 1 << 20)

    def memory(self) -> bytes:
        """The guest's one mapped region, with everything laid into it.

        Raises:
            ValueError: when the region asked for is smaller than the layout
                needs.  A ``bytearray`` slice assignment past the end *appends at
                the end* rather than writing where it was aimed, so an
                undersized region would quietly put every page somewhere the
                pointer array does not say -- a dump that reassembles to zeros
                and looks like a defect in the extractor.
        """
        size = self.segment_bytes if self.segment_bytes is not None else self.span()
        if size < self.span():
            raise ValueError(
                f"a {size}-byte region cannot hold this layout, which needs "
                f"{self.span()} bytes"
            )
        memory = bytearray(size)
        slots = [0] * (self.logical // PAGE)
        for index in self.resident:
            slots[index] = self.page_at(index)
        if self.lay_struct:
            memory[self.struct_offset:self.struct_offset + 24] = struct.pack(
                "<QQQ", self.pages_at, self.logical, self.resident_bytes
            )
        table = struct.pack(f"<{len(slots)}Q", *slots)
        memory[self.pages_offset:self.pages_offset + len(table)] = table
        for index, page in self.resident.items():
            at = PAGE_OFFSET + index * self.page_stride
            memory[at:at + len(page)] = page
        for offset, fields in self.extra_structs:
            memory[offset:offset + 24] = struct.pack("<QQQ", *fields)
        return bytes(memory)

    def write(self, path: Path, **kwargs) -> Path:
        """Write the guest as a **physical** dump, the way QEMU writes one.

        The memory is laid out at virtual addresses, because that is the space
        the guest's pointers live in; the file's ``PT_LOAD`` entries are keyed by
        the physical addresses those translate to, because QEMU refuses
        ``paging`` for every compressed format.  The gap between the two is the
        translation the extractor has to perform, so a dump that skipped it
        would be testing a reader that does not exist.
        """
        return write_dump(
            path, [(LOAD.physical(GUEST_BASE), self.memory())], **kwargs
        )


def open_dump(path: Path, **kwargs) -> rd.GuestMemoryDump:
    """Open one of the dumps below the way the extractor is meant to be used."""
    return rd.GuestMemoryDump.open(
        path, keying=rd.DumpKeying.PHYSICAL, load=LOAD, **kwargs
    )


def page_of(text: str) -> bytes:
    """A 4 KiB page whose first bytes are ``text``.

    Built rather than written as a literal because ``b"the first"`` is nine bytes
    and assigning it into an eight-byte slice of a ``bytearray`` *extends* it --
    which turns a volume that is not a whole number of pages into a packer
    refusal three stack frames away from the test that cares.
    """
    return text.encode()[:PAGE].ljust(PAGE, b"\0")


def resident_pages(blob: bytes) -> dict[int, bytes]:
    """A seed's nonzero pages by index, without expanding the whole volume.

    The same walk :func:`unipad.host.ramdisk_extract._logical_pages` does, minus
    the zero pages -- which is what ``ramSparseImport`` keeps.  Written out here
    rather than imported because the point is to build the fake guest from the
    seed independently: a helper shared with the module under test would agree
    with it by construction.
    """
    found = pr.validate(blob)
    pages: dict[int, bytes] = {}
    cursor = pr.HEADER_SIZE + len(found.extents) * 16
    index = 0
    for extent in found.extents:
        index = extent.offset // PAGE
        for _ in range(extent.length // PAGE):
            page = blob[cursor:cursor + PAGE]
            if page != ZERO:
                pages[index] = page
            cursor += PAGE
            index += 1
    return pages


def synthetic_seed(*, logical: int = 1 << 20) -> bytes:
    """A packed seed this host can build, for a checkout without ``local-private``.

    Three dirty pages, so the volume is sparse enough that a reassembly which
    dropped the absent pages would be visibly wrong.
    """
    image = bytearray(logical)
    image[0:16] = b"a synthetic Data"
    image[5 * PAGE:5 * PAGE + 8] = b"pagefive"
    image[(logical // PAGE - 1) * PAGE:(logical // PAGE - 1) * PAGE + 8] = b"the last"
    return pr.pack(bytes(image))


@pytest.fixture(scope="session")
def seed_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The delivered seed, or a synthetic stand-in where it is not committed."""
    if SEED_PATH.is_file():
        return SEED_PATH
    directory = tmp_path_factory.mktemp("synthetic-seed")
    path = directory / "synthetic-data.dbr"
    path.write_bytes(synthetic_seed())
    return path


@pytest.fixture(scope="session")
def seed_blob(seed_path: Path) -> bytes:
    return seed_path.read_bytes()


def guest_for(seed_blob: bytes, **kwargs) -> FakeGuest:
    """A fake guest holding the pages the seed describes."""
    return FakeGuest(
        logical=pr.validate(seed_blob).logical,
        resident=resident_pages(seed_blob),
        **kwargs,
    )


def digest_of(dump: rd.GuestMemoryDump, logical: int) -> str:
    """``sha256`` of the volume reassembled out of one dump."""
    disk = rx.find_sparse_disk(dump, logical_bytes=logical)
    return hashlib.sha256(rx.reassemble(dump, disk)).hexdigest()


# ---------------------------------------------------------------------------
# the round-trip control
# ---------------------------------------------------------------------------
def test_the_delivered_seed_round_trips_through_the_repacker_byte_for_byte(
    seed_path: Path,
) -> None:
    """``repack_packed(unpack_packed(seed)) == seed``, on the real seed.

    This is the control the whole module is written against: the repacker has to
    be the exact inverse of the expansion, or a volume extracted from a guest and
    written back would be a *different* volume that still validates, still boots,
    and is not the one the guest had.  The delivered seed is the only one whose
    answer is known independently, so it is the one asserted here.
    """
    blob = seed_path.read_bytes()
    logical = rx.unpack_packed(blob)
    repacked = rx.repack_packed(logical)
    assert repacked == blob, (
        f"the round trip changed the seed: {len(repacked)} bytes back from "
        f"{len(blob)}, first difference at {_first_difference(repacked, blob)}"
    )


def test_a_synthetic_seed_round_trips_too_so_the_control_is_not_seed_specific() -> None:
    """The same control on a seed built here.

    A checkout without ``local-private`` runs the round-trip test against a
    stand-in, so the control has to hold for a seed nothing but this host has
    seen -- otherwise the delivered seed's round trip would be a fact about that
    file rather than about the repacker.
    """
    blob = synthetic_seed()
    assert rx.repack_packed(rx.unpack_packed(blob)) == blob


def test_a_zero_page_inside_an_extent_expands_to_zeros_rather_than_being_kept(
    tmp_path: Path,
) -> None:
    """A zero page the seed carries is a zero page in the volume.

    ``ramSparseImport`` skips an all-zero page when it builds the volume -- it
    ``continue``s past it and allocates nothing -- so the guest's ``pages`` array
    has no entry for it and the storage stack serves the static zero page
    instead.  The extractor has to do the same: reassemble it as zeros *without*
    a ``pages`` entry, because the seed carrying the bytes does not mean the guest
    allocated them.

    Note what is deliberately *not* asserted: that this seed round-trips.  A
    repacker that drops zero pages cannot reproduce one that carries them, which
    is why the round-trip control is only claimed for a seed that has none.
    """
    image = bytearray(8 * PAGE)
    image[0:8] = b"firstpge"
    image[2 * PAGE:2 * PAGE + 8] = b"thirdpge"  # page 1 stays all zero
    # Written by hand rather than packed: the packer's whole job is to *drop* a
    # zero page, so it could not produce a seed that carries one.
    blob = struct.pack("<8sIIQQ", pr.MAGIC, pr.VERSION, PAGE, len(image), 1)
    blob += struct.pack("<QQ", 0, 3 * PAGE)
    blob += bytes(image[:3 * PAGE])
    assert blob[pr.HEADER_SIZE:pr.HEADER_SIZE + 16] == struct.pack("<QQ", 0, 3 * PAGE)

    guest = FakeGuest(
        logical=len(image), resident={0: image[:PAGE], 2: image[2 * PAGE:3 * PAGE]}
    )
    dump = guest.write(tmp_path / "zero-page.kdump")
    with open_dump(dump) as memory:
        disk = rx.find_sparse_disk(memory, logical_bytes=len(image))
        assert disk.resident_pages == 2, "the zero page must not be counted as resident"
        volume = rx.reassemble(memory, disk)

    assert volume[:8] == b"firstpge"
    assert volume[PAGE:2 * PAGE] == ZERO
    assert volume[2 * PAGE:2 * PAGE + 8] == b"thirdpge"
    assert rx.unpack_packed(blob) == volume


# ---------------------------------------------------------------------------
# the extractor against the seed's own identity
# ---------------------------------------------------------------------------
def test_the_extractor_reassembles_the_volume_the_seed_describes(
    tmp_path: Path, seed_blob: bytes, seed_path: Path
) -> None:
    """The reassembled volume hashes to what the guest computes.

    ``NativeRamBlock.cpp:236-240`` hashes the logical image page by page --
    ``sparse.pages[i] ? pages[i] : zero`` -- into
    ``native_ram_block_seed_sha256``, so ``sha256`` of the reassembly is directly
    comparable to the number the guest itself produced.  :func:`verify_against_seed`
    raises if it does not match, which makes this the one test that would fail if
    the extractor followed the wrong pointer, dropped a page, or invented zeros.
    """
    guest = guest_for(seed_blob)
    dump = guest.write(tmp_path / "seed.kdump")
    with open_dump(dump) as memory:
        disk = rx.find_sparse_disk(memory, logical_bytes=guest.logical)
        assert disk.bytes == guest.logical
        assert disk.resident_pages == len(guest.resident)
        volume = rx.reassemble(memory, disk)
        identity = rx.verify_against_seed(volume, seed_path)

    assert identity.logical_bytes == guest.logical
    assert identity.logical_sha256 == hashlib.sha256(volume).hexdigest()
    assert identity.extent_count == len(pr.validate(seed_blob).extents)


def test_the_volume_reassembles_identically_from_a_plain_elf_dump(
    tmp_path: Path, seed_blob: bytes
) -> None:
    """The same volume out of the other format.

    The two formats differ only in how the segment bytes are stored, so a volume
    that came out differently would mean the reader had grown a dependence on the
    compression.  It also pins that the format is taken from the file and not
    from the name: this dump is written as ``elf`` and named ``.kdump``.
    """
    guest = guest_for(seed_blob)
    elf = guest.write(tmp_path / "but-this-is-an-elf.kdump", fmt=rd.DumpFormat.ELF)
    kdump = guest.write(tmp_path / "and-this-is-a-kdump.kdump")

    with open_dump(elf) as memory:
        assert memory.format == rd.DumpFormat.ELF
        from_elf = digest_of(memory, guest.logical)
    with open_dump(kdump) as memory:
        assert memory.format == rd.DumpFormat.KDUMP_ZLIB
        from_kdump = digest_of(memory, guest.logical)
    assert from_elf == from_kdump


def test_a_zlib_stream_without_a_final_block_is_still_read(
    tmp_path: Path, seed_blob: bytes
) -> None:
    """A stream QEMU flushed without finishing is a stream, not a corrupt one.

    QEMU does not promise the compressed segment ends with a final block, so the
    reader inflates with :class:`zlib.decompressobj` rather than
    :func:`zlib.decompress`: the latter raises ``incomplete or truncated stream``
    and would refuse a dump that is perfectly readable.  Written here with
    ``Z_SYNC_FLUSH`` so the difference is measured rather than assumed.
    """
    guest = guest_for(seed_blob)
    flushed = guest.write(tmp_path / "sync-flushed.kdump", terminate=False)
    with open_dump(flushed) as memory:
        volume = rx.reassemble(
            memory, rx.find_sparse_disk(memory, logical_bytes=guest.logical)
        )
    assert hashlib.sha256(volume).hexdigest() == rx._logical_digest(seed_blob)


# ---------------------------------------------------------------------------
# the signature
# ---------------------------------------------------------------------------
def test_the_struct_is_found_when_the_volume_is_not_one_gibibyte(tmp_path: Path) -> None:
    """The volume's size is a search key, not a constant.

    The delivered Data volume is 1 GiB and that is the default key, but a 4 MiB
    volume is found by asking for 4 MiB -- and the same dump must *not* match when
    1 GiB is asked for, because a signature that matched any size would match
    every size in a 6 GiB dump.
    """
    logical = 4 << 20
    guest = FakeGuest(logical=logical, resident={0: page_of("the first page")})
    dump = guest.write(tmp_path / "four-mib.kdump")
    with open_dump(dump) as memory:
        found = rx.find_sparse_disk(memory, logical_bytes=logical)
        assert found.bytes == logical
        assert found.page_count == logical // PAGE
        with pytest.raises(UniPadError) as refusal:
            rx.find_sparse_disk(memory, logical_bytes=rx.DELIVERED_BYTES)
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["matches"] == 0


def test_two_structs_matching_the_signature_are_a_refusal_not_a_choice(tmp_path: Path) -> None:
    """Two candidates means the signature is wrong, and picking one is a guess.

    A second 24-byte run holding the volume's size, a mapped pointer and a sane
    resident count is indistinguishable from the real one by anything this
    signature checks -- so the extractor reports both and stops.  The alternative
    is a Data volume assembled from whichever struct the scan happened to reach
    first, which is a corruption that looks like success.
    """
    logical = 1 << 20
    decoy = FakeGuest(
        logical=logical,
        resident={0: page_of("the real page")},
        segment_bytes=HANDWRITTEN_BYTES,
        extra_structs=((DECOY_OFFSET, (GUEST_BASE + DECOY_OFFSET, logical, 3 * PAGE)),),
    )
    dump = decoy.write(tmp_path / "two-structs.kdump")
    with open_dump(dump) as memory:
        with pytest.raises(UniPadError) as refusal:
            rx.find_sparse_disk(memory, logical_bytes=logical)

    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    details = refusal.value.details
    assert details["matches"] == 2
    assert sorted(details["candidates"]) == sorted(
        [f"{decoy.struct_at:#x}", f"{GUEST_BASE + DECOY_OFFSET:#x}"]
    )


def test_a_dump_with_no_matching_struct_is_a_refusal(tmp_path: Path) -> None:
    """Nothing matching is incomplete evidence, not an empty volume.

    A dump taken before the driver published the volume, or one whose signature
    has drifted, must not produce a volume of zeros: that would be a Data volume
    which is wrong in a way nothing can detect.  The refusal names the size it
    looked for, because "no match" and "no match at 1 GiB" are different
    diagnoses.
    """
    logical = 1 << 20
    guest = FakeGuest(logical=logical, resident={0: page_of("a page with no struct")}, lay_struct=False)
    dump = guest.write(tmp_path / "no-struct.kdump")
    with open_dump(dump) as memory:
        with pytest.raises(UniPadError) as refusal:
            rx.find_sparse_disk(memory, logical_bytes=logical)
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["matches"] == 0


def test_a_struct_whose_pages_pointer_is_unmapped_is_not_a_match(tmp_path: Path) -> None:
    """A mapped pointer is part of the signature, not a bonus check.

    A run of bytes holding the volume's size at offset 8 is common in a dump full
    of volume-sized data; what makes it the struct is that offset 0 is an address
    the dump maps.  Without that check the extractor would find a page of the
    volume's own contents and follow it as a pointer table.  This is the only
    signature-shaped struct in the dump, so the refusal has to be "no match" and
    not "two matches".
    """
    logical = 1 << 20
    guest = FakeGuest(
        logical=logical,
        resident={0: page_of("the only page")},
        lay_struct=False,
        # A struct whose pages pointer is nowhere near the mapped region.
        segment_bytes=HANDWRITTEN_BYTES,
        extra_structs=((DECOY_OFFSET, (GUEST_BASE + 0x7FFFFF00, logical, PAGE)),),
    )
    dump = guest.write(tmp_path / "unmapped-pages.kdump")
    with open_dump(dump) as memory:
        with pytest.raises(UniPadError) as refusal:
            rx.find_sparse_disk(memory, logical_bytes=logical)
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["matches"] == 0


@pytest.mark.parametrize(
    "resident",
    [
        pytest.param(0, id="a volume with no resident page was never published"),
        pytest.param(PAGE // 2, id="a resident count that is not a whole number of pages"),
        pytest.param((1 << 20) + PAGE, id="a resident count larger than the volume"),
    ],
)
def test_a_struct_with_an_impossible_resident_count_is_not_a_match(
    tmp_path: Path, resident: int
) -> None:
    """``resident`` is checked, not assumed.

    It is nonzero, a whole number of pages and no larger than the volume, because
    those are the three things ``ramSparseImport`` makes true of the real struct:
    it adds 4096 per allocated page and never allocates more pages than the volume
    has.  A run of bytes that holds the volume's size and a mapped pointer but
    breaks one of them is a coincidence, and a signature that accepted it would
    find a second "volume" in every dump that had one.
    """
    logical = 1 << 20
    guest = FakeGuest(
        logical=logical,
        resident={0: page_of("one page only")},
        lay_struct=False,
        segment_bytes=HANDWRITTEN_BYTES,
        extra_structs=((DECOY_OFFSET, (GUEST_BASE + SHA_OFFSET, logical, resident)),),
    )
    dump = guest.write(tmp_path / "impossible-resident.kdump")
    with open_dump(dump) as memory:
        with pytest.raises(UniPadError) as refusal:
            rx.find_sparse_disk(memory, logical_bytes=logical)
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["matches"] == 0


def test_a_resident_count_that_disagrees_with_the_page_array_is_a_refusal(tmp_path: Path) -> None:
    """``resident`` and the array are the same number computed twice.

    ``ramSparseImport`` and ``ramSparseIO`` both store the pointer and then add
    4096, and neither frees, so a disagreement means the struct found is not the
    volume's -- or the dump caught the driver mid-update.  Either way the extractor
    stops, because a volume assembled from a struct it cannot trust is a volume
    assembled from a guess.
    """
    logical = 1 << 20
    guest = FakeGuest(
        logical=logical,
        resident={0: page_of("one page only")},
        lay_struct=False,
        # The only struct in the dump.  Its pointer array is a genuinely empty
        # stretch of the region, so the count is the only thing wrong with it.
        segment_bytes=HANDWRITTEN_BYTES,
        extra_structs=((DECOY_OFFSET, (GUEST_BASE + SHA_OFFSET, logical, 9 * PAGE)),),
    )
    dump = guest.write(tmp_path / "wrong-resident.kdump")
    with open_dump(dump) as memory:
        with pytest.raises(UniPadError) as refusal:
            rx.find_sparse_disk(memory, logical_bytes=logical)
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["resident"] == 9 * PAGE
    assert refusal.value.details["allocated"] == 0


def test_a_field_straddling_a_window_boundary_is_found(tmp_path: Path) -> None:
    """A struct that crosses a window edge is still a struct.

    The scan walks the dump in fixed-size windows, so a field that *starts* in the
    last few bytes of one window and finishes in the next is found only if those
    bytes are carried forward.  Without that carry, whether the volume is found
    depends on where the kernel happened to allocate it -- a defect that would
    look like a dump with no Data volume in it.

    The struct is placed so its ``bytes`` field starts six bytes before the
    boundary: a field that merely *spans* it is found by the second window alone,
    which is why the offset is not simply ``PAGE - 8``.
    """
    logical = 1 << 20
    guest = FakeGuest(
        logical=logical,
        resident={0: page_of("a page at the edge")},
        struct_offset=PAGE - 6 - FIELD_BYTES,
    )
    assert (guest.struct_at + FIELD_BYTES) % PAGE == PAGE - 6
    dump = guest.write(tmp_path / "straddling.kdump")
    with open_dump(dump, window_bytes=PAGE) as memory:
        found = rx.find_sparse_disk(memory, logical_bytes=logical)
        assert found.bytes == logical
        assert found.resident_pages == 1


# ---------------------------------------------------------------------------
# the reader
# ---------------------------------------------------------------------------
def test_a_read_that_runs_past_the_segment_is_refused_rather_than_truncated(
    tmp_path: Path,
) -> None:
    """A short read is a refusal, in both of its shapes.

    Past the end of a segment, and straddling two: neither may return the bytes it
    happens to have.  A truncated buffer becomes a Data volume with the pages it
    did get in the places it did get them, and nothing downstream can tell -- the
    digest would catch it, but only after the volume had been written back and
    believed.
    """
    guest = FakeGuest(logical=1 << 20, resident={0: page_of("the only page")})
    dump = guest.write(tmp_path / "short.kdump")
    with open_dump(dump) as memory:
        # ``regions`` is in the dump's own keying -- physical -- so the bounds
        # are checked through the physical primitive.
        start, size = memory.regions[0]
        with pytest.raises(UniPadError) as past_the_end:
            memory.read_physical(start + size - 16, 32)
        assert past_the_end.value.code == ErrorCode.EVIDENCE_INCOMPLETE

        with pytest.raises(UniPadError) as before_the_start:
            memory.read_physical(start - 8, 16)
        assert before_the_start.value.code == ErrorCode.EVIDENCE_INCOMPLETE

        # And the same refusal through the translating entry point: a virtual
        # address whose physical image is past the end of the segment.
        with pytest.raises(UniPadError) as translated:
            memory.read(LOAD.physical(start) + size - 16, 32)
        assert translated.value.code == ErrorCode.EVIDENCE_INCOMPLETE

        second = write_dump(
            tmp_path / "two-regions.kdump",
            [
                (start, bytes(guest.memory()[:PAGE])),
                (start + size, bytes(guest.memory()[:PAGE])),
            ],
        )
        with open_dump(second) as split:
            with pytest.raises(UniPadError) as straddling:
                split.read_physical(start + size - 8, 16)
            assert straddling.value.code == ErrorCode.EVIDENCE_INCOMPLETE


def test_a_physical_dump_without_the_load_addresses_refuses_rather_than_treating_a_virtual_address_as_a_physical_one(
    tmp_path: Path,
) -> None:
    """A physical dump and no load addresses is a refusal, not a guess.

    The kernel's pointers are virtual and the dump is keyed physically, so the
    only way to read one is through the load addresses the guest printed.  Using
    the virtual address as a physical one is not a degraded mode: it lands a
    constant away -- ``0xFFFFFFF000000000`` on this board -- which is either
    outside every segment or, worse, inside an unrelated one, and either way the
    volume reassembled from it is silently wrong.  So the reader says it needs
    the mapping and names what to give it.

    Both entry points refuse, because both need the translation: the signature
    scan has to name a hit, and :meth:`read` has to fetch it.
    """
    guest = FakeGuest(logical=1 << 20, resident={0: page_of("the only page")})
    dump = guest.write(tmp_path / "no-load.kdump")
    with rd.GuestMemoryDump.open(dump, keying=rd.DumpKeying.PHYSICAL) as memory:
        assert memory.keying == rd.DumpKeying.PHYSICAL
        with pytest.raises(UniPadError) as refusal:
            memory.read(guest.struct_at, 24)
        assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
        assert "load addresses" in refusal.value.message

        with pytest.raises(UniPadError) as scan:
            list(memory.find_struct(FIELD_BYTES, 1 << 20))
        assert scan.value.code == ErrorCode.EVIDENCE_INCOMPLETE

        # The physical primitive still works without them: it is the caller's
        # address that needed translating, not the reader's.  And what it reads
        # back is a *virtual* address, because that is what the kernel stores in
        # the field -- which is exactly why the translation is needed at all.
        assert memory.read_physical(GUEST_PA + STRUCT_OFFSET, 8) == struct.pack(
            "<Q", guest.pages_at
        )

    # And the same dump, read the other way, is keyed virtually -- so no
    # translation happens and the addresses are the file's own.
    with open_dump(dump) as translated:
        assert translated.read(guest.struct_at, 8) == struct.pack(
            "<Q", guest.pages_at
        )


def test_an_elf_dump_must_say_which_address_space_it_is_keyed_by(tmp_path: Path) -> None:
    """A plain ``elf`` dump has no implied keying, and the file does not say.

    QEMU writes it keyed physically without ``paging`` and virtually with it, and
    nothing in the program headers records which was used.  So the caller says,
    and a caller who does not is refused rather than guessed at -- the two
    readings differ by the kernel's whole load delta.
    """
    guest = FakeGuest(logical=1 << 20, resident={0: page_of("the only page")})
    dump = guest.write(tmp_path / "elf.kdump", fmt=rd.DumpFormat.ELF)
    with pytest.raises(UniPadError) as refusal:
        rd.GuestMemoryDump.open(dump)
    assert refusal.value.code == ErrorCode.CONFIG_INVALID

    with rd.GuestMemoryDump.open(
        dump, keying=rd.DumpKeying.VIRTUAL
    ) as virtual:
        assert virtual.keying == rd.DumpKeying.VIRTUAL
        # Keyed virtually, so the file's own addresses are the guest's -- no
        # translation happens, and reading the struct's physical key gives back
        # the virtual pointer it holds.
        assert virtual.read(GUEST_PA + STRUCT_OFFSET, 8) == struct.pack(
            "<Q", guest.pages_at
        )


def test_the_reader_reports_which_address_space_a_dump_is_keyed_by(tmp_path: Path) -> None:
    """The keying is reported, because it is the one thing a caller can get wrong.

    A report that says only "1 segment, kdump-zlib" leaves out whether the
    addresses in it are physical or virtual, and that is the difference between a
    volume and a constant-offset pile of the wrong pages.
    """
    guest = FakeGuest(logical=1 << 20, resident={0: page_of("the only page")})
    compressed = guest.write(tmp_path / "compressed.kdump")
    plain = guest.write(tmp_path / "plain.kdump", fmt=rd.DumpFormat.ELF)

    # Derived, not assumed: QEMU refuses paging for every compressed format, so a
    # compressed dump cannot be virtual-keyed.
    with rd.GuestMemoryDump.open(compressed, load=LOAD) as memory:
        assert memory.format == rd.DumpFormat.KDUMP_ZLIB
        assert memory.keying == rd.DumpKeying.PHYSICAL
        assert memory.regions == [(GUEST_PA, len(guest.memory()))]

    with open_dump(plain) as memory:
        assert memory.format == rd.DumpFormat.ELF
        assert memory.keying == rd.DumpKeying.PHYSICAL


def test_a_makedumpfile_container_is_refused_by_name(tmp_path: Path) -> None:
    """QEMU's real ``kdump-zlib`` output is not an ELF, and says so.

    Measured against the dumps this project actually took
    (``local-private/tmp/dump28/guest-memory-kdump-zlib.dump``): 16 bytes of
    ``makedumpfile`` signature, a page of bitmap, then a ``KDUMP`` sub-header at
    offset 4112, and no ELF magic anywhere.  A caller who hands one of those to
    this reader should be told which ``--format`` to use instead of being told
    it is not an ELF file.
    """
    container = tmp_path / "guest-memory.kdump"
    container.write_bytes(
        b"makedumpfile\x00\x00\x00\x00" + b"\x00" * 24 + b"KDUMP" + b"\x00" * 32
    )
    with pytest.raises(UniPadError) as refusal:
        rd.GuestMemoryDump.open(container)
    assert refusal.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID
    assert "format=elf" in refusal.value.message


def test_reading_the_pointer_array_does_not_load_the_dump(tmp_path: Path) -> None:
    """A 2 MiB read out of a 64 MiB dump holds the 2 MiB and not the dump.

    The pointer array of a 1 GiB volume is 2 MiB and is read once; the resident
    pages are read once each; a signature scan touches every window once.  None of
    those want the dump resident, and a host that read it whole could not run any
    of them on a 6 GiB guest.  Peak allocation is measured rather than reasoned
    about, and the cache's own budget is asserted to have held.
    """
    guest = FakeGuest(
        logical=rx.DELIVERED_BYTES,
        resident={0: page_of("a page in a big dump")},
        segment_bytes=64 << 20,
    )
    dump = guest.write(tmp_path / "big.kdump")
    budget = 1 << 20
    with open_dump(dump, cache_bytes=budget) as memory:
        tracemalloc.start()
        try:
            array = memory.read(guest.pages_at, guest.logical // PAGE * 8)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        stats = memory.cache_stats

    assert len(array) == guest.logical // PAGE * 8
    # The array is 2 MiB and the windows it was read from are 1 MiB each, so
    # anything close to the dump's 64 MiB means the reader materialised it.
    assert peak < 16 << 20, f"peak {peak} bytes while reading a 2 MiB pointer array"
    assert stats.cached_bytes <= budget
    assert stats.evictions >= 1, "the second window should have evicted the first"
    # And the array really is the one that was written, not the first 2 MiB of the
    # segment that happens to be there.
    slots = struct.unpack_from(f"<{guest.logical // PAGE}Q", array)
    assert slots[0] == guest.page_at(0)
    assert slots[1] == 0


def test_a_run_of_pages_split_by_a_segment_boundary_is_still_read(tmp_path: Path) -> None:
    """Contiguous pages across two segments are read, not refused.

    The reader refuses a range that is not inside one segment -- correctly, because
    the two are not necessarily adjacent in the guest -- so the extractor has to
    split a run of pages at the boundary.  Two pages adjacent in memory *and* in
    the volume, with the boundary exactly between them, is the case that proves the
    split happens.
    """
    logical = 4 * PAGE
    guest = FakeGuest(
        logical=logical,
        resident={0: page_of("page zero"), 1: page_of("page one")},
        # Adjacent in memory, so the two are one run and the boundary splits it.
        page_stride=PAGE,
    )
    memory = bytearray(guest.memory())
    boundary = guest.page_at(1) - GUEST_BASE
    dump = write_dump(
        tmp_path / "split-run.kdump",
        [
            (LOAD.physical(GUEST_BASE), bytes(memory[:boundary])),
            (LOAD.physical(GUEST_BASE + boundary), bytes(memory[boundary:])),
        ],
    )
    with open_dump(dump) as reader:
        disk = rx.find_sparse_disk(reader, logical_bytes=logical)
        volume = rx.reassemble(reader, disk)
    assert volume[:9] == b"page zero"
    assert volume[PAGE:PAGE + 8] == b"page one"
    assert volume[2 * PAGE:] == ZERO * 2


def test_a_window_larger_than_the_whole_budget_is_handed_back_uncached(tmp_path: Path) -> None:
    """A window that cannot fit the cache is not put in it.

    Caching one anyway would evict everything else to hold itself -- a cache that
    never hits, dressed up as a cache.  The reader still works, because a window
    is produced on demand and the segments are read forward; what is lost is the
    reuse, which is the honest outcome of asking for less room than one window
    takes.
    """
    guest = FakeGuest(logical=1 << 20, resident={0: page_of("a page")})
    dump = guest.write(tmp_path / "tiny-cache.kdump")
    budget = PAGE
    with open_dump(dump, cache_bytes=budget) as memory:
        first = memory.read(guest.struct_at, 24)
        again = memory.read(guest.struct_at, 24)
        stats = memory.cache_stats

    assert first == again
    assert stats.cached_bytes <= budget
    assert stats.hits == 0, "nothing could be cached, so nothing could be reused"
    assert stats.evictions == 0, (
        "a window too big for the budget is handed back, not put in and "
        "immediately thrown out"
    )


def test_the_notes_and_the_format_are_reported(tmp_path: Path) -> None:
    """What the dump says about itself, before anything is read from it.

    The note section is how a dump is identified as a kdump, and reporting it is
    what lets a caller say which artifact it has before spending a scan on it.
    """
    guest = FakeGuest(logical=1 << 20, resident={0: page_of("a page")})
    dump = guest.write(tmp_path / "noted.kdump")
    assert rd.dump_format(dump) == rd.DumpFormat.KDUMP_ZLIB
    with open_dump(dump) as memory:
        assert memory.notes == ("VMCOREINFO",)
        assert memory.elf[0] == rd.ET_CORE
        assert memory.format == rd.DumpFormat.KDUMP_ZLIB


def test_a_file_that_is_not_a_dump_is_refused(tmp_path: Path) -> None:
    """A wrong artifact is a wrong artifact, and says so.

    Distinguished from ``evidence_incomplete`` on purpose: a file that is not a
    dump at all is not evidence that is missing something, and a caller that
    branched on the incomplete code and retried would retry forever.
    """
    not_a_dump = tmp_path / "not-a-dump.bin"
    not_a_dump.write_bytes(b"this is not an ELF file" * 64)
    with pytest.raises(UniPadError) as refusal:
        rd.GuestMemoryDump.open(not_a_dump)
    assert refusal.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID

    with pytest.raises(UniPadError) as missing:
        rd.GuestMemoryDump.open(tmp_path / "absent.kdump")
    assert missing.value.code == ErrorCode.ARTIFACT_MISSING


def test_an_absent_seed_is_missing_rather_than_a_digest_mismatch(tmp_path: Path) -> None:
    """A seed that is not there is not a seed that does not match.

    :func:`verify_against_seed` refuses with ``artifact_missing`` so a caller does
    not have to tell "absent" from "wrong" by reading the message.
    """
    with pytest.raises(UniPadError) as refusal:
        rx.verify_against_seed(ZERO, tmp_path / "absent.dbr")
    assert refusal.value.code == ErrorCode.ARTIFACT_MISSING


def test_an_image_of_the_wrong_size_is_a_refusal_not_a_digest_race(tmp_path: Path) -> None:
    """Length is checked before the digest, and separately.

    A reassembly that produced a different number of bytes is a different failure
    from one that produced the right number of wrong bytes, and conflating them
    into one digest mismatch would send a reader looking for a corrupt page when
    the bug is in the size.
    """
    with pytest.raises(UniPadError) as refusal:
        rx.verify_against_seed(ZERO, stand_in_seed(tmp_path))
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert "size" in refusal.value.message


def stand_in_seed(directory: Path) -> Path:
    """A seed written into ``directory``, for the tests that need one on disk."""
    path = directory / "stand-in.dbr"
    if not path.is_file():
        path.write_bytes(synthetic_seed())
    return path


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def test_the_cli_repacks_a_dump_and_says_what_it_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture, seed_blob: bytes, seed_path: Path
) -> None:
    """The whole of the extraction as one command, against the guest's own digest.

    ``--sha-global`` names a 32-byte global in the fake guest holding the digest
    the kernel would have computed, so the run is checked against the *guest's*
    number rather than only against the seed's -- which is the comparison
    ``NativeRamBlock.cpp:236-240`` makes possible and the only one that catches a
    reassembly the seed would also accept.
    """
    guest = guest_for(seed_blob)
    memory = bytearray(guest.memory())
    digest = rx._logical_digest(seed_blob)
    memory[SHA_OFFSET:SHA_OFFSET + 32] = bytes.fromhex(digest)
    dump = write_dump(
        tmp_path / "cli.kdump", [(LOAD.physical(GUEST_BASE), bytes(memory))]
    )
    out = tmp_path / "repacked.dbr"

    exit_code = rx.main(
        [
            "--dump", str(dump),
            "--seed", str(seed_path),
            "--out", str(out),
            "--sha-global", f"{GUEST_BASE + SHA_OFFSET:#x}",
            "--bytes", str(guest.logical),
            "--kernel-va", f"{KERNEL_VA:#x}",
            "--kernel-pa", f"{KERNEL_PA:#x}",
        ]
    )
    assert exit_code == 0
    report = capsys.readouterr().out
    assert f"{len(guest.resident)} of {guest.logical // PAGE} pages resident" in report
    assert digest in report
    assert "matches" in report
    assert f"va - pa = {KERNEL_VA - KERNEL_PA:#x}" in report
    assert out.is_file()
    # The repacked seed is the seed it came from, byte for byte: that is the
    # round-trip control, reached through the whole command rather than through
    # one function.
    assert out.read_bytes() == seed_blob


def test_the_cli_refuses_a_digest_the_guest_did_not_compute(tmp_path: Path) -> None:
    """A wrong global is a refusal, and the exit code says so.

    The guest's digest is the control: a reassembly that does not match it is not
    the volume the guest published, and writing it back would replace a working
    Data volume with a plausible one.
    """
    logical = 1 << 20
    guest = FakeGuest(
        logical=logical, resident={0: page_of("a page")}, segment_bytes=HANDWRITTEN_BYTES
    )
    memory = bytearray(guest.memory())
    memory[SHA_OFFSET:SHA_OFFSET + 32] = b"\x11" * 32
    dump = write_dump(
        tmp_path / "wrong-digest.kdump", [(LOAD.physical(GUEST_BASE), bytes(memory))]
    )
    with pytest.raises(UniPadError) as refusal:
        rx.main(
            [
                "--dump", str(dump),
                "--sha-global", f"{GUEST_BASE + SHA_OFFSET:#x}",
                "--bytes", str(logical),
                "--kernel-va", f"{KERNEL_VA:#x}",
                "--kernel-pa", f"{KERNEL_PA:#x}",
            ]
        )
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["guest"] == "11" * 32


def test_the_cli_refuses_a_dump_with_no_matching_struct(tmp_path: Path) -> None:
    """Nothing matching means nothing written.

    ``--out`` is only reached after the struct is found and the volume
    reassembled, so a dump with no signature in it leaves no file behind rather
    than a seed of zeros.
    """
    guest = FakeGuest(logical=1 << 20, resident={0: page_of("a page")})
    memory = bytearray(guest.memory())
    memory[guest.struct_offset:guest.struct_offset + 24] = ZERO
    dump = write_dump(
        tmp_path / "empty.kdump", [(LOAD.physical(GUEST_BASE), bytes(memory))]
    )
    out = tmp_path / "never-written.dbr"
    with pytest.raises(UniPadError) as refusal:
        rx.main(
            [
                "--dump", str(dump),
                "--out", str(out),
                "--bytes", str(1 << 20),
                "--kernel-va", f"{KERNEL_VA:#x}",
                "--kernel-pa", f"{KERNEL_PA:#x}",
            ]
        )
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert not out.exists()


def _first_difference(left: bytes, right: bytes) -> Optional[int]:
    """The first offset two byte strings differ at, or ``None``.

    Reported rather than left to a reader of a 1.5 MB diff, because "the round
    trip changed the seed" is not something anyone can act on.
    """
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return None if len(left) == len(right) else min(len(left), len(right))
