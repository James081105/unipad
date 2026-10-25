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

import binascii
import plistlib
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

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

#: Zero bytes written per call when filling a zero-fill chunk.  One MiB is
#: large enough that the loop is not the bottleneck and small enough that the
#: block stays in cache whatever the chunk's size.
_ZERO_BLOCK = 1 << 20

#: Bounds on a GPT entry table, applied before it is read.  A real GPT has at
#: most a few hundred entries of 128 bytes; anything wildly beyond that is a
#: corrupt header, and reading what it names is a multi-gigabyte allocation
#: inside the one function whose job is to report corruption.
_MAX_GPT_ENTRIES = 1 << 16
_MAX_GPT_ENTRY_SIZE = 1 << 16


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


def default_lzfse() -> LzfseDecoder:
    """The real decoder, so a caller does not have to hand-roll the wiring.

    ``LzfseDecoder`` takes any ``(compressed, expected) -> bytes`` callable, and
    :func:`unipad.host.lzfse.decode` is that callable -- pure stdlib, no wheel,
    because ``pip`` is broken on this host.  A caller that wants its own can
    still build :class:`LzfseDecoder` directly; the default exists so the
    obvious call is the correct one.

    Note what this does *not* change: :class:`LzfseDecoder` built with no
    argument still refuses, because a half-decoder that silently produced a
    wrong image would be worse than a refusal, and because the module must work
    on a host where ``lzfse`` is unavailable.  The import is inside the function
    for the same reason: nothing here makes ``udif`` depend on ``lzfse`` at
    import time.
    """
    from unipad.host import lzfse

    return LzfseDecoder(lzfse.decode)


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

    The file is not held open -- it is opened per read and closed again -- so a
    long-lived instance costs no descriptor.  It does read 512 bytes of trailer
    and the XML plist (234,240 bytes for the pinned image) at construction,
    which is what makes the constructor cheap even for a 5 GB file: no chunk
    payload is touched until :meth:`write_raw` asks for it.
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
        trailer = self._read_at(self.size - TRAILER_SIZE, TRAILER_SIZE)
        if len(trailer) != TRAILER_SIZE or trailer[0:4] != TRAILER_SIGNATURE:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the file does not end in a UDIF 'koly' trailer, so it is not a "
                "UDIF image",
                path=safe_location(self.path),
                signature=trailer[0:4].hex(),
            )
        self.data_fork_offset, self.data_fork_length = struct.unpack_from(">QQ", trailer, 24)
        xml_offset, xml_length = struct.unpack_from(">QQ", trailer, 216)
        if xml_length <= 0 or xml_offset + xml_length > self.size:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the UDIF trailer names an XML plist outside the file",
                path=safe_location(self.path),
                xml_offset=xml_offset,
                xml_length=xml_length,
            )
        xml = self._read_at(xml_offset, xml_length)
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

    def _read_at(self, offset: int, length: int) -> bytes:
        """``length`` bytes at ``offset``, or a refusal naming what went wrong.

        Every read in this module goes through here, and the wrapping is not
        decoration: a bare :class:`OSError` carries the path it was given, which
        on this host means the account name ends up in an error message -- the
        exact leak PRD 9.2 forbids.  ``safe_location`` is what redacts it, and it
        is only reached if the exception is caught and re-raised as a
        :class:`UniPadError`.
        """
        try:
            with self.path.open("rb") as handle:
                handle.seek(offset)
                return handle.read(length)
        except OSError as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the UDIF image could not be read",
                path=safe_location(self.path),
                offset=offset,
                length=length,
                error="%s: %s" % (type(exc).__name__, exc.strerror or exc),
            ) from exc

    def _read_compressed(self, chunk: Chunk) -> bytes:
        if chunk.compressed_offset + chunk.compressed_length > self.data_fork_length:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a chunk's compressed range runs past the end of the data fork",
                compressed_offset=chunk.compressed_offset,
                compressed_length=chunk.compressed_length,
                data_fork_length=self.data_fork_length,
            )
        produced = self._read_at(
            self.data_fork_offset + chunk.compressed_offset, chunk.compressed_length
        )
        # A short read is not a short chunk: it means the file ended where the
        # trailer promised it did not.  The RAW path would be caught by its own
        # length check below, but the LZFSE path hands the result straight to the
        # caller's decoder, which would decode whatever it was given and produce
        # a plausible-looking wrong image.
        if len(produced) != chunk.compressed_length:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the data fork is shorter than a chunk's compressed range",
                compressed_offset=chunk.compressed_offset,
                compressed_length=chunk.compressed_length,
                bytes_read=len(produced),
            )
        return produced

    def chunk_bytes(self, chunk: Chunk, lzfse: LzfseDecoder) -> bytes:
        """The decompressed bytes of one chunk, exactly ``byte_length`` of them."""
        if chunk.type == CHUNK_ZERO_FILL:
            # Materialised here, and streamed in write_raw -- see _write_zero_fill
            # for why this one is allowed to allocate.
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

    def _write_zero_fill(self, handle, count: int) -> None:
        """Write ``count`` zero bytes in bounded blocks.

        A zero-fill chunk is *ordinarily* the whole disk in an Apple UDIF image,
        so ``b"\\0" * count`` is a 6 GB allocation for the common case.  The
        pinned image's largest is 203,313,152 bytes, which is why nothing has
        noticed yet; a whole-disk zero-fill is an ordinary image and this host
        would swap rather than refuse.
        """
        block = b"\0" * min(count, _ZERO_BLOCK)
        remaining = count
        while remaining > 0:
            take = min(remaining, len(block))
            handle.write(block[:take])
            remaining -= take

    def _verify_coverage(self) -> None:
        """Every sector in ``[0, raw_size)`` covered exactly once, or a refusal.

        Chunks are written by absolute offset, so nothing else checks this.  A
        gap leaves zeros the image never declared and an overlap silently lets
        the later chunk win -- both produce an image of exactly the right size
        with the wrong bytes, and ``check_gpt`` cannot help because it returns
        ``None`` for any image without a GPT, which is the corrupt case.
        """
        spans = sorted(
            (chunk.sector, chunk.sector + chunk.sector_count) for chunk in self.chunks()
        )
        if not spans:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the image has no chunks, so nothing can be written",
                path=safe_location(self.path),
            )
        cursor = 0
        for start, end in spans:
            if start > cursor:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "the chunk table leaves a gap: sectors no chunk declares",
                    first_uncovered_sector=cursor,
                    gap_starts_at=start,
                )
            if end <= start:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "a chunk covers no sectors",
                    sector=start,
                    sector_count=end - start,
                )
            if start < cursor:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "two chunks claim the same sector; the later would silently "
                    "overwrite the earlier",
                    overlapping_sector=start,
                    already_covered_through=cursor,
                )
            cursor = end
        if cursor != self.sector_count:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the chunk table stops short of the image it declares",
                covered_sectors=cursor,
                declared_sectors=self.sector_count,
            )

    def write_raw(
        self,
        destination: Path,
        lzfse: LzfseDecoder,
        progress: Optional[Callable[[int, int, float], None]] = None,
    ) -> dict[str, object]:
        """Write the whole image to ``destination`` and verify what was written.

        The file is created at its full size first and every chunk is written at
        its absolute sector offset, so the order the table lists them in does not
        matter.  It is **not** sparse: NTFS only leaves the unwritten ranges
        unallocated when the file is marked sparse with ``FSCTL_SET_SPARSE``,
        which neither ``open("wb")`` nor ``truncate`` requests.  Measured on this
        host: a 256 MiB all-zero-fill image costs 256 MiB of free space.  An
        earlier version of this docstring claimed otherwise, and the claim was
        wrong.

        Returns a record of what was written and whether the GPT survived.  The
        GPT check is the point: every chunk is written by offset, so a single
        wrong offset produces an image of exactly the right size with the wrong
        bytes in it, and only the CRC says so.

        ``progress`` is called as ``(chunks_done, chunks_total, seconds_elapsed)``
        after each chunk, and it exists because the LZFSE path is the
        bottleneck by two orders of magnitude: measured on this host, the
        host's own pure-Python LZFSE decoder runs at ~2 MB/s of output, so a
        6 GB image is roughly an hour of pure CPU while the I/O side of the
        same decode is ~31 s.  A loop that prints nothing per chunk gives an
        operator no way to tell decode from a hang.  It is a callback rather
        than a print so the caller decides where the output goes.

        A failure part-way through leaves **nothing** behind.  The overwrite
        guard runs before the file is created, so without this a recoverable
        refusal -- an LZFSE chunk with no decoder supplied, say -- would leave a
        full-size partial image that every retry then refuses to touch.
        """
        destination = Path(destination)
        if destination.exists() and destination.stat().st_size > 0:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "refusing to overwrite an existing image; delete it first",
                path=safe_location(destination),
            )
        self._verify_coverage()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the destination directory could not be created",
                path=safe_location(destination.parent),
                error="%s: %s" % (type(exc).__name__, exc.strerror or exc),
            ) from exc
        chunks = [chunk for chunk in self.chunks() if not chunk.is_terminator]
        try:
            with destination.open("wb") as handle:
                # Full size first, so a chunk that is shorter than the sectors it
                # claims cannot leave a hole at the end of the file.  With the
                # coverage check above guaranteeing every sector is written, this
                # is a belt-and-braces no-op -- it is *not* what makes the image
                # sparse on NTFS, which it does not do at all.
                handle.truncate(self.raw_size)
                written = 0
                begun = time.monotonic()
                for index, chunk in enumerate(chunks, start=1):
                    if chunk.type == CHUNK_ZERO_FILL:
                        self._write_zero_fill(handle, chunk.byte_length)
                        written += chunk.byte_length
                    else:
                        produced = self.chunk_bytes(chunk, lzfse)
                        handle.seek(chunk.byte_offset)
                        handle.write(produced)
                        written += len(produced)
                    if progress is not None:
                        progress(index, len(chunks), time.monotonic() - begun)
        except BaseException:
            # BaseException, not Exception: a KeyboardInterrupt half-way through
            # a 6 GB write is exactly the case that would otherwise brick the
            # destination.
            try:
                destination.unlink()
            except OSError:
                pass
            raise
        return {
            "path": safe_location(destination),
            "size_bytes": self.raw_size,
            "sector_count": self.sector_count,
            "chunks": len(chunks),
            "bytes_written": written,
            "gpt": self.check_gpt(destination),
        }

    def check_gpt(self, image: Path) -> Optional[dict[str, object]]:
        """The GPT header and partition-entry CRCs of a decoded image, if any.

        ``None`` when the image has no GPT at sector 1 -- which is a fact about
        the image, not a failure, and a caller that expected one should say so
        rather than have this module guess.

        The entry table is bounded before it is read.  ``entry_count`` and
        ``entry_size`` are attacker-controlled u32s in a header this function
        exists to check, and ``entry_count * entry_size`` can name a ~2 TiB read
        -- which on a corrupt image is the case, and which used to raise a bare
        :class:`MemoryError` out of a function whose whole job is to report
        corruption.
        """
        try:
            with Path(image).open("rb") as handle:
                handle.seek(SECTOR)
                header = handle.read(92)
                if len(header) < 92 or header[0:8] != GPT_SIGNATURE:
                    return None
                entries_lba, entry_count, entry_size = struct.unpack_from("<QII", header, 72)
                table_bytes = entry_count * entry_size
                if entry_count > _MAX_GPT_ENTRIES or entry_size > _MAX_GPT_ENTRY_SIZE:
                    return {
                        "header_crc_ok": None,
                        "entries_crc_ok": False,
                        "entry_count": entry_count,
                        "entries_lba": entries_lba,
                        "reason": "the GPT header names an implausible entry table",
                    }
                handle.seek(entries_lba * SECTOR)
                entries = handle.read(table_bytes)
        except OSError as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the decoded image could not be read back",
                path=safe_location(Path(image)),
                error="%s: %s" % (type(exc).__name__, exc.strerror or exc),
            ) from exc
        if len(entries) != table_bytes:
            return {
                "header_crc_ok": None,
                "entries_crc_ok": False,
                "entry_count": entry_count,
                "entries_lba": entries_lba,
                "reason": "the image is shorter than its GPT entry table",
            }
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
