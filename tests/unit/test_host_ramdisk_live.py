"""``unipad.host.ramdisk_live`` against a synthetic guest with a fake QMP client.

Why this file exists
--------------------
The live half of task #28 was measured end to end on the real guest before any of
it was written into the repository: 366 non-null of 262,144 slots, 366 pages
read, 1,073,741,824 bytes reassembled, sha256
``d75f7752f4510856634fcf30f8b28e560783dff3037c1b1412c32d04f0bc7956`` --
byte-identical to the delivered seed's own logical image.  This file is the proof
that the *method* survived being moved out of a gitignored diagnostic script and
into ``src/``.

The fake guest below is a ``bytearray`` of physical memory plus a VA-to-PA map,
answering exactly the two commands the module uses: ``pmemsave`` writes bytes
from the array to a file -- because that is what QEMU's ``pmemsave`` does, there
is no QMP command that returns bytes -- and ``gva2gpa`` consults the map.  A fake
that returned memory directly would test a transport this module never uses.

The defects these tests are written after
-----------------------------------------
Every one of them was hit for real on the real guest, and each produced an answer
that looked like a fact:

* **The capabilities negotiation.**  A QMP client that issues a command before
  ``qmp_capabilities`` is answered ``CommandNotFound: Expecting capabilities
  negotiation`` for *every* command, which reads exactly like "this QEMU does not
  have that command".  That is how ``pmemsave`` and ``gva2gpa`` were both written
  off as unavailable and how a 6 GiB scan was recorded as "zero candidates" when
  it had never read a byte.  The fake here is handed straight to the module, so
  the negotiation is ``QmpClient``'s business and not this module's -- but the
  module must not open a socket of its own, and the tests would notice.
* **The labelled ``gva2gpa`` answer.**  ``gpa: 0x59977000``, not a bare hex
  number.  A ``startswith("0x")`` check reports "does not translate" for an
  address that had just translated perfectly.
* **A contiguous virtual range read as a contiguous physical one.**  The 2 MiB
  pointer array is one ``IOMallocZero``, so its virtual range is contiguous and
  its physical pages are not.  Reading 2 MiB from the first page's physical
  address returns 261,902 non-null slots instead of 366 -- a table that is 99.86%
  garbage and looks like a volume.  This is the teeth test: if anyone
  "simplifies" :func:`read_virtual` into one ``pmemsave``, it goes red, and it
  goes red by showing that the contiguous read is *observably* wrong rather than
  merely different.
* **The virtual-address window.**  An earlier, narrower window
  ``[0xfffffff000000000, 0xfffffff800000000)`` silently discarded the one true
  candidate, whose ``pages`` pointer was above it.  A search that throws away the
  one real hit is indistinguishable from a search that found nothing.  The fake's
  ``pages`` pointer is deliberately above that window for the same reason.
* **A short read.**  A truncated buffer becomes a Data volume with the pages it
  did get in the places it did get them, and nothing downstream can tell.
* **A read that produced no file at all.**  ``pmemsave`` writes a file and QMP
  returns nothing, so the file is the only evidence a read happened -- and a
  stale one is the right length.  A reader that did not unlink first hands back
  the previous page for a read that returned nothing, which is the failure that
  made a 6 GiB scan report "zero candidates" after reading nothing, one level
  worse.
* **A struct that straddles a scan boundary.**  The scan walks physical memory in
  fixed chunks, so a struct crossing a chunk edge is found only if those bytes
  are carried forward -- otherwise whether the volume is found depends on where
  the kernel happened to allocate it, which is a defect that looks like a guest
  with no Data volume in it.  This is the same defect
  ``test_host_ramdisk_extract.py`` pins for the dump reader, and it is pinned here
  too because the two scanners are two scanners.
* **A cached address.**  The struct's physical address varies between boots, so
  the scan has to run every time; an address measured once answers ``Unmapped``
  on the next boot.
* **A signature that is not specific enough.**  A ``pages`` pointer the guest does
  not map, and a ``resident`` count that is not a whole number of pages, are both
  coincidences rather than the struct -- and a ``resident`` count that disagrees
  with the array is the same number computed twice, so a disagreement means the
  struct found is not the volume's.
* **A signature whose specificity degrades as the guest runs.**  Measured: a boot
  where the Data volume has just mounted (2 processes) matches exactly one struct,
  and a boot where the userland has been running for ~18 minutes (100+ processes,
  SpringBoard up) matches **38** -- guest RAM fills with unrelated data carrying
  the volume's size at offset +8 with a plausible ``resident`` and a kernel-map
  ``pages``.  So "exactly one match" cannot be the definition of success: a
  scanner built that way works on a fresh guest and refuses a long-running one,
  which is the guest task #28 exists to read.  The tests below therefore cover
  one candidate (the fast path, which must not pay for disambiguation it does not
  need), several candidates where only one verifies, several that all verify (a
  refusal that says the verification is not specific enough), and one that does
  not translate at all (skipped, not fatal).

What is *not* proven here
------------------------
That a real guest's memory looks like this.  No QEMU, no kernel, no booted guest
on this host: the layout is synthetic, chosen so the extractor has to follow
pointers rather than read a range, and so that the pointer table's physical pages
are far enough apart that a contiguous read is provably wrong.  The measured
numbers above came from the real guest, and a change that breaks them will not be
caught here -- which is why the page-by-page path is a teeth test rather than a
hope.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any, Optional

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import ramdisk_live as live
from unipad.host.packed_ramdisk import PAGE_SIZE as PAGE
from unipad.host.ramdisk_extract import STRUCT_SIZE

#: How much guest physical memory the fake has, as an offset from
#: :data:`unipad.host.ramdisk_live.RAM_BASE`.  The aarch64 ``virt`` machine's RAM
#: starts at ``0x40000000`` and the fake's ``bytearray`` is indexed from there,
#: so every layout constant below is an offset rather than an address.
RAM_BYTES = 8 << 20

#: The volume's logical size: 4 MiB, not the delivered 1 GiB.  That is a
#: deliberate choice and not a shortcut -- 4 MiB gives 1024 pointer slots, which
#: is an 8 KiB pointer table, which is **two** pages.  A 1 MiB volume would give a
#: 2 KiB table that fits in one page, and the page-by-page path would never be
#: exercised by anything except the dedicated test below.
LOGICAL = 4 << 20

#: How much one scan ``pmemsave`` asks for.  The module's default is 32 MiB over
#: 6 GiB, which is 192 reads; 1 MiB over this fake's 8 MiB is 8, which keeps the
#: tests quick and -- more to the point -- lets the straddle test below put a
#: struct exactly on a chunk edge, which a chunk the size of the whole guest
#: could not do.
CHUNK = 1 << 20

#: Where each piece of the layout sits, as an offset from ``RAM_BASE``.
OFF_STRUCT = 0x00100000
OFF_TABLE = 0x00200000
OFF_PAGES = 0x00400000
OFF_DECOY = 0x00600000
OFF_EXTRA = 0x00700000

#: How far apart the pointer table's physical pages are placed, and how far apart
#: the resident pages are.  Both are *separately allocated* in the guest --
#: ``ramSparseImport`` calls ``ops.allocate()`` per page -- so an extractor that
#: read a contiguous range instead of following the pointers would get a hole
#: where the second page is, which is exactly the defect the whole design exists
#: to avoid.
TABLE_STRIDE = 0x8000
PAGE_STRIDE = 0x8000

#: The kernel's virtual map on this build.  ``pages`` points at
#: ``0xFFFFFFFB1FFB0000``, which is *above* the window that hid the real
#: candidate -- so a scan that narrows the window back finds nothing here, which
#: is the failure being pinned.
KERNEL_VA_BASE = 0xFFFFFFFB00000000
VA_TABLE = KERNEL_VA_BASE + 0x01FFB0000

#: The top of the window that discarded the one true candidate.  Stated as a
#: constant so the test can say what the old code did rather than describing it.
NARROW_WINDOW_HIGH = 0xFFFFFFF800000000

#: How far apart the resident pages' *virtual* addresses are.  Unrelated to
#: :data:`PAGE_STRIDE` on purpose: the guest allocates the pages sequentially
#: from its heap whatever their slot index is, and a layout that made the two
#: strides equal would be a layout that only works by coincidence.
VA_PAGE_STRIDE = 0x20000

#: A kernel-map address the fake guest does not map.  Inside the window, so the
#: signature's numeric rules pass and only the translation rejects it -- which is
#: the difference between "the pointer is in the kernel's map" and "the pointer
#: is a pointer".
UNMAPPED_KERNEL_VA = KERNEL_VA_BASE + 0x07FFFFF00

#: The virtual address of a resident page, which is where the decoy's ``pages`` is
#: aimed when a test wants a candidate that translates perfectly and is still not
#: the volume.  It holds the volume's own *contents* -- 4 KiB of ASCII with a
#: slot index in front -- so it is the exact shape of a false positive: mapped,
#: plausible, and not a table of pointers.
DECOY_TABLE_LOOKALIKE = KERNEL_VA_BASE + 0x020000000

#: Where a test puts a hand-built two-page "table": page zero shaped like
#: pointers, page one not.  The sieve reads page zero, so this is the only shape
#: that reaches the whole-table check -- and the only thing that can prove the
#: whole-table check exists.
SPLIT_TABLE_VA = KERNEL_VA_BASE + 0x040000000

#: Which slots of the volume the guest allocated.  Index 100 is there so a page
#: landing in the wrong place is visible: a reassembly that dropped the absent
#: pages or put them in read order would be wrong at offset 100.  Index 600 is
#: there for a different reason -- it falls in the *second* page of the pointer
#: table, so a contiguous physical read of the table (which gets the first page
#: and then the gap) is byte-different from the page-by-page one.  Without it the
#: two table pages are both almost entirely zeros and the teeth test below would
#: pass either way.
RESIDENT = (0, 1, 5, 100, 600)


def page_of(index: int) -> bytes:
    """A 4 KiB page whose first eight bytes name the slot it belongs in.

    Every byte is derived from the index, so two pages are never equal and a page
    swapped for another is visible without reading the whole volume.
    """
    body = struct.pack("<Q", index) + b"the Data volume's page"
    return (body * (PAGE // len(body) + 1))[:PAGE]


# ---------------------------------------------------------------------------
# a synthetic guest
# ---------------------------------------------------------------------------
class FakeGuest:
    """Guest physical memory, a VA-to-PA map, and the two commands QEMU answers.

    ``pmemsave`` writes to a file because that is what QEMU's ``pmemsave`` does
    -- there is no QMP command that returns bytes -- so the fake has to reproduce
    that shape rather than returning a value.  A fake that returned bytes would
    test a transport this module never uses, and would also hide the stale-scratch
    hazard the module unlinks against.

    The layout is laid out in the constructor rather than by the tests, so every
    test starts from the same guest and a test that wants a different one says
    which knob it turned.
    """

    def __init__(
        self,
        *,
        logical: int = LOGICAL,
        resident: tuple[int, ...] = RESIDENT,
        ram_bytes: int = RAM_BYTES,
        struct_at: int = OFF_STRUCT,
        lay_struct: bool = True,
        decoy_at: Optional[int] = None,
        decoy_pages: Optional[int] = None,
        decoy_resident: Optional[int] = None,
        struct_resident: Optional[int] = None,
        bare: bool = False,
        short: Optional[dict[int, int]] = None,
        silent: Optional[set[int]] = None,
        extra_pages: Optional[dict[int, bytes]] = None,
    ) -> None:
        self.logical = logical
        self.resident = resident
        self.ram_bytes = ram_bytes
        self.struct_at = struct_at
        self.lay_struct = lay_struct
        self.decoy_at = decoy_at
        #: The decoy's ``pages`` field, when a test wants it to be an address the
        #: guest does not map.  ``None`` points it at the real array, which is
        #: what makes two candidates indistinguishable.
        self.decoy_pages = decoy_pages
        #: The decoy's ``resident`` field, when a test needs the two structs to
        #: disagree about it.  ``None`` makes it agree, which is the ambiguous
        #: case.
        self.decoy_resident = decoy_resident
        #: The value the struct's ``resident`` field carries, when a test wants it
        #: to disagree with the array it points at.
        self.struct_resident = struct_resident
        #: Hand-built virtual pages, as ``{virtual: bytes}``.  Each is mapped to
        #: its own physical page, so a test can build a "table" of any shape
        #: without disturbing the layout above.
        self.extra_pages = dict(extra_pages or {})
        #: Whether ``gva2gpa`` answers a bare hex number instead of the labelled
        #: line QEMU is measured to produce.  Both are accepted, and the
        #: difference between them is the trap.
        self.bare = bare
        #: Guest physical addresses whose ``pmemsave`` returns fewer bytes than
        #: were asked for, as ``{address: bytes_written}``.
        self.short = dict(short or {})
        #: Guest physical addresses whose ``pmemsave`` writes no file at all.
        #: That is what a refused command does, and it is the shape that made a
        #: 6 GiB scan report "zero candidates" after reading nothing.
        self.silent = set(silent or ())
        self.ram = bytearray(ram_bytes)
        #: Virtual address to physical address.  Deliberately **not** a function
        #: of the address: the guest's pages are scattered, and a map that was
        #: ``va - constant`` would make a contiguous read correct and the whole
        #: design untestable.
        self.translations: dict[int, int] = {}
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self._lay_out()

    # -- the layout ------------------------------------------------------

    @property
    def struct_pa(self) -> int:
        """Where the struct sits, in guest physical memory."""
        return live.RAM_BASE + self.struct_at

    @property
    def pages_va(self) -> int:
        """The value of the struct's ``pages`` field: a kernel virtual address."""
        return VA_TABLE

    @property
    def resident_bytes(self) -> int:
        """``resident`` as the driver counts it: 4096 per allocated page."""
        return len(self.resident) * PAGE

    @property
    def struct_resident_bytes(self) -> int:
        """What the struct's ``resident`` field says, lied or not."""
        return self.resident_bytes if self.struct_resident is None else self.struct_resident

    @property
    def table_pages(self) -> int:
        """How many 4 KiB pages the pointer array occupies."""
        return (self.logical // PAGE * 8 + PAGE - 1) // PAGE

    def page_va(self, index: int) -> int:
        """The virtual address of the page in slot ``index``."""
        return KERNEL_VA_BASE + 0x020000000 + index * VA_PAGE_STRIDE

    def span(self) -> int:
        """How much of the physical memory the layout actually needs."""
        needed = max(
            OFF_TABLE + self.table_pages * TABLE_STRIDE,
            OFF_PAGES + len(self.resident) * PAGE_STRIDE,
            OFF_EXTRA + len(self.extra_pages) * PAGE_STRIDE,
        )
        for offset in (self.struct_at, self.decoy_at):
            if offset is not None:
                needed = max(needed, offset + STRUCT_SIZE)
        return needed

    def _lay_out(self) -> None:
        """Write the struct, the pointer array and the resident pages.

        Raises:
            ValueError: when the memory asked for is smaller than the layout
                needs.  A ``bytearray`` slice assignment past the end *appends at
                the end* rather than writing where it was aimed, so an undersized
                guest would quietly put every page somewhere the pointer array
                does not say -- a guest that reassembles to a hole and looks like
                a defect in the extractor.
        """
        if self.ram_bytes < self.span():
            raise ValueError(
                f"{self.ram_bytes} bytes of guest memory cannot hold this layout, "
                f"which needs {self.span()}"
            )
        slots = self.logical // PAGE
        entries = [0] * slots
        for index in self.resident:
            entries[index] = self.page_va(index)
        self.table = struct.pack(f"<{slots}Q", *entries)

        # The pointer array.  Its virtual range is contiguous and its physical
        # pages are TABLE_STRIDE apart, which is the one property the whole
        # module turns on: reading the array as one contiguous physical block
        # returns the first page and then whatever happens to be in the gap.
        for offset in range(0, len(self.table), PAGE):
            page = self.table[offset:offset + PAGE]
            physical = OFF_TABLE + (offset // PAGE) * TABLE_STRIDE
            self.ram[physical:physical + len(page)] = page
            # A full guest physical address, because that is what ``gva2gpa``
            # returns and what ``pmemsave`` takes -- the fake's ``ram`` is
            # indexed by offset from RAM_BASE, the map is not.
            self.translations[VA_TABLE + offset] = live.RAM_BASE + physical

        # The resident pages, separately allocated, each at its own physical
        # address and its own virtual one.
        for position, index in enumerate(self.resident):
            vmaddr = self.page_va(index)
            physical = OFF_PAGES + position * PAGE_STRIDE
            self.ram[physical:physical + PAGE] = page_of(index)
            self.translations[vmaddr] = live.RAM_BASE + physical

        # The struct, and a decoy when the test wants two candidates.  The decoy
        # points at the same array, because the point of the decoy is that the
        # signature cannot tell the two apart -- not that one of them is broken.
        structs = ([self.struct_at] if self.lay_struct else []) + (
            [self.decoy_at] if self.decoy_at is not None else []
        )
        # The real struct points at the array this guest actually laid out; a
        # decoy points wherever the test aimed it, defaulting to the same array so
        # that two candidates really are indistinguishable.
        pages_of = {
            self.struct_at: VA_TABLE,
            self.decoy_at: VA_TABLE if self.decoy_pages is None else self.decoy_pages,
        }
        resident_of = {
            self.struct_at: self.struct_resident_bytes,
            self.decoy_at: (
                self.resident_bytes if self.decoy_resident is None else self.decoy_resident
            ),
        }
        for offset in structs:
            self.ram[offset:offset + STRUCT_SIZE] = struct.pack(
                "<QQQ", pages_of[offset], self.logical, resident_of[offset]
            )

        # Hand-built pages, last, so a test can aim a decoy at a shape of its own.
        for position, (vmaddr, page) in enumerate(self.extra_pages.items()):
            physical = OFF_EXTRA + position * PAGE_STRIDE
            self.ram[physical:physical + len(page)] = page
            self.translations[vmaddr] = live.RAM_BASE + physical

    def logical_image(self) -> bytes:
        """The volume as the guest holds it: the resident pages, zeros elsewhere."""
        image = bytearray(self.logical)
        for index in self.resident:
            image[index * PAGE:(index + 1) * PAGE] = page_of(index)
        return bytes(image)

    # -- the two commands -------------------------------------------------

    def execute(
        self, command: str, arguments: Optional[dict[str, Any]] = None, **_: Any
    ) -> dict[str, Any]:
        """Answer ``pmemsave`` the way QEMU does: by writing a file.

        Raises:
            AssertionError: for any other command, and for a read outside the
                fake's memory.  Both mean the test asked the guest something it
                was not built to answer, and the answer would be a fiction.
        """
        payload = dict(arguments or {})
        self.commands.append((command, payload))
        if command != "pmemsave":
            raise AssertionError(f"the extractor issued an unexpected command: {command}")
        val, size = int(payload["val"]), int(payload["size"])
        start = val - live.RAM_BASE
        if start < 0 or size < 0 or start + size > len(self.ram):
            raise AssertionError(f"pmemsave outside the fake guest's RAM: {val:#x} +{size}")
        data = bytes(self.ram[start:start + size])
        if val in self.short:
            data = data[: self.short[val]]
        if val not in self.silent:
            Path(payload["filename"]).write_bytes(data)
        return {"return": {}}

    def human_monitor(self, command_line: str, timeout: Optional[float] = None) -> str:
        """Answer ``gva2gpa`` from the map, or ``Unmapped``.

        The labelled form is the default because it is what QEMU is measured to
        produce; ``bare=True`` gives the other shape, so the parser has to accept
        both and neither by accident.

        Recorded in ``commands`` like ``pmemsave`` is, so a test can count the
        round trips of each kind -- which is the only way to pin that the
        single-candidate path does not pay for a sieve it does not need.
        """
        self.commands.append(("human-monitor-command", {"command-line": command_line}))
        match = re.fullmatch(r"gva2gpa (0x[0-9a-fA-F]+)", command_line.strip())
        if match is None:
            raise AssertionError(f"unexpected monitor command: {command_line!r}")
        vmaddr = int(match.group(1), 16)
        if vmaddr not in self.translations:
            return "Unmapped"
        gpa = self.translations[vmaddr]
        return f"{gpa:#x}" if self.bare else f"gpa: {gpa:#x}"


class ChattyGuest:
    """A guest that answers with something containing a hex and meaning nothing.

    ``invalid address 0x1234`` has a perfectly good address in it.  A parser that
    searched the answer for a hex would return ``0x1234`` and read a page of the
    wrong guest memory, so :func:`guest_physical` accepts only the two shapes
    QEMU is measured to produce and quotes what it got otherwise.
    """

    def human_monitor(self, command_line: str, timeout: Optional[float] = None) -> str:
        return "invalid address 0x1234"


# ---------------------------------------------------------------------------
# the calls under test
# ---------------------------------------------------------------------------
def scan(guest: FakeGuest, scratch: Path, **kwargs: Any) -> live.LiveSparseDisk:
    """The scan over one fake guest, with the guest's own size as the range."""
    return live.scan_for_sparse_disk(
        guest,
        logical_bytes=guest.logical,
        ram_base=live.RAM_BASE,
        ram_size=guest.ram_bytes,
        chunk=CHUNK,
        scratch=scratch,
        **kwargs,
    )


def extract(guest: FakeGuest, scratch: Path, **kwargs: Any) -> bytes:
    """The extraction over one fake guest, the same way."""
    return live.extract_data_volume(
        guest,
        scratch=scratch,
        logical_bytes=guest.logical,
        ram_base=live.RAM_BASE,
        ram_size=guest.ram_bytes,
        chunk=CHUNK,
        **kwargs,
    )


def pmemsaves(guest: FakeGuest) -> int:
    """How many ``pmemsave`` round trips the guest has answered so far."""
    return sum(1 for name, _ in guest.commands if name == "pmemsave")


def translates(guest: FakeGuest) -> int:
    """How many ``gva2gpa`` round trips the guest has answered so far."""
    return sum(1 for name, _ in guest.commands if name == "human-monitor-command")


def test_the_single_candidate_path_does_not_pay_for_the_verification(tmp_path: Path) -> None:
    """One candidate is read once, not sieved and then read.

    The just-mounted guest is the case that must not pay for disambiguation it
    does not need.  The sieve exists so that 38 candidates cost 38 pages rather
    than 38 tables; with one candidate there is one table read either way, and
    sieving first would add a translate and a page to a read that is already
    going to happen.  So the round-trip count is exactly the scan's chunks plus
    the table's pages, and nothing else.
    """
    guest = FakeGuest()
    disk = scan(guest, tmp_path / "scratch.bin")
    assert disk.resident_pages == len(guest.resident)

    # One translate per page of the table, plus the one the signature's own
    # mapped-pointer check performs.  A sieve would make it one more.
    assert translates(guest) == guest.table_pages + 1, (
        f"{translates(guest)} gva2gpa calls for a {guest.table_pages}-page table: "
        "the one-page sieve should not run when there is nothing to disambiguate"
    )
    # ...and the same for the reads: the chunks the scan walked, plus the table.
    assert pmemsaves(guest) == guest.ram_bytes // CHUNK + guest.table_pages


# ---------------------------------------------------------------------------
# the extraction
# ---------------------------------------------------------------------------
def test_the_extractor_reassembles_the_logical_image_the_fake_guest_holds(
    tmp_path: Path,
) -> None:
    """Scan, follow the pointers, read the pages: the volume comes back whole.

    This is the control for the whole module, and it is the same comparison the
    real guest run made against the seed's own digest -- here against the image
    the fake guest holds.  The resident pages are at slot indices 0, 1, 5 and 100
    and the rest of the volume is zeros, so a reassembly that dropped the absent
    pages, put a page in the wrong slot, or read a contiguous range instead of
    following the pointers is wrong in a way this sees.
    """
    guest = FakeGuest()
    volume = extract(guest, tmp_path / "scratch.bin")

    assert volume == guest.logical_image()
    assert len(volume) == guest.logical
    # Each resident page is where its slot says, not where the read order put it.
    assert volume[:8] == struct.pack("<Q", 0)
    assert volume[PAGE:PAGE + 8] == struct.pack("<Q", 1)
    assert volume[5 * PAGE:5 * PAGE + 8] == struct.pack("<Q", 5)
    assert volume[100 * PAGE:100 * PAGE + 8] == struct.pack("<Q", 100)
    assert volume[600 * PAGE:600 * PAGE + 8] == struct.pack("<Q", 600)
    # ...and the pages the guest never allocated are real zero pages, because
    # ``ramSparseIO`` serves the static zero page for them.
    assert volume[2 * PAGE:3 * PAGE] == b"\0" * PAGE

    disk = scan(guest, tmp_path / "scan.bin")
    assert disk.struct_pa == guest.struct_pa
    assert disk.pages_va == guest.pages_va
    assert disk.bytes == guest.logical
    assert disk.resident == guest.resident_bytes
    assert disk.page_count == guest.logical // PAGE
    assert disk.resident_pages == len(guest.resident)


# ---------------------------------------------------------------------------
# translating a virtual address
# ---------------------------------------------------------------------------
def test_a_labelled_gva2gpa_answer_is_parsed_and_a_bare_one_is_too(tmp_path: Path) -> None:
    """``gpa: 0x…`` is the answer, and it does not start with ``0x``.

    That is the trap, and it is asserted rather than described: the measured
    answer is a *labelled* line, so a parser that checked ``startswith("0x")``
    reported "does not translate" for an address that had just translated
    perfectly.  A bare hex number is accepted too, because the same command
    answers that way on other builds and because accepting it costs one
    alternative in the pattern.
    """
    labelled = FakeGuest()
    bare = FakeGuest(bare=True)
    expected = labelled.translations[VA_TABLE]

    answer = labelled.human_monitor(f"gva2gpa {VA_TABLE:#x}")
    assert answer == f"gpa: {expected:#x}"
    assert not answer.startswith("0x"), "the measured answer is labelled, not bare"
    assert live.guest_physical(labelled, VA_TABLE) == expected

    assert bare.human_monitor(f"gva2gpa {VA_TABLE:#x}") == f"{expected:#x}"
    assert live.guest_physical(bare, VA_TABLE) == expected


def test_gva2gpa_answering_unmapped_raises_rather_than_returning_a_wrong_address(
    tmp_path: Path,
) -> None:
    """An unmapped address is a refusal, and so is an answer with no address.

    Returning a wrong address is not a degraded mode: every page read through one
    is a page of the volume in the wrong place, and nothing downstream can tell.
    Both shapes are pinned -- ``Unmapped``, which is what QEMU answers for an
    address the guest does not map, and an answer that merely *contains* a hex
    without being a translation.
    """
    guest = FakeGuest()
    unmapped = VA_TABLE + (1 << 40)
    assert guest.human_monitor(f"gva2gpa {unmapped:#x}") == "Unmapped"

    with pytest.raises(UniPadError) as refusal:
        live.guest_physical(guest, unmapped)
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["vmaddr"] == f"{unmapped:#x}"
    assert "does not map" in refusal.value.message

    # ...and through read_virtual, where the consequence would be a wrong page.
    with pytest.raises(UniPadError) as through_read:
        live.read_virtual(guest, unmapped, PAGE, tmp_path / "scratch.bin")
    assert through_read.value.code == ErrorCode.EVIDENCE_INCOMPLETE

    # An answer that is not one of the two shapes QEMU produces.  It has a hex in
    # it; it is not a translation; a parser that searched for a number would
    # return 0x1234 and read the wrong guest memory.  The message says which of
    # the two refusals this is, because they are different diagnoses.
    with pytest.raises(UniPadError) as chatty:
        live.guest_physical(ChattyGuest(), VA_TABLE)
    assert chatty.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert "shape" in chatty.value.message
    assert "invalid address" in chatty.value.details["answer"]


# ---------------------------------------------------------------------------
# reading a virtual range, one page at a time
# ---------------------------------------------------------------------------
def test_a_virtual_range_whose_physical_pages_are_scattered_is_read_page_by_page(
    tmp_path: Path,
) -> None:
    """The teeth test: the page-by-page read is right, the contiguous one is wrong.

    The pointer array's virtual range is contiguous and its physical pages are
    not, which is the one fact this module exists for.  So this asserts both
    halves: that :func:`read_virtual` returns the two pages it was asked for, and
    that reading the same range as one contiguous physical block -- what a
    "simplified" ``read_virtual`` that skipped the translation would do -- is
    observably wrong, not merely different.  If anyone collapses the loop into one
    ``pmemsave``, this goes red.
    """
    guest = FakeGuest()
    first = guest.translations[VA_TABLE]
    second = guest.translations[VA_TABLE + PAGE]
    assert second - first == TABLE_STRIDE, "the two pages must not be adjacent"

    scratch = tmp_path / "scratch.bin"
    read = live.read_virtual(guest, VA_TABLE, 2 * PAGE, scratch)
    assert read == guest.table[: 2 * PAGE]
    # One round trip per page, which is the shape being pinned.
    assert pmemsaves(guest) == 2

    # And the contiguous read the simplification would do: the first page right,
    # and then the gap.
    guest.execute("pmemsave", {"val": first, "size": 2 * PAGE, "filename": str(scratch)})
    contiguous = scratch.read_bytes()
    assert len(contiguous) == 2 * PAGE
    assert contiguous != read
    assert contiguous[:PAGE] == read[:PAGE]
    assert contiguous[PAGE:] == b"\0" * PAGE, "the gap is zeros, not the second page"


# ---------------------------------------------------------------------------
# the signature
# ---------------------------------------------------------------------------
def test_a_pages_pointer_above_the_narrow_window_is_still_accepted(tmp_path: Path) -> None:
    """The window is the whole kernel map, and the fake's pointer is above the old one.

    An earlier scan used ``[0xfffffff000000000, 0xfffffff800000000)`` and
    discarded the one true candidate, whose ``pages`` pointer was
    ``0xfffffffb1ffba000``.  A search that throws away the one real hit is
    indistinguishable from a search that found nothing, so the fake's pointer is
    deliberately above that window: narrowing the constant back turns this into
    "no candidates", which is exactly the failure being pinned.
    """
    guest = FakeGuest()
    assert guest.pages_va > NARROW_WINDOW_HIGH
    assert not (0xFFFFFFF000000000 <= guest.pages_va < NARROW_WINDOW_HIGH)

    disk = scan(guest, tmp_path / "scratch.bin")
    assert disk.pages_va == guest.pages_va
    assert disk.struct_pa == guest.struct_pa
    assert disk.resident_pages == len(guest.resident)


def test_zero_candidates_is_a_refusal(tmp_path: Path) -> None:
    """Nothing matching is incomplete evidence, not an empty volume.

    A guest whose driver never published the volume must not produce a volume of
    zeros: that would be a Data volume which is wrong in a way nothing can detect.
    The refusal names the size it looked for, because "no match" and "no match at
    4 MiB" are different diagnoses.
    """
    absent = FakeGuest(lay_struct=False)
    with pytest.raises(UniPadError) as none:
        scan(absent, tmp_path / "none.bin")
    assert none.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert none.value.details["matches"] == 0
    assert none.value.details["logical_bytes"] == absent.logical
    # The message, not just the count: falling through to the "none of them
    # verified" refusal would also report ``matches: 0``, and a reader told "none
    # of the candidates points at a table" when there were no candidates at all is
    # being sent looking for the wrong fault.
    assert "does not hold a RamSparseDisk" in none.value.message


def test_two_candidates_where_only_one_has_a_pointer_shaped_table_return_the_right_one(
    tmp_path: Path,
) -> None:
    """The signature is a filter; the table is what says which candidate is real.

    Measured on the real guest: a boot where the Data volume has just mounted
    matches one struct, and a boot where the userland has been running for ~18
    minutes matches **38** -- guest RAM fills with unrelated data carrying the
    volume's size at offset +8 with a plausible ``resident`` and a kernel-map
    ``pages``.  So a scanner that treated "exactly one match" as the answer would
    work on a fresh guest and refuse a long-running one, which is the guest task
    #28 exists to read.

    Here the decoy's ``pages`` points at a page of the volume's own *contents*,
    which translates perfectly and is not pointer-shaped at all.  It is skipped
    and the real struct is returned.
    """
    guest = FakeGuest(decoy_at=OFF_DECOY, decoy_pages=DECOY_TABLE_LOOKALIKE)
    assert guest.decoy_pages != guest.pages_va

    disk = scan(guest, tmp_path / "scratch.bin")
    assert disk.struct_pa == guest.struct_pa
    assert disk.pages_va == guest.pages_va
    assert disk.resident_pages == len(guest.resident)

    # Every candidate costs one sieve page, and only the survivor's table is read
    # in full.  That is the whole economy of the sieve: on a guest with 38
    # candidates the difference is 38 pages and 38 tables.  Counted before the
    # extraction below, which runs a scan of its own.
    assert pmemsaves(guest) == guest.ram_bytes // CHUNK + guest.table_pages + 2

    # And the volume it describes is the one the guest holds.
    assert extract(guest, tmp_path / "extract.bin") == guest.logical_image()


def test_two_candidates_that_both_look_pointer_shaped_raise_rather_than_pick(
    tmp_path: Path,
) -> None:
    """Two candidates that both verify is a refusal, and the message says so.

    The decoy points at the same table the real struct does, so both produce a
    table of kernel virtual addresses whose non-null count matches ``resident``.
    Nothing in the verification can tell them apart, and picking one would be a
    guess that yields a plausible-looking Data volume built from the wrong pages
    -- which is worse than no volume, because it would be believed.  The refusal
    names how many verified, because that is the number that says the
    verification is not specific enough rather than the guest being empty.
    """
    guest = FakeGuest(decoy_at=OFF_DECOY)
    with pytest.raises(UniPadError) as refusal:
        scan(guest, tmp_path / "scratch.bin")
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["matches"] == 2
    assert refusal.value.details["verified"] == 2
    assert sorted(refusal.value.details["candidates"]) == sorted(
        [f"{guest.struct_pa:#x}", f"{live.RAM_BASE + OFF_DECOY:#x}"]
    )


def test_a_candidate_whose_later_table_pages_are_not_pointers_is_not_the_volume(
    tmp_path: Path,
) -> None:
    """The sieve reads one page; the verification reads all of them.

    A false candidate whose *first* table page happens to be pointer-shaped is
    exactly what a one-page sieve waves through, so the full read applies the same
    rule to every page.  Here the decoy's table opens with a page of kernel
    virtual addresses -- the sieve passes it -- and its second page is the volume's
    own contents.  Its ``resident`` is set to the count its table really has, so
    the count check agrees with it too and only the shape check can reject it:
    drop that check and this decoy verifies, two candidates verify, and the scan
    refuses instead of returning the volume.
    """
    pointerish = bytearray(PAGE)
    for at, vmaddr in ((0, KERNEL_VA_BASE + 0x050000000), (5 * 8, KERNEL_VA_BASE + 0x050010000)):
        struct.pack_into("<Q", pointerish, at, vmaddr)
    contents = page_of(0)
    allocated = 2 + sum(
        1 for at in range(0, PAGE, 8) if struct.unpack_from("<Q", contents, at)[0]
    )

    guest = FakeGuest(
        decoy_at=OFF_DECOY,
        decoy_pages=SPLIT_TABLE_VA,
        decoy_resident=allocated * PAGE,
        extra_pages={SPLIT_TABLE_VA: bytes(pointerish), SPLIT_TABLE_VA + PAGE: contents},
    )
    assert live._pointer_shaped(bytes(pointerish))
    assert not live._pointer_shaped(contents)

    disk = scan(guest, tmp_path / "scratch.bin")
    assert disk.struct_pa == guest.struct_pa
    assert disk.pages_va == guest.pages_va
    assert disk.resident_pages == len(guest.resident)


def test_a_candidate_whose_pages_does_not_translate_is_skipped_not_fatal(
    tmp_path: Path,
) -> None:
    """A candidate the guest does not map is discarded, and the scan continues.

    "Skipped, not fatal" is the whole point: on a guest with 38 candidates, one
    whose ``pages`` is an address the guest never mapped is a false positive like
    any other, and aborting the scan on it would mean the volume is unreachable
    exactly when the guest is busiest.  The decoy here is inside the virtual
    window and unmapped; the real struct beside it is still returned.
    """
    guest = FakeGuest(decoy_at=OFF_DECOY, decoy_pages=UNMAPPED_KERNEL_VA)
    assert guest.human_monitor(f"gva2gpa {UNMAPPED_KERNEL_VA:#x}") == "Unmapped"

    disk = scan(guest, tmp_path / "scratch.bin")
    assert disk.struct_pa == guest.struct_pa
    assert disk.resident_pages == len(guest.resident)


def test_a_struct_whose_pages_pointer_does_not_translate_is_not_a_match(
    tmp_path: Path,
) -> None:
    """A mapped pointer is part of the signature, not a bonus check.

    A run of bytes holding the volume's size at offset 8 and a kernel-map address
    at offset 0 is common in a 6 GiB scan of a guest whose own volume is that
    size; what makes it the struct is that the guest actually maps the pointer.
    The decoy's address is inside the window, so only the translation rejects it
    -- and this is the only signature-shaped struct in the guest, so the refusal
    has to be "no match" and not "two matches".
    """
    guest = FakeGuest(
        lay_struct=False, decoy_at=OFF_DECOY, decoy_pages=UNMAPPED_KERNEL_VA
    )
    assert guest.human_monitor(f"gva2gpa {UNMAPPED_KERNEL_VA:#x}") == "Unmapped"

    with pytest.raises(UniPadError) as refusal:
        scan(guest, tmp_path / "scratch.bin")
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["matches"] == 0


@pytest.mark.parametrize(
    "resident",
    [
        pytest.param(0, id="a volume with no resident page was never published"),
        pytest.param(PAGE // 2, id="a resident count that is not a whole number of pages"),
        pytest.param(LOGICAL + PAGE, id="a resident count larger than the volume"),
    ],
)
def test_a_struct_with_an_impossible_resident_count_is_not_a_match(
    tmp_path: Path, resident: int
) -> None:
    """``resident`` is checked by the signature, not only by the cross-check.

    It is nonzero, a whole number of pages and no larger than the volume, because
    those are the three things ``ramSparseImport`` makes true of the real struct.
    The count is also cross-checked against the array after the table is read, so
    dropping these three rules would not change *whether* this guest is refused
    -- it would change *when*, and a refusal that names ``matches: 0`` is a
    different diagnosis from one that names ``resident``.  That is what is pinned
    here: the predicate is what rejects it.
    """
    guest = FakeGuest(struct_resident=resident)
    with pytest.raises(UniPadError) as refusal:
        scan(guest, tmp_path / "scratch.bin")
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["matches"] == 0


def test_a_resident_count_above_the_page_array_is_a_refusal(tmp_path: Path) -> None:
    """``resident`` claiming more pages than the array holds is not the volume.

    ``ramSparseImport`` and ``ramSparseIO`` both store the pointer and then add
    4096, and neither frees, so the array can only ever hold *at least* as many
    pages as ``resident`` says.  The other direction is the measured one: on a
    live guest the userland keeps writing while the table is read, so the array
    holds more than the struct reported when it was scanned.  A struct claiming
    more than its array can deliver is therefore the wrong struct.
    """
    # The array holds len(RESIDENT) pages; claim more than that.
    guest = FakeGuest(struct_resident=(len(RESIDENT) + 1) * PAGE)
    with pytest.raises(UniPadError) as refusal:
        scan(guest, tmp_path / "scratch.bin")
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refusal.value.details["resident"] == (len(RESIDENT) + 1) * PAGE
    assert refusal.value.details["allocated"] == len(guest.resident) * PAGE


def test_a_page_array_larger_than_resident_is_the_guest_still_writing(
    tmp_path: Path,
) -> None:
    """More pages than ``resident`` reported is expected, not a refusal.

    ``resident`` is read while scanning physical memory and the table is read
    afterwards, page by page, over hundreds of QMP round trips.  Every write the
    userland makes in that window allocates a page and raises ``resident`` by
    4096, so the table is stale-low by however much landed in between.  Measured
    on the real guest: 40,183 reported against 40,426 in the table, 243 pages of
    writes between the two reads.

    Demanding equality here would refuse every boot that has a userland in it --
    which is the only kind of boot task #28 cares about.
    """
    guest = FakeGuest(struct_resident=PAGE)          # the array holds more
    assert len(guest.resident) * PAGE > guest.struct_resident

    disk = scan(guest, tmp_path / "scratch.bin")

    assert disk.resident == PAGE                     # what the struct said
    assert sum(1 for page in disk.pages if page) * PAGE > disk.resident
    assert len(disk.pages) == LOGICAL // PAGE


def test_a_struct_straddling_a_scan_chunk_boundary_is_still_found(tmp_path: Path) -> None:
    """A struct that crosses a chunk edge is a struct, not a miss.

    The scan walks physical memory in fixed chunks, so a struct that starts in
    the last few bytes of one and finishes in the next is found only if those
    bytes are carried forward.  Without the carry, whether the volume is found
    depends on where the kernel happened to allocate it -- a defect that looks
    exactly like a guest with no Data volume in it.
    """
    guest = FakeGuest(struct_at=CHUNK - 16)
    assert guest.struct_pa + STRUCT_SIZE > live.RAM_BASE + CHUNK, "it must straddle"

    disk = scan(guest, tmp_path / "scratch.bin")
    assert disk.struct_pa == guest.struct_pa
    assert disk.resident_pages == len(guest.resident)


# ---------------------------------------------------------------------------
# the reads
# ---------------------------------------------------------------------------
def test_a_short_pmemsave_raises_rather_than_silently_truncating(tmp_path: Path) -> None:
    """A truncated buffer is a refusal, not a shorter volume.

    A short read becomes a Data volume with the pages it did get in the places it
    did get them, and nothing downstream can tell -- the digest would catch it,
    but only after the volume had been written back and believed.  The fake is
    built to answer the scan's very first read with 16 bytes instead of a chunk,
    so the failure surfaces at the scan rather than somewhere deeper.
    """
    guest = FakeGuest(short={live.RAM_BASE: 16})
    with pytest.raises(UniPadError) as refusal:
        extract(guest, tmp_path / "scratch.bin")
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE


def test_a_pmemsave_that_writes_nothing_raises_rather_than_returning_the_last_page(
    tmp_path: Path,
) -> None:
    """A read that produced no file is not the read that came before it.

    ``pmemsave`` writes a file and QMP returns nothing, so the file is the only
    evidence a read happened -- and a stale one is the right length.  A reader
    that did not unlink first would hand back the previous page's bytes for a
    read that returned nothing at all, which is the same failure that made a 6
    GiB scan report "zero candidates" after reading nothing, one level worse.

    The second page of the table is the read that fails, so the first one has
    already put a perfectly good page on disk.
    """
    second_page = live.RAM_BASE + OFF_TABLE + TABLE_STRIDE
    guest = FakeGuest(silent={second_page})
    scratch = tmp_path / "scratch.bin"

    with pytest.raises(UniPadError) as refusal:
        live.read_virtual(guest, VA_TABLE, 2 * PAGE, scratch)
    assert refusal.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    # Nothing is left behind that could be mistaken for the answer.
    assert not scratch.exists()


def test_the_struct_is_rescanned_each_call_rather_than_cached(tmp_path: Path) -> None:
    """The struct's address moves between boots, so the scan runs every time.

    An address measured once answers ``Unmapped`` on the next boot -- measured on
    the real guest, where a hardcoded ``pages`` pointer from an earlier boot was
    exactly that.  So nothing here may be cached: two guests with the struct in
    different places give two different answers, and the third call goes back to
    the first guest's address rather than to whatever was scanned last.
    """
    scratch = tmp_path / "scratch.bin"
    first = FakeGuest()
    second = FakeGuest(struct_at=OFF_DECOY)

    one = scan(first, scratch)
    assert one.struct_pa == first.struct_pa

    two = scan(second, scratch)
    assert two.struct_pa == second.struct_pa
    assert two.struct_pa != one.struct_pa
    # The second call really scanned: it issued its own reads rather than
    # answering from a remembered address.
    assert pmemsaves(second) >= 1

    again = scan(first, scratch)
    assert again.struct_pa == one.struct_pa
