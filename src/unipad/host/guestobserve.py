"""Read a live UniXNU guest's kernel state over QMP, on Windows.

What this is for
----------------
The Windows boot has reached the kernel hand-off: ``local-private/boot7/run2``'s
serial log ends with ``DARWINBOOT: hand-off to the kernel`` and

    kernel   va 0xFFFFFFF00427C000  pa 0x4807C000
    entry    pa 0x4B1EC488

and then prints nothing more.  Nothing in that silence says whether XNU
initialised, and the guest shows its kernel log neither on screen nor on serial.
The only way to find out is to read the guest's memory while it runs, which is
what this module does.

It is the Windows-native replacement for UniXNU's ``scripts/qemu-ps.py``, which
cannot run here for three concrete reasons rather than stylistic ones:

* it speaks QMP over ``AF_UNIX``.  Windows has no AF_UNIX socket for QEMU, so
  the transport must be ``tcp:127.0.0.1:<port>`` -- which is what
  :mod:`unipad.host.qmp` already implements, and what ``unixnu_boot`` already
  asks QEMU for (``-qmp tcp:127.0.0.1:<port>,server=on,wait=off``);
* it resolves kernel symbols with ``xcrun nm``.  There is no ``xcrun`` on
  Windows, so the Mach-O symbol table is parsed here instead;
* it discovers the run directory and kernel path by macOS conventions.  Every
  path is supplied by the caller, and the kernel image is *verified* before a
  single byte of guest memory is read.

Refusal, not guessing
---------------------
Everything below is built around one rule: **a wrong answer is worse than no
answer.**  A page-table walk that hits an unmapped descriptor, a symbol the
image does not carry, a kernel whose digest is not the one that was verified,
or a process list that does not terminate -- each of those raises rather than
returning something plausible.  Reading guest memory through the wrong page
tables produces addresses that look like PIDs and text that looks like a log,
which is precisely the failure mode an observation channel must not have.

Privacy (PRD 9.2)
-----------------
Nothing in this module writes an absolute path into a sample.  The kernel
digest, the load addresses, PIDs, process names and the log text are all either
host-independent or supplied by the caller.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from unipad.errors import ErrorCode, UniPadError

__all__ = [
    "PROBE_VERSION",
    "REQUIRED_SYMBOLS",
    "GuestMemory",
    "GuestObserver",
    "KernelImage",
    "LoadAddresses",
    "Process",
    "Sample",
    "load_addresses_from_serial",
]

#: Bumped whenever the reading layout or the stamp changes, so a sample can be
#: attributed to the code that produced it and two samples from different
#: versions are never silently compared.
PROBE_VERSION = "1.0.0"

#: Symbols without which nothing here can be read.  ``cpu_ttep`` is the root of
#: the kernel's own page tables, ``allproc`` the head of the process list and
#: ``msgbufp`` the kernel log.  A Mach-O that carries none of them is not a
#: kernel this probe can read, and saying so beats reading garbage.
REQUIRED_SYMBOLS = ("cpu_ttep", "allproc", "msgbufp")

#: ``struct proc`` field offsets, taken from UniXNU's ``scripts/qemu-entry/
#: gdb-stop.py`` -- the same defaults ``qemu-ps.py`` uses.  They are recorded
#: here rather than derived because they are a property of the XNU *build*, not
#: of anything this module can inspect.
PROC_P_LIST = 0        # p_list.le_next
PROC_P_PID = 96        # p_pid
PROC_P_STAT = 100      # p_stat
PROC_P_COMM = 1468     # p_comm (17 bytes, NUL-padded)
PROC_COMM_BYTES = 17

#: How much of ``struct proc`` this probe reads: from ``p_list`` to the end of
#: ``p_comm``.  One page read covers the whole node, which is the point -- see
#: :meth:`GuestObserver._proc_fields`.
PROC_SPAN = PROC_P_COMM + PROC_COMM_BYTES

#: The most kernel log this probe will read in one go.  XNU's default ``msgbuf``
#: is a few hundred KiB; 16 MiB is generous and still bounded, because the size
#: comes out of guest memory and a corrupted header must not turn into a million
#: guest reads.
MAX_KERNEL_LOG_BYTES = 16 << 20

#: The most page-table pages one observer will cache.  A walk over a whole
#: kernel touches a few hundred; the bound is there so a long-lived observer
#: cannot grow without limit.
MAX_CACHED_PAGES = 4096

#: ``p_stat`` values.  An unknown value is reported as its number rather than
#: guessed at, because a mislabelled state is worse than an unnamed one.
PROC_STATES = {1: "idle", 2: "run", 3: "stop", 4: "zombie", 5: "reaped"}

#: AArch64 stage-1 translation, 4 KiB granules, as the guest's ``TCR_EL1``
#: selects.  Level 1 indexes with 8 bits at shift 30; levels 2 and 3 with 9 bits
#: at shifts 21 and 12.
_TABLE_SHIFTS = ((1, 30, 0xFF), (2, 21, 0x1FF), (3, 12, 0x1FF))
_DESCRIPTOR_OUTPUT_MASK = 0x0000_FFFF_FFFF_F000
_DESCRIPTOR_VALID = 0x1
#: bits[1:0] of a descriptor.  ``0b11`` is a *table* at level 1 or 2 and a *page*
#: at level 3; ``0b01`` is a *block* at level 1 or 2, which maps a whole 1 GiB or
#: 2 MiB range without descending.  The two are one bit apart, and reading a
#: table as a block (or the reverse) produces addresses that look plausible --
#: which is why the distinction is a named constant rather than a bare ``3``.
_DESCRIPTOR_TYPE_MASK = 0x3
_DESCRIPTOR_BLOCK = 0x1
#: Kept explicit because Python's integers are unbounded: ``~((1 << 30) - 1)`` is
#: a *negative* number, and ``0x400000 & ~0x3FFFFFFF`` is 0 rather than
#: 0x400000.  Masking with an unsigned 64-bit value says what is meant.
_ADDRESS_MASK = 0xFFFF_FFFF_FFFF_FFFF

#: Mach-O.
_MH_MAGIC_64 = 0xFEEDFACF
_MH_CIGAM_64 = 0xCFFAEDFE
_LC_SEGMENT_64 = 0x19
_LC_SYMTAB = 0x02
#: ``n_type & N_TYPE_MASK``.  ``0x0E`` is ``N_SECT`` -- a symbol with a section,
#: i.e. a definition.  ``0x00`` is ``N_UNDF``, which is what an undefined
#: reference *and* a common symbol look like; a common carries a size in
#: ``n_value`` rather than an address, which is why only ``N_SECT`` is accepted.
_N_TYPE_MASK = 0x0E
_N_SECT = 0x0E

#: The kernel load line DarwinBoot prints.  Both addresses are required: the
#: virtual one is the base the page tables are built for, the physical one is
#: where those bytes actually are in guest RAM.
_LOAD_LINE = re.compile(
    rb"kernel\s+va\s+0x([0-9A-Fa-f]+)\s+pa\s+0x([0-9A-Fa-f]+)"
)


def _refuse(code: str, message: str, **details: Any) -> UniPadError:
    """A refusal, with enough detail to act on and nothing secret (PRD 9.2)."""
    return UniPadError(code, message, **details)


# ---------------------------------------------------------------------------
# the kernel image
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Segment:
    """One ``LC_SEGMENT_64``: where it sits in memory and in the file."""

    name: str
    vmaddr: int
    vmsize: int
    fileoff: int
    filesize: int

    def contains(self, vmaddr: int) -> bool:
        return self.vmaddr <= vmaddr < self.vmaddr + self.vmsize


@dataclass(frozen=True)
class LoadAddresses:
    """Where the running kernel was loaded, as the guest itself reported it.

    Both halves come from the serial log's load line.  The mapping between them
    is the identity map the kernel was linked for, and it is what makes
    ``cpu_ttep`` readable: the symbol's *virtual* address is known from the
    image, and its *physical* address is only known once the loader says where
    it put the image.
    """

    kernel_va: int
    kernel_pa: int
    entry_pa: Optional[int] = None

    def physical(self, vmaddr: int) -> int:
        """The physical address of a virtual address inside the kernel image."""
        return vmaddr - self.kernel_va + self.kernel_pa


def load_addresses_from_serial(text: str) -> LoadAddresses:
    """The kernel load addresses from a serial log, or a refusal.

    The log is read as bytes because it is a boot log: it contains a partial
    line if the run was cut short, and ``str`` decoding of a truncated multibyte
    sequence would raise on a file that is otherwise perfectly good evidence.
    """
    blob = text.encode("utf-8", "replace") if isinstance(text, str) else text
    match = _LOAD_LINE.search(blob)
    if match is None:
        raise _refuse(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "the serial log has no kernel load line, so there is no verified "
            "mapping between the kernel's virtual and physical addresses",
        )
    entry = re.search(rb"entry\s+pa\s+0x([0-9A-Fa-f]+)", blob)
    return LoadAddresses(
        kernel_va=int(match[1], 16),
        kernel_pa=int(match[2], 16),
        entry_pa=int(entry[1], 16) if entry else None,
    )


class KernelImage:
    """A verified kernel Mach-O: its digest, its segments and its symbols.

    The image is parsed once and then treated as read-only data.  Verification
    happens at construction: an image that is not a 64-bit Mach-O, or that does
    not carry every symbol in :data:`REQUIRED_SYMBOLS` as a *definition*, is
    refused here rather than discovered half way through a walk.
    """

    def __init__(
        self,
        path: Path,
        *,
        expected_sha256: Optional[str] = None,
        required_symbols: Sequence[str] = REQUIRED_SYMBOLS,
    ) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise _refuse(
                ErrorCode.IMAGE_NOT_FOUND,
                "the kernel image is not present",
                image=self.path.name,
            )
        blob = self.path.read_bytes()
        self.size = len(blob)
        self.sha256 = hashlib.sha256(blob).hexdigest()
        if expected_sha256 and self.sha256 != expected_sha256.lower():
            # The one comparison that must never be relaxed: an image that is
            # not the one that was verified is not the kernel we think we are
            # reading, and every address derived from it would be wrong.
            raise _refuse(
                ErrorCode.IMAGE_HASH_MISMATCH,
                "the kernel image does not match the digest it was verified against",
                image=self.path.name,
                expected=expected_sha256.lower(),
                actual=self.sha256,
            )

        magic = struct.unpack_from(">I", blob, 0)[0]
        if magic == _MH_CIGAM_64:
            end = "<"
        elif magic == _MH_MAGIC_64:
            end = ">"
        else:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                "the kernel image is not a 64-bit Mach-O",
                image=self.path.name,
                magic=hex(magic),
            )
        self._end = end

        ncmds = struct.unpack_from(end + "7I", blob, 0)[4]
        self.segments: tuple[Segment, ...] = ()
        symtab: Optional[tuple[int, int, int, int]] = None
        at = 32
        for _ in range(ncmds):
            cmd, cmdsize = struct.unpack_from(end + "II", blob, at)
            if cmdsize < 8:
                raise _refuse(
                    ErrorCode.CONFIG_INVALID,
                    "the kernel image has a load command smaller than its header",
                    image=self.path.name,
                    command=hex(cmd),
                )
            if cmd == _LC_SEGMENT_64 and cmdsize >= 72:
                name = blob[at + 8:at + 24].rstrip(b"\0").decode("utf-8", "replace")
                vmaddr, vmsize, fileoff, filesize = struct.unpack_from(
                    end + "4Q", blob, at + 24
                )
                self.segments += (
                    Segment(name, vmaddr, vmsize, fileoff, filesize),
                )
            elif cmd == _LC_SYMTAB and cmdsize >= 24:
                symtab = struct.unpack_from(end + "4I", blob, at + 8)
            at += cmdsize
        if symtab is None:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                "the kernel image carries no symbol table, so no kernel global "
                "can be located",
                image=self.path.name,
            )

        symoff, nsyms, stroff, strsize = symtab
        self.symbol_count = nsyms
        strings = blob[stroff:stroff + strsize]
        # A symbol may appear more than once -- once as a definition and once as
        # an external reference.  Both spellings are in the table; only the
        # definition has a value, and only the definition is what we want.
        # Measured on this host: ``_cpu_ttep`` appears at index 100158 as
        # ``N_UNDF`` with value 0 and again at 261414 as ``N_SECT`` at
        # 0xfffffff007166bb8.  Taking the first occurrence, which is what a
        # naive scan does, yields zero and a walk that cannot start.
        self._symbols: dict[str, int] = {}
        for index in range(nsyms):
            entry_at = symoff + index * 16
            if entry_at + 16 > len(blob):
                break
            strx, n_type, _n_sect, _n_desc, n_value = struct.unpack_from(
                end + "IBBHQ", blob, entry_at
            )
            if (n_type & _N_TYPE_MASK) != _N_SECT or n_value == 0:
                continue
            if strx == 0 or strx >= len(strings):
                continue
            stop = strings.find(b"\0", strx)
            name = strings[strx:stop].decode("utf-8", "replace")
            self._symbols.setdefault(name, n_value)

        missing = [name for name in required_symbols if self.symbol(name) is None]
        if missing:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                "the kernel image does not define the symbols this probe reads, "
                "so it is not a kernel this probe can read",
                image=self.path.name,
                missing=missing,
            )
        self.required_symbols = tuple(required_symbols)

    # -- lookups ----------------------------------------------------------
    def symbol(self, name: str) -> Optional[int]:
        """The virtual address of a defined symbol, with or without its ``_``.

        Mach-O prefixes C symbols with an underscore, so both spellings are
        accepted: a caller that writes ``allproc`` and a caller that writes
        ``_allproc`` mean the same symbol, and rejecting one of them would be a
        trap rather than a check.
        """
        for candidate in (name, name if name.startswith("_") else "_" + name):
            found = self._symbols.get(candidate)
            if found is not None:
                return found
        return None

    def require_symbol(self, name: str) -> int:
        found = self.symbol(name)
        if found is None:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                "the kernel image does not define a symbol this probe needs",
                image=self.path.name,
                symbol=name,
            )
        return found

    def segment_of(self, vmaddr: int) -> Optional[Segment]:
        for segment in self.segments:
            if segment.contains(vmaddr):
                return segment
        return None

    def summary(self) -> dict[str, Any]:
        """What a sample records about the image: identity, never contents."""
        return {
            "image": self.path.name,
            "bytes": self.size,
            "sha256": self.sha256,
            "segments": len(self.segments),
            "symbols": self.symbol_count,
        }


# ---------------------------------------------------------------------------
# guest physical memory
# ---------------------------------------------------------------------------
class GuestMemory:
    """Guest physical memory, read through QMP ``pmemsave``.

    One connection for the whole session: QEMU takes a single QMP client at a
    time and is slow to take the next, so a connection per read would often be
    refused.  Reads go to a scratch file because ``pmemsave`` writes to a file
    rather than returning bytes -- there is no QMP command that does.

    ``client`` is anything with ``execute(command, arguments)`` returning the
    reply dict -- :class:`unipad.host.qmp.QmpClient` in production.  The
    arguments are passed as a mapping rather than as keywords because that is
    :class:`~unipad.host.qmp.QmpClient`'s signature.
    """

    def __init__(self, client: Any, scratch: Path) -> None:
        self._client = client
        self._scratch = Path(scratch)
        self.reads = 0
        self.bytes_read = 0

    def read(self, physical: int, size: int) -> bytes:
        if size <= 0:
            return b""
        self._scratch.parent.mkdir(parents=True, exist_ok=True)
        self._client.execute(
            "pmemsave",
            {"val": int(physical), "size": int(size), "filename": str(self._scratch)},
        )
        data = self._scratch.read_bytes()
        if len(data) != size:
            raise _refuse(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "the guest did not return the memory that was asked for",
                physical=hex(physical),
                asked=int(size),
                returned=len(data),
            )
        self.reads += 1
        self.bytes_read += size
        return data


# ---------------------------------------------------------------------------
# the observer
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Process:
    """One row of the guest's process list."""

    pid: int
    state: str
    name: str
    address: int


@dataclass(frozen=True)
class Sample:
    """One stamped observation of the guest.

    The stamp is not decoration: two samples taken minutes apart, from two
    different kernels, under two different probe versions, are not comparable,
    and a sample that does not say which of those it was cannot be used as
    evidence for anything (PRD 9.2).
    """

    probe_version: str
    taken_at: str
    run_id: str
    kernel_sha256: str
    kernel_va: int
    kernel_pa: int
    entry_pa: Optional[int]
    processes: tuple[Process, ...]
    kernel_log_bytes: int
    kernel_log_sha256: str
    memory_reads: int
    memory_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_version": self.probe_version,
            "taken_at": self.taken_at,
            "run_id": self.run_id,
            "kernel": {
                "sha256": self.kernel_sha256,
                "va": hex(self.kernel_va),
                "pa": hex(self.kernel_pa),
                "entry_pa": hex(self.entry_pa) if self.entry_pa else None,
            },
            "process_count": len(self.processes),
            "processes": [
                {
                    "pid": process.pid,
                    "state": process.state,
                    "name": process.name,
                    "address": hex(process.address),
                }
                for process in self.processes
            ],
            "kernel_log": {
                "bytes": self.kernel_log_bytes,
                "sha256": self.kernel_log_sha256,
            },
            "memory": {"reads": self.memory_reads, "bytes": self.memory_bytes},
        }


@dataclass
class GuestObserver:
    """Translate kernel virtual addresses through the guest's own page tables.

    The translation table root is ``cpu_ttep``, read out of guest memory at the
    physical address the loader reported.  That is deliberate: the monitor's
    ``x`` command would ask the virtual CPU for its state on every read, which
    under some accelerators is not seen at all and leaves QEMU's main loop, its
    window and its monitor waiting.  Reading physical memory and translating
    here needs nothing from the virtual CPU.
    """

    memory: GuestMemory
    image: KernelImage
    load: LoadAddresses
    run_id: str = ""
    max_processes: int = 1024
    _tables: dict[int, bytes] = field(default_factory=dict, repr=False)
    _root: Optional[int] = field(default=None, repr=False)

    # -- physical ---------------------------------------------------------
    def _page(self, physical: int) -> bytes:
        key = physical & ~0xFFF
        cached = self._tables.get(key)
        if cached is None:
            if len(self._tables) >= MAX_CACHED_PAGES:
                # Drop the whole cache rather than evicting one entry: the pages
                # are all part of one translation tree, so a partial eviction
                # would keep re-reading the ones that matter.
                self._tables.clear()
            cached = self.memory.read(key, 4096)
            self._tables[key] = cached
        return cached

    def physical_read(self, physical: int, size: int) -> bytes:
        data = b""
        while size:
            offset = physical & 0xFFF
            chunk = min(size, 4096 - offset)
            data += self._page(physical)[offset:offset + chunk]
            physical += chunk
            size -= chunk
        return data

    # -- translation ------------------------------------------------------
    @property
    def table_root(self) -> int:
        """``cpu_ttep``: the physical address of the kernel's level-1 table.

        A root of zero is refused rather than used.  That is not pedantry: a
        kernel that has not installed its own tables yet holds zero here, and
        zero is a *readable* physical page, so walking from it returns whatever
        the first 4 KiB of guest RAM happen to look like.  If those bytes happen
        to parse as a valid table descriptor -- which they can, because the
        check is only "is bit 0 set" -- the walk produces addresses and never
        raises.  Refusing on the one value that is certainly wrong is what keeps
        that from reading as an observation.
        """
        if self._root is None:
            symbol = self.image.require_symbol("cpu_ttep")
            raw = self.physical_read(self.load.physical(symbol), 8)
            root = int.from_bytes(raw, "little")
            if root == 0:
                raise _refuse(
                    ErrorCode.EVIDENCE_INCOMPLETE,
                    "the guest's page-table root is zero, so its own tables are "
                    "not installed yet and no kernel address can be translated",
                    symbol="cpu_ttep",
                )
            if root & 0xFFF:
                raise _refuse(
                    ErrorCode.EVIDENCE_INCOMPLETE,
                    "the guest's page-table root is not page-aligned, so it is "
                    "not a translation table",
                    symbol="cpu_ttep",
                    root=hex(root),
                )
            self._root = root
        assert self._root is not None
        return self._root

    def translate(self, vmaddr: int) -> int:
        """The physical address of a kernel virtual address, or a refusal.

        A descriptor that is not valid means the address is not mapped, and the
        only honest answer is to say so.  Returning the last plausible-looking
        output instead would read whatever happens to live there.
        """
        table = self.table_root & _DESCRIPTOR_OUTPUT_MASK
        for level, shift, index_mask in _TABLE_SHIFTS:
            index = (vmaddr >> shift) & index_mask
            descriptor = int.from_bytes(
                self._page(table)[index * 8:index * 8 + 8], "little"
            )
            if not descriptor & _DESCRIPTOR_VALID:
                raise _refuse(
                    ErrorCode.EVIDENCE_INCOMPLETE,
                    "a kernel virtual address is not mapped in the guest's own "
                    "page tables",
                    vmaddr=hex(vmaddr),
                    level=level,
                    table=hex(table),
                )
            output = descriptor & _DESCRIPTOR_OUTPUT_MASK
            if level == 3 or (descriptor & _DESCRIPTOR_TYPE_MASK) == _DESCRIPTOR_BLOCK:
                # A leaf.  The output is masked to the leaf's own alignment,
                # because a block's output address field is aligned by the
                # architecture and the bits below it are not part of the address.
                alignment = (1 << shift) - 1
                return (output & (_ADDRESS_MASK & ~alignment)) | (vmaddr & alignment)
            table = output
        raise _refuse(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "a kernel virtual address has no leaf descriptor",
            vmaddr=hex(vmaddr),
        )

    def read(self, vmaddr: int, size: int) -> bytes:
        """Read kernel virtual memory, a page at a time."""
        data = b""
        while size:
            chunk = min(size, 4096 - (vmaddr & 0xFFF))
            data += self.physical_read(self.translate(vmaddr), chunk)
            vmaddr += chunk
            size -= chunk
        return data

    def value(self, name: str, size: int = 8) -> int:
        """A kernel global, as an unsigned little-endian integer."""
        return int.from_bytes(self.read(self.image.require_symbol(name), size), "little")

    # -- what the guest is doing -----------------------------------------
    def _proc_fields(self, address: int) -> tuple[int, int, str, int]:
        """One ``struct proc``'s pid, state, name and next pointer.

        Read as one span rather than four.  ``p_pid``, ``p_stat``, ``p_comm`` and
        ``p_list`` are all inside ``struct proc``, which is 1468+17 bytes and
        therefore spans at most two pages.

        What this does and does not buy, measured: it does **not** reduce guest
        reads, because ``_page`` already caches by physical page and all four
        fields land in the same one or two pages either way.  What it buys is one
        read instead of four -- so the four values are guaranteed to come from
        the same bytes -- and it puts the ``struct proc`` layout in one place
        instead of four.  A row assembled from four separate reads could in
        principle be torn; a row read as one span cannot.
        """
        blob = self.read(address, PROC_SPAN)
        pid = int.from_bytes(blob[PROC_P_PID:PROC_P_PID + 4], "little", signed=True)
        state = blob[PROC_P_STAT]
        name = (
            blob[PROC_P_COMM:PROC_P_COMM + PROC_COMM_BYTES]
            .split(b"\0")[0]
            .decode("utf-8", "replace")
        )
        following = int.from_bytes(
            blob[PROC_P_LIST:PROC_P_LIST + 8], "little"
        )
        return pid, state, name, following

    def processes(self) -> tuple[Process, ...]:
        """Walk ``allproc``.

        The walk is bounded and the bound is enforced: a corrupted ``p_list``
        that loops would otherwise spin forever, and a truncated one would
        silently report fewer processes than exist.  Both are refusals, because
        "the process list could not be read to its end" is a fact about the
        observation and must not be flattened into a shorter list.
        """
        head = self.value("allproc")
        rows: list[Process] = []
        current = head
        seen: set[int] = set()
        while current:
            if current in seen:
                raise _refuse(
                    ErrorCode.EVIDENCE_INCOMPLETE,
                    "the process list loops back on itself, so it cannot be "
                    "walked to its end",
                    at=hex(current),
                    walked=len(rows),
                )
            if len(rows) >= self.max_processes:
                raise _refuse(
                    ErrorCode.EVIDENCE_INCOMPLETE,
                    "the process list did not terminate within the bound",
                    bound=self.max_processes,
                )
            seen.add(current)
            pid, state, name, following = self._proc_fields(current)
            rows.append(
                Process(
                    pid=pid,
                    state=PROC_STATES.get(state, str(state)),
                    name=name,
                    address=current,
                )
            )
            current = following
        return tuple(rows)

    def kernel_log(self) -> bytes:
        """The kernel log, from ``msgbufp``'s ring buffer.

        ``struct msgbuf`` is magic, size, write index, read index (four ints)
        and then the buffer pointer.  The buffer is a ring, so the live text is
        ``data[write:] + data[:write]`` -- reading it linearly would return the
        oldest bytes last and the newest first, which reads as a log running
        backwards.

        ``size`` comes out of guest memory, so it is bounded before it is used:
        a corrupted header claiming a 4 GiB ring would otherwise ask for a
        million guest reads, and the refusal says which bound was hit rather
        than timing out.
        """
        buffer = self.value("msgbufp")
        header = self.read(buffer, 24)
        size = int.from_bytes(header[4:8], "little")
        write = int.from_bytes(header[8:12], "little")
        data_pointer = int.from_bytes(header[16:24], "little")
        if size <= 0 or write > size:
            raise _refuse(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "the kernel's message buffer does not describe a readable ring",
                size=size,
                write=write,
            )
        if size > MAX_KERNEL_LOG_BYTES:
            raise _refuse(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "the kernel's message buffer claims more than this probe will "
                "read, so the header is not trustworthy",
                size=size,
                bound=MAX_KERNEL_LOG_BYTES,
            )
        data = self.read(data_pointer, size)
        return (data[write:] + data[:write]).replace(b"\0", b"")

    def sample(self, *, taken_at: str = "", log_path: Optional[Path] = None) -> Sample:
        """One stamped observation, optionally writing the kernel log out.

        ``taken_at`` defaults to now, so a caller that does not care still gets a
        stamped sample.  A sample with no timestamp cannot be compared with any
        other, which is the one thing the stamp exists to prevent.
        """
        from datetime import datetime, timezone

        stamp = taken_at or datetime.now(timezone.utc).isoformat()
        rows = self.processes()
        log = self.kernel_log()
        if log_path is not None:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_bytes(log)
        return Sample(
            probe_version=PROBE_VERSION,
            taken_at=stamp,
            run_id=self.run_id,
            kernel_sha256=self.image.sha256,
            kernel_va=self.load.kernel_va,
            kernel_pa=self.load.kernel_pa,
            entry_pa=self.load.entry_pa,
            processes=rows,
            kernel_log_bytes=len(log),
            kernel_log_sha256=hashlib.sha256(log).hexdigest(),
            memory_reads=self.memory.reads,
            memory_bytes=self.memory.bytes_read,
        )


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Read one guest and print one JSON object.

    ``python -m unipad.host.guestobserve --qmp-port 4444 --kernel KERNEL
    [--serial serial.log] [--expected-sha256 HEX] [--run-id ID] [--log FILE]
    [--json]``

    Exactly one JSON object goes to stdout -- on the success path *and* on the
    refusal path, because ``tests/acceptance/test_m0_ai_interface_smoke.py``
    enforces that convention for every other command by ``json.loads``-ing the
    whole buffer, and a caller that has to try stdout and then stderr would be a
    second thing to keep in step.  A refusal is distinguished by ``ok: false``
    and a frozen ``code``, and it exits non-zero, so a run that could not be
    observed can never read as an observation.

    ``--kernel`` is verified *before* the guest is touched, so a wrong image
    costs one hash and no guest reads.  The kernel's path is never printed --
    only its basename and its digest -- because PRD 9.2 forbids absolute user
    paths in evidence and ``--kernel`` is exactly where one would arrive.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Read a live UniXNU guest's kernel state.")
    parser.add_argument("--qmp-port", type=int, required=True,
                        help="QEMU's QMP TCP port (-qmp tcp:127.0.0.1:<port>)")
    parser.add_argument("--qmp-host", default="127.0.0.1",
                        help="QMP host; loopback unless told otherwise")
    parser.add_argument("--kernel", type=Path, required=True, help="the kernel.macho the guest booted")
    parser.add_argument("--expected-sha256", default=None,
                        help="refuse unless the kernel's digest is exactly this")
    parser.add_argument("--serial", type=Path, default=None,
                        help="the run's serial log, for the kernel load line")
    parser.add_argument("--kernel-va", default=None,
                        help="kernel virtual base, if --serial is unavailable")
    parser.add_argument("--kernel-pa", default=None,
                        help="kernel physical base, if --serial is unavailable")
    parser.add_argument("--run-id", default="", help="run id, stamped into the sample")
    parser.add_argument("--log", type=Path, default=None, help="write the kernel log here")
    parser.add_argument("--scratch", type=Path, default=None,
                        help="where pmemsave writes; defaults to a temporary file")
    parser.add_argument("--max-processes", type=int, default=1024,
                        help="refuse if the process list does not end within this many rows")
    args = parser.parse_args(argv)

    import json
    import tempfile

    from unipad.host.qmp import QmpClient

    def emit(document: dict) -> None:
        print(json.dumps(document, indent=1, sort_keys=True))

    def _discard(directories: list[Path]) -> None:
        import shutil

        for directory in directories:
            shutil.rmtree(directory, ignore_errors=True)

    # Declared before the try so every exit path can clean up after itself.
    owned: list[Path] = []
    try:
        image = KernelImage(args.kernel, expected_sha256=args.expected_sha256)

        if args.serial is not None:
            if not args.serial.is_file():
                raise _refuse(
                    ErrorCode.EVIDENCE_INCOMPLETE,
                    "the serial log is not present, so the kernel's load "
                    "addresses cannot be read from it",
                    serial=args.serial.name,
                )
            load = load_addresses_from_serial(args.serial.read_text(encoding="utf-8", errors="replace"))
        elif args.kernel_va and args.kernel_pa:
            load = LoadAddresses(
                kernel_va=int(args.kernel_va, 0), kernel_pa=int(args.kernel_pa, 0)
            )
        else:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                "the kernel's load addresses are required: pass --serial, or "
                "--kernel-va together with --kernel-pa",
            )

        if args.scratch is not None:
            scratch = args.scratch
        else:
            # pmemsave writes a file per page, so the scratch file is real disk
            # traffic.  A caller that does not say where gets a temporary
            # directory that is removed on the way out -- otherwise every
            # observation leaks one.
            directory = Path(tempfile.mkdtemp(prefix="unipad-observe-"))
            owned.append(directory)
            scratch = directory / "pmem.bin"
        client = QmpClient(host=args.qmp_host, port=args.qmp_port)
        try:
            client.connect()
            observer = GuestObserver(
                memory=GuestMemory(client, scratch),
                image=image,
                load=load,
                run_id=args.run_id,
                max_processes=args.max_processes,
            )
            sample = observer.sample(log_path=args.log)
        finally:
            client.close()

        emit(
            {
                "ok": True,
                "probe_version": sample.probe_version,
                "run_id": sample.run_id,
                "kernel": {
                    **image.summary(),
                    "va": hex(sample.kernel_va),
                    "pa": hex(sample.kernel_pa),
                    "entry_pa": hex(sample.entry_pa) if sample.entry_pa else None,
                },
                "processes": [
                    {"pid": row.pid, "state": row.state, "name": row.name}
                    for row in sample.processes
                ],
                "kernel_log": {
                    "bytes": sample.kernel_log_bytes,
                    "sha256": sample.kernel_log_sha256,
                    "written_to": args.log.name if args.log else None,
                },
                "memory": {"reads": sample.memory_reads, "bytes": sample.memory_bytes},
                "taken_at": sample.taken_at,
            }
        )
        _discard(owned)
        return 0
    except UniPadError as exc:
        _discard(owned)
        emit(
            {
                "ok": False,
                "code": exc.code,
                "reason": exc.message,
                "details": exc.details,
            }
        )
        return int(exc.exit_code)


if __name__ == "__main__":  # pragma: no cover - the entry point is exercised through main()
    raise SystemExit(main())
