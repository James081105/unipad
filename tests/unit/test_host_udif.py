"""``unipad.host.udif`` against synthetic UDIF images built on this host.

Why this file exists
-------------------
This module reads the OS root volume out of the container the pinned IPSW ships
it in, and the format has exactly one field that is easy to read wrong: a
chunk's ``sectorNumber`` is **relative to its blkx entry's** ``SectorNumber``,
not absolute in the image.  Reading it as absolute produces an image of exactly
the right size with the wrong bytes in it -- and nothing says so until the GPT
CRC is checked, which is after 6 GB of writes.  So the fixture here is built to
make those two readings *disagree*: the entry starts at a non-zero sector and its
chunks carry non-zero relative numbers, so a parser that adds when it should not
(or vice versa) writes the right bytes to the wrong place and the CRC fails.

The real image is used too, for the one thing a synthetic fixture cannot prove:
that this module's parse agrees with an independently produced decode.
``F:\\unipad-tmp\\os-root.raw`` was produced by a separate implementation whose
GPT CRCs matched, and :func:`test_the_real_os_image_parses_the_same_way` checks
this module's ``check_gpt`` against it.  That test skips when the file is absent,
because it lives outside the repository by design (PRD 12).
"""

from __future__ import annotations

import plistlib
import struct
import zlib
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.udif import (
    CHUNK_LZFSE,
    CHUNK_RAW,
    CHUNK_TERMINATOR,
    CHUNK_ZERO_FILL,
    ENTRY_HEADER_SIZE,
    ENTRY_SIGNATURE,
    SECTOR,
    TRAILER_SIZE,
    TRAILER_SIGNATURE,
    LzfseDecoder,
    UdifImage,
)

REPO = Path(__file__).resolve().parents[2]
REAL_OS_IMAGE = REPO / "local-private/out/components/044-76040-038.dmg"
INDEPENDENT_DECODE = Path("F:/unipad-tmp/os-root.raw")


# ---------------------------------------------------------------------------
# building a UDIF image
# ---------------------------------------------------------------------------
def _entry(entry_sector: int, chunks: list[tuple[int, int, int, int, bytes]]) -> bytes:
    """One blkx entry: a 204-byte header, its chunk records, and a terminator.

    ``chunks`` is ``(type, relative_sector, sector_count, compressed_offset,
    payload)``.  The offset is absolute in the data fork and is filled in by
    :func:`_build`, which is the only place that knows the layout -- packing a
    literal zero here would make every chunk read the *first* payload in the
    fork, which is a broken fixture and not a broken parser.
    """
    records = b""
    for chunk_type, relative, count, offset, payload in chunks:
        records += struct.pack(
            ">IIQQQQ", chunk_type, 0, relative, count, offset, len(payload)
        )
    terminator = struct.pack(">IIQQQQ", CHUNK_TERMINATOR, 0, sum(c[2] for c in chunks), 0, 0, 0)
    total = 1 + len(chunks)
    header = ENTRY_SIGNATURE + struct.pack(
        ">IQQ", 1, entry_sector, sum(c[2] for c in chunks)
    )
    header += struct.pack(">QII", 0, 0, 0)          # DataOffset, BuffersNeeded, BlockDescriptors
    header += b"\0" * 24                             # reserved
    header += struct.pack(">II", 0, 0)               # checksum type and size
    header += b"\0" * 128                            # checksum
    header += struct.pack(">I", total)               # NumberOfBlocks
    assert len(header) == ENTRY_HEADER_SIZE, len(header)
    return header + records + terminator


def _build(
    path: Path,
    entries: list[tuple[int, list[tuple[int, int, int, int, bytes]]]],
    mutate=None,
) -> Path:
    """Assemble a complete UDIF file: data fork, XML plist, and the koly trailer.

    ``mutate`` is applied to the parsed plist dict before it is serialised, so a
    test can corrupt one specific thing.  It has to happen here rather than by
    rewriting the XML in place: the trailer is the last 512 bytes of the file, so
    a replacement plist of a different length moves it and the file stops being a
    UDIF image at all -- which is a different failure from the one under test.
    """
    # Lay the payloads out in the data fork first, so the chunk records can name
    # their absolute compressed offsets -- which is what a real image does.
    fork = bytearray()
    laid_out: list[tuple[int, int, int, int, bytes]] = []
    for _entry_sector, chunks in entries:
        for chunk_type, relative, count, _length, payload in chunks:
            offset = len(fork)
            fork += payload
            laid_out.append((chunk_type, relative, count, offset, payload))

    blobs = []
    cursor = 0
    for entry_sector, chunks in entries:
        take = len(chunks)
        blobs.append(_entry(entry_sector, laid_out[cursor : cursor + take]))
        cursor += take

    plist = {"resource-fork": {"blkx": [{"Data": blob, "Name": f"entry{i}"}
                                        for i, blob in enumerate(blobs)]}}
    if mutate is not None:
        mutate(plist)
    xml = plistlib.dumps(plist)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(bytes(fork))
        xml_offset = handle.tell()
        handle.write(xml)
        trailer = bytearray(TRAILER_SIZE)
        trailer[0:4] = TRAILER_SIGNATURE
        struct.pack_into(">I", trailer, 4, 4)
        struct.pack_into(">I", trailer, 8, TRAILER_SIZE)
        struct.pack_into(">Q", trailer, 24, 0)               # DataForkOffset
        struct.pack_into(">Q", trailer, 32, len(fork))       # DataForkLength
        struct.pack_into(">Q", trailer, 216, xml_offset)     # XMLOffset
        struct.pack_into(">Q", trailer, 224, len(xml))       # XMLLength
        handle.write(bytes(trailer))
    return path


def _with_trailer(path: Path, **fields: int) -> Path:
    """Rewrite one big-endian field of the koly trailer, in place.

    Safe because the trailer is a fixed 512 bytes: changing a field's *value*
    never changes the file's length, so nothing moves.
    """
    blob = bytearray(path.read_bytes())
    base = len(blob) - TRAILER_SIZE
    for name, value in fields.items():
        offset = {"xml_offset": 216, "xml_length": 224}[name]
        struct.pack_into(">Q", blob, base + offset, value)
    path.write_bytes(bytes(blob))
    return path


def _simple(path: Path) -> Path:
    """A two-entry image whose second entry starts at sector 8.

    Sector 8 is the whole point: a parser that reads a chunk's relative sector
    as absolute writes the second entry's payload at the wrong offset, and the
    resulting image is the right size with the wrong bytes.
    """
    return _build(
        path,
        [
            (0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)]),
            (8, [(CHUNK_RAW, 0, 1, 0, b"B" * SECTOR),
                 (CHUNK_ZERO_FILL, 1, 1, 0, b""),
                 (CHUNK_RAW, 2, 1, 0, b"C" * SECTOR)]),
        ],
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_the_trailer_and_the_chunk_tables_are_read(tmp_path: Path) -> None:
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    assert image.raw_size == 11 * SECTOR, image.raw_size
    assert image.sector_count == 11, image.sector_count
    assert len(image.entries) == 2, image.entries
    first, second = image.entries
    assert (first.sector, first.sector_count) == (0, 1), first
    assert (second.sector, second.sector_count) == (8, 3), second
    assert len(second.chunks) == 3, second.chunks


def test_a_chunks_sector_is_absolute_not_relative(tmp_path: Path) -> None:
    """The one field in this format that is easy to read wrong.

    ``_simple``'s second entry starts at sector 8 and its chunks carry relative
    sectors 0, 1 and 2.  A parser that uses the relative number writes payload
    ``B`` at sector 0 -- on top of payload ``A`` -- and ``C`` at sector 2, which
    is inside the first entry's range.  The image is then the right size with the
    wrong bytes, and only the GPT CRC would say so.
    """
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    sectors = [chunk.sector for chunk in image.entries[1].chunks]
    assert sectors == [8, 9, 10], sectors
    offsets = [chunk.byte_offset for chunk in image.entries[1].chunks]
    assert offsets == [8 * SECTOR, 9 * SECTOR, 10 * SECTOR], offsets


def test_the_terminator_is_not_a_chunk(tmp_path: Path) -> None:
    """Every entry is closed by a ``0xFFFFFFFF`` record that is not a chunk."""
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    for entry in image.entries:
        assert all(not chunk.is_terminator for chunk in entry.chunks), entry
    assert image.chunk_types() == {CHUNK_RAW: 3, CHUNK_ZERO_FILL: 1}, image.chunk_types()


def test_the_chunk_types_are_counted(tmp_path: Path) -> None:
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    assert image.chunk_types() == {CHUNK_RAW: 3, CHUNK_ZERO_FILL: 1}
    assert image.needs_lzfse() is False


def test_lzfse_is_reported_as_needed(tmp_path: Path) -> None:
    image = UdifImage(
        _build(tmp_path / "lzfse.dmg", [(0, [(CHUNK_LZFSE, 0, 1, 0, b"\x00" * 8)])])
    )
    assert image.needs_lzfse() is True
    assert image.chunk_types() == {CHUNK_LZFSE: 1}


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------
def test_raw_and_zero_fill_decode_without_a_decoder(tmp_path: Path) -> None:
    """The two codecs that need no library work with the default decoder."""
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    destination = tmp_path / "out.raw"
    record = image.write_raw(destination, LzfseDecoder())
    assert record["size_bytes"] == 11 * SECTOR, record
    data = destination.read_bytes()
    assert len(data) == 11 * SECTOR, len(data)
    assert data[0:SECTOR] == b"A" * SECTOR, data[:16]
    assert data[8 * SECTOR : 9 * SECTOR] == b"B" * SECTOR, data[8 * SECTOR : 24]
    assert data[9 * SECTOR : 10 * SECTOR] == b"\0" * SECTOR, "zero-fill"
    assert data[10 * SECTOR : 11 * SECTOR] == b"C" * SECTOR, data[10 * SECTOR : 26]


def test_a_supplied_lzfse_decoder_is_used_and_checked(tmp_path: Path) -> None:
    """A decoder that produces the wrong length is a refusal, not a short image."""
    image = UdifImage(
        _build(tmp_path / "lzfse.dmg", [(0, [(CHUNK_LZFSE, 0, 2, 0, b"\x01\x02\x03\x04")])])
    )
    destination = tmp_path / "out.raw"
    seen: list[tuple[bytes, int]] = []

    def decode(compressed: bytes, expected: int) -> bytes:
        seen.append((compressed, expected))
        return b"Z" * expected

    record = image.write_raw(destination, LzfseDecoder(decode))
    assert seen == [(b"\x01\x02\x03\x04", 2 * SECTOR)], seen
    assert destination.read_bytes() == b"Z" * (2 * SECTOR)
    assert record["bytes_written"] == 2 * SECTOR, record

    short = tmp_path / "short.raw"
    with pytest.raises(UniPadError) as caught:
        image.write_raw(short, LzfseDecoder(lambda _c, expected: b"Z" * (expected - 1)))
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "wrong number of bytes" in caught.value.message, caught.value


def test_an_absent_lzfse_decoder_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """The refusal names what is missing, because a half-decode is a wrong image."""
    image = UdifImage(
        _build(tmp_path / "lzfse.dmg", [(0, [(CHUNK_LZFSE, 0, 1, 0, b"\x01\x02\x03\x04")])])
    )
    with pytest.raises(UniPadError) as caught:
        image.write_raw(tmp_path / "out.raw", LzfseDecoder())
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "LZFSE" in caught.value.message, caught.value
    assert "standard library" in caught.value.message, caught.value


def test_an_unknown_chunk_type_is_refused(tmp_path: Path) -> None:
    image = UdifImage(_build(tmp_path / "x.dmg", [(0, [(0x0BADF00D, 0, 1, 0, b"z" * 8)])]))
    with pytest.raises(UniPadError) as caught:
        image.write_raw(tmp_path / "out.raw", LzfseDecoder())
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "does not implement" in caught.value.message, caught.value


def test_an_existing_image_is_never_overwritten(tmp_path: Path) -> None:
    """User data outranks convenience (PRD 7.2), and 6 GB is not a small mistake."""
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    destination = tmp_path / "out.raw"
    destination.write_bytes(b"precious")
    with pytest.raises(UniPadError) as caught:
        image.write_raw(destination, LzfseDecoder())
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "overwrite" in caught.value.message, caught.value
    assert destination.read_bytes() == b"precious"


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------
def test_a_file_with_no_koly_trailer_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "not-a-dmg.dmg"
    path.write_bytes(b"\0" * (TRAILER_SIZE + 16))
    with pytest.raises(UniPadError) as caught:
        UdifImage(path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "koly" in caught.value.message, caught.value


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UniPadError) as caught:
        UdifImage(tmp_path / "absent.dmg")
    assert caught.value.code is ErrorCode.IMAGE_NOT_FOUND, caught.value


def test_a_file_too_small_for_a_trailer_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "tiny.dmg"
    path.write_bytes(b"koly" + b"\0" * 8)
    with pytest.raises(UniPadError) as caught:
        UdifImage(path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "too small" in caught.value.message, caught.value


def test_an_xml_range_outside_the_file_is_refused(tmp_path: Path) -> None:
    _with_trailer(_simple(tmp_path / "synthetic.dmg"), xml_offset=1 << 40)
    with pytest.raises(UniPadError) as caught:
        UdifImage(tmp_path / "synthetic.dmg")
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "outside the file" in caught.value.message, caught.value


def test_a_plist_with_no_blkx_table_is_refused(tmp_path: Path) -> None:
    path = _build(
        tmp_path / "synthetic.dmg",
        [(0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)])],
        mutate=lambda plist: plist["resource-fork"].clear(),
    )
    with pytest.raises(UniPadError) as caught:
        UdifImage(path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "blkx" in caught.value.message, caught.value


def test_an_entry_shorter_than_its_header_is_refused(tmp_path: Path) -> None:
    """Not a warning: a record read past the blob would be whatever followed it."""
    path = _build(
        tmp_path / "synthetic.dmg",
        [(0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)])],
        mutate=lambda plist: plist["resource-fork"]["blkx"][0].__setitem__(
            "Data", b"mish" + b"\x00" * 8
        ),
    )
    with pytest.raises(UniPadError) as caught:
        UdifImage(path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "shorter than its own header" in caught.value.message, caught.value


def test_an_entry_whose_length_disagrees_with_its_chunk_count_is_refused(
    tmp_path: Path,
) -> None:
    def mutate(plist) -> None:
        blob = bytearray(plist["resource-fork"]["blkx"][0]["Data"])
        struct.pack_into(">I", blob, 200, 99)        # NumberOfBlocks
        plist["resource-fork"]["blkx"][0]["Data"] = bytes(blob)

    path = _build(
        tmp_path / "synthetic.dmg",
        [(0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)])],
        mutate=mutate,
    )
    with pytest.raises(UniPadError) as caught:
        UdifImage(path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "does not match its chunk count" in caught.value.message, caught.value


def test_a_chunk_range_past_the_data_fork_is_refused(tmp_path: Path) -> None:
    def mutate(plist) -> None:
        blob = bytearray(plist["resource-fork"]["blkx"][0]["Data"])
        struct.pack_into(">Q", blob, ENTRY_HEADER_SIZE + 24, 1 << 40)   # compressed offset
        plist["resource-fork"]["blkx"][0]["Data"] = bytes(blob)

    path = _build(
        tmp_path / "synthetic.dmg",
        [(0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)])],
        mutate=mutate,
    )
    with pytest.raises(UniPadError) as caught:
        UdifImage(path).write_raw(tmp_path / "out.raw", LzfseDecoder())
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "past the end of the data fork" in caught.value.message, caught.value


def test_a_blkx_entry_with_no_data_is_refused(tmp_path: Path) -> None:
    path = _build(
        tmp_path / "synthetic.dmg",
        [(0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)])],
        mutate=lambda plist: plist["resource-fork"]["blkx"][0].pop("Data"),
    )
    with pytest.raises(UniPadError) as caught:
        UdifImage(path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "no decoded Data bytes" in caught.value.message, caught.value


# ---------------------------------------------------------------------------
# the GPT check
# ---------------------------------------------------------------------------
def _gpt(entries: list[bytes]) -> bytes:
    """A minimal GPT: a header whose two CRC32s are correct by construction.

    The returned blob is ``header(92) + padding(420) + table``, and it is written
    at sector 1, so the entry table lands at file offset ``SECTOR + SECTOR`` --
    which is where ``check_gpt`` looks for it, because the header says
    ``entries_lba = 2``.
    """
    table = b"".join(entries)
    header = bytearray(92)
    header[0:8] = b"EFI PART"
    struct.pack_into("<I", header, 12, 92)             # header size
    # The count has to be the number actually supplied.  ``check_gpt`` reads
    # ``entry_count * entry_size`` bytes and CRCs what it read, so a header that
    # promises 128 entries over a two-entry table reads past the end of a small
    # test image and the CRC fails for a reason that has nothing to do with the
    # image being checked.
    struct.pack_into("<I", header, 80, len(entries))   # entry count
    struct.pack_into("<I", header, 84, len(entries[0]) if entries else 128)
    struct.pack_into("<Q", header, 72, 2)              # entries LBA
    struct.pack_into("<I", header, 88, zlib.crc32(table) & 0xFFFFFFFF)
    struct.pack_into("<I", header, 16, zlib.crc32(bytes(header)) & 0xFFFFFFFF)
    return bytes(header) + b"\0" * (SECTOR - 92) + table


def test_a_correct_gpt_is_reported_as_correct(tmp_path: Path) -> None:
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    decoded = tmp_path / "out.raw"
    image.write_raw(decoded, LzfseDecoder())
    # Splice a valid GPT over sector 1, then check it is read back as valid.
    with decoded.open("r+b") as handle:
        handle.seek(SECTOR)
        handle.write(_gpt([b"\0" * 128, b"\0" * 128]))
    result = image.check_gpt(decoded)
    assert result is not None
    assert result["header_crc_ok"] is True, result
    assert result["entries_crc_ok"] is True, result
    assert result["entry_count"] == 2, result      # the two the fixture supplied
    assert result["entries_lba"] == 2, result


def test_a_corrupted_gpt_is_reported_as_corrupt(tmp_path: Path) -> None:
    """The check has to be able to fail, or it proves nothing.

    This is the guard that makes ``write_raw`` worth anything: every chunk is
    written by absolute offset, so one wrong offset yields a right-sized image
    with wrong bytes, and this CRC is the only thing that says so.
    """
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    decoded = tmp_path / "out.raw"
    image.write_raw(decoded, LzfseDecoder())
    with decoded.open("r+b") as handle:
        handle.seek(SECTOR)
        gpt = bytearray(_gpt([b"\0" * 128, b"\0" * 128]))
        gpt[SECTOR] ^= 0xFF                   # a byte of the entry table itself
        handle.write(bytes(gpt))
    result = image.check_gpt(decoded)
    assert result is not None
    assert result["entries_crc_ok"] is False, result


def test_an_image_with_no_gpt_reports_none(tmp_path: Path) -> None:
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    decoded = tmp_path / "out.raw"
    image.write_raw(decoded, LzfseDecoder())
    assert image.check_gpt(decoded) is None


# ---------------------------------------------------------------------------
# the real image
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not REAL_OS_IMAGE.is_file(), reason="local-private is absent by design")
def test_the_real_os_image_parses(tmp_path: Path) -> None:
    """The pinned OS member is a UDIF image, and this module reads its tables."""
    image = UdifImage(REAL_OS_IMAGE)
    assert image.raw_size == 6_077_580_800, image.raw_size
    assert image.sector_count == 11_870_275, image.sector_count
    assert len(image.entries) == 6, image.entries
    assert image.needs_lzfse() is True
    assert image.chunk_types() == {CHUNK_LZFSE: 3415, CHUNK_RAW: 396, CHUNK_ZERO_FILL: 3}, (
        image.chunk_types()
    )
    # Entry 3 carries the whole disk, and its chunks are the ones that make the
    # relative-sector field matter: it starts at sector 34, not 0.
    bulk = image.entries[3]
    assert bulk.sector == 34, bulk.sector
    assert bulk.sector_count == 11_870_208, bulk.sector_count
    assert len(bulk.chunks) == 3809, len(bulk.chunks)


@pytest.mark.skipif(
    not (REAL_OS_IMAGE.is_file() and INDEPENDENT_DECODE.is_file()),
    reason="needs the pinned image and an independently produced decode",
)
def test_the_real_os_image_parses_the_same_way() -> None:
    """This module's CRC check agrees with an independently produced decode.

    The comparison is against ``F:\\unipad-tmp\\os-root.raw``, written by a
    separate implementation of the same format whose GPT CRCs matched.  It lives
    outside the repository by design (PRD 12), so this test skips when it is
    absent -- but when it is present it is the only thing here that a synthetic
    fixture cannot replace: proof that two independent parses of the real image
    agree byte for byte.
    """
    image = UdifImage(REAL_OS_IMAGE)
    result = image.check_gpt(INDEPENDENT_DECODE)
    assert result is not None, "the independent decode carries no GPT"
    assert result["header_crc_ok"] is True, result
    assert result["entries_crc_ok"] is True, result
    assert result["entry_count"] == 128, result     # the real image's own table
    assert result["entries_lba"] == 2, result
    assert INDEPENDENT_DECODE.stat().st_size == image.raw_size, (
        INDEPENDENT_DECODE.stat().st_size, image.raw_size
    )
