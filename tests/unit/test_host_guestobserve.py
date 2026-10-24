"""``unipad.host.guestobserve``: reading a live guest without guessing.

Why these are unit tests and not a boot test
--------------------------------------------
Everything here that can go wrong goes wrong *silently*: a page-table walk
through the wrong tables returns bytes that look like text, a process list that
loops returns rows that look like PIDs, a linear read of a ring buffer returns a
log that runs backwards.  None of that raises.  So the walk, the ring and the
list are each exercised against a synthetic guest whose contents are known, and
the refusals are exercised against guests that are broken in one specific way.

The real kernel image is used for the symbol tests, because the interesting
failure there was real: measured on this host, ``_cpu_ttep`` appears twice in
``kernel.macho``'s symbol table -- once as ``N_UNDF`` with value 0 and once as
``N_SECT`` at ``0xfffffff007166bb8``.  A scan that takes the first occurrence
gets zero, and the walk cannot start.  ``test_a_symbol_that_appears_twice_
resolves_to_its_definition`` pins that.

What is deliberately *not* tested here
--------------------------------------
A live QMP session.  That needs a running guest, and the guest has not yet
initialised XNU on this host -- which is the whole reason the observation
channel is being built.  The transport is :mod:`unipad.host.qmp`, which has its
own tests.

The synthetic guest's address map
---------------------------------
``cpu_ttep``'s value is read through the load line's identity mapping, because
that is the one read that has to happen *before* any translation exists.  After
that, everything goes through the planted tables, which map virtual page *i* to
physical page *i*.  Two different paths to what looks like the same address is
not an accident of the fixture -- it is the actual design, and
``test_the_table_root_comes_from_the_load_line_not_the_page_tables`` is what
keeps the two apart.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import guestobserve as go

# No ``acceptance`` marker: this is a unit test of a host module against a
# synthetic guest, not a milestone-gate simulation, and marking it as one would
# put it in the ``-m acceptance`` selection by mistake.

#: The real kernel this host has, and the one the Windows boot handed control
#: to.  Its digest is recorded so a test can prove the image did not change
#: underneath it rather than assuming it did not.
REAL_KERNEL = (
    Path(__file__).resolve().parents[2]
    / "local-private"
    / "boot7"
    / "boot-inputs"
    / "unixnu-out"
    / "qemu"
    / "kernel"
    / "kernel.macho"
)
REAL_KERNEL_SHA256 = (
    "9d4b1ab14d74fdfa6afeac12871427e26441cab9f3e2efc97e42b103275643e0"
)

#: The load line DarwinBoot printed on this host, verbatim from
#: ``local-private/boot7/run2/serial.log``.
REAL_LOAD_LINE = (
    "kernel   va 0xFFFFFFF00427C000  pa 0x4807C000\n"
    "entry    pa 0x4B1EC488\n"
)
REAL_KERNEL_VA = 0xFFFFFFF00427C000
REAL_KERNEL_PA = 0x4807C000

#: The synthetic guest's kernel window.  Small on purpose: the level-1, level-2
#: and level-3 indices of ``0x1000``, ``0x2000`` and ``0x3000`` are all zero or
#: one, so the fixture's table tree stays readable.  The load addresses are
#: supplied per test, because the *offset* between them is what is under test.
STUB_VA = {"cpu_ttep": 0x1000, "allproc": 0x2000, "msgbufp": 0x3000}

#: The default load offset.  It is deliberately non-zero so that *every* read in
#: the fixture has to go through the load line: the identity address of a global
#: (``va + 0x30000``) is a different physical page from the one the planted
#: tables map its virtual address to.  A fixture with ``va == pa`` would pass
#: even if the load line were ignored.
LOAD_PA_OFFSET = 0x30000

#: ``cpu_ttep`` is the one global read through the load line's *identity*
#: mapping, because its value is what makes translation possible at all.  So it
#: physically lives at ``symbol_va + LOAD_PA_OFFSET``.
PA_TTEP = 0x1000 + LOAD_PA_OFFSET       # holds the page-table root

#: Everything else is read through the planted tables, which map virtual page
#: *i* to physical page *i* -- so these are the virtual addresses too.
#: ``msgbufp`` is a *pointer* to ``struct msgbuf``, and the struct's last field
#: points at the ring; writing the struct at ``msgbufp`` instead reads the
#: pointer as the struct.
PA_ALLPROC = 0x2000        # holds the process list head
PA_MSGBUF = 0x3000         # holds a pointer to struct msgbuf
PA_PROC_A = 0x5000         # a process node
PA_PROC_B = 0x6000         # another process node
PA_LOG_RING = 0x7000       # the kernel log's ring buffer
PA_MSGBUF_STRUCT = 0x8000  # struct msgbuf itself
PA_FIRST_PAGE = 0x0000     # what virtual page 0 maps to

#: The page tables themselves.  Raw physical addresses, never translated.
PA_L1 = 0x10000
PA_L2 = 0x11000
PA_L3 = 0x12000
TTEP_VALUE = PA_L1      # what cpu_ttep points at


# ---------------------------------------------------------------------------
# a synthetic guest
# ---------------------------------------------------------------------------
class FakeGuest:
    """Guest physical memory plus the one QMP command the observer uses.

    ``pmemsave`` writes to a file because that is what QEMU's ``pmemsave``
    does -- there is no QMP command that returns bytes -- so the fake has to
    reproduce that shape rather than returning a value.  A fake that returned
    bytes would test a transport this module never uses.
    """

    def __init__(self, pages: int = 64) -> None:
        self.ram = bytearray(pages * 4096)
        self.commands: list[tuple[str, dict[str, Any]]] = []

    def write(self, physical: int, data: bytes) -> None:
        self.ram[physical:physical + len(data)] = data

    def execute(
        self, command: str, arguments: Optional[Mapping[str, Any]] = None, **_: Any
    ) -> dict[str, Any]:
        # ``QmpClient``'s signature: the command, then a mapping.  Matching it
        # here means the fake exercises the same call shape production uses.
        payload = dict(arguments or {})
        self.commands.append((command, payload))
        if command != "pmemsave":
            raise AssertionError(f"the observer issued an unexpected command: {command}")
        val, size = int(payload["val"]), int(payload["size"])
        Path(payload["filename"]).write_bytes(bytes(self.ram[val:val + size]))
        return {"return": {}}


@pytest.fixture()
def guest(tmp_path: Path) -> FakeGuest:
    return FakeGuest()


class StubImage:
    """A kernel image that answers symbol lookups and nothing else.

    The observer asks the image for addresses and a digest; both are supplied so
    the fixture controls exactly what the guest is asked to read.
    """

    def __init__(self, symbols: Optional[dict[str, int]] = None) -> None:
        self._symbols = dict(symbols if symbols is not None else STUB_VA)
        self.sha256 = "0" * 64
        self.size = 1
        self.path = Path("stub-kernel.macho")
        self.segments = ()
        self.symbol_count = 0

    def symbol(self, name: str) -> Optional[int]:
        for candidate in (name, "_" + name):
            if candidate in self._symbols:
                return self._symbols[candidate]
        return None

    def require_symbol(self, name: str) -> int:
        found = self.symbol(name)
        if found is None:
            raise UniPadError(ErrorCode.CONFIG_INVALID, "missing", symbol=name)
        return found

    def segment_of(self, vmaddr: int) -> Any:
        return None

    def summary(self) -> dict[str, Any]:
        return {"image": "stub"}


def _observer(
    guest: FakeGuest,
    tmp_path: Path,
    *,
    image: Optional[StubImage] = None,
    kernel_va: int = 0,
    kernel_pa: int = LOAD_PA_OFFSET,
    **kwargs: Any,
) -> go.GuestObserver:
    """An observer over the synthetic guest, ready for tables to be planted."""
    return go.GuestObserver(
        memory=go.GuestMemory(guest, tmp_path / "pmem.bin"),
        image=image or StubImage(),  # type: ignore[arg-type]
        load=go.LoadAddresses(kernel_va=kernel_va, kernel_pa=kernel_pa),
        **kwargs,
    )


def _plant_tables(guest: FakeGuest) -> None:
    """Three levels of 4 KiB tables, mapping virtual page *i* to physical page *i*.

    ``cpu_ttep``'s value is planted too, so the observer can find the root the
    way it will in a real guest: by reading the global, not by being told.
    """
    guest.write(PA_L1, struct.pack("<Q", PA_L2 | 0b11))
    guest.write(PA_L2, struct.pack("<Q", PA_L3 | 0b11))
    for index in range(16):
        guest.write(PA_L3 + index * 8, struct.pack("<Q", (index * 0x1000) | 0b11))
    guest.write(PA_TTEP, struct.pack("<Q", TTEP_VALUE))


def _plant_process(
    guest: FakeGuest, address: int, pid: int, name: str, next_: int, state: int = 2
) -> None:
    guest.write(address + go.PROC_P_PID, struct.pack("<i", pid))
    guest.write(address + go.PROC_P_STAT, bytes([state]))
    guest.write(
        address + go.PROC_P_COMM,
        name.encode()[: go.PROC_COMM_BYTES - 1] + b"\0",
    )
    guest.write(address + go.PROC_P_LIST, struct.pack("<Q", next_))


def _plant_empty_kernel(guest: FakeGuest, log: bytes = b"XNU: up") -> None:
    """A guest with no processes and one ring of log text.

    ``msgbufp`` holds a *pointer* to ``struct msgbuf``, so the value at
    ``PA_MSGBUF`` is an address and the struct itself lives at
    ``PA_MSGBUF_STRUCT`` -- whose last field points at the ring.  Writing the
    struct at ``PA_MSGBUF`` instead reads the pointer as the struct.
    """
    guest.write(PA_ALLPROC, struct.pack("<Q", 0))
    size = 64
    guest.write(PA_LOG_RING, log + b"\0" * (size - len(log)))
    header = struct.pack("<4I", 0x1234, size, len(log), 0) + struct.pack("<Q", PA_LOG_RING)
    guest.write(PA_MSGBUF_STRUCT, header)
    guest.write(PA_MSGBUF, struct.pack("<Q", PA_MSGBUF_STRUCT))


# ---------------------------------------------------------------------------
# the kernel image
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not REAL_KERNEL.is_file(), reason="the local kernel image is absent")
def test_the_real_kernel_resolves_the_symbols_the_probe_reads() -> None:
    """The three globals exist, as definitions, in the image that booted.

    ``cpu_ttep`` is the page-table root, ``allproc`` the process list head and
    ``msgbufp`` the kernel log.  If any of them is missing the observation
    channel cannot start, so this is the test that says the channel is not
    speculative.
    """
    image = go.KernelImage(REAL_KERNEL, expected_sha256=_digest(REAL_KERNEL))
    assert image.sha256 == REAL_KERNEL_SHA256, (
        "the kernel image changed; re-measure before trusting any address "
        "derived from it, and update this digest deliberately"
    )
    for name in go.REQUIRED_SYMBOLS:
        address = image.require_symbol(name)
        assert address, name
        assert image.segment_of(address) is not None, (name, hex(address))


@pytest.mark.skipif(not REAL_KERNEL.is_file(), reason="the local kernel image is absent")
def test_a_symbol_that_appears_twice_resolves_to_its_definition() -> None:
    """The measured trap: ``N_UNDF`` first, ``N_SECT`` second.

    ``_cpu_ttep`` sits at index 100158 as an undefined reference with value 0
    and again at index 261414 as a definition at ``0xfffffff007166bb8``.  A scan
    that keeps the first occurrence gets zero, and every walk then starts from
    physical address 0 -- which is mapped, so nothing raises, and every address
    read afterwards is wrong.
    """
    image = go.KernelImage(REAL_KERNEL, expected_sha256=_digest(REAL_KERNEL))
    assert image.require_symbol("cpu_ttep") == 0xFFFF_FFF0_0716_6BB8


@pytest.mark.skipif(not REAL_KERNEL.is_file(), reason="the local kernel image is absent")
def test_symbols_are_found_with_and_without_the_underscore() -> None:
    """Mach-O's leading underscore is a spelling, not a different symbol."""
    image = go.KernelImage(REAL_KERNEL, expected_sha256=_digest(REAL_KERNEL))
    assert image.symbol("allproc") == image.symbol("_allproc")
    assert image.require_symbol("msgbufp") == image.require_symbol("_msgbufp")


@pytest.mark.skipif(not REAL_KERNEL.is_file(), reason="the local kernel image is absent")
def test_an_image_whose_digest_is_not_the_verified_one_is_refused() -> None:
    """The one comparison that must never be relaxed.

    An image that is not the kernel that was verified is not the kernel being
    read, and every address derived from it would be wrong -- so this refuses
    rather than warning.
    """
    with pytest.raises(UniPadError) as refused:
        go.KernelImage(REAL_KERNEL, expected_sha256="f" * 64)
    assert refused.value.code == ErrorCode.IMAGE_HASH_MISMATCH
    assert refused.value.details["expected"] == "f" * 64
    assert refused.value.details["actual"] == REAL_KERNEL_SHA256


def _digest(path: Path) -> str:
    """The sha256 a test must pass back, so the digest is never spelled twice."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_file_that_is_not_a_mach_o_is_refused(tmp_path: Path) -> None:
    bogus = tmp_path / "not-a-kernel"
    bogus.write_bytes(b"\x7fELF" + b"\0" * 60)
    with pytest.raises(UniPadError) as refused:
        go.KernelImage(bogus, expected_sha256=_digest(bogus))
    assert refused.value.code == ErrorCode.CONFIG_INVALID
    assert "Mach-O" in refused.value.message


def test_an_image_with_no_symbol_table_is_refused(tmp_path: Path) -> None:
    """A Mach-O with no ``LC_SYMTAB`` is not a kernel this probe can read.

    Built rather than sourced: the point is that the refusal names what is
    missing, so an operator knows which of the three globals to look for.
    """
    blob = bytearray(4096)
    struct.pack_into("<7I", blob, 0, go._MH_MAGIC_64, 0, 0, 0, 0, 0, 0)
    path = tmp_path / "no-symbols.macho"
    path.write_bytes(bytes(blob))
    with pytest.raises(UniPadError) as refused:
        go.KernelImage(path, expected_sha256=_digest(path))
    assert refused.value.code == ErrorCode.CONFIG_INVALID
    assert "symbol" in refused.value.message


# ---------------------------------------------------------------------------
# a synthetic Mach-O, so the symbol rules can be tested against data that
# actually discriminates between them
# ---------------------------------------------------------------------------
def _build_macho(tmp_path: Path, entries: list[tuple[str, int, int]]) -> Path:
    """A minimal 64-bit Mach-O whose symbol table holds ``entries``.

    ``entries`` are ``(name, n_type, n_value)`` in table order.  Only
    ``LC_SEGMENT_64`` and ``LC_SYMTAB`` are emitted, because those are the two
    load commands :class:`go.KernelImage` reads.

    This exists because the real kernel does *not* discriminate between the two
    symbol rules worth testing: measured on this host, ``_cpu_ttep``'s undefined
    occurrence has value 0, so "first non-zero value" and "first definition"
    give the same answer.  A rule that only one image can distinguish needs an
    image built to distinguish it.
    """
    names = sorted({name for name, _, _ in entries})
    strtab = bytearray(b"\0")
    offsets: dict[str, int] = {}
    for name in names:
        offsets[name] = len(strtab)
        strtab += name.encode() + b"\0"

    ncmds = 2
    header_size = 32
    segment_size = 72
    symtab_size = 24
    sizeofcmds = segment_size + symtab_size
    symoff = header_size + sizeofcmds
    nsyms = len(entries)
    stroff = symoff + nsyms * 16

    blob = bytearray(stroff + len(strtab))
    # mach_header_64 is eight uint32 fields: magic, cputype, cpusubtype,
    # filetype, ncmds, sizeofcmds, flags, reserved.
    struct.pack_into("<8I", blob, 0, go._MH_MAGIC_64, 0, 0, 0, ncmds, sizeofcmds, 0, 0)
    at = header_size
    # LC_SEGMENT_64: one segment holding everything, so symbol addresses land
    # inside it.
    struct.pack_into("<II", blob, at, go._LC_SEGMENT_64, segment_size)
    blob[at + 8:at + 24] = b"__TEXT".ljust(16, b"\0")
    struct.pack_into("<4Q", blob, at + 24, 0x1000, 0x10000, 0, len(blob))
    at += segment_size
    struct.pack_into(
        "<II4I", blob, at, go._LC_SYMTAB, symtab_size, symoff, nsyms, stroff, len(strtab)
    )
    for index, (name, n_type, n_value) in enumerate(entries):
        struct.pack_into(
            "<IBBHQ", blob, symoff + index * 16, offsets[name], n_type, 1, 0, n_value
        )
    blob[stroff:] = strtab

    path = tmp_path / "synthetic.macho"
    path.write_bytes(bytes(blob))
    return path


def test_a_definition_beats_an_earlier_undefined_reference(tmp_path: Path) -> None:
    """The same symbol twice: the definition wins, not the first row.

    This is the discriminating version of
    ``test_a_symbol_that_appears_twice_resolves_to_its_definition``.  The real
    kernel's undefined occurrence has value 0, so both rules agree there; here
    the undefined row is given a *non-zero* value -- which is what a Mach-O
    common symbol looks like -- so the two rules genuinely differ.
    """
    path = _build_macho(
        tmp_path,
        [
            ("_dup", 0x20, 0x9999),          # undefined, but carrying a value
            ("_dup", 0x0E, 0x1000),          # the real definition
            ("_cpu_ttep", 0x0E, 0x2000),
            ("_allproc", 0x0E, 0x3000),
            ("_msgbufp", 0x0E, 0x4000),
        ],
    )
    image = go.KernelImage(path, expected_sha256=_digest(path))
    assert image.require_symbol("dup") == 0x1000


def test_a_symbol_with_no_definition_is_not_reported_as_one(tmp_path: Path) -> None:
    """A value with no section is not a definition.

    ``N_UNDF`` with a non-zero value is a common symbol: it has a size, not an
    address.  Reporting it as an address would send the walk to a place the
    kernel never put anything.
    """
    path = _build_macho(
        tmp_path,
        [
            ("_cpu_ttep", 0x20, 0x1234),     # undefined with a value
            ("_allproc", 0x0E, 0x3000),
            ("_msgbufp", 0x0E, 0x4000),
        ],
    )
    with pytest.raises(UniPadError) as refused:
        go.KernelImage(path, expected_sha256=_digest(path))
    assert refused.value.code == ErrorCode.CONFIG_INVALID
    assert "cpu_ttep" in str(refused.value.details.get("missing"))


def test_an_absent_image_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UniPadError) as refused:
        go.KernelImage(tmp_path / "nowhere.macho")
    assert refused.value.code == ErrorCode.IMAGE_NOT_FOUND


def test_a_kernel_image_without_a_digest_is_refused(tmp_path: Path) -> None:
    """The digest gate is not optional.

    The symbol addresses this probe walks with come out of this file, so an
    image nobody verified makes every address derived from it wrong -- and the
    walk would still produce PIDs and text that look like a log.  ``if
    expected_sha256 and ...`` made the gate opt-in, which is a gate nobody runs;
    this pins that it is required.
    """
    path = _build_macho(
        tmp_path,
        [
            ("_cpu_ttep", 0x0E, 0x2000),
            ("_allproc", 0x0E, 0x3000),
            ("_msgbufp", 0x0E, 0x4000),
        ],
    )
    with pytest.raises(UniPadError) as refused:
        go.KernelImage(path)
    assert refused.value.code == ErrorCode.CONFIG_INVALID
    assert "digest" in refused.value.message


# ---------------------------------------------------------------------------
# the load line
# ---------------------------------------------------------------------------
def test_the_real_load_line_parses() -> None:
    """Both halves, from the log the Windows boot actually produced."""
    load = go.load_addresses_from_serial(REAL_LOAD_LINE)
    assert load.kernel_va == REAL_KERNEL_VA
    assert load.kernel_pa == REAL_KERNEL_PA
    assert load.entry_pa == 0x4B1EC488
    assert load.physical(REAL_KERNEL_VA) == REAL_KERNEL_PA
    assert load.physical(REAL_KERNEL_VA + 0x1000) == REAL_KERNEL_PA + 0x1000


def test_a_log_with_no_load_line_is_refused() -> None:
    """A log that stops before the hand-off has no verified mapping.

    This is the honest state of most runs on this host, and it must read as
    "not observed" rather than as an empty process list.
    """
    with pytest.raises(UniPadError) as refused:
        go.load_addresses_from_serial("UNIXNU: starting\nDARWINBOOT: loader v2\n")
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE


# ---------------------------------------------------------------------------
# translation
# ---------------------------------------------------------------------------
def test_translation_walks_three_levels_of_four_kib_tables(
    guest: FakeGuest, tmp_path: Path
) -> None:
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    guest.write(PA_FIRST_PAGE, b"first page contents")

    assert observer.translate(0x0000) == PA_FIRST_PAGE
    assert observer.read(0x0000, 5) == b"first"


def test_translation_refuses_an_unmapped_address(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """An invalid descriptor is a refusal, not a guess.

    Physical address 0 is mapped, so returning the last plausible output would
    read whatever lives there and call it kernel memory.
    """
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    with pytest.raises(UniPadError) as refused:
        observer.translate(0x4000_0000)  # level-1 index 1, never planted
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert "not mapped" in refused.value.message


def test_a_level_two_block_descriptor_is_followed(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """A 2 MiB block at level 2 maps without descending to level 3.

    Its own, larger guest: a level-2 block's output has to be 2 MiB aligned, and
    the default fixture is 256 KiB.  A non-zero output is the point -- if the
    descriptor were read as a table the walk would descend into 0x400000 and
    read whatever lives there, and if the block bit were ignored the address
    would not map at all.
    """
    # Eight MiB: the block covers [0x400000, 0x600000), and the second address
    # asserted below lands inside it.
    big = FakeGuest(pages=2048)
    observer = _observer(big, tmp_path)
    block_output = 0x400000
    # Level 1 still points at the level-2 table; level 2 entry 0 becomes a block.
    big.write(PA_L1, struct.pack("<Q", PA_L2 | 0b11))
    big.write(PA_L2, struct.pack("<Q", block_output | 0b01))
    big.write(PA_TTEP, struct.pack("<Q", TTEP_VALUE))
    big.write(block_output + 0x1000, b"block base")

    # A virtual address inside the block, and one well past the first page of it.
    assert observer.translate(0x0000_1000) == block_output + 0x1000
    assert observer.translate(0x0010_1000) == block_output + 0x101000
    assert observer.read(0x0000_1000, 5) == b"block"


def test_a_block_output_is_masked_to_the_block_alignment(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """The low bits of a block's output are ignored, because the hardware ignores them.

    A level-2 block covers 2 MiB, so its output address field is 2 MiB aligned
    and the bits below that are not part of the address.  Getting them back
    would mean the probe invented an address the guest never had, so this pins
    that they are masked off -- and, with them, that the mask is unsigned:
    ``~((1 << 21) - 1)`` in Python is a negative number, and ``0x400000 &`` that
    is 0 rather than 0x400000.
    """
    big = FakeGuest(pages=1024)
    observer = _observer(big, tmp_path)
    big.write(PA_L1, struct.pack("<Q", PA_L2 | 0b11))
    big.write(PA_L2, struct.pack("<Q", 0x400000 | 0b01 | 0x1234))
    big.write(PA_TTEP, struct.pack("<Q", TTEP_VALUE))
    assert observer.translate(0x0000_1000) == 0x401000


def test_reads_cross_page_boundaries(guest: FakeGuest, tmp_path: Path) -> None:
    """A read that spans two pages is assembled from both, in order."""
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    # Ten bytes straddling the page-0/page-1 boundary, written at the physical
    # addresses those two virtual pages map to.
    guest.write(PA_FIRST_PAGE + 4090, b"ABCDEF")
    guest.write(0x1000, b"GHIJKLMNOPQRST")
    assert observer.read(4090, 20) == b"ABCDEFGHIJKLMNOPQRST"


def test_the_table_root_comes_from_the_load_line_not_the_page_tables(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """``cpu_ttep`` is read where the loader put the kernel, not through a walk.

    The symbol's virtual address comes from the image; only the load line says
    where those bytes are in guest RAM.  Getting this wrong reads the image's own
    bytes as a page-table root -- and, worse, would need a translation to read
    the thing that establishes translation.
    """
    # A load offset of one page, so the identity address and the translated
    # address of cpu_ttep are provably different.
    observer = _observer(guest, tmp_path, kernel_va=0x1000, kernel_pa=0x2000)
    _plant_tables(guest)
    # cpu_ttep's VA is 0x1000, so its physical address under this load is 0x2000
    # -- which is allproc's page, not the table the walk would find.
    guest.write(0x2000, struct.pack("<Q", TTEP_VALUE))
    assert observer.table_root == TTEP_VALUE


# ---------------------------------------------------------------------------
# the process list
# ---------------------------------------------------------------------------
def test_the_process_list_is_walked(guest: FakeGuest, tmp_path: Path) -> None:
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    guest.write(PA_ALLPROC, struct.pack("<Q", PA_PROC_A))
    _plant_process(guest, PA_PROC_A, 1, "launchd", PA_PROC_B)
    _plant_process(guest, PA_PROC_B, 42, "SpringBoard", 0)

    rows = observer.processes()
    assert [row.pid for row in rows] == [1, 42]
    assert [row.name for row in rows] == ["launchd", "SpringBoard"]
    assert rows[0].state == "run"


def test_a_process_list_that_loops_is_refused(guest: FakeGuest, tmp_path: Path) -> None:
    """A corrupted ``p_list`` must not spin, and must not report a short list."""
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    guest.write(PA_ALLPROC, struct.pack("<Q", PA_PROC_A))
    _plant_process(guest, PA_PROC_A, 1, "launchd", PA_PROC_A)  # points at itself

    with pytest.raises(UniPadError) as refused:
        observer.processes()
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert "loops" in refused.value.message


def test_a_cycle_through_different_nodes_is_refused(guest: FakeGuest, tmp_path: Path) -> None:
    """A -> B -> A is a cycle even though no node repeats immediately.

    The ``seen`` set is what catches it; the bound is the backstop for a chain
    that never repeats at all.  Either way the answer is the same: the list could
    not be walked to its end, which is a fact about the observation and must not
    be flattened into a shorter list.
    """
    observer = _observer(guest, tmp_path, max_processes=4)
    _plant_tables(guest)
    guest.write(PA_ALLPROC, struct.pack("<Q", PA_PROC_A))
    _plant_process(guest, PA_PROC_A, 1, "a", PA_PROC_B)
    _plant_process(guest, PA_PROC_B, 2, "b", PA_PROC_A)

    with pytest.raises(UniPadError) as refused:
        observer.processes()
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert "loops" in refused.value.message or "bound" in refused.value.message


def test_an_unknown_process_state_is_reported_as_its_number(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """An unnamed state beats a mislabelled one."""
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    guest.write(PA_ALLPROC, struct.pack("<Q", PA_PROC_A))
    _plant_process(guest, PA_PROC_A, 7, "mystery", 0, state=99)

    rows = observer.processes()
    assert rows[0].state == "99"


# ---------------------------------------------------------------------------
# the kernel log
# ---------------------------------------------------------------------------
def test_the_kernel_log_is_unrolled_from_the_ring(guest: FakeGuest, tmp_path: Path) -> None:
    """Newest bytes first: the buffer is a ring, not a stream.

    A linear read returns the oldest text last and the newest first, which reads
    as a log running backwards -- and a backwards log still looks like a log.
    """
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    _plant_empty_kernel(guest, b"XNU: arm64 root attached; launchd PID 1")
    assert observer.kernel_log() == b"XNU: arm64 root attached; launchd PID 1"


def test_a_message_buffer_that_describes_no_ring_is_refused(
    guest: FakeGuest, tmp_path: Path
) -> None:
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    header = struct.pack("<4I", 0x1234, 0, 0, 0) + struct.pack("<Q", PA_LOG_RING)
    guest.write(PA_MSGBUF, header)
    with pytest.raises(UniPadError) as refused:
        observer.kernel_log()
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE


def test_a_page_table_root_of_zero_is_refused(guest: FakeGuest, tmp_path: Path) -> None:
    """A kernel that has not installed its tables holds zero in ``cpu_ttep``.

    Zero is a *readable* physical page, so walking from it returns whatever the
    first 4 KiB of guest RAM look like -- and if those bytes happen to have bit 0
    set, the walk produces addresses and never raises.  That is the exact shape
    of a wrong answer this module exists to refuse.
    """
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    guest.write(PA_TTEP, struct.pack("<Q", 0))
    with pytest.raises(UniPadError) as refused:
        observer.processes()
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert "zero" in refused.value.message


def test_a_page_table_root_that_is_not_page_aligned_is_refused(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """A translation table is page-aligned; anything else is not a table."""
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    guest.write(PA_TTEP, struct.pack("<Q", TTEP_VALUE | 0x40))
    with pytest.raises(UniPadError) as refused:
        observer.processes()
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert "page-aligned" in refused.value.message


def test_one_process_node_is_read_as_a_single_span(guest: FakeGuest, tmp_path: Path) -> None:
    """A node's four fields come from one read, not four.

    The benefit is not fewer guest reads -- ``_page`` already caches by physical
    page, so four field reads of the same node cost the same guest reads as one
    span, which is what the first draft of this test claimed and what the 对质
    step disproved.  The benefit is that pid, state, name and next-pointer are
    guaranteed to describe the *same* bytes: four separate reads could in
    principle observe a node changing between them and produce a torn row.

    The node straddles a page boundary on purpose, so the span costs two reads
    rather than one -- and still not four.
    """
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    node = 0x4000 - 8
    guest.write(PA_ALLPROC, struct.pack("<Q", node))
    _plant_process(guest, node, 5, "straddler", 0)

    reads: list[tuple[int, int]] = []
    real_read = observer.read

    def counting(vmaddr: int, size: int) -> bytes:
        reads.append((vmaddr, size))
        return real_read(vmaddr, size)

    observer.read = counting  # type: ignore[method-assign]
    try:
        pid, state, name, following = observer._proc_fields(node)
    finally:
        del observer.read

    assert (pid, state, name, following) == (5, 2, "straddler", 0)
    assert len(reads) <= 2, (
        f"one process node took {len(reads)} reads ({reads}); pid, state, name "
        "and next-pointer are one contiguous 1485-byte structure and belong in "
        "a single span"
    )


def test_a_message_buffer_claiming_more_than_the_bound_is_refused(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """``size`` comes out of guest memory, so it is bounded before it is used.

    A header claiming 4 GiB would otherwise ask for a million guest reads and
    the refusal would arrive as a timeout rather than as a reason.
    """
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    header = (
        struct.pack("<4I", 0x1234, 1 << 30, 0, 0) + struct.pack("<Q", PA_LOG_RING)
    )
    guest.write(PA_MSGBUF, struct.pack("<Q", PA_MSGBUF_STRUCT))
    guest.write(PA_MSGBUF_STRUCT, header)
    with pytest.raises(UniPadError) as refused:
        observer.kernel_log()
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert refused.value.details["bound"] == go.MAX_KERNEL_LOG_BYTES
    assert refused.value.details["size"] == 1 << 30


# ---------------------------------------------------------------------------
# the sample
# ---------------------------------------------------------------------------
def test_a_sample_is_stamped_and_carries_no_absolute_path(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """Run id, timestamp, kernel digest and probe version, all four.

    Two samples taken minutes apart from two kernels under two probe versions
    are not comparable, and a sample that does not say which it was cannot be
    evidence for anything (PRD 9.2).
    """
    observer = _observer(guest, tmp_path, run_id="run-observe-1")
    _plant_tables(guest)
    _plant_empty_kernel(guest)

    sample = observer.sample(
        taken_at="2026-10-25T00:00:00+00:00", log_path=tmp_path / "kernel.log"
    )
    document = json.dumps(sample.to_dict(), indent=1)

    assert sample.probe_version == go.PROBE_VERSION
    assert sample.taken_at == "2026-10-25T00:00:00+00:00"
    assert sample.run_id == "run-observe-1"
    assert sample.kernel_sha256 == "0" * 64
    assert sample.kernel_log_bytes == len(b"XNU: up")
    assert sample.processes == ()
    assert (tmp_path / "kernel.log").read_bytes() == b"XNU: up"

    # PRD 9.2: a sample is evidence, and evidence must not carry a user path.
    assert str(tmp_path) not in document
    assert "\\Users\\" not in document


def test_the_observer_never_asks_the_virtual_cpu_for_anything(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """Only ``pmemsave`` is issued.

    The monitor's ``x`` command asks the virtual CPU for its state on every
    read, and under some accelerators that request is never seen -- leaving
    QEMU's main loop, its window and its monitor all waiting.  Everything here
    reads physical memory and translates locally instead, and this is the test
    that keeps it that way.
    """
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    _plant_empty_kernel(guest)
    observer.kernel_log()
    assert {name for name, _ in guest.commands} == {"pmemsave"}


def test_a_missing_scratch_file_is_a_refusal_not_a_traceback(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """QEMU answered but wrote nothing: that is a refusal, not ``FileNotFoundError``.

    The length check below the read was unreachable in exactly this case --
    ``read_bytes()`` raised first, and ``main()`` caught only ``UniPadError``, so
    a transport that answered without writing produced a traceback on stderr and
    *nothing* on stdout.  That breaks the one-JSON-object contract the module
    itself cites, and it is the shape a controller parsing stdout cannot use.
    """
    observer = _observer(guest, tmp_path)

    class Silent:
        def execute(self, command, arguments=None, **_):
            return {"return": {}}

    memory = go.GuestMemory(Silent(), tmp_path / "never-written.bin")
    with pytest.raises(UniPadError) as refused:
        memory.read(0x1000, 4096)
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert "scratch" in refused.value.message


def test_a_second_sample_rereads_the_guests_page_tables(
    guest: FakeGuest, tmp_path: Path
) -> None:
    """The caches are a snapshot, not a property of the observer.

    Measured without this: a second ``sample()`` on one observer issued **zero**
    guest reads, because every page was still cached.  A guest that reloads its
    tables -- a reset, a second kernel -- would then be read through the old
    ones, and both sets of bytes are mapped, so nothing would raise.
    """
    observer = _observer(guest, tmp_path)
    _plant_tables(guest)
    _plant_empty_kernel(guest)

    first = observer.sample(taken_at="2026-10-25T00:00:00+00:00")
    assert first.memory_reads > 0
    assert observer.memory.reads > 0

    observer.sample(taken_at="2026-10-25T00:00:01+00:00")
    assert observer.memory.reads > first.memory_reads, (
        "the second sample read nothing new, so it was served from the first "
        "one's page-table cache"
    )


def test_a_truncated_load_address_is_refused() -> None:
    """``pa 0x4`` is not an address.

    A short hex run matched the pattern and produced a physical base of 4, from
    which ``cpu_ttep`` would be read out of the first page of guest RAM.  The
    guard is a minimum width, because every real load line prints a full 64-bit
    address.
    """
    with pytest.raises(UniPadError) as refused:
        go.load_addresses_from_serial("kernel   va 0x4  pa 0x4\n")
    assert refused.value.code == ErrorCode.EVIDENCE_INCOMPLETE


def test_a_malformed_image_does_not_escape_as_a_traceback(tmp_path: Path) -> None:
    """A truncated load-command table is a refusal with a code.

    Without the bounds check the symbol loop's sibling has, a short file with a
    large ``ncmds`` raised ``struct.error`` -- which ``main()`` did not catch, so
    the caller got a traceback and an empty stdout.
    """
    # 36 bytes: the header is 32, so the first command's 8-byte header does not
    # fit and the walk runs off the end of the file rather than reading a
    # zero-sized command.
    blob = bytearray(36)
    struct.pack_into("<8I", blob, 0, go._MH_MAGIC_64, 0, 0, 0, 2, 0, 0, 0)
    path = tmp_path / "truncated.macho"
    path.write_bytes(bytes(blob))
    with pytest.raises(UniPadError) as refused:
        go.KernelImage(path, expected_sha256=_digest(path))
    assert refused.value.code == ErrorCode.CONFIG_INVALID
    assert "load-command" in refused.value.message
