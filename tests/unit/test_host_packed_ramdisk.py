"""``unipad.host.packed_ramdisk`` against synthetic seeds built on this host.

The format is defined twice, in two languages, in two repositories that cannot
see each other: ``DarwinBootPkg/Driver/PackedRamdisk.h`` in the UEFI driver and
``NativeRamBlock/SparseDisk.h`` in the kernel driver.  This module is the third
implementation, and the point of these tests is that it agrees with both -- so
the seeds here are built byte by byte rather than read from a file, and the
constants are pinned against the two headers rather than against this module's
own idea of them.

No real seed is used: one is Apple-derived material that does not belong in the
repository (PRD 12), and ``prepare-owner-data-fixture.py`` needs macOS to make
one anyway.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Iterable

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import packed_ramdisk as pr

ROOT = Path(__file__).resolve().parents[2]

PAGE = pr.PAGE_SIZE

#: The two places the format is defined.
BOOTLOADER_HEADER = (
    ROOT / "vendor/UniXNU/drivers/DarwinBoot/DarwinBootPkg/Driver/PackedRamdisk.h"
)
KERNEL_HEADER = ROOT / "vendor/UniXNU/drivers/NativeRamBlock/SparseDisk.h"


def seed(
    *,
    logical: int = 1 << 30,
    extents: tuple[tuple[int, int], ...] = ((0, 4096), (65536, 8192)),
    magic: bytes = b"DBRDISK1",
    version: int = 1,
    page: int = 4096,
    count: int | None = None,
    trailing: bytes = b"",
) -> bytes:
    """A packed ramdisk, with every field overridable so a rule can be broken."""
    if count is None:
        count = len(extents)
    blob = struct.pack("<8sIIQQ", magic, version, page, logical, count)
    blob += b"".join(struct.pack("<QQ", offset, length) for offset, length in extents)
    blob += bytes(sum(length for _, length in extents))
    return blob + trailing


# ---------------------------------------------------------------------------
# the constants are the two headers' own
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not BOOTLOADER_HEADER.is_file(), reason="vendor/UniXNU is not present"
)
def test_the_constants_are_the_bootloaders_own() -> None:
    """``SIZE_MAX_LOGICAL`` and ``SIZE_256MB`` are what this module enforces.

    A third implementation that quietly used different bounds would accept seeds
    the bootloader refuses, and the only symptom would be a guest that boots and
    finds no Data volume.
    """
    text = BOOTLOADER_HEADER.read_text(encoding="utf-8")
    assert "#define SIZE_MAX_LOGICAL  (1ULL << 40)" in text, text
    assert pr.MAX_LOGICAL_BYTES == 1 << 40, pr.MAX_LOGICAL_BYTES
    # ``SIZE_256MB`` is EDK II's, not this header's, so what is pinned here is
    # the comparison that uses it -- and the numeric ceiling is pinned against
    # the kernel driver's own literal in the next test.
    assert "if ((InputSize > SIZE_256MB)" in text, text
    assert pr.MAX_PACKED_BYTES == 256 * 1024 * 1024, pr.MAX_PACKED_BYTES
    assert "ReadUnaligned32 ((CONST UINT32 *)(Bytes + 12)) != EFI_PAGE_SIZE" in text, text
    assert pr.PAGE_SIZE == 4096, pr.PAGE_SIZE


@pytest.mark.skipif(not KERNEL_HEADER.is_file(), reason="vendor/UniXNU is not present")
def test_the_kernel_driver_agrees_on_the_page_and_the_ceiling() -> None:
    """``ramPackedLayout`` spells the same numbers as literals, not defines.

    Reading them out of the kernel driver is the only way to notice if the two
    ever drift, and they are the *binding* ones: this module refuses what either
    would refuse.
    """
    text = KERNEL_HEADER.read_text(encoding="utf-8")
    assert "ramU32(p+12)!=4096" in text, text
    assert pr.PAGE_SIZE == 4096, pr.PAGE_SIZE
    assert "size>(1ULL<<40)" in text, text
    assert "n>0x10000000" in text, text
    assert "count>65536" in text, text
    assert pr.MAX_PACKED_BYTES == 0x10000000, hex(pr.MAX_PACKED_BYTES)
    assert pr.MAX_EXTENTS == 65536, pr.MAX_EXTENTS


# ---------------------------------------------------------------------------
# a valid seed
# ---------------------------------------------------------------------------
def test_a_valid_seed_describes_itself() -> None:
    """The record is what both parsers see, not what the header claims."""
    blob = seed(logical=1 << 30, extents=((0, 4096), (65536, 8192)))
    found = pr.validate(blob)
    assert found.size == len(blob), found
    assert found.logical == 1 << 30, found
    assert found.resident == 4096 + 8192, found
    assert found.sha256 == hashlib.sha256(blob).hexdigest(), found
    assert [(e.offset, e.length) for e in found.extents] == [(0, 4096), (65536, 8192)]
    assert found.sparse is True, found


def test_a_seed_that_carries_the_whole_disk_is_not_sparse() -> None:
    """``sparse`` is the resident/logical comparison, not a flag in the header."""
    blob = seed(logical=4096, extents=((0, 4096),))
    assert pr.validate(blob).sparse is False


def test_a_hundred_mib_disk_in_four_extents_is_the_shape_that_matters() -> None:
    """The seed this project stages is 1 GiB of disk in four dirty regions.

    Measured 2026-10-26 on the guest: ``_native_ram_block_bytes`` read
    1073741824 and ``_native_ram_block_resident_bytes`` read 36864, so the two
    numbers the driver publishes are the two this record carries.
    """
    blob = seed(
        logical=1 << 30,
        extents=((0, 12288), (65536, 4096), (98304, 8192), (421888, 12288)),
    )
    found = pr.validate(blob)
    assert found.logical == 1 << 30, found
    assert found.resident == 36864, found
    assert len(found.extents) == 4, found
    assert found.size == 32 + 4 * 16 + 36864, found


# ---------------------------------------------------------------------------
# the header
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"magic": b"DBRDISK2"}, "magic"),
        ({"version": 2}, "version"),
        ({"page": 512}, "page size"),
        ({"logical": 0}, "logical size"),
        ({"logical": 4097}, "logical size"),
        ({"logical": (1 << 40) + 4096}, "logical size"),
    ],
)
def test_a_header_either_parser_rejects_is_refused(kwargs: dict, expected: str) -> None:
    """Every one of these is a rule in both implementations, verbatim."""
    with pytest.raises(UniPadError) as caught:
        pr.validate(seed(**kwargs))
    assert caught.value.code is ErrorCode.ARTIFACT_MANIFEST_INVALID, caught.value
    assert expected in caught.value.message, caught.value.message


def test_a_count_that_does_not_fit_the_file_is_refused() -> None:
    """``count > (size - 32) / 16`` is checked before the table is walked.

    A count far past the end of the file would otherwise be read as a table of
    whatever bytes follow, which is how a truncated file turns into a disk with
    random dirty regions instead of an error.
    """
    with pytest.raises(UniPadError) as caught:
        pr.validate(seed(extents=((0, 4096),), count=1000))
    assert "extent count 1000 does not fit" in caught.value.message, caught.value


def test_a_file_larger_than_the_ceiling_is_refused() -> None:
    """256 MiB is the bootloader's own bound, and the kernel driver's too."""
    with pytest.raises(UniPadError) as caught:
        pr.validate(b"\0" * (256 * 1024 * 1024 + 1))
    assert "exceeds the" in caught.value.message, caught.value


def test_a_file_shorter_than_the_header_is_refused() -> None:
    with pytest.raises(UniPadError) as caught:
        pr.validate(b"DBRDISK1")
    assert "shorter than the 32-byte header" in caught.value.message, caught.value


# ---------------------------------------------------------------------------
# the extent table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "extents, expected",
    [
        (((0, 0),), "extent 0 is empty"),
        (((1, 4096),), "extent 0 is not 4096-aligned"),
        (((0, 4097),), "extent 0 is not 4096-aligned"),
        (((4096, 4096), (0, 4096)), "before the previous extent ends"),
        (((1 << 30, 4096),), "runs past the"),
        (((0, 8192), (4096, 4096)), "before the previous extent ends"),
    ],
)
def test_an_extent_either_parser_rejects_is_refused(
    extents: tuple[tuple[int, int], ...], expected: str
) -> None:
    """The table is walked with the same comparisons both implementations use."""
    with pytest.raises(UniPadError) as caught:
        pr.validate(seed(extents=extents))
    assert expected in caught.value.message, caught.value


def test_a_truncated_payload_names_the_extent_that_runs_off_the_end() -> None:
    """The message names the extent, because "invalid seed" is not actionable."""
    blob = seed(extents=((0, 4096),))[:-512]
    with pytest.raises(UniPadError) as caught:
        pr.validate(blob)
    assert "extent 0 needs 4096 bytes but only 3584 remain" in caught.value.message


def test_trailing_bytes_are_refused() -> None:
    """Both parsers end on ``cursor == size``; extra bytes fail it."""
    with pytest.raises(UniPadError) as caught:
        pr.validate(seed(trailing=b"\0" * 4))
    assert "account for" in caught.value.message, caught.value


# ---------------------------------------------------------------------------
# the file path
# ---------------------------------------------------------------------------
def test_an_absent_seed_is_missing_not_invalid(tmp_path: Path) -> None:
    """``artifact_missing`` and ``artifact_manifest_invalid`` are different answers.

    A caller that stages a seed has to be able to tell "I gave the wrong path"
    from "that file is not a seed" without parsing the message.
    """
    with pytest.raises(UniPadError) as caught:
        pr.validate_file(tmp_path / "not-there.dbr")
    assert caught.value.code is ErrorCode.ARTIFACT_MISSING, caught.value


def test_validate_file_reads_it_once_and_names_the_source(tmp_path: Path) -> None:
    path = tmp_path / "seed.dbr"
    blob = seed()
    path.write_bytes(blob)
    found = pr.validate_file(path)
    assert found.sha256 == hashlib.sha256(blob).hexdigest(), found
    with pytest.raises(UniPadError) as caught:
        pr.validate(b"nope" * 12, source=str(path))
    assert caught.value.details.get("source") == str(path), caught.value.details


# ---------------------------------------------------------------------------
# packing
# ---------------------------------------------------------------------------
def test_packing_keeps_only_the_nonzero_pages() -> None:
    """A disk that is mostly zeroes packs to its dirty pages, not its size."""
    disk = bytearray(64 * 1024 * 1024)
    disk[0:4096] = b"\x11" * 4096
    disk[4096:8192] = b"\x22" * 4096
    disk[32 * 1024 * 1024:32 * 1024 * 1024 + 4096] = b"\x33" * 4096
    blob = pr.pack(bytes(disk))
    found = pr.validate(blob)
    assert found.logical == 64 * 1024 * 1024, found
    assert found.resident == 3 * 4096, found
    assert [(e.offset, e.length) for e in found.extents] == [
        (0, 8192), (32 * 1024 * 1024, 4096)
    ], found
    # And it expands back to exactly what went in.
    assert _expand(blob) == bytes(disk)


def test_an_all_zero_disk_packs_to_just_the_header() -> None:
    """The extreme case the format exists for."""
    blob = pr.pack(bytes(64 * 1024 * 1024))
    assert len(blob) == 32, len(blob)
    assert pr.validate(blob).extents == ()


def test_a_fully_used_disk_round_trips() -> None:
    disk = bytes(range(256)) * (1024 * 1024 // 256)
    blob = pr.pack(disk)
    assert pr.validate(blob).resident == len(disk)
    assert _expand(blob) == disk


def test_packing_a_disk_that_is_not_whole_pages_is_refused() -> None:
    with pytest.raises(ValueError, match="multiple of the"):
        pr.pack(b"\0" * 4097)


def test_packing_more_extents_than_the_format_allows_is_refused() -> None:
    """65537 dirty pages is over the ceiling before the extent count is reached.

    Both limits are real, but they are not independent: the smallest possible
    extent is one page, so 65537 of them is already 65537 * 4096 bytes of
    payload -- more than the 256 MiB the file itself may not exceed.  What this
    pins is that such a disk is still refused, and by the bound that actually
    fires; the extent-count check is the belt to that ceiling's braces.
    """
    page = pr.PAGE_SIZE
    disk = bytearray((pr.MAX_EXTENTS + 1) * page)
    for index in range(pr.MAX_EXTENTS + 1):
        disk[index * page] = 1
    with pytest.raises(ValueError, match="over the"):
        pr.pack(bytes(disk))
    # The bounds are not independent: 65536 extents of one page each is already
    # the whole byte ceiling before the extent table is added, so a disk can
    # never reach the extent count without first hitting the file size.
    assert pr.MAX_EXTENTS * page >= pr.MAX_PACKED_BYTES, "the bounds moved"


def test_packing_at_a_page_size_either_parser_rejects_is_refused() -> None:
    """The page size is not a knob: both parsers compare it to a fixed 4096.

    A caller that passed 512 to pack a 512-sector image would get a seed whose
    header says 512, and the bootloader would refuse it as "not a valid seed"
    with nothing in the message about the sector size it was given.
    """
    with pytest.raises(ValueError, match="page size 512 is not 4096"):
        pr.pack(b"\0" * 512, page=512)


def test_packing_past_the_input_ceiling_is_refused() -> None:
    """256 MiB of dirty pages is more than either parser will read."""
    page = pr.PAGE_SIZE
    pages = pr.MAX_PACKED_BYTES // page + 1
    disk = bytearray(pages * page)
    for index in range(pages):
        disk[index * page] = 1
    with pytest.raises(ValueError, match="ceiling"):
        pr.pack(bytes(disk))


def _expand(blob: bytes) -> bytes:
    """Apply a seed's extents to a zeroed image of its logical size."""
    logical, count = struct.unpack_from("<QQ", blob, 16)
    disk = bytearray(logical)
    cursor = 32 + count * 16
    for index in range(count):
        offset, length = struct.unpack_from("<QQ", blob, 32 + index * 16)
        disk[offset:offset + length] = blob[cursor:cursor + length]
        cursor += length
    return bytes(disk)


def image(pages: int, dirty: Iterable[int]) -> bytes:
    """A ``pages``-page disk image with exactly the listed pages nonzero.

    Small on purpose, and the reason is not politeness to the disk: everything
    the streaming packer decides is decided per *page*, so a 16-page image
    exercises every boundary a 1 GiB one does.  A fixture measured in gigabytes
    would prove the same boundaries more slowly and would also put a gigabyte of
    synthetic APFS on a host that does not have the room.
    """
    disk = bytearray(pages * PAGE)
    for index in dirty:
        disk[index * PAGE:(index + 1) * PAGE] = b"\xa5" * PAGE
    return bytes(disk)


# ---------------------------------------------------------------------------
# packing an image that is on disk
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pages, dirty, expected",
    [
        # No run at all: the shape the format exists for, and the one where an
        # off-by-one would invent an extent out of the terminator.
        (8, [], []),
        # One run of one page, in the middle of the disk.
        (8, [3], [(3 * PAGE, PAGE)]),
        # Two runs separated by exactly one zero page: adjacent, and therefore
        # the case where merging them or dropping the page between them is the
        # easiest mistake to make.
        (8, [1, 3], [(PAGE, PAGE), (3 * PAGE, PAGE)]),
        # ... and the same pair with a third page joining the second run.  The
        # zero page between them stays out of both extents: it is real zeroed RAM
        # in the guest, which is the entire point of the format.
        (8, [1, 3, 4], [(PAGE, PAGE), (3 * PAGE, 2 * PAGE)]),
        # A run that starts and ends on the last page.
        (8, [7], [(7 * PAGE, PAGE)]),
        # A run that ends on the last page, and one that starts on the first and
        # ends on the last: the whole disk, so resident == logical.
        (8, [5, 6, 7], [(5 * PAGE, 3 * PAGE)]),
        (8, list(range(8)), [(0, 8 * PAGE)]),
        # Contiguous dirty pages are one extent, not eight.
        (8, [2, 3, 4, 5], [(2 * PAGE, 4 * PAGE)]),
    ],
)
def test_the_streaming_packer_finds_the_boundaries_pack_finds(
    tmp_path: Path, pages: int, dirty: list[int], expected: list[tuple[int, int]]
) -> None:
    """``pack_file`` and ``pack`` are one implementation of "dirty", not two.

    They share :func:`unipad.host.packed_ramdisk._dirty_extents`, and this is
    the test that makes the sharing load-bearing: if the streaming path ever
    grew its own boundary rule, every case here would still pass through
    ``pack`` while the seeds the two produce diverged -- and a seed that unpacks
    to a different disk than the one that was packed is a defect nothing
    downstream can see, because both seeds validate and both boot.
    """
    disk = image(pages, dirty)
    path = tmp_path / "disk.img"
    path.write_bytes(disk)
    blob = pr.pack_file(path)
    assert blob == pr.pack(disk), "the two packers disagree about this image"
    found = pr.validate(blob)
    assert [(e.offset, e.length) for e in found.extents] == expected, found


def test_the_streaming_packer_round_trips(tmp_path: Path) -> None:
    """The seed expands back to the exact image that was packed.

    ``validate`` only says both parsers would accept the file.  It says nothing
    about whether the bytes that come back out are the bytes that went in, which
    is the property the guest depends on: the kernel publishes this disk to APFS,
    and a Data volume that is mostly right is a corrupted one.
    """
    disk = image(64, [0, 1, 40, 63])
    path = tmp_path / "disk.img"
    path.write_bytes(disk)
    blob = pr.pack_file(path)
    found = pr.validate(blob)
    assert found.logical == len(disk), found
    assert found.resident == 4 * PAGE, found
    assert found.sparse is True, found
    assert _expand(blob) == disk


def test_the_streaming_packer_of_a_fully_used_disk_is_not_sparse(tmp_path: Path) -> None:
    """The degenerate case: nothing is dropped, so the seed is the whole disk.

    Worth its own test because it is the one where the extent table and the
    payload are the same size as the image, and a packer that only handled the
    sparse case would be the one that mattered.
    """
    disk = image(8, list(range(8)))
    path = tmp_path / "disk.img"
    path.write_bytes(disk)
    found = pr.validate(pr.pack_file(path))
    assert found.sparse is False, found
    assert found.resident == found.logical, found


def test_a_page_with_one_nonzero_byte_is_stored_whole(tmp_path: Path) -> None:
    """The unit of residency is the page, not the byte.

    A page whose last byte is ``0x01`` is dirty, and all 4096 bytes of it are
    stored -- which is the difference between a seed that unpacks to the disk
    that was packed and one that unpacks to a disk with the page shifted.  It is
    also the failure mode of a zero test written as "does this page contain any
    nonzero byte and if so keep only that byte", or of one written against the
    wrong zero constant.
    """
    disk = bytearray(image(8, []))
    disk[3 * PAGE + PAGE - 1] = 0x01
    path = tmp_path / "disk.img"
    path.write_bytes(disk)

    found = pr.validate(pr.pack_file(path))
    assert [(e.offset, e.length) for e in found.extents] == [(3 * PAGE, PAGE)], found
    assert found.resident == PAGE, found
    assert _expand(pr.pack_file(path)) == bytes(disk)


def test_a_buffer_smaller_than_a_run_gives_the_same_seed(tmp_path: Path) -> None:
    """The read buffer is a read size, not a page size.

    A run that spans several buffer boundaries is still one extent, and a page
    that straddles one is still one page.  Both are the failure mode of a
    streaming packer that splits on the chunk it happened to read rather than on
    the page, and neither is visible in the extent *count* -- only in the bytes.
    """
    disk = image(32, list(range(4, 20)) + [30])
    path = tmp_path / "disk.img"
    path.write_bytes(disk)
    assert pr.pack_file(path, buffer_bytes=3 * PAGE) == pr.pack(disk)
    assert pr.pack_file(path, buffer_bytes=PAGE) == pr.pack(disk)
    # A buffer that is not a whole number of pages is rounded up to one, rather
    # than producing a short page the boundary rule would then misread.
    assert pr.pack_file(path, buffer_bytes=PAGE + 7) == pr.pack(disk)


def test_the_streaming_packer_never_reads_the_image_whole(tmp_path: Path) -> None:
    """The reason :func:`pack_file` exists, stated as something measurable.

    A host that packed a 5.4 GB OS root by reading it whole would need 5.4 GB of
    RAM, so the claim this pins is about the largest read, not about the output:
    every ``read`` the packer issues is bounded by its buffer.  Counted through
    the real file object rather than by mocking one, so a packer that switched to
    ``path.read_bytes()`` would fail here.
    """
    disk = image(64, [0, 1, 63])
    path = tmp_path / "disk.img"
    path.write_bytes(disk)
    largest = 0
    real_open = Path.open

    class Counting:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def read(self, size: int) -> bytes:
            nonlocal largest
            largest = max(largest, size)
            return self._handle.read(size)  # type: ignore[attr-defined]

        # ``pack_file`` opens the image with ``with``, so a proxy that only
        # answers ``read`` is not a usable stand-in for the real handle.
        def __enter__(self) -> "Counting":
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self._handle, name)

    def counting_open(self: Path, *args: object, **kwargs: object) -> Counting:
        return Counting(real_open(self, *args, **kwargs))  # type: ignore[arg-type]

    Path.open = counting_open  # type: ignore[method-assign]
    try:
        pr.pack_file(path, buffer_bytes=16 * PAGE)
    finally:
        Path.open = real_open  # type: ignore[method-assign]
    assert largest <= 16 * PAGE, largest
    assert largest > 0, "nothing was read"


def test_the_streaming_packer_reads_only_the_pages_it_keeps(tmp_path: Path) -> None:
    """Pass two seeks to the extents it found and reads nothing else.

    The second pass is the whole I/O saving for a sparse disk: 1 GiB of volume
    with four dirty regions is four seeks and 36 KB, not 1 GiB read twice.  If a
    future change made pass two re-read the image, this is the test that says so
    -- and the guest would not notice, because the seed would still be correct.
    """
    disk = image(64, [10, 11])
    path = tmp_path / "disk.img"
    path.write_bytes(disk)
    seeks: list[int] = []
    reads: list[int] = []
    real_open = Path.open

    class Recording:
        def __init__(self, handle: object) -> None:
            self._handle = handle

        def seek(self, offset: int) -> object:
            seeks.append(offset)
            return self._handle.seek(offset)  # type: ignore[attr-defined]

        def read(self, size: int) -> bytes:
            reads.append(size)
            return self._handle.read(size)  # type: ignore[attr-defined]

        def __enter__(self) -> "Recording":
            return self

        def __exit__(self, *exc: object) -> None:
            self._handle.close()  # type: ignore[attr-defined]

        def __getattr__(self, name: str) -> object:
            return getattr(self._handle, name)

    def recording_open(self: Path, *args: object, **kwargs: object) -> Recording:
        return Recording(real_open(self, *args, **kwargs))  # type: ignore[arg-type]

    Path.open = recording_open  # type: ignore[method-assign]
    try:
        blob = pr.pack_file(path)
    finally:
        Path.open = real_open  # type: ignore[method-assign]
    # The image once, in pass one, plus the kept pages in pass two -- stated as a
    # total so it holds whatever the buffer size is.  A pass two that re-read the
    # image would put 2 * len(disk) here.
    assert sum(reads) == len(disk) + 2 * PAGE, reads
    # And what pass two reads is the extent, seeked to: not the whole image
    # filtered down afterwards.
    assert seeks[-1] == 10 * PAGE, seeks
    assert reads[-1] == 2 * PAGE, reads
    assert pr.validate(blob).resident == 2 * PAGE


def test_the_streaming_packer_refuses_an_image_that_is_not_whole_pages(tmp_path: Path) -> None:
    """Same refusal, same message, as ``pack``.

    ``pack``'s message names the byte count, and a caller converting the error
    does not get to know which of the two functions produced it -- so the two
    have to be indistinguishable here.
    """
    path = tmp_path / "ragged.img"
    path.write_bytes(b"\0" * (PAGE + 1))
    with pytest.raises(ValueError, match="multiple of the"):
        pr.pack_file(path)


def test_the_streaming_packer_refuses_a_foreign_page_size(tmp_path: Path) -> None:
    """The page size is not a knob on either path."""
    path = tmp_path / "disk.img"
    path.write_bytes(b"\0" * PAGE)
    with pytest.raises(ValueError, match="page size 512 is not 4096"):
        pr.pack_file(path, page=512)


def test_the_streaming_packer_refuses_more_extents_than_the_format_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """65537 extents is refused without walking the rest of the disk.

    The in-memory test above buys this with a real 256 MiB image; this one lowers
    the bound instead, because what is new on the streaming path is the *check*,
    and a second 256 MiB fixture is 256 MiB of disk this host does not have free.
    The bound itself is pinned against both headers by the first two tests.
    """
    monkeypatch.setattr(pr, "MAX_EXTENTS", 3)
    path = tmp_path / "disk.img"
    path.write_bytes(image(16, [0, 2, 4, 6]))
    with pytest.raises(ValueError, match="extents exceeds the 3"):
        pr.pack_file(path)


def test_the_streaming_packer_refuses_a_disk_that_cannot_fit_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 256 MiB seed ceiling, enforced on the streaming path too.

    Same substitution as the extent test, and for the same reason: the ceiling is
    already pinned against ``SparseDisk.h``'s ``n>0x10000000`` and
    ``PackedRamdisk.h``'s ``SIZE_256MB``; what is unproven without this is that
    the streaming path enforces it at all.
    """
    monkeypatch.setattr(pr, "MAX_PACKED_BYTES", 8 * PAGE)
    path = tmp_path / "disk.img"
    path.write_bytes(image(16, list(range(16))))
    with pytest.raises(ValueError, match="over the") as caught:
        pr.pack_file(path)
    assert "ceiling" in str(caught.value), caught.value


def test_an_absent_disk_is_missing_not_a_valueerror(tmp_path: Path) -> None:
    """``artifact_missing`` and ``config_invalid`` are different answers.

    A caller that packs a volume has to be able to tell "I gave the wrong path"
    from "that image cannot be packed" without parsing the message -- the same
    distinction :func:`unipad.host.packed_ramdisk.validate_file` makes for a seed.
    """
    with pytest.raises(UniPadError) as caught:
        pr.pack_file(tmp_path / "not-there.img")
    assert caught.value.code is ErrorCode.ARTIFACT_MISSING, caught.value


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
def test_the_cli_packs_a_disk_and_says_what_it_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The packing step as a repo-level operation, end to end.

    This is the test that makes ``python -m unipad.host.packed_ramdisk`` a
    contract rather than a script: it packs a real file, writes a real seed, and
    prints the record an operator or a workflow step reads.  Every number in the
    summary is taken from :func:`unipad.host.packed_ramdisk.validate`, so a
    summary that disagreed with the file on disk would fail here.
    """
    disk = image(32, [0, 1, 17])
    source = tmp_path / "owner-data.img"
    source.write_bytes(disk)
    out = tmp_path / "nested" / "owner-data-1g.dbr"

    assert pr.main(["--disk", str(source), "--out", str(out)]) == 0

    found = pr.validate(out.read_bytes())
    assert found.logical == len(disk), found
    assert found.resident == 3 * PAGE, found
    printed = capsys.readouterr().out
    assert f"packed data ramdisk: {found.size} bytes, sha256 {found.sha256}" in printed, printed
    assert (
        f"  logical {found.logical} bytes; {len(found.extents)} extents; "
        f"{found.resident} bytes resident" in printed
    ), printed


def test_the_cli_writes_nothing_it_has_not_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate, then write -- in that order.

    The order is the point.  A seed that ``validate`` refuses is a defect in the
    packer, and writing it first would put an unbootable file where the boot step
    looks for a good one: ``stage_esp`` validates what it is given
    (``unixnu_boot.py``), so the failure would surface as a boot that finds no
    Data volume rather than as a pack that failed.
    """
    source = tmp_path / "disk.img"
    source.write_bytes(image(8, [1]))
    out = tmp_path / "seed.dbr"

    def refuse(blob: bytes, *, source: str = "") -> object:
        raise UniPadError(ErrorCode.ARTIFACT_MANIFEST_INVALID, "sabotaged for the test")

    monkeypatch.setattr(pr, "validate", refuse)
    with pytest.raises(UniPadError):
        pr.main(["--disk", str(source), "--out", str(out)])
    assert not out.exists(), "the seed was written before it was validated"


def test_the_cli_converts_a_valueerror_into_a_config_invalid(tmp_path: Path) -> None:
    """``pack``'s bare ``ValueError`` is a library contract; the CLI's is not.

    Kept as a ``ValueError`` in the module because the module's own tests pin it
    and a caller can catch it without importing the error taxonomy.  At the CLI
    boundary it becomes ``config_invalid`` -- not ``artifact_manifest_invalid``,
    which is what a *seed* that fails to parse gets.  The fault is in the input
    image, and retrying can never make a 4097-byte file a whole number of pages:
    a caller that branched on the manifest code and retried would retry forever.
    ``build_argv`` makes the same distinction for an empty root volume.
    """
    source = tmp_path / "ragged.img"
    source.write_bytes(b"\0" * (PAGE + 1))
    out = tmp_path / "seed.dbr"

    with pytest.raises(UniPadError) as caught:
        pr.main(["--disk", str(source), "--out", str(out)])
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "multiple of the" in caught.value.message, caught.value.message
    assert not out.exists(), "a refused pack still wrote a seed"


def test_the_cli_names_a_missing_disk_as_missing(tmp_path: Path) -> None:
    """A wrong path is not a usage error, and the two exit differently.

    ``config_invalid`` exits 2; ``artifact_missing`` exits 1.  A caller that
    retries a missing file once the build finishes is a caller that has to be
    able to tell the two apart.
    """
    with pytest.raises(UniPadError) as caught:
        pr.main(["--disk", str(tmp_path / "absent.img"), "--out", str(tmp_path / "seed.dbr")])
    assert caught.value.code is ErrorCode.ARTIFACT_MISSING, caught.value
    assert int(caught.value.exit_code) == 1, caught.value.exit_code


def test_the_cli_help_names_both_paths(capsys: pytest.CaptureFixture) -> None:
    """``--help`` is the discoverability of the command, and it is free."""
    with pytest.raises(SystemExit) as caught:
        pr.main(["--help"])
    assert caught.value.code == 0, caught.value
    printed = capsys.readouterr().out
    assert "--disk" in printed, printed
    assert "--out" in printed, printed


def test_the_cli_requires_both_paths() -> None:
    """A default would write a seed somewhere nobody asked for."""
    with pytest.raises(SystemExit) as caught:
        pr.main(["--disk", "x"])
    assert caught.value.code == 2, caught.value
