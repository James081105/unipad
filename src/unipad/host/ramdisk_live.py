"""Read the Data volume out of a **live** guest, by following its ``RamSparseDisk``.

Why this exists
---------------
Task #28 asks whether the Data volume's changes can be persisted host-side.
:mod:`unipad.host.ramdisk_extract` answers the other half of that question -- it
takes a whole-memory *dump* on this host, finds the struct, follows its pointers
and repacks the result -- and the repack half is byte-exact on the real seed.
What it cannot do is get that dump without stopping the guest, and a stopped
guest is a guest whose userland has not written anything yet.

So this module is the live-guest half, and the whole of the method was measured
end to end on the real guest before a line of it was written here.  The
``RamSparseDisk`` is, verbatim from ``SparseDisk.h``::

    struct RamSparseDisk {
      uint8_t **pages;          // offset 0
      uint64_t bytes, resident; // offsets 8 and 16
    };

``pages`` is an array of ``bytes / 4096`` pointers to **individually allocated**
4 KiB buffers -- ``ramSparseImport`` calls ``ops.allocate()`` once per page -- so
the volume is contiguous in its own index space and scattered in the guest's.
``sparse`` is a *member* of the ``NativeRamBlock`` IOService object
(``NativeRamBlock.cpp:55``) and ``sparse.pages`` is a separate
``IOMallocZero`` allocation, so nothing in the symbol table reaches either and
the only way in is by signature.

The four steps, in the order they have to happen
-----------------------------------------------
1. **Find the struct by scanning guest physical memory.**  ``pmemsave`` writes
   ``n`` bytes of guest physical memory to a host file; the scan walks physical
   ``[0x40000000, 0x40000000 + 0x180000000)`` -- the aarch64 ``virt`` RAM base
   and the board's 6 GiB -- in 32 MiB chunks, looking for an 8-byte field equal
   to the volume's logical size at struct offset +8.  The struct's physical
   address **varies between boots**, so this scan runs every time: a hardcoded
   address answers ``Unmapped`` on the next boot and nothing else.
2. **Translate with QEMU's own page-table walk.**  ``gva2gpa`` over HMP answers
   the question directly, which means no page-table walker has to be written
   here -- QEMU already has one, and a second copy of it would be a second thing
   to get wrong.
3. **Read the 2 MiB pointer table one page at a time.**  Its *virtual* range is
   contiguous; its *physical* pages are not.  Reading 2 MiB from the first
   page's physical address returns 261,902 non-null slots instead of 366 -- a
   table that is 99.86% garbage and looks like a volume.
4. **Read each resident page the same way**, and place it at its slot index,
   because the index is what says where in the volume a page goes.

The four traps this module is written after
-------------------------------------------
All four were hit for real, and each one produced an answer that looked like a
fact:

* **The QMP capabilities negotiation.**  A client that issues ``pmemsave``
  before ``qmp_capabilities`` is answered ``CommandNotFound: Expecting
  capabilities negotiation with 'qmp_capabilities'`` for *every* command -- which
  reads exactly like "this QEMU does not have that command".  That is how both
  ``pmemsave`` and ``gva2gpa`` were written off as unavailable, and how a 6 GiB
  scan was recorded as "zero candidates" when it had never read a byte.  So this
  module takes a :class:`~unipad.host.qmp.QmpClient`, which negotiates in
  :meth:`~unipad.host.qmp.QmpClient.connect`, and does not open a socket of its
  own.  A hand-rolled client that skips the negotiation fails here in a way that
  names the wrong cause.
* **``gva2gpa`` answers a labelled line.**  ``gpa: 0x59977000``, not a bare hex
  number.  Checking ``startswith("0x")`` reports "does not translate" for an
  address that had just translated perfectly.  :func:`guest_physical` parses the
  label out and refuses anything that is not one of the two shapes QEMU is
  measured to produce.
* **The virtual-address window.**  An earlier, narrower window
  ``[0xfffffff000000000, 0xfffffff800000000)`` silently discarded the one true
  candidate, whose ``pages`` pointer was ``0xfffffffb1ffba000`` -- above it.  A
  search that throws away the one real hit is indistinguishable from a search
  that found nothing, and it is the single most expensive mistake in this
  investigation.  The window here is ``[0xffffff0000000000,
  0x10000000000000000)``: the whole of the kernel's map, which is what ``TTBR1``
  addressing gives an aarch64 guest, and it is a module constant rather than a
  parameter so no caller can narrow it by accident.
* **``pmemsave`` writes a file and returns nothing.**  So the file is the only
  evidence a read happened, and a stale one left over from the previous read is
  indistinguishable from a fresh one of the same length.  Every read here
  unlinks the scratch file first.

The verified result, which is the control
-----------------------------------------
366 non-null of 262,144 slots, 366 pages read, reassembled 1,073,741,824 bytes,
sha256 ``d75f7752f4510856634fcf30f8b28e560783dff3037c1b1412c32d04f0bc7956`` --
byte-identical to the delivered seed's own logical image.  That is what an
extraction taken as the volume mounts is supposed to produce; an extraction
taken after the userland has written is supposed to differ, and the difference
is the persistence.

The signature is a filter, not an answer
----------------------------------------
Its specificity **degrades as the guest runs**, and that is measured rather than
assumed: on a boot where the Data volume has just mounted (2 processes) the
signature matches exactly one struct, and on a boot where the userland has been
running for ~18 minutes (100+ processes, SpringBoard up) it matches **38** --
guest RAM fills with unrelated data that happens to carry the volume's logical
size at struct offset +8 with a plausible ``resident`` and a kernel-map
``pages``.  A scanner that treated "exactly one match" as the definition of
success would therefore work on a freshly mounted guest and refuse a
long-running one, which is precisely the guest task #28 wants to read.

So :func:`scan_for_sparse_disk` treats a match as a candidate and verifies it
against the table it points at: one page first, as a cheap sieve, then the whole
table.  The volume's own table is made of kernel virtual addresses and its
non-null count is exactly ``resident // 4096`` -- the same number computed twice,
by ``ramSparseImport`` when it allocates and by ``resident`` when it counts.  A
false candidate either does not translate, translates to something that is not
pointer-shaped, or gives a count that disagrees.  Only the real struct produces
all three.  That verification is not an optimisation and it is not optional.

The digest comparison itself is
:func:`unipad.host.ramdisk_extract.verify_against_seed`, and it is not
reimplemented here: ``NativeRamBlock.cpp:236-240`` hashes the logical image page
by page into ``native_ram_block_seed_sha256``, and a second implementation of
that hash would be a second answer.

What is *not* proven here
------------------------
That a real guest's memory looks like the synthetic one in
``tests/unit/test_host_ramdisk_live.py``.  No QEMU, no kernel and no guest run
in the tests: the fake answers ``pmemsave`` and ``gva2gpa`` from an in-memory
model, chosen so the extractor has to follow pointers rather than read a range.
The measured numbers above came from the real guest, and a change to this module
that breaks them will not be caught by these tests -- which is why the
page-by-page path is pinned as a teeth test rather than left to inspection.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unipad.errors import ErrorCode, UniPadError
from unipad.host.guestobserve import GuestMemory
from unipad.host.packed_ramdisk import PAGE_SIZE
from unipad.host.ramdisk_extract import (
    DELIVERED_BYTES,
    FIELD_BYTES,
    FIELD_PAGES,
    FIELD_RESIDENT,
    STRUCT_SIZE,
)

__all__ = [
    "KERNEL_VA_HIGH",
    "KERNEL_VA_LOW",
    "LiveSparseDisk",
    "RAM_BASE",
    "RAM_SIZE",
    "SCAN_CHUNK",
    "extract_data_volume",
    "guest_physical",
    "read_virtual",
    "scan_for_sparse_disk",
]

#: Where the aarch64 ``virt`` machine's RAM starts.  Everything the scan reads
#: is at or above this, and a struct found below it is not in guest RAM.
RAM_BASE = 0x40000000

#: How much RAM the board gives the machine: 6 GiB, which is ``board.toml:92``'s
#: ``dram_size="0x180000000"`` and
#: :data:`unipad.host.unixnu_boot.MEMORY_MIB` counted in MiB.  The machine and
#: the device tree describe the same memory, so this is the scan's ceiling -- and
#: the struct was measured at physical ``0x55723900``, ``0x55720cc0`` and
#: ``0x54843cc0`` on three boots, all inside it.
RAM_SIZE = 0x180000000

#: How much guest physical memory one ``pmemsave`` asks for.  32 MiB is the
#: measured size that keeps the scratch file inside this host's disk while
#: making the number of round trips for a 6 GiB scan small enough to sit
#: through; it is a parameter because a host with less room wants less.
SCAN_CHUNK = 32 << 20

#: The bottom of the kernel's virtual map.  Every pointer ``sparse.pages``
#: holds is a kernel virtual address, and the three measured ones were
#: ``0xfffffffb1ffba000``, ``0xfffffffb1959c000`` and ``0xfffffff9370ba000`` --
#: all about 1 TiB above this floor, so the floor is the start of the ``TTBR1``
#: map rather than a value derived from the measurements.
KERNEL_VA_LOW = 0xFFFFFF0000000000

#: The top, exclusive: the end of the 64-bit address space.  ``TTBR1``
#: addressing puts the whole kernel map between ``0xffffff0000000000`` and
#: ``2**64``, so this is the entire kernel map and not a guess at a sub-range,
#: and all three measured pointers are inside it.
#:
#: The earlier window was ``[0xfffffff000000000, 0xfffffff800000000)`` and it
#: discarded the one real candidate -- its top was about 5 GiB *below* the lowest
#: pointer ever measured.  It is *not* a parameter: a caller who narrows this is
#: a caller reproducing that mistake, and the cost of the wide window is nothing.
KERNEL_VA_HIGH = 0x10000000000000000

#: The shape ``gva2gpa`` is measured to answer in: a labelled line, ``gpa:
#: 0x59977000``.  A bare hex number is accepted too, because that is what the
#: same command answers on other builds and because accepting it costs one
#: alternative in the pattern.  Anything else is refused with the answer quoted
#: rather than searched for a number inside it -- a reply such as ``invalid
#: address 0x1234`` has a perfectly good hex in it and is not a translation.
_GVA_LINE = re.compile(r"(?:gpa:\s*)?(0x[0-9a-fA-F]+)")


@dataclass(frozen=True)
class LiveSparseDisk:
    """The ``RamSparseDisk`` as the live guest holds it, in both address spaces.

    This is deliberately **not** :class:`unipad.host.ramdisk_extract.SparseDisk`.
    That one's ``struct_at`` is a *virtual* address, because it was found in a
    dump whose reader translates; here the struct is found by scanning physical
    memory and nothing in this module can go the other way -- ``gva2gpa``
    translates virtual to physical and there is no ``gpa2gva``.  So the two
    fields are named for the space each one is in rather than collapsed into one
    ``struct_at`` whose meaning would change with the module that produced it.
    A caller who fed this one's ``struct_at`` to a dump reader would be off by
    the kernel's whole load delta.

    ``pages_va`` is the value of the ``pages`` field: a kernel virtual address,
    which is what the guest stores.  ``pages`` is the pointer array read out,
    one entry per page of the volume in page order, zero where the guest never
    allocated.  Keeping the zeros matters, because page *index* is what places a
    page in the volume and a list of only the resident pages cannot say where
    any of them goes.
    """

    #: Where the struct was found, in guest **physical** memory.
    struct_pa: int
    #: The value of the ``pages`` field, a kernel **virtual** address.
    pages_va: int
    #: The volume's logical size, and the signature's search key.
    bytes: int
    #: 4096 per allocated page, as the driver's own count says.
    resident: int
    #: The pointer array, one entry per page, zero where nothing was allocated.
    pages: list[int]

    @property
    def page_count(self) -> int:
        """How many pages the volume has, allocated or not."""
        return self.bytes // PAGE_SIZE

    @property
    def resident_pages(self) -> int:
        """How many pages the guest actually allocated."""
        return sum(1 for address in self.pages if address)


# ---------------------------------------------------------------------------
# translating a virtual address, with QEMU's own page-table walk
# ---------------------------------------------------------------------------
def guest_physical(client: Any, vmaddr: int) -> int:
    """The guest physical address ``vmaddr`` maps to, via ``gva2gpa``.

    ``gva2gpa`` is an HMP command, so it goes through
    :meth:`~unipad.host.qmp.QmpClient.human_monitor` -- which means it goes
    through a client that has already negotiated ``qmp_capabilities``.  That
    matters more than it sounds: an unnegotiated client is answered
    ``CommandNotFound: Expecting capabilities negotiation`` for *every* command,
    which reads exactly like "this QEMU does not have that command", and that
    misreading is what cost this investigation days.

    Args:
        client: A :class:`~unipad.host.qmp.QmpClient`, or anything with the same
            ``human_monitor(command_line)`` shape.
        vmaddr: A guest **virtual** address.

    Returns:
        The guest physical address.

    Raises:
        UniPadError: ``evidence_incomplete`` when the guest does not map
            ``vmaddr``, or when the answer is not one of the two shapes QEMU is
            measured to produce.  Both are refusals rather than defaults: the
            alternative is returning a wrong address, and every page read
            through a wrong address is a page of the volume in the wrong place
            with nothing downstream able to tell.
    """
    answer = client.human_monitor(f"gva2gpa {vmaddr:#x}")
    text = answer.strip() if isinstance(answer, str) else ""
    # Two refusals, not one, because they are two different diagnoses: the guest
    # does not map the address, and the guest answered in a shape this module does
    # not accept.  Collapsing them would send a reader looking for a mapping
    # problem when QEMU had changed its output format.
    if "unmapped" in text.lower():
        raise UniPadError(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "the guest does not map this virtual address, so nothing can be read "
            "through it",
            vmaddr=f"{vmaddr:#x}",
            answer=text[:120],
        )
    match = _GVA_LINE.fullmatch(text)
    if match is None:
        raise UniPadError(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "gva2gpa answered in a shape this module does not accept; the two "
            "shapes QEMU is measured to produce are 'gpa: 0x...' and a bare hex "
            "number",
            vmaddr=f"{vmaddr:#x}",
            answer=text[:120],
        )
    return int(match.group(1), 16)


# ---------------------------------------------------------------------------
# reading the guest, one page at a time
# ---------------------------------------------------------------------------
def _read_physical(client: Any, scratch: Path, physical: int, size: int) -> bytes:
    """``size`` bytes of guest physical memory at ``physical``.

    :class:`unipad.host.guestobserve.GuestMemory` does the work -- it is this
    repository's ``pmemsave`` reader, it already refuses a short read, and a
    second implementation of it would be a second thing to get wrong.  What is
    added here is the unlink, and it is not tidiness: ``pmemsave`` writes a file
    and QMP returns nothing, so the file is the only evidence a read happened.
    Leaving the previous read's file in place means a ``pmemsave`` that wrote
    nothing at all returns the previous page's bytes with the right length --
    the same failure, one level worse, that made a 6 GiB scan report "zero
    candidates" after reading nothing.

    Raises:
        UniPadError: ``evidence_incomplete`` when the guest did not return the
            memory that was asked for.  A truncated buffer is a Data volume with
            the pages it did get in the places it did get them, and nothing
            downstream can tell.
    """
    target = Path(scratch)
    target.unlink(missing_ok=True)
    return GuestMemory(client, target).read(physical, size)


def read_virtual(client: Any, vmaddr: int, size: int, scratch: Path) -> bytes:
    """``size`` bytes of guest **virtual** memory at ``vmaddr``, page by page.

    A contiguous virtual range is not contiguous physically, and this module
    exists because of that one fact.  The 2 MiB pointer array of a 1 GiB volume
    is a single ``IOMallocZero`` allocation, so its virtual range is contiguous
    and its physical pages are not: reading 2 MiB from the first page's physical
    address returns 261,902 non-null slots instead of 366, which is a table that
    is 99.86% garbage and looks exactly like a volume.  So each page is
    translated on its own and read on its own, and there is no path through here
    that asks for more than one page at a time.

    Args:
        client: A :class:`~unipad.host.qmp.QmpClient`, or anything with
            ``human_monitor`` and ``execute``.
        vmaddr: The virtual address to start at.
        size: How many bytes.  A trailing partial page is read as one short
            read, because the translation is per page and the last page of a
            range may simply be shorter.
        scratch: Host file ``pmemsave`` writes into; unlinked before every read.

    Returns:
        Exactly ``size`` bytes.

    Raises:
        UniPadError: ``config_invalid`` for a negative size;
            ``evidence_incomplete`` when any page does not translate or does not
            come back whole.  Never a short buffer: a caller that got fewer
            bytes than it asked for would assemble a volume with a hole in it
            and no way to know.
    """
    if size < 0:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a read of a negative number of bytes is a defect in the caller",
            size=size,
        )
    if size == 0:
        return b""
    target = Path(scratch)
    image = bytearray(size)
    for offset in range(0, size, PAGE_SIZE):
        take = min(PAGE_SIZE, size - offset)
        physical = guest_physical(client, vmaddr + offset)
        image[offset:offset + take] = _read_physical(client, target, physical, take)
    return bytes(image)


# ---------------------------------------------------------------------------
# the signature
# ---------------------------------------------------------------------------
def _fields_are_sane(pages_ptr: int, size: int, resident: int, logical_bytes: int) -> bool:
    """Whether three 8-byte fields could be the ``RamSparseDisk``.

    The numeric half of :func:`unipad.host.ramdisk_extract`'s own predicate,
    restated rather than imported because the other half of that one -- "does the
    dump map this pointer" -- is a question about a file.  Here the equivalent
    question is "does ``gva2gpa`` translate this pointer", which is a round trip
    to the guest and belongs in the scan loop where the client is.

    A run of 24 bytes holding the volume's size at offset 8 is not hypothetical
    in a 6 GiB scan of a guest whose own volume is that size; what makes it the
    struct is that ``resident`` is a whole number of pages no larger than the
    volume, and that ``pages`` is inside the kernel's map.
    """
    # The size needs no rule of its own here: the scan found this offset by
    # searching for the exact bytes of ``logical_bytes``, so ``size`` is that
    # value by construction, and :func:`scan_for_sparse_disk` has already refused
    # a logical size that is not a whole number of pages.  A clause that cannot
    # reject anything is a reader's puzzle, so only the equality stays -- as an
    # assertion about the caller, not as a filter.
    if size != logical_bytes:
        return False
    if not resident or resident % PAGE_SIZE or resident > size:
        return False
    return KERNEL_VA_LOW <= pages_ptr < KERNEL_VA_HIGH


def _translates(client: Any, pages_ptr: int) -> bool:
    """Whether the guest maps ``pages_ptr`` at all.

    The live guest's half of "the pointer array is mapped", and the difference
    between a candidate and a coincidence: a run of bytes that holds the volume's
    size and a kernel-map address is common, and one whose address the guest
    actually maps is the struct.
    """
    try:
        guest_physical(client, pages_ptr)
    except UniPadError:
        return False
    return True


def _signature_hits(window: bytes, needle: bytes) -> Iterator[int]:
    """Every offset in ``window`` where the struct's ``bytes`` field sits."""
    at = window.find(needle)
    while at >= 0:
        yield at
        at = window.find(needle, at + 1)


# ---------------------------------------------------------------------------
# verifying a candidate
# ---------------------------------------------------------------------------
def _pointer_shaped(page: bytes) -> bool:
    """Whether a 4 KiB window could be a page of the ``RamSparseDisk``'s array.

    Every non-null 8-byte value has to be a kernel virtual address.  That is the
    whole test and it is a strong one: a window of guest RAM that happens to
    carry the volume's size at some offset is full of *other* data -- userland
    pointers, file contents, counter values, timestamps -- and almost none of
    that is a 47-bit kernel-map address.
    """
    for at in range(0, len(page) - 7, 8):
        value = struct.unpack_from("<Q", page, at)[0]
        if value and not (KERNEL_VA_LOW <= value < KERNEL_VA_HIGH):
            return False
    return True


def _volume_from_table(
    client: Any,
    struct_pa: int,
    pages_va: int,
    resident: int,
    slots: int,
    scratch: Path,
) -> LiveSparseDisk:
    """The candidate at ``pages_va``, verified against the table it points at.

    The one definition of "this candidate is the volume", shared by the
    single-candidate fast path and by the multi-candidate verification, so the
    two cannot drift into different answers about the same bytes.

    Raises:
        UniPadError: ``evidence_incomplete`` when the table this candidate points
            at is not made of kernel virtual addresses, or when its non-null count
            disagrees with ``resident``.  Either way the struct is not the
            volume's, and a volume assembled from it would be a plausible-looking
            disk built from the wrong pages.
    """
    target = Path(scratch)
    # Read page by page for the reason :func:`read_virtual` exists: the array's
    # virtual range is contiguous and its physical pages are not.
    table = read_virtual(client, pages_va, slots * 8, target)
    pages = list(struct.unpack_from(f"<{slots}Q", table))

    for index, address in enumerate(pages):
        if address and not (KERNEL_VA_LOW <= address < KERNEL_VA_HIGH):
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "the table this struct points at is not made of kernel virtual "
                "addresses, so the struct is not the volume's",
                struct_pa=f"{struct_pa:#x}",
                pages_va=f"{pages_va:#x}",
                slot=index,
                value=f"{address:#x}",
            )

    # ``ramSparseImport`` and ``ramSparseIO`` both store the pointer and then add
    # 4096 to ``resident``, and neither ever frees a page, so the two counts are
    # the same number computed twice.  A disagreement means either the struct
    # found is not the one, or the guest changed under the scan -- and both are
    # reasons to stop rather than to publish a volume.
    allocated = sum(1 for page in pages if page)
    if resident != allocated * PAGE_SIZE:
        raise UniPadError(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "the RamSparseDisk's resident count does not match the pages it "
            "points at, so the struct found is not the volume's",
            struct_pa=f"{struct_pa:#x}",
            resident=resident,
            allocated=allocated * PAGE_SIZE,
        )

    return LiveSparseDisk(
        struct_pa=struct_pa,
        pages_va=pages_va,
        # ``slots`` pages of ``PAGE_SIZE`` is the volume's logical size, which is
        # the number the scan was keyed on.
        bytes=slots * PAGE_SIZE,
        resident=resident,
        pages=pages,
    )


def _sieve(client: Any, pages_va: int, scratch: Path) -> bool:
    """Whether the first page of this candidate's table looks like pointers.

    One ``gva2gpa`` and one 4 KiB read, where :func:`_volume_from_table` is
    ``slots`` of each.  That is the whole point of it: when the signature matches
    38 candidates -- which it does on a guest whose userland has been running --
    reading every table in full would be 38 full traversals to answer a question
    one page each answers.

    A candidate this rejects is *skipped*, not fatal.  A false candidate is
    exactly what the multi-candidate path exists to discard, and one that does
    not even translate is discarded by the same rule.
    """
    try:
        physical = guest_physical(client, pages_va)
        page = _read_physical(client, Path(scratch), physical, PAGE_SIZE)
    except UniPadError:
        return False
    return _pointer_shaped(page)


def scan_for_sparse_disk(
    client: Any,
    *,
    logical_bytes: int = DELIVERED_BYTES,
    ram_base: int = RAM_BASE,
    ram_size: int = RAM_SIZE,
    chunk: int = SCAN_CHUNK,
    scratch: Path,
) -> LiveSparseDisk:
    """Find the ``RamSparseDisk`` in the live guest and read its page array.

    The scan walks guest physical memory in ``chunk``-sized ``pmemsave`` reads
    and looks for the signature.  It runs **every time**: the struct's physical
    address varies between boots, so an address measured once answers
    ``Unmapped`` on the next boot and nothing else.  Nothing here is cached
    between calls, and a caller that wants the same answer twice is a caller
    asking about the same guest twice.

    Chunks are overlapped by ``STRUCT_SIZE - 1`` bytes, because a struct that
    straddles a chunk boundary is a struct the scan would otherwise miss -- and
    whether the volume is found would then depend on where the kernel happened
    to allocate it, which is a defect that looks like a guest with no Data
    volume in it.  The overlap cannot produce a duplicate: a 24-byte window that
    fits inside one chunk cannot also start inside the 23 bytes the next chunk
    carries forward.

    **The signature is a filter, not an answer.**  Its specificity degrades as
    the guest runs: measured, a boot where the Data volume has just mounted (2
    processes) matches exactly one struct, and a boot where the userland has been
    running for ~18 minutes (100+ processes, SpringBoard up) matches **38** --
    guest RAM fills with unrelated data that happens to carry ``logical_bytes``
    at struct offset +8 with a plausible ``resident`` and a kernel-map ``pages``.
    So when the signature matches more than once, each candidate is verified
    against the table it points at -- one page first as a cheap sieve, then the
    whole table -- and the one whose table is made of kernel virtual addresses
    and whose non-null count matches its ``resident`` is returned.  Only the real
    struct produces that.  That verification is not an optimisation; without it
    this module cannot extract the "after the userland has written" state, which
    is the whole point of task #28.

    Args:
        client: A :class:`~unipad.host.qmp.QmpClient`.
        logical_bytes: The volume's logical size, the signature's search key.
        ram_base: Where guest physical memory starts.
        ram_size: How much of it to scan.
        chunk: How much one ``pmemsave`` asks for.
        scratch: Host file ``pmemsave`` writes into; unlinked before every read.

    Returns:
        The struct, with its page array read out and cross-checked.

    Raises:
        UniPadError: ``config_invalid`` for a volume size, range or chunk that
            cannot describe anything; ``evidence_incomplete`` when the signature
            matches nothing, when it matches several and none of them verifies,
            or when several verify -- the last of which says so in the message,
            because it means the verification is not specific enough and picking
            one would be a guess.  Every one of those is a refusal to guess: a
            wrong struct yields a plausible-looking volume assembled from the
            wrong pages, which is worse than no volume, because it would be
            believed.
    """
    if logical_bytes <= 0 or logical_bytes % PAGE_SIZE:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the volume's logical size must be a whole number of pages",
            logical_bytes=logical_bytes,
            page_size=PAGE_SIZE,
        )
    if ram_size <= 0:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a scan of no guest memory finds nothing, and saying so as a "
            "refusal would be a lie about why",
            ram_size=ram_size,
        )
    if chunk <= 0:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a scan chunk must be at least one byte",
            chunk=chunk,
        )

    target = Path(scratch)
    needle = struct.pack("<Q", logical_bytes)
    candidates: list[tuple[int, int, int]] = []
    carry = b""
    start = ram_base
    end = ram_base + ram_size
    while start < end:
        size = min(chunk, end - start)
        window = carry + _read_physical(client, target, start, size)
        # Where ``window``'s byte 0 sits in guest physical memory.  It is behind
        # ``start`` by exactly the bytes carried forward from the chunk before.
        base = start - len(carry)
        for at in _signature_hits(window, needle):
            # The ``bytes`` field is at struct offset +8, so the struct starts
            # eight bytes before the hit -- and both ends have to be inside this
            # window for the fields to be readable at all.
            if at < FIELD_BYTES or at + STRUCT_SIZE - FIELD_BYTES > len(window):
                continue
            head = window[at - FIELD_BYTES:at - FIELD_BYTES + STRUCT_SIZE]
            # Named offsets rather than a tuple unpack, so each field below says
            # which one it is about -- the three offsets *are* the signature,
            # and a reader who has to count to eight to find out which is which
            # is a reader who cannot check it.  The order is also the order
            # ``SparseDisk.h`` declares them in.
            pages_ptr = struct.unpack_from("<Q", head, FIELD_PAGES)[0]
            size_field = struct.unpack_from("<Q", head, FIELD_BYTES)[0]
            resident = struct.unpack_from("<Q", head, FIELD_RESIDENT)[0]
            if not _fields_are_sane(pages_ptr, size_field, resident, logical_bytes):
                continue
            if not _translates(client, pages_ptr):
                continue
            candidates.append((base + at - FIELD_BYTES, pages_ptr, resident))
        carry = window[-(STRUCT_SIZE - 1):]
        start += size

    if not candidates:
        raise UniPadError(
            ErrorCode.EVIDENCE_INCOMPLETE,
            f"the live guest does not hold a RamSparseDisk of {logical_bytes} bytes",
            logical_bytes=logical_bytes,
            matches=0,
            candidates=[],
        )

    slots = logical_bytes // PAGE_SIZE

    # The fast path.  One candidate means there is nothing to disambiguate, so the
    # one-page sieve below would only add a round trip -- and the just-mounted
    # guest is the case that must not pay for it.  The full read still applies
    # every check, including the ones the sieve performs.
    if len(candidates) == 1:
        struct_pa, pages_va, resident = candidates[0]
        return _volume_from_table(client, struct_pa, pages_va, resident, slots, target)

    # More than one candidate: the signature is a *filter* here, not an answer.
    # See the module docstring -- this is the normal state of a guest whose
    # userland has been running, not an anomaly.
    verified: list[LiveSparseDisk] = []
    for struct_pa, pages_va, resident in candidates:
        if not _sieve(client, pages_va, target):
            continue
        try:
            verified.append(
                _volume_from_table(client, struct_pa, pages_va, resident, slots, target)
            )
        except UniPadError:
            # This candidate is not the volume.  It is a false positive, and
            # discarding it is the whole purpose of the loop.
            continue

    if len(verified) == 1:
        return verified[0]
    if not verified:
        raise UniPadError(
            ErrorCode.EVIDENCE_INCOMPLETE,
            f"the signature matched {len(candidates)} candidates and none of them "
            "points at a table of kernel virtual addresses whose non-null count "
            "matches its resident count, so the volume is not among them",
            logical_bytes=logical_bytes,
            matches=len(candidates),
            verified=0,
            candidates=[f"{address:#x}" for address, _pages, _resident in candidates],
        )
    raise UniPadError(
        ErrorCode.EVIDENCE_INCOMPLETE,
        f"{len(verified)} candidates verified, so the verification is not specific "
        "enough to choose between them",
        logical_bytes=logical_bytes,
        matches=len(candidates),
        verified=len(verified),
        candidates=[f"{disk.struct_pa:#x}" for disk in verified],
    )


# ---------------------------------------------------------------------------
# the volume
# ---------------------------------------------------------------------------
def extract_data_volume(
    client: Any,
    *,
    scratch: Path,
    logical_bytes: int = DELIVERED_BYTES,
    ram_base: int = RAM_BASE,
    ram_size: int = RAM_SIZE,
    chunk: int = SCAN_CHUNK,
) -> bytes:
    """The Data volume's logical image, read straight out of the live guest.

    The scan comes first and the pages second, and that order is the whole
    design: the scan refuses a wrong signature before a single one of the
    volume's pages is read, so a signature that matches nothing costs one
    traversal of guest memory instead of a full extraction that produces a
    plausible-looking volume from the wrong struct.

    The resident pages are read in **slot order**, not in ascending address
    order.  :mod:`unipad.host.ramdisk_extract` sorts by address because a dump's
    segments are forward-only byte sources and a scattered read order would
    re-inflate a segment per page; here every page is its own ``pmemsave``, so
    order costs nothing and slot order keeps "the index places the page" in one
    place instead of two.

    A page the guest never allocated is a real zero page -- ``ramSparseIO``
    serves the static zero page for it -- so the absent pages are left as zeros
    rather than filled with whatever was in the buffer.

    Args:
        client: A :class:`~unipad.host.qmp.QmpClient`.
        scratch: Host file ``pmemsave`` writes into; unlinked before every read.
        logical_bytes: The volume's logical size, the signature's search key.
        ram_base: Where guest physical memory starts.
        ram_size: How much of it to scan.
        chunk: How much one scan ``pmemsave`` asks for.

    Returns:
        The logical image, ``logical_bytes`` long.

    Raises:
        UniPadError: whatever :func:`scan_for_sparse_disk` and
            :func:`read_virtual` raise.  The digest check is deliberately not
            here: :func:`unipad.host.ramdisk_extract.verify_against_seed` is
            that comparison against the seed, and it needs a file this function
            does not have.
    """
    target = Path(scratch)
    disk = scan_for_sparse_disk(
        client,
        logical_bytes=logical_bytes,
        ram_base=ram_base,
        ram_size=ram_size,
        chunk=chunk,
        scratch=target,
    )
    image = bytearray(disk.bytes)
    for index, page_va in enumerate(disk.pages):
        if not page_va:
            continue
        start = index * PAGE_SIZE
        image[start:start + PAGE_SIZE] = read_virtual(client, page_va, PAGE_SIZE, target)
    return bytes(image)
