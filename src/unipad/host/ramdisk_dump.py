"""Read a QEMU guest-memory dump as an address-keyed byte map, without loading it.

Why this exists
---------------
The Data volume's live state is not on a disk.  It is a packed ramdisk in guest
RAM, and the kernel driver keeps it as an array of *individually allocated* 4
KiB pages (``SparseDisk.h``: ``uint8_t **pages``, one ``ops.allocate()`` per
page), so neither the pointer array nor any page it names is reachable from a
static symbol -- ``sparse`` is a member of the ``NativeRamBlock`` IOService
object and ``sparse.pages`` is a separate allocation
(``NativeRamBlock.cpp:55``).  Host-side persistence therefore starts from a
whole-memory dump: ``dump-guest-memory`` writes every mapped guest page in one
command, and then the work is a sequential search for one 24-byte struct
followed by a few hundred scattered 4 KiB reads.

That shape is the whole reason this module is a *streaming* reader rather than
``mmap`` plus slices.  A dump of a 6 GiB guest is a file whose decompressed
size is 6 GiB, and a host that reads it whole to find one struct is a host that
cannot find it.  So:

* segments are decompressed lazily and only when a caller asks for bytes in
  them (:meth:`GuestMemoryDump.read`);
* what is produced is cached in fixed-size *windows* behind an LRU with a byte
  budget, so a scan of a 6 GiB dump holds ``CACHE_BUDGET_BYTES`` of it and no
  more (:meth:`GuestMemoryDump.find_struct` streams window by window);
* a segment is a forward-only byte source.  A zlib stream cannot be entered in
  the middle, so reading a window behind the reader's position restarts the
  stream from the segment's start -- correct, and rare, because both callers
  here read in ascending order.

The two formats
---------------
``dump-guest-memory`` accepts two, and they differ only in how the segment bytes
are stored:

``kdump-zlib``
    An ELF64 header, a ``PT_NOTE`` carrying ``VMCOREINFO``, then one ``PT_LOAD``
    per mapped region.  ``p_memsz`` is the uncompressed size and ``p_filesz``
    the compressed size actually in the file, so the segment's data has to be
    inflated to be read.  The stream is *not* required to end with a final
    block, which is why every segment is inflated with
    :class:`zlib.decompressobj` rather than :func:`zlib.decompress`: a raw
    ``decompress`` raises ``incomplete or truncated stream`` on a stream QEMU
    flushed without finishing, and refusing a good dump is worse than accepting
    a partial one.

``elf``
    The same header and the same ``PT_LOAD`` table, but ``p_filesz`` bytes at
    ``p_offset`` are the segment's bytes verbatim.

The format is *sniffed from the file*, never from the name: a dump named
``.elf`` that QEMU wrote as ``kdump-zlib`` is a dump that reads correctly or
not at all, and the name has no say in it.  The sniff is the two bytes at the
first ``PT_LOAD``'s data -- a zlib header is a 16-bit value that is 0 mod 31
with a deflate method of 8, and guest memory does not begin with that by
accident often enough to matter (and a caller who knows can pass ``format=``).

Which address space
-------------------
A dump's ``PT_LOAD`` entries are keyed by either *physical* or guest *virtual*
addresses, and the two are not interchangeable: every pointer in a kernel dump
is a virtual address, so reading one as the other is off by a constant and
produces a volume that is silently wrong.

* Every compressed format is physical.  QEMU refuses ``paging`` outright for
  them -- ``kdump-compressed format doesn't support paging or filter`` -- so a
  compressed dump *cannot* be virtual-keyed.  That is derived, not assumed.
* A plain ``elf`` dump is the one form that can be either, and **the file does
  not record which**: ``paging=true`` writes it keyed by virtual address,
  ``paging=false`` by physical.  So the caller says, through ``keying=``, and a
  caller who does not is refused rather than guessed at.

This module therefore takes *virtual* addresses everywhere in its public API and
keeps :meth:`GuestMemoryDump.read_physical` as the primitive underneath, exactly
as :mod:`unipad.host.guestobserve` already does: the VA-to-PA mapping is
:meth:`unipad.host.guestobserve.LoadAddresses.physical`, computed from the
``kernel va ... pa ...`` line the guest prints on its own serial port, and it is
never re-derived here.  On this board that line gives a delta of
``0xFFFFFFF000000000`` for the whole kernel map, which is what makes a pointer
out of ``sparse.pages`` translatable at all.

Nothing in here interprets what it finds.  A struct is found by
:meth:`GuestMemoryDump.find_struct` and validated by a caller-supplied
predicate, so the knowledge of what a ``RamSparseDisk`` looks like stays in
:mod:`unipad.host.ramdisk_extract` where the header that defines it is the
documentation.
"""

from __future__ import annotations

import struct
import zlib
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional, Sequence

from unipad.errors import ErrorCode, UniPadError
from unipad.host.guestobserve import LoadAddresses

__all__ = [
    "CACHE_BUDGET_BYTES",
    "CacheStats",
    "DumpFormat",
    "DumpKeying",
    "GuestMemoryDump",
    "INPUT_CHUNK_BYTES",
    "NOTE_LIMIT_BYTES",
    "OUTPUT_CHUNK_BYTES",
    "PT_LOAD",
    "PT_NOTE",
    "Segment",
    "WINDOW_BYTES",
    "dump_format",
    "dump_keying",
]


class DumpFormat(str):
    """The two ``format`` values QEMU's ``dump-guest-memory`` accepts."""

    #: Plain ELF64: the segment bytes are stored verbatim.
    ELF = "elf"
    #: The kdump flavour: every segment is a zlib stream.  This is the one worth
    #: using on a Windows host, because a 6 GiB guest of mostly zero pages is a
    #: file that does not fit on one uncompressed.
    KDUMP_ZLIB = "kdump-zlib"


class DumpKeying(str):
    """Which address space a dump's ``PT_LOAD`` entries are keyed by."""

    #: ``p_vaddr`` is a guest physical address.  Every compressed format, and a
    #: plain ``elf`` dump taken without ``paging``.
    PHYSICAL = "physical"
    #: ``p_vaddr`` is a guest virtual address.  Only a plain ``elf`` dump taken
    #: with ``paging=true``.
    VIRTUAL = "virtual"


#: The page size the guest runs with.  Only used to keep window arithmetic
#: aligned, so a 4 KiB page read is never split across more windows than it has
#: to be.
GUEST_PAGE = 4096

#: How much decompressed segment data one window holds.  A window is the unit of
#: both the cache and the scan, and 1 MiB is 256 guest pages: small enough that
#: the budget below is a meaningful bound on a 6 GiB dump, large enough that the
#: per-window overhead (one dict entry, one bytes object) is noise.
WINDOW_BYTES = 1 << 20

#: The default LRU budget, in bytes of *decompressed* data.  The pointer array
#: of a 1 GiB volume is 2 MiB and is read once; each resident page is read once;
#: a scan touches every window once.  None of those want the whole dump
#: resident, and 64 MiB is what keeps the resident set of a 6 GiB dump bounded
#: without evicting the window a scan is currently inside.
CACHE_BUDGET_BYTES = 64 << 20

#: Compressed bytes pulled from the file per read.  Deliberately small: the
#: ratio between this and a window is the worst-case expansion a single read can
#: produce, and a run of zero pages expands about a thousand to one, so 64 KiB in
#: is 1 MiB out and never 6 GiB.
INPUT_CHUNK_BYTES = 1 << 16

#: Decompressed bytes produced per pull by default.  Bounded for the same reason:
#: the reader never holds more of a segment than one window plus what the caller
#: asked for.  A reader constructed with a smaller window uses that instead.
OUTPUT_CHUNK_BYTES = WINDOW_BYTES

#: The note section is informational -- it is reported, not parsed for meaning --
#: so a header that lies about its size costs this many bytes and nothing else.
NOTE_LIMIT_BYTES = 1 << 20

#: ``Elf64_Phdr::p_type`` values this reader cares about.
PT_LOAD = 1
PT_NOTE = 4

#: ``ET_CORE``.  Recorded and reported, but *not* required: a dump is a dump
#: because of what its program headers describe, and refusing a readable one
#: over a type field would be a judgement call dressed up as a check.
ET_CORE = 4

#: The ELF64 little-endian header: ident, type, machine, version, entry, phoff,
#: shoff, flags, ehsize, phentsize, phnum, shentsize, shnum, shstrndx.
_ELF_HEADER = struct.Struct("<16sHHIQQQIHHHHHH")
ELF_HEADER_SIZE = _ELF_HEADER.size

#: ``Elf64_Phdr``: type, flags, offset, vaddr, paddr, filesz, memsz, align.
_ELF_PHDR = struct.Struct("<IIQQQQQQ")
ELF_PHDR_SIZE = _ELF_PHDR.size

_ELF_MAGIC = b"\x7fELF"
_ELFCLASS64 = 2
_ELFDATA2LSB = 1


@dataclass(frozen=True)
class Segment:
    """One ``PT_LOAD``: ``size`` bytes of guest memory mapped at ``vaddr``.

    ``size`` is ``p_memsz`` -- what :attr:`GuestMemoryDump.regions` reports and
    what a read is bounded by.  ``stored`` is ``p_filesz`` -- what the file
    actually holds, which for ``kdump-zlib`` is the *compressed* size and can
    therefore be larger than ``size`` for data that does not compress.  The
    reader never assumes the relationship between the two; it just stops when
    the file does, and a read past that point is refused rather than shortened.
    """

    index: int
    vaddr: int
    size: int
    offset: int
    stored: int
    compressed: bool


@dataclass(frozen=True)
class CacheStats:
    """What the window cache did, so a test can prove the bound held."""

    hits: int
    misses: int
    evictions: int
    cached_bytes: int
    budget_bytes: int
    window_bytes: int


class _SegmentReader:
    """A forward-only byte source for one segment, holding one output chunk.

    The file handle is shared with every other reader and with the dump itself,
    so nothing here assumes where the handle is: every read seeks first.  That
    is what makes interleaved reads -- a scan that validates a candidate in a
    different segment, say -- safe without a handle per segment.

    A zlib stream cannot be entered in the middle, so ``window()`` is the only
    way in: it either resumes from where this reader stopped, or restarts from
    the segment's first byte when the window asked for is behind that.  Both are
    correct; the first is the common case and the second is the price of the
    format.
    """

    def __init__(self, handle: BinaryIO, segment: Segment, window_bytes: int) -> None:
        self._handle = handle
        self._segment = segment
        self._window = window_bytes
        # The pull size is the window size, so a window is one pull and a partial
        # one is never split and reassembled.  A larger chunk would mean copying
        # the remainder aside for every window after the first, which for a
        # 4096-byte window over a 16 MiB segment is gigabytes of memcpy to read
        # the same bytes once.
        self._output = window_bytes
        self._pos = 0
        self._fed = 0
        self._pending = b""
        self._done = False
        self._inflater: Optional[zlib._Decompress] = None
        if segment.compressed:
            self._inflater = zlib.decompressobj()

    @property
    def position(self) -> int:
        """Decompressed bytes handed out so far, for the invariant's own sake."""
        return self._pos

    def window(self, index: int) -> bytes:
        """The ``index``-th window of this segment, zeros never invented.

        The reader is forward-only: a window behind :attr:`position` restarts the
        segment's stream from its first byte, which is correct and rare, because
        both callers here read in ascending order.

        Raises:
            UniPadError: ``evidence_incomplete`` when the segment ends before
                the window does.  The caller asked for bytes the dump does not
                have, and the alternative -- handing back a short buffer -- is a
                silently corrupt Data volume.
        """
        start = index * self._window
        if self._pos > start:
            self._restart()
        while self._pos < start:
            before = self._pos
            self._discard(start - self._pos)
            if self._pos == before:
                # The segment ended before this window starts.  Bailing out here
                # rather than looping is what keeps a truncated segment a
                # refusal instead of a hang.
                break
        want = min(self._window, self._segment.size - start)
        chunk = self._take(want)
        if len(chunk) < want:
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "the guest memory dump ran out of data inside a segment",
                vaddr=self._segment.vaddr + start,
                wanted=want,
                produced=len(chunk),
                segment_size=self._segment.size,
            )
        return chunk

    def _restart(self) -> None:
        """Seek back to the segment's first byte and start its stream over."""
        self._pos = 0
        self._fed = 0
        self._pending = b""
        self._done = False
        if self._segment.compressed:
            self._inflater = zlib.decompressobj()

    def _discard(self, count: int) -> None:
        """Consume ``count`` bytes without keeping them.

        ``_pos`` counts bytes handed to the caller, and this is a caller: a
        window skipped over is a window this reader is past, and a reader that
        did not know that would stream the segment again for every window behind
        it.
        """
        while count > 0:
            chunk = self._pull()
            if not chunk:
                return
            if len(chunk) > count:
                self._pending = chunk[count:]
                self._pos += count
                return
            self._pos += len(chunk)
            count -= len(chunk)

    def _take(self, count: int) -> bytes:
        """Consume exactly ``count`` bytes, or fewer if the segment ends."""
        parts: list[bytes] = []
        taken = 0
        while taken < count:
            chunk = self._pull()
            if not chunk:
                break
            if len(chunk) > count - taken:
                parts.append(chunk[:count - taken])
                self._pending = chunk[count - taken:]
                taken = count
                break
            parts.append(chunk)
            taken += len(chunk)
        self._pos += taken
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def _pull(self) -> bytes:
        """The next up-to-``self._output`` bytes, or ``b""`` at the end."""
        if self._pending:
            chunk, self._pending = self._pending, b""
            return chunk
        segment = self._segment
        while not self._done:
            if not segment.compressed:
                chunk = self._read(min(self._output, segment.stored - self._fed))
                self._fed += len(chunk)
                if not chunk:
                    self._done = True
                    break
                return chunk

            assert self._inflater is not None
            if self._inflater.unconsumed_tail:
                # The previous call stopped because its output budget was hit,
                # not because the input was used up: feed the rest back in.
                raw = self._inflater.unconsumed_tail
            elif self._fed < segment.stored:
                raw = self._read(min(INPUT_CHUNK_BYTES, segment.stored - self._fed))
                self._fed += len(raw)
                if not raw:
                    self._done = True
                    break
            else:
                # Every compressed byte is in and nothing is waiting behind the
                # output limit, so the segment is over.  A stream that was
                # flushed without a final block ends exactly here, and that is
                # not an error: QEMU does not promise one.
                self._done = True
                break
            chunk = self._inflater.decompress(raw, self._output)
            if chunk:
                return chunk
        return b""

    def _read(self, count: int) -> bytes:
        if count <= 0:
            return b""
        self._handle.seek(self._segment.offset + self._fed)
        return self._handle.read(count)


class GuestMemoryDump:
    """A guest-memory dump read as an address-keyed map, in bounded memory.

    Open one with :meth:`open`; the handle is owned and closed by
    :meth:`close` (or the context manager).  Every read is bounded by the
    window cache's budget, and the dump itself is never resident.

    Every address this class takes or returns in its public API is a guest
    *virtual* address, except on the ``_physical`` methods, which are the
    primitive the rest is built on.  A physical dump therefore needs the
    guest's load addresses to be usable at all -- see :meth:`open`.

    Args:
        handle: An open binary file positioned anywhere; every read seeks.
        path: Where it came from, for error details only.
        format: ``elf`` or ``kdump-zlib``.  ``None`` sniffs it from the file.
        keying: ``physical`` or ``virtual``.  ``None`` derives it from the
            format, which settles every compressed format and refuses a plain
            ``elf`` dump -- the file does not say, and guessing is the defect
            this parameter exists to prevent.
        load: The guest's kernel load addresses, used to translate a virtual
            address to the physical one a physical dump is keyed by.  Required
            by every virtual read of a physical dump; ignored by a virtual one.
        window_bytes: The cache's unit.  Must be at least a guest page so a page
            read is never split across more windows than it has to be.
        cache_bytes: The LRU budget in decompressed bytes.
    """

    def __init__(
        self,
        handle: BinaryIO,
        *,
        path: Optional[Path] = None,
        format: Optional[str] = None,
        keying: Optional[str] = None,
        load: Optional[LoadAddresses] = None,
        window_bytes: int = WINDOW_BYTES,
        cache_bytes: int = CACHE_BUDGET_BYTES,
    ) -> None:
        if window_bytes < GUEST_PAGE:
            raise ValueError(
                f"a {window_bytes}-byte window cannot hold a {GUEST_PAGE}-byte "
                "guest page, so a page read would span more windows than it has to"
            )
        if cache_bytes <= 0:
            raise ValueError(f"a {cache_bytes}-byte cache budget cannot hold anything")
        self._handle = handle
        self._path = Path(path) if path is not None else None
        self._window = window_bytes
        self._budget = cache_bytes
        self._load = load
        self._cache: "OrderedDict[tuple[int, int], bytes]" = OrderedDict()
        self._cached_bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._readers: dict[int, _SegmentReader] = {}

        elf = _read_elf_header(handle, self._where())
        segments = _read_segments(handle, elf, self._where())
        if format is None:
            format = _sniff_format(handle, segments, self._where())
        elif format not in (DumpFormat.ELF, DumpFormat.KDUMP_ZLIB):
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "the dump format asked for is not one dump-guest-memory writes",
                requested=format,
                accepted=[DumpFormat.ELF, DumpFormat.KDUMP_ZLIB],
            )
        self._keying = _resolve_keying(format, keying, self._where())
        compressed = format == DumpFormat.KDUMP_ZLIB
        self._format = format
        self._segments = tuple(
            Segment(
                index=index,
                vaddr=vaddr,
                size=size,
                offset=offset,
                stored=stored,
                compressed=compressed,
            )
            for index, (vaddr, size, offset, stored) in enumerate(segments)
        )
        self._starts = [segment.vaddr for segment in self._segments]
        self._elf_type = elf.type
        self._elf_machine = elf.machine
        self._note_names = _read_note_names(handle, elf)

    # -- construction ------------------------------------------------------
    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        format: Optional[str] = None,
        keying: Optional[str] = None,
        load: Optional[LoadAddresses] = None,
        window_bytes: int = WINDOW_BYTES,
        cache_bytes: int = CACHE_BUDGET_BYTES,
    ) -> "GuestMemoryDump":
        """Open the dump at ``path`` and sniff (or accept) its format.

        Args:
            path: The dump file.
            format: ``elf`` or ``kdump-zlib``; ``None`` sniffs it from the data.
            keying: ``physical`` or ``virtual``; ``None`` derives it from the
                format.  Every compressed format is physical, because QEMU
                refuses ``paging`` for them; a plain ``elf`` dump has to be told,
                because the file does not record whether ``paging`` was used.
            load: The guest's kernel load addresses, from
                :func:`unipad.host.guestobserve.load_addresses_from_serial`.
                Required to read a physical dump at a virtual address, which is
                what every kernel pointer is.

        Raises:
            UniPadError: ``artifact_missing`` when the file is not there;
                ``artifact_manifest_invalid`` when it is not a little-endian
                ELF64 with at least one ``PT_LOAD``, or when the format asked
                for is not one QEMU writes; ``config_invalid`` when a plain
                ``elf`` dump is opened without saying which address space it is
                keyed by.  The distinction from ``evidence_incomplete`` is
                deliberate: a file that is not a dump is a wrong artifact, and a
                dump that is missing the bytes wanted is incomplete evidence.
        """
        target = Path(path)
        if not target.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "the guest memory dump is not at the path given",
                source=str(target),
            )
        handle = target.open("rb")
        try:
            return cls(
                handle,
                path=target,
                format=format,
                keying=keying,
                load=load,
                window_bytes=window_bytes,
                cache_bytes=cache_bytes,
            )
        except BaseException:
            handle.close()
            raise

    def close(self) -> None:
        """Release the file handle.  Idempotent, so a caller may always call it."""
        handle, self._handle = self._handle, None  # type: ignore[assignment]
        if handle is not None:
            handle.close()

    def __enter__(self) -> "GuestMemoryDump":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- what is mapped ----------------------------------------------------
    @property
    def regions(self) -> list[tuple[int, int]]:
        """The mapped ``(address, size)`` pairs, ascending, in the dump's keying.

        Physical for a physical dump, virtual for a virtual one -- these are the
        keys the file's program headers carry, before any translation.
        """
        return [(segment.vaddr, segment.size) for segment in self._segments]

    @property
    def format(self) -> str:
        """The format that was sniffed (or given)."""
        return self._format

    @property
    def keying(self) -> str:
        """The address space the segments are keyed by."""
        return self._keying

    @property
    def notes(self) -> tuple[str, ...]:
        """The note names the dump carries -- ``VMCOREINFO`` for a kdump."""
        return self._note_names

    @property
    def elf(self) -> tuple[int, int]:
        """``(e_type, e_machine)``, reported rather than gated on."""
        return (self._elf_type, self._elf_machine)

    @property
    def cache_stats(self) -> CacheStats:
        """What the window cache has done so far."""
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
            cached_bytes=self._cached_bytes,
            budget_bytes=self._budget,
            window_bytes=self._window,
        )

    # -- reading -----------------------------------------------------------
    def _to_physical(self, vmaddr: int) -> int:
        """The physical address a guest virtual address lives at in this dump.

        Raises:
            UniPadError: ``evidence_incomplete`` when the dump is physical and
                no load addresses were given.  Treating a virtual address as a
                physical one is not a fallback: every pointer in the dump would
                be off by the kernel's load delta and the volume reassembled
                from them would be silently wrong, which is worse than no volume
                at all.
        """
        if self._keying == DumpKeying.VIRTUAL:
            return vmaddr
        if self._load is None:
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "this dump is keyed by physical address, so reading a guest "
                "virtual address needs the kernel's load addresses",
                source=str(self._path) if self._path is not None else None,
                keying=self._keying,
            )
        return self._load.physical(vmaddr)

    def _to_virtual(self, physical: int) -> int:
        """The inverse of :meth:`_to_physical`, for reporting a hit."""
        if self._keying == DumpKeying.VIRTUAL:
            return physical
        if self._load is None:
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "this dump is keyed by physical address, so naming a hit by its "
                "guest virtual address needs the kernel's load addresses",
                source=str(self._path) if self._path is not None else None,
                keying=self._keying,
            )
        # One constant subtraction, the other direction.  Written out rather
        # than solved, because ``LoadAddresses`` owns the mapping and a second
        # copy of it here is a second thing to keep right.
        return physical - self._load.kernel_pa + self._load.kernel_va

    def segment_for(self, vmaddr: int, size: int) -> Optional[Segment]:
        """The one segment holding ``[vmaddr, vmaddr + size)``, or ``None``.

        :meth:`read`'s question without the refusal, for a caller that wants to
        plan a read rather than attempt one -- :func:`unipad.host.ramdisk_extract
        .reassemble` uses it to split a run of pages at a segment boundary, which
        it cannot do by catching an error it would then have to undo.
        """
        return self.segment_for_physical(self._to_physical(vmaddr), size)

    def segment_for_physical(self, physical: int, size: int) -> Optional[Segment]:
        """The one segment holding a physical range, or ``None``."""
        index = bisect_right(self._starts, physical) - 1
        for segment in reversed(self._segments[: index + 1]):
            if segment.vaddr <= physical and physical + size <= segment.vaddr + segment.size:
                return segment
        return None

    def read(self, vmaddr: int, size: int) -> bytes:
        """``size`` bytes at the guest virtual address ``vmaddr``, or a refusal.

        The range must lie wholly inside one mapped segment.  A range that
        straddles two is refused rather than stitched: the two segments are not
        necessarily adjacent in the guest, and a caller that gets a stitched
        buffer cannot tell which half came from where.

        Raises:
            UniPadError: ``evidence_incomplete`` when the range is not wholly
                inside one segment, when the segment does not actually hold that
                many bytes, or when a physical dump was opened without the load
                addresses a virtual address needs.  Never returns a short buffer
                -- a short read here is a Data volume that is silently wrong, and
                every caller of this module would rather be told.
            ValueError: when ``size`` is negative.
        """
        return self.read_physical(self._to_physical(vmaddr), size)

    def read_physical(self, physical: int, size: int) -> bytes:
        """``size`` bytes at the physical address ``physical``, or a refusal.

        The primitive :meth:`read` is built on, and the one to use when the
        address in hand is already physical -- a dump's own program-header
        keys, say.  Same refusal, same refusal to hand back a short buffer.
        """
        if size < 0:
            raise ValueError(f"a read of {size} bytes is not a read")
        # A zero-length read still has to be somewhere: an address outside every
        # segment is not a place, and returning ``b""`` for it would say so.
        segment = self._segment_for_physical(physical, size)
        if size == 0:
            return b""
        first = (physical - segment.vaddr) // self._window
        last = (physical + size - 1 - segment.vaddr) // self._window
        parts: list[bytes] = []
        for index in range(first, last + 1):
            start = segment.vaddr + index * self._window
            window = self._window_for(segment, index)
            take_from = max(physical, start) - start
            take_to = min(physical + size, start + len(window)) - start
            parts.append(window[take_from:take_to])
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def find_struct(
        self,
        field_offset: int,
        expected_value: int,
        *,
        validate: Optional[Callable[[int], bool]] = None,
    ) -> Iterator[int]:
        """Every address whose 8-byte little-endian field matches, streamed.

        This is the signature search.  ``field_offset`` is where inside the
        candidate struct the field lives, so a hit at ``address`` means the
        struct itself starts at ``address - field_offset``; that is what is
        yielded, because that is the address a caller dereferences.

        ``validate`` is called with the candidate struct address -- the same one
        that is yielded, in the same address space -- and answers whether the
        surrounding bytes are consistent with the struct being looked for.  It is
        the caller's, because what makes a ``RamSparseDisk`` a ``RamSparseDisk``
        is knowledge this module does not have and should not grow.

        The whole dump is walked window by window and nothing is materialised:
        a 6 GiB dump costs ``CACHE_BUDGET_BYTES`` of resident decompressed data
        and one pass of I/O, not 6 GiB of RAM.

        Addresses are yielded in the *guest virtual* space, so a hit is directly
        usable with :meth:`read` -- which for a physical dump means the load
        addresses were needed to find it in the first place.

        Args:
            field_offset: Byte offset of the field inside the struct.
            expected_value: The value the field must hold.
            validate: Optional predicate over the struct's start address.

        Yields:
            Struct start addresses, ascending.

        Raises:
            ValueError: when ``field_offset`` is negative, or when
                ``expected_value`` does not fit the unsigned 64-bit little-endian
                field being searched for.  Both are raised on the call, not on the
                first iteration: a generator that validates lazily is a generator
                whose contract only holds for the caller who remembered to pull
                from it.
        """
        if field_offset < 0:
            raise ValueError(f"a field at offset {field_offset} is not a field")
        if not 0 <= expected_value < 1 << 64:
            raise ValueError(
                f"{expected_value} does not fit an unsigned 64-bit field, so it "
                "cannot be what the scan is looking for"
            )
        return self._scan(struct.pack("<Q", expected_value), field_offset, validate)

    def _scan(
        self,
        needle: bytes,
        field_offset: int,
        validate: Optional[Callable[[int], bool]],
    ) -> Iterator[int]:
        """The walk itself, so the argument checks above happen on the call."""
        for segment in self._segments:
            windows = (segment.size + self._window - 1) // self._window
            carry = b""
            carry_at = segment.vaddr
            for index in range(windows):
                window = self._window_for(segment, index)
                data = carry + window if carry else window
                base = carry_at
                at = data.find(needle)
                while at != -1:
                    start = base + at - field_offset
                    if start >= segment.vaddr:
                        # The predicate sees exactly the address the caller is
                        # about to be handed.  Handing it the dump's own keying
                        # instead would have it read a different struct -- a
                        # constant away -- and answer ``False`` about the right
                        # one, which is the whole failure this parameter exists
                        # to make impossible.
                        hit = self._to_virtual(start)
                        if validate is None or validate(hit):
                            yield hit
                    at = data.find(needle, at + 1)
                # The last few bytes of this window may be the beginning of a
                # field that finishes in the next one.  Carrying them forward is
                # the only way to find a struct that straddles a boundary, and
                # carrying them *backwards* is impossible, so a match that
                # completes in the window it started in is reported exactly once.
                carry = data[1 - len(needle):]
                carry_at = base + len(data) - len(carry)

    # -- internals ---------------------------------------------------------
    def _where(self) -> dict[str, str]:
        return {"source": str(self._path)} if self._path is not None else {}

    def _segment_for_physical(self, physical: int, size: int) -> Segment:
        """The one segment holding ``[physical, physical + size)``, or a refusal."""
        segment = self.segment_for_physical(physical, size)
        if segment is None:
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "the guest memory dump has no segment holding this range",
                address=physical,
                size=size,
                keying=self._keying,
                regions=self.regions,
            )
        return segment

    def _window_for(self, segment: Segment, index: int) -> bytes:
        """The window, from the cache if it is still there."""
        key = (segment.index, index)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self._hits += 1
            return cached
        self._misses += 1
        reader = self._readers.get(segment.index)
        if reader is None:
            reader = _SegmentReader(self._handle, segment, self._window)
            self._readers[segment.index] = reader
        window = reader.window(index)
        if len(window) <= self._budget:
            # A window larger than the whole budget is handed back uncached: it
            # would evict everything else to hold itself, which is a cache that
            # never hits.
            self._cache[key] = window
            self._cached_bytes += len(window)
            self._cache.move_to_end(key)
            while self._cached_bytes > self._budget:
                _, evicted = self._cache.popitem(last=False)
                self._cached_bytes -= len(evicted)
                self._evictions += 1
        return window


# ---------------------------------------------------------------------------
# the ELF layer
# ---------------------------------------------------------------------------
#: What QEMU 11 actually writes for ``format=kdump-zlib``: a *makedumpfile*
#: container, not an ELF.  Measured on this host against
#: ``local-private/tmp/dump28/guest-memory-kdump-zlib.dump`` -- 16 bytes of
#: signature, a page of bitmap, then a ``KDUMP`` sub-header at offset 4112, and
#: no ELF magic anywhere.  Recognising it is worth the four lines: a caller who
#: hands this module one of those files deserves to be told which ``--format``
#: to use instead of being told it is not an ELF.
MAKEDUMPFILE_SIGNATURE = b"makedumpfile\x00"


def _refuse_makedumpfile(where: dict[str, str]) -> None:
    """Refuse a makedumpfile container, naming the format that does work.

    Raises:
        UniPadError: ``artifact_manifest_invalid``.  Not ``evidence_incomplete``:
            the file is complete and valid, it is simply a container this reader
            does not parse, and the fix is a different ``format`` argument to
            ``dump-guest-memory`` rather than more evidence.
    """
    raise UniPadError(
        ErrorCode.ARTIFACT_MANIFEST_INVALID,
        "the guest memory dump is a makedumpfile container, which is what this "
        "QEMU writes for format=kdump-zlib; it is not an ELF, so this reader "
        "cannot parse it -- take the dump with format=elf instead",
        signature=MAKEDUMPFILE_SIGNATURE.split(b"\0")[0].decode("ascii"),
        **where,
    )


def _resolve_keying(
    format: str, keying: Optional[str], where: dict[str, str]
) -> str:
    """Which address space this dump is keyed by, derived or declared.

    Every compressed format is physical, and that is derived rather than assumed:
    QEMU refuses ``paging`` for them outright -- ``kdump-compressed format
    doesn't support paging or filter`` -- so a compressed dump *cannot* be
    virtual-keyed.  A plain ``elf`` dump is the one form that can be either, and
    nothing in the file records which, so the caller says.

    Raises:
        UniPadError: ``config_invalid`` when a plain ``elf`` dump is opened
            without saying which address space it is keyed by, or when the keying
            given is not one of the two.
    """
    if keying is None:
        if format == DumpFormat.KDUMP_ZLIB:
            return DumpKeying.PHYSICAL
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "an elf dump is keyed by physical address when it is taken without "
            "paging and by virtual address when it is taken with it, and the "
            "file does not record which -- say keying=physical or keying=virtual",
            format=format,
            **where,
        )
    if keying not in (DumpKeying.PHYSICAL, DumpKeying.VIRTUAL):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "the dump keying asked for is not one of the two address spaces",
            requested=keying,
            accepted=[DumpKeying.PHYSICAL, DumpKeying.VIRTUAL],
            **where,
        )
    return keying


def dump_keying(path: Path | str) -> str:
    """The keying a dump at ``path`` implies, derived from its format.

    The same derivation :meth:`GuestMemoryDump.open` performs, exposed so a
    caller can report what a dump is before deciding to spend a scan on it.  A
    plain ``elf`` dump has no implied keying, and raises rather than guessing.

    Raises:
        UniPadError: ``config_invalid`` for a plain ``elf`` dump, and whatever
            :func:`dump_format` raises for anything that is not a dump.
    """
    return _resolve_keying(dump_format(path), None, {"source": str(Path(path))})


@dataclass(frozen=True)
class _ElfHeader:
    type: int
    machine: int
    phoff: int
    phentsize: int
    phnum: int


def _read_elf_header(handle: BinaryIO, where: dict[str, str]) -> _ElfHeader:
    """The ELF64 header, refusing anything that is not one.

    Raises:
        UniPadError: ``artifact_manifest_invalid`` when the file is not a
            little-endian 64-bit ELF, or when its program-header table is
            outside itself.
    """
    handle.seek(0)
    head = handle.read(ELF_HEADER_SIZE)
    if len(head) < ELF_HEADER_SIZE:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "the guest memory dump is shorter than an ELF64 header",
            bytes_read=len(head),
            **where,
        )
    if head[: len(MAKEDUMPFILE_SIGNATURE)] == MAKEDUMPFILE_SIGNATURE:
        _refuse_makedumpfile(where)
    ident, etype, machine, _version, _entry, phoff, _shoff, _flags, _ehsize, \
        phentsize, phnum, _shentsize, _shnum, _shstrndx = _ELF_HEADER.unpack(head)
    if ident[:4] != _ELF_MAGIC:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "the guest memory dump is not an ELF file",
            magic=ident[:4].hex(),
            **where,
        )
    if ident[4] != _ELFCLASS64 or ident[5] != _ELFDATA2LSB:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "the guest memory dump is not a little-endian 64-bit ELF, and "
            "dump-guest-memory writes nothing else",
            elf_class=ident[4],
            elf_data=ident[5],
            **where,
        )
    if phnum == 0 or phentsize != ELF_PHDR_SIZE:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "the guest memory dump's program-header table is empty or the wrong "
            "size, so there is no way to find what is mapped",
            phnum=phnum,
            phentsize=phentsize,
            **where,
        )
    if phoff < ELF_HEADER_SIZE:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "the guest memory dump's program headers overlap its ELF header",
            phoff=phoff,
            **where,
        )
    return _ElfHeader(
        type=etype,
        machine=machine,
        phoff=phoff,
        phentsize=phentsize,
        phnum=phnum,
    )


def _read_segments(
    handle: BinaryIO, elf: _ElfHeader, where: dict[str, str]
) -> list[tuple[int, int, int, int]]:
    """Every ``PT_LOAD`` as ``(vaddr, memsz, offset, filesz)``, ascending.

    Raises:
        UniPadError: ``artifact_manifest_invalid`` when no segment maps anything,
            or when a segment's data is outside the file.
    """
    handle.seek(elf.phoff)
    table = handle.read(elf.phentsize * elf.phnum)
    if len(table) < elf.phentsize * elf.phnum:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "the guest memory dump's program-header table is truncated",
            wanted=elf.phentsize * elf.phnum,
            bytes_read=len(table),
            **where,
        )
    found: list[tuple[int, int, int, int]] = []
    for index in range(elf.phnum):
        (
            ptype,
            _pflags,
            offset,
            vaddr,
            _paddr,
            filesz,
            memsz,
            _palign,
        ) = _ELF_PHDR.unpack_from(table, index * elf.phentsize)
        if ptype != PT_LOAD or memsz == 0:
            continue
        if offset + filesz < offset:
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "the guest memory dump has a segment whose data range overflows",
                vaddr=vaddr,
                offset=offset,
                filesz=filesz,
                **where,
            )
        found.append((vaddr, memsz, offset, filesz))
    if not found:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "the guest memory dump maps no memory, so there is nothing to read",
            **where,
        )
    found.sort()
    return found


def _sniff_format(
    handle: BinaryIO,
    segments: Sequence[tuple[int, int, int, int]],
    where: dict[str, str],
) -> str:
    """``kdump-zlib`` when the first segment's data is a zlib stream, else ``elf``.

    The two formats share a header and differ only in how the bytes are stored,
    so the only honest test is the data.  A zlib header is a 16-bit value that
    is 0 mod 31 with a deflate method of 8 and no preset dictionary; guest memory
    begins with that by chance about once in 65536, and a caller who knows can
    pass ``format=`` to skip the question.

    Raises:
        UniPadError: ``artifact_manifest_invalid`` when no segment has any bytes
            in the file at all, so there is nothing to sniff.
    """
    for _vaddr, _memsz, offset, filesz in segments:
        if filesz == 0:
            continue
        handle.seek(offset)
        head = handle.read(2)
        if len(head) < 2:
            break
        return DumpFormat.KDUMP_ZLIB if _is_zlib_header(head) else DumpFormat.ELF
    raise UniPadError(
        ErrorCode.ARTIFACT_MANIFEST_INVALID,
        "the guest memory dump's segments hold no bytes, so their format cannot "
        "be told from the file",
        **where,
    )


def _is_zlib_header(head: bytes) -> bool:
    """Whether two bytes are a zlib header with no preset dictionary."""
    cmf, flg = head[0], head[1]
    if cmf & 0x0F != 8:  # CM_DEFLATED
        return False
    if cmf >> 4 > 7:  # a window larger than 32 KiB is not a zlib window
        return False
    if flg & 0x20:  # FDICT: a preset dictionary this reader does not have
        return False
    return ((cmf << 8) | flg) % 31 == 0  # the header's own check value


def _read_note_names(handle: BinaryIO, elf: _ElfHeader) -> tuple[str, ...]:
    """The note names the ``PT_NOTE`` carries, for reporting.

    Informational only: the notes are how a dump is *identified* as a kdump, and
    a reader that needed them would refuse a dump whose note section it could
    not parse for a reason that has nothing to do with whether the memory is
    there.  A malformed section yields no names rather than an error.
    """
    names: list[str] = []
    for ptype, offset, filesz in _note_ranges(handle, elf):
        if ptype != PT_NOTE or filesz == 0:
            continue
        handle.seek(offset)
        blob = handle.read(min(filesz, NOTE_LIMIT_BYTES))
        names.extend(_walk_notes(blob))
    return tuple(names)


def _note_ranges(handle: BinaryIO, elf: _ElfHeader) -> list[tuple[int, int, int]]:
    """``(p_type, p_offset, p_filesz)`` for every program header.

    Read separately from :func:`_read_segments`, because that one keeps only the
    ``PT_LOAD`` entries and the note section is not one of them.
    """
    handle.seek(elf.phoff)
    table = handle.read(elf.phentsize * elf.phnum)
    ranges = []
    for index in range(elf.phnum):
        ptype, _flags, offset, _vaddr, _paddr, filesz, _memsz, _align = _ELF_PHDR.unpack_from(
            table, index * elf.phentsize
        )
        ranges.append((ptype, offset, filesz))
    return ranges


def _walk_notes(blob: bytes) -> list[str]:
    """The names in a note section, stopping at the first thing that is not one."""
    names: list[str] = []
    at = 0
    while at + 12 <= len(blob):
        namesz, descsz, _ntype = struct.unpack_from("<III", blob, at)
        at += 12
        if namesz == 0 or namesz > len(blob) - at:
            break
        name = blob[at:at + namesz].split(b"\0", 1)[0]
        if name:
            names.append(name.decode("ascii", "replace"))
        at += (namesz + 3) & ~3
        at += (descsz + 3) & ~3
    return names


def dump_format(path: Path | str) -> str:
    """The format of the dump at ``path``, sniffed from its program headers.

    The same sniff :meth:`GuestMemoryDump.open` performs, exposed so a caller can
    report what a dump is before deciding to spend a scan on it.

    Raises:
        UniPadError: ``artifact_manifest_invalid`` for anything that is not a
            dump, ``artifact_missing`` when the file is not there.
    """
    target = Path(path)
    if not target.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "the guest memory dump is not at the path given",
            source=str(target),
        )
    with target.open("rb") as handle:
        elf = _read_elf_header(handle, {"source": str(target)})
        segments = _read_segments(handle, elf, {"source": str(target)})
        return _sniff_format(handle, segments, {"source": str(target)})
