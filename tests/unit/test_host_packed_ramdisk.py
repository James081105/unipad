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

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import packed_ramdisk as pr

ROOT = Path(__file__).resolve().parents[2]

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
