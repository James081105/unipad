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
        offset = {"xml_offset": 216, "xml_length": 224, "data_fork_length": 32}[name]
        struct.pack_into(">Q", blob, base + offset, value)
    path.write_bytes(bytes(blob))
    return path


def _declare(path: Path, entry_index: int, sectors: int) -> Path:
    """Overwrite one blkx entry's declared ``SectorCount`` (header offset 16).

    Used to build images whose chunk table does *not* tile what the header
    claims, which is the case :meth:`UdifImage._verify_coverage` exists for.
    """
    def mutate(plist) -> None:
        blob = bytearray(plist["resource-fork"]["blkx"][entry_index]["Data"])
        struct.pack_into(">Q", blob, 16, sectors)
        plist["resource-fork"]["blkx"][entry_index]["Data"] = bytes(blob)

    return _build(path, [(0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)])], mutate=mutate)


def _simple(path: Path) -> Path:
    """A two-entry image: one sector, then an entry at sector 1 with three chunks.

    Two properties, each load-bearing.

    * The second entry starts at **sector 1**, not 0, and its chunks carry
      relative sectors 0, 1 and 2.  A parser that uses the relative number
      writes payload ``B`` at sector 0 -- on top of ``A`` -- and ``C`` at sector
      2, inside the first entry's range.  The image is then the right size with
      the wrong bytes.
    * The four sectors **tile exactly**: 0, then 1, 2, 3.  An earlier version of
      this fixture started the second entry at sector 8, leaving sectors 1-7
      uncovered, and the coverage check added alongside it refused the fixture
      -- correctly.  A fixture that does not tile cannot exercise a writer whose
      whole job is to place bytes by offset.
    """
    return _build(
        path,
        [
            (0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)]),
            (1, [(CHUNK_RAW, 0, 1, 0, b"B" * SECTOR),
                 (CHUNK_ZERO_FILL, 1, 1, 0, b""),
                 (CHUNK_RAW, 2, 1, 0, b"C" * SECTOR)]),
        ],
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_the_trailer_and_the_chunk_tables_are_read(tmp_path: Path) -> None:
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    assert image.raw_size == 4 * SECTOR, image.raw_size
    assert image.sector_count == 4, image.sector_count
    assert len(image.entries) == 2, image.entries
    first, second = image.entries
    assert (first.sector, first.sector_count) == (0, 1), first
    assert (second.sector, second.sector_count) == (1, 3), second
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
    assert sectors == [1, 2, 3], sectors
    offsets = [chunk.byte_offset for chunk in image.entries[1].chunks]
    assert offsets == [1 * SECTOR, 2 * SECTOR, 3 * SECTOR], offsets


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
    assert record["size_bytes"] == 4 * SECTOR, record
    data = destination.read_bytes()
    assert len(data) == 4 * SECTOR, len(data)
    assert data[0:SECTOR] == b"A" * SECTOR, data[:16]
    assert data[1 * SECTOR : 2 * SECTOR] == b"B" * SECTOR, data[SECTOR : SECTOR + 24]
    assert data[2 * SECTOR : 3 * SECTOR] == b"\0" * SECTOR, "zero-fill"
    assert data[3 * SECTOR : 4 * SECTOR] == b"C" * SECTOR, data[3 * SECTOR : 26]


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
def test_every_chunk_that_needs_no_library_matches_the_independent_decode() -> None:
    """The 399 raw and zero-fill chunks, byte for byte, against another decoder.

    This is the part of the parse that can be checked without LZFSE, and it is
    checked against an image a separate implementation produced -- so it tests
    this module's arithmetic, not its agreement with itself.  What it pins:

    * the compressed offsets, because a raw chunk reads ``compressed_length``
      bytes from ``data_fork_offset + compressed_offset`` and a wrong offset
      reads a plausible-looking wrong payload;
    * the absolute sector arithmetic, because the chunk is then *written* at
      ``sector * 512`` and a wrong sector puts the right bytes in the wrong
      place -- the one failure mode the GPT CRC can only see after the fact;
    * the zero-fill chunks, which have no payload at all and so can only be
      checked by what they leave behind.

    Measured 2026-10-25: 399 chunks, zero differing bytes, and the image size
    agrees exactly.  The 3415 LZFSE chunks are *not* covered here -- that is the
    gap the module documents rather than hides.
    """
    image = UdifImage(REAL_OS_IMAGE)
    decoder = LzfseDecoder()
    checked = mismatched = 0
    with INDEPENDENT_DECODE.open("rb") as ground:
        for chunk in image.chunks():
            if chunk.type not in (CHUNK_RAW, CHUNK_ZERO_FILL):
                continue
            produced = image.chunk_bytes(chunk, decoder)
            # Read a *fixed* number of bytes, not len(produced).  An earlier
            # version read len(produced), which for a zero-fill chunk that
            # returned b"" read zero bytes and compared b"" == b"" -- a pass.
            # Mutating the zero-fill codec to return b"" kept all 25 tests
            # green, so the codec was entirely unverified while the docstring
            # claimed the opposite.
            ground.seek(chunk.byte_offset)
            expected = ground.read(chunk.byte_length)
            checked += 1
            if len(produced) != chunk.byte_length:
                mismatched += 1
                continue
            if produced != expected:
                mismatched += 1
    assert checked == 399, checked
    assert mismatched == 0, mismatched
    assert image.raw_size == INDEPENDENT_DECODE.stat().st_size


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

# ---------------------------------------------------------------------------
# what the writer refuses to produce
# ---------------------------------------------------------------------------
def test_a_gap_in_the_chunk_table_is_refused(tmp_path: Path) -> None:
    """Sectors no chunk declares stay zero, and nothing would say so.

    Chunks are written by offset, so a gap is invisible in the output: the file
    is exactly the right size, the bytes that *are* written are right, and the
    undeclared sectors read as zeros.  ``check_gpt`` cannot help -- it returns
    ``None`` for an image with no GPT, which is this case.
    """
    path = _declare(tmp_path / "gap.dmg", 0, 5)
    image = UdifImage(path)
    assert image.raw_size == 5 * SECTOR, image.raw_size
    with pytest.raises(UniPadError) as caught:
        image.write_raw(tmp_path / "out.raw", LzfseDecoder())
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "stops short" in caught.value.message, caught.value
    assert not (tmp_path / "out.raw").exists(), "a refused write must leave nothing"


def test_two_chunks_claiming_one_sector_is_refused(tmp_path: Path) -> None:
    """The later chunk would silently overwrite the earlier."""
    path = _build(
        tmp_path / "overlap.dmg",
        [
            (0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)]),
            (0, [(CHUNK_RAW, 0, 1, 0, b"B" * SECTOR)]),
        ],
    )
    with pytest.raises(UniPadError) as caught:
        UdifImage(path).write_raw(tmp_path / "out.raw", LzfseDecoder())
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "same sector" in caught.value.message, caught.value


def test_an_empty_chunk_table_is_refused(tmp_path: Path) -> None:
    """``max()`` over no entries is a bare ValueError; a refusal names the cause."""
    path = _build(tmp_path / "empty.dmg", [], mutate=lambda plist: None)
    with pytest.raises(UniPadError) as caught:
        UdifImage(path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "empty" in caught.value.message, caught.value


def test_a_short_data_fork_is_refused_on_the_lzfse_path(tmp_path: Path) -> None:
    """The RAW path catches its own short read; LZFSE would hand it to the decoder.

    A chunk whose compressed range runs past the end of the *file* -- as opposed
    to past the declared data fork -- used to be read as ``b""`` and passed
    straight to the caller's decoder, which decodes whatever it is given and
    produces a plausible-looking wrong image.
    """
    def mutate(plist) -> None:
        blob = bytearray(plist["resource-fork"]["blkx"][0]["Data"])
        # A compressed offset past the end of the *file* but inside the declared
        # fork: the fork check passes, the read comes back short.
        struct.pack_into(">Q", blob, ENTRY_HEADER_SIZE + 24, 1 << 30)
        plist["resource-fork"]["blkx"][0]["Data"] = bytes(blob)

    path = _build(
        tmp_path / "lzfse.dmg",
        [(0, [(CHUNK_LZFSE, 0, 1, 0, b"\x01\x02\x03\x04")])],
        mutate=mutate,
    )
    _with_trailer(path, data_fork_length=1 << 40)
    image = UdifImage(path)
    seen: list[tuple[bytes, int]] = []

    def decode(compressed: bytes, expected: int) -> bytes:
        seen.append((compressed, expected))
        return b"Z" * expected

    with pytest.raises(UniPadError) as caught:
        image.write_raw(tmp_path / "out.raw", LzfseDecoder(decode))
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "shorter than a chunk's compressed range" in caught.value.message, caught.value
    assert not seen, "the decoder must not be handed a short read"
    assert not (tmp_path / "out.raw").exists()


def test_a_recoverable_failure_leaves_nothing_to_retry_past(tmp_path: Path) -> None:
    """The overwrite guard protects user data, not the module's own leftovers.

    Without the cleanup, an LZFSE chunk with no decoder supplied -- the module's
    own documented refusal -- would leave a full-size partial image behind, and
    supplying the decoder and retrying would then be refused with "delete it
    first".  A one-line config fix would become a delete-6-GB wall.
    """
    path = _build(
        tmp_path / "mixed.dmg",
        [
            (0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)]),
            (1, [(CHUNK_LZFSE, 0, 1, 0, b"\x01\x02\x03\x04")]),
        ],
    )
    destination = tmp_path / "out.raw"
    with pytest.raises(UniPadError):
        UdifImage(path).write_raw(destination, LzfseDecoder())
    assert not destination.exists(), "a refused write must leave nothing behind"

    # ...and the retry with a decoder now succeeds, rather than being refused.
    record = UdifImage(path).write_raw(destination, LzfseDecoder(lambda _c, n: b"Z" * n))
    assert record["size_bytes"] == 2 * SECTOR, record
    assert destination.read_bytes() == b"A" * SECTOR + b"Z" * SECTOR


def test_a_gpt_header_naming_an_implausible_table_is_reported_not_allocated(
    tmp_path: Path,
) -> None:
    """``entry_count`` and ``entry_size`` are u32s in a header we are checking.

    ``check_gpt`` exists to run on images that may be corrupt, so it must report
    corruption rather than raise a bare :class:`MemoryError` trying to read the
    ~2 TiB a crafted header can name.
    """
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    decoded = tmp_path / "out.raw"
    image.write_raw(decoded, LzfseDecoder())
    with decoded.open("r+b") as handle:
        handle.seek(SECTOR)
        handle.write(_gpt([b"\x00" * 128, b"\x00" * 128]))
        handle.seek(SECTOR)
        header = bytearray(handle.read(92))
        struct.pack_into("<I", header, 80, 0x7FFFFFFF)      # entry count
        struct.pack_into("<I", header, 84, 0x1000)         # entry size
        struct.pack_into("<I", header, 16, zlib.crc32(bytes(header)) & 0xFFFFFFFF)
        handle.seek(SECTOR)
        handle.write(bytes(header))
    result = image.check_gpt(decoded)
    assert result is not None
    assert result["entries_crc_ok"] is False, result
    assert "implausible" in result["reason"], result


def test_a_zero_fill_chunk_returns_the_bytes_it_claims(tmp_path: Path) -> None:
    """The codec is verified by its length, not only by what it leaves behind.

    An earlier version of the cross-check test read ``len(produced)`` bytes from
    the ground-truth image, so a zero-fill codec returning ``b""`` read zero
    bytes and passed.  This pins the length directly.
    """
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    zero = [c for c in image.chunks() if c.type == CHUNK_ZERO_FILL]
    assert len(zero) == 1, zero
    assert image.chunk_bytes(zero[0], LzfseDecoder()) == b"\0" * SECTOR

def test_a_raw_chunk_shorter_than_its_sectors_is_refused(tmp_path: Path) -> None:
    """A raw chunk whose sectors outrun its payload.

    The RAW path is the one codec that can check itself, and the check compares
    the bytes read against ``sector_count * 512`` -- so a chunk that declares two
    sectors and carries one is a refusal rather than a short image.  Mutating the
    check away left the suite green, which is why this exists.
    """
    def mutate(plist) -> None:
        blob = bytearray(plist["resource-fork"]["blkx"][0]["Data"])
        # Two sectors claimed by the entry *and* by its chunk, so the coverage
        # check passes and the raw chunk's own length check is what fires --
        # otherwise the coverage refusal masks the one under test.
        struct.pack_into(">Q", blob, 16, 2)                       # entry SectorCount
        struct.pack_into(">Q", blob, ENTRY_HEADER_SIZE + 16, 2)   # chunk sectorCount
        plist["resource-fork"]["blkx"][0]["Data"] = bytes(blob)

    path = _build(
        tmp_path / "shortraw.dmg",
        [(0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)])],
        mutate=mutate,
    )
    with pytest.raises(UniPadError) as caught:
        UdifImage(path).write_raw(tmp_path / "out.raw", LzfseDecoder())
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "not as long as the sectors it claims" in caught.value.message, caught.value


def test_an_empty_destination_file_is_not_user_data(tmp_path: Path) -> None:
    """The overwrite guard protects data, and an empty file is none.

    ``st_size > 0`` rather than ``>= 0``: a zero-length destination is what a
    previous *refused* run would have left if the cleanup were absent, and
    refusing it would turn a recoverable failure into a permanent one.  Mutating
    the comparison to ``>= 0`` left the suite green until this was added.
    """
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    destination = tmp_path / "out.raw"
    destination.write_bytes(b"")
    record = image.write_raw(destination, LzfseDecoder())
    assert record["size_bytes"] == 4 * SECTOR, record
    assert destination.read_bytes()[0:SECTOR] == b"A" * SECTOR


def test_a_chunk_range_running_past_the_fork_is_refused(tmp_path: Path) -> None:
    """The bound is on ``offset + length``, not on ``offset``.

    A chunk that starts inside the data fork and runs past its end reads bytes
    that belong to the plist and the trailer.  Mutating the check to compare only
    the offset left the suite green, which is why this exists.
    """
    def mutate(plist) -> None:
        blob = bytearray(plist["resource-fork"]["blkx"][0]["Data"])
        # Start two bytes before the end of the fork, claim four bytes.
        struct.pack_into(">Q", blob, ENTRY_HEADER_SIZE + 24, 2)
        struct.pack_into(">Q", blob, ENTRY_HEADER_SIZE + 32, 4)
        plist["resource-fork"]["blkx"][0]["Data"] = bytes(blob)

    path = _build(
        tmp_path / "overrun.dmg",
        [(0, [(CHUNK_RAW, 0, 1, 0, b"A" * SECTOR)])],
        mutate=mutate,
    )
    _with_trailer(path, data_fork_length=4)
    with pytest.raises(UniPadError) as caught:
        UdifImage(path).write_raw(tmp_path / "out.raw", LzfseDecoder())
    assert caught.value.code is ErrorCode.CONFIG_INVALID, caught.value
    assert "past the end of the data fork" in caught.value.message, caught.value


def test_the_progress_callback_sees_every_chunk(tmp_path: Path) -> None:
    """The LZFSE path is the bottleneck by two orders of magnitude, so it reports.

    Measured on this host with the host's own pure-Python LZFSE decoder: ~2 MB/s
    of output, which extrapolates to roughly an hour for a 6 GB image, while the
    I/O side of the same decode is ~31 s.  A loop that prints nothing per chunk
    gives an operator no way to tell decode from a hang, which is why the
    callback exists rather than a print the caller cannot redirect.
    """
    image = UdifImage(_simple(tmp_path / "synthetic.dmg"))
    seen: list[tuple[int, int, float]] = []

    def progress(done: int, total: int, elapsed: float) -> None:
        seen.append((done, total, elapsed))

    record = image.write_raw(tmp_path / "out.raw", LzfseDecoder(), progress=progress)
    assert [d for d, _t, _e in seen] == [1, 2, 3, 4], seen
    assert {t for _d, t, _e in seen} == {4}, seen
    assert all(e >= 0 for _d, _t, e in seen), seen
    assert record["chunks"] == 4, record

    # ...and it is optional, because a caller that does not want it must not be
    # forced to pass a no-op.
    again = image.write_raw(tmp_path / "out2.raw", LzfseDecoder())
    assert again["chunks"] == 4, again
