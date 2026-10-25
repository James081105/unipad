"""Read an Apple UDIF disk image (``.dmg``) and write out its raw bytes.

Why this module exists
----------------------
``unipad.host.unixnu_boot`` attaches the OS root volume as a raw virtio-blk
device, and QEMU's ``format=raw`` cannot read a UDIF image.  The OS member of
the pinned IPSW is a UDIF image (``044-76040-038.dmg``), so without this module
there is no way to give the guest the volume it is waiting for -- and the
acceptance gate then reports ``disks.system_image is null``, which reads as "the
image is missing" when the image is present and only in the wrong container.

Measured 2026-10-25: the decoded OS image's APFS volume UUID is
``04769D26-2CA0-4825-B443-E2E5F88300F2``, which is exactly the UUID the kernel
blocks on in ``vfs_mountroot`` (``Waiting on <dict ...><string>04769D26-...``).
So the volume the guest needs is the volume this module produces; what was
missing was the container, not the bytes.

What it does *not* do
---------------------
It does not decode LZFSE.  Python's standard library has no LZFSE, and the
compressed chunks of this image are LZFSE (``bvxn`` LZVN and ``bvx2`` LZFSE v2
blocks).  The parser is complete and the two codecs that need no library -- raw
and zero-fill -- are implemented here, so a caller that supplies an LZFSE
decoder gets the whole image and a caller that does not gets a refusal naming
exactly what is missing.  Shipping a half-decoder that silently produced a
wrong image would be worse than refusing: the GPT CRCs would catch it, but only
after 6 GB of writes.

The format
----------
The trailer is a 512-byte big-endian ``koly`` block at ``filesize - 512``.  It
names an XML plist (``XMLOffset``/``XMLLength``) whose ``resource-fork/blkx``
array holds the chunk tables, each entry's ``Data`` being base64 in the plist
and decoded bytes once parsed.  Each entry is a 204-byte header followed by
``NumberOfBlocks`` 40-byte chunk records; a record's ``sectorNumber`` is
**relative to its entry's** ``SectorNumber``, which is the one offset in this
format that is easy to get wrong -- upstream's own ``extract-target-volume.py``
reads it that way and so does this module.
"""

from __future__ import annotations

import base64
import binascii
import plistlib
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from unipad.errors import ErrorCode, UniPadError
from unipad.host.paths import safe_location

#: ``koly``, the UDIF trailer signature.
TRAILER_SIGNATURE = b"koly"
#: ``mish``, the blkx entry signature.
ENTRY_SIGNATURE = b"mish"
#: Size of the ``koly`` trailer.
TRAILER_SIZE = 512
#: Size of a blkx entry header, before its chunk records.
ENTRY_HEADER_SIZE = 204
#: Size of one chunk record.
CHUNK_SIZE = 40

#: Chunk types, as the ``mish`` records spell them.
CHUNK_TERMINATOR = 0xFFFFFFFF
CHUNK_RAW = 0x00000001
CHUNK_ZERO_FILL = 0x00000002
#: Apple's LZFSE.  There is no standard-library decoder; see the module docstring.
CHUNK_LZFSE = 0x80000007

#: Bytes per sector, as the UDIF records count them.
SECTOR = 512

#: The two CRC32s a decoded image is checked against, both inside the GPT: the
#: header's own CRC at offset 0x10 and the partition-entry array's at 0x58.
GPT_SIGNATURE = b"EFI PART"
GPT_HEADER_CRC_OFFSET = 0x10
GPT_ENTRIES_CRC_OFFSET = 0x58


class LzfseDecoder:
    """A caller-supplied LZFSE decoder, or the refusal that stands in for one.

    ``decode`` takes the compressed bytes of one chunk and the number of bytes
    it expands to, and returns exactly that many bytes.  Anything else -- a short
    result, an exception -- is a refusal, because an image that decoded wrong is
    indistinguishable from one that decoded right until the GPT CRC is checked,
    and by then 6 GB has been written.
    """

    def __init__(self, decode: Optional[Callable[[bytes, int], bytes]] = None) -> None:
        self._decode = decode

    @property
    def available(self) -> bool:
        return self._decode is not None

    def decode(self, compressed: bytes, expected: int) -> bytes:
        if self._decode is None:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "this image's chunks are LZFSE-compressed and no LZFSE decoder "
                "was supplied; Python's standard library has none, so the "
                "decoder has to come from the caller",
                chunk_bytes=len(compressed),
                expected_bytes=expected,
            )
        produced = self._decode(compressed, expected)
        if len(produced) != expected:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the LZFSE decoder produced the wrong number of bytes; the image "
                "would be corrupt",
                expected_bytes=expected,
                produced_bytes=len(produced),
            )
        return produced


@dataclass(frozen=True)
class Chunk:
    """One ``mish`` chunk record, with its sector range already made absolute."""

    type: int
    #: First sector of this chunk in the *image*, not in its entry.
    sector: int
    sector_count: int
    compressed_offset: int
    compressed_length: int

    @property
    def byte_offset(self) -> int:
        return self.sector * SECTOR

    @property
    def byte_length(self) -> int:
        return self.sector_count * SECTOR

    @property
    def is_terminator(self) -> bool:
        return self.type == CHUNK_TERMINATOR


@dataclass(frozen=True)
class Entry:
    """One ``mish`` blkx entry: a header and the chunks it contains."""

    sector: int
    sector_count: int
    chunk_count: int
    chunks: tuple[Chunk, ...]

    @property
    def byte_length(self) -> int:
        return self.sector_count * SECTOR


class UdifImage:
    """A parsed UDIF image: the trailer, the chunk tables, and nothing else.

    The file is *not* held open and no bytes are read until :meth:`write_raw` is
    called, so constructing one of these is cheap even for a 5 GB image.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise UniPadError(
                ErrorCode.IMAGE_NOT_FOUND,
                "the UDIF image does not exist",
                path=safe_location(self.path),
            )
        self.size = self.path.stat().st_size
        if self.size <= TRAILER_SIZE:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the file is too small to hold a UDIF trailer",
                path=safe_location(self.path),
                size_bytes=self.size,
            )
        with self.path.open("rb") as handle:
            handle.seek(self.size - TRAILER_SIZE)
            trailer = handle.read(TRAILER_SIZE)
        if trailer[0:4] != TRAILER_SIGNATURE:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the file does not end in a UDIF 'koly' trailer, so it is not a "
                "UDIF image",
                path=safe_location(self.path),
                signature=trailer[0:4].hex(),
            )
        self.data_fork_offset, self.data_fork_length = struct.unpack_from(">QQ", trailer, 24)
        xml_offset, xml_length = struct.unpack_from(">QQ", trailer, 216)
        if xml_length <= 0 or xml_offset < 0 or xml_offset + xml_length > self.size:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the UDIF trailer names an XML plist outside the file",
                path=safe_location(self.path),
                xml_offset=xml_offset,
                xml_length=xml_length,
            )
        with self.path.open("rb") as handle:
            handle.seek(xml_offset)
            xml = handle.read(xml_length)
        try:
            plist = plistlib.loads(xml)
        except Exception as exc:  # plistlib raises several unrelated types
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the UDIF XML plist does not parse",
                path=safe_location(self.path),
                error=str(exc),
            ) from exc
        fork = plist.get("resource-fork")
        if not isinstance(fork, dict) or not isinstance(fork.get("blkx"), list):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the UDIF plist carries no resource-fork/blkx chunk table",
                path=safe_location(self.path),
            )
        blobs: list[bytes] = []
        for index, item in enumerate(fork["blkx"]):
            # Each entry is a dict whose ``Data`` is the base64 ``<data>``
            # plistlib has already decoded.  Iterating the list yields the
            # *dicts*, and ``len()`` of one is its key count -- five -- which is
            # under the header size and reads as "a blkx entry is shorter than
            # its own header".
            data = item.get("Data") if isinstance(item, dict) else None
            if not isinstance(data, bytes):
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "a blkx entry carries no decoded Data bytes",
                    path=safe_location(self.path),
                    entry_index=index,
                )
            blobs.append(data)
        self.entries = tuple(self._entry(blob) for blob in blobs)
        if not self.entries:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the UDIF chunk table is empty",
                path=safe_location(self.path),
            )
        last = max(entry.sector + entry.sector_count for entry in self.entries)
        self.sector_count = last
        self.raw_size = last * SECTOR

    # -- parsing ---------------------------------------------------------

    @staticmethod
    def _entry(blob: bytes) -> Entry:
        """One blkx entry, from its ``Data`` bytes to its chunk records."""
        if len(blob) < ENTRY_HEADER_SIZE:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a blkx entry is shorter than its own header",
                entry_bytes=len(blob),
            )
        if blob[0:4] != ENTRY_SIGNATURE:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a blkx entry does not start with the 'mish' signature",
                signature=blob[0:4].hex(),
            )
        sector, sector_count = struct.unpack_from(">QQ", blob, 8)
        chunk_count, = struct.unpack_from(">I", blob, 200)
        expected = ENTRY_HEADER_SIZE + chunk_count * CHUNK_SIZE
        if len(blob) != expected:
            # Not a warning: a record read past the end of the blob would be
            # whatever bytes followed it, and the resulting image would be wrong
            # in a way only the GPT CRC can see.
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a blkx entry's length does not match its chunk count",
                entry_bytes=len(blob),
                chunk_count=chunk_count,
                expected_bytes=expected,
            )
        chunks = []
        for at in range(ENTRY_HEADER_SIZE, len(blob), CHUNK_SIZE):
            (
                chunk_type,
                _comment,
                relative_sector,
                chunk_sectors,
                compressed_offset,
                compressed_length,
            ) = struct.unpack_from(">IIQQQQ", blob, at)
            if chunk_type == CHUNK_TERMINATOR:
                continue
            chunks.append(
                Chunk(
                    type=chunk_type,
                    # Relative to the entry, which is the one field in this
                    # format that is easy to read as absolute and then produce a
                    # silently wrong image.
                    sector=sector + relative_sector,
                    sector_count=chunk_sectors,
                    compressed_offset=compressed_offset,
                    compressed_length=compressed_length,
                )
            )
        return Entry(sector=sector, sector_count=sector_count, chunk_count=chunk_count,
                     chunks=tuple(chunks))

    # -- what is inside --------------------------------------------------

    def chunk_types(self) -> dict[int, int]:
        """How many chunks of each type, so a caller can see what it needs."""
        counts: dict[int, int] = {}
        for entry in self.entries:
            for chunk in entry.chunks:
                counts[chunk.type] = counts.get(chunk.type, 0) + 1
        return counts

    def needs_lzfse(self) -> bool:
        return CHUNK_LZFSE in self.chunk_types()

    def chunks(self) -> Iterator[Chunk]:
        for entry in self.entries:
            yield from entry.chunks

    # -- decoding --------------------------------------------------------

    def _read_compressed(self, chunk: Chunk) -> bytes:
        if chunk.compressed_offset < 0 or chunk.compressed_length < 0:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                    "a chunk names a compressed range outside the data fork",
                    compressed_offset=chunk.compressed_offset,
                    compressed_length=chunk.compressed_length,
            )
        if chunk.compressed_offset + chunk.compressed_length > self.data_fork_length:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a chunk's compressed range runs past the end of the data fork",
                compressed_offset=chunk.compressed_offset,
                compressed_length=chunk.compressed_length,
                data_fork_length=self.data_fork_length,
            )
        with self.path.open("rb") as handle:
            handle.seek(self.data_fork_offset + chunk.compressed_offset)
            return handle.read(chunk.compressed_length)

    def chunk_bytes(self, chunk: Chunk, lzfse: LzfseDecoder) -> bytes:
        """The decompressed bytes of one chunk, exactly ``byte_length`` of them."""
        if chunk.type == CHUNK_ZERO_FILL:
            return b"\0" * chunk.byte_length
        if chunk.type == CHUNK_RAW:
            produced = self._read_compressed(chunk)
            if len(produced) != chunk.byte_length:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "a raw chunk is not as long as the sectors it claims",
                    chunk_bytes=len(produced),
                    sector_bytes=chunk.byte_length,
                )
            return produced
        if chunk.type == CHUNK_LZFSE:
            return lzfse.decode(self._read_compressed(chunk), chunk.byte_length)
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a chunk has a compression type this module does not implement",
            chunk_type=hex(chunk.type),
        )

    def write_raw(self, destination: Path, lzfse: LzfseDecoder) -> dict[str, object]:
        """Write the whole image to ``destination`` and verify what was written.

        The file is created with :func:`os.truncate` first, so an image that is
        mostly zero-fill stays sparse on NTFS rather than costing its full size
        on disk.  Chunks are written at their absolute sector offsets, so the
        order they appear in the table does not matter.

        Returns a record of what was written and whether the GPT survived.  The
        GPT check is the point: every chunk is written by offset, so a single
        wrong offset produces an image of exactly the right size with the wrong
        bytes in it, and only the CRC says so.
        """
        destination = Path(destination)
        if destination.exists() and destination.stat().st_size > 0:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "refusing to overwrite an existing image; delete it first",
                path=safe_location(destination),
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as handle:
            handle.truncate(self.raw_size)
            written = 0
            for chunk in self.chunks():
                if chunk.is_terminator:
                    continue
                produced = self.chunk_bytes(chunk, lzfse)
                handle.seek(chunk.byte_offset)
                handle.write(produced)
                written += len(produced)
        return {
            "path": safe_location(destination),
            "size_bytes": self.raw_size,
            "sector_count": self.sector_count,
            "chunks": len(list(self.chunks())),
            "bytes_written": written,
            "gpt": self.check_gpt(destination),
        }

    def check_gpt(self, image: Path) -> Optional[dict[str, object]]:
        """The GPT header and partition-entry CRCs of a decoded image, if any.

        ``None`` when the image has no GPT at sector 1 -- which is a fact about
        the image, not a failure, and a caller that expected one should say so
        rather than have this module guess.
        """
        with Path(image).open("rb") as handle:
            handle.seek(SECTOR)
            header = handle.read(92)
            if len(header) < 92 or header[0:8] != GPT_SIGNATURE:
                return None
            entries_lba, entry_count, entry_size = struct.unpack_from("<QII", header, 72)
            handle.seek(entries_lba * SECTOR)
            entries = handle.read(entry_count * entry_size)
        stored_header, = struct.unpack_from("<I", header, GPT_HEADER_CRC_OFFSET)
        computed_header = binascii.crc32(header[:GPT_HEADER_CRC_OFFSET] + b"\0\0\0\0"
                                        + header[GPT_HEADER_CRC_OFFSET + 4:])
        stored_entries, = struct.unpack_from("<I", header, GPT_ENTRIES_CRC_OFFSET)
        computed_entries = binascii.crc32(entries)
        return {
            "header_crc_ok": stored_header == computed_header,
            "entries_crc_ok": stored_entries == computed_entries,
            "entry_count": entry_count,
            "entries_lba": entries_lba,
        }
