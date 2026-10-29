"""Read a member out of a payload FAT32 image.

Why this exists
---------------
The payload build packs the material the guest boots into the payload volume
and records each piece's digest in the ``payload.json`` beside the image
(``vendor/UniXNU/scripts/unixnu/payload.py:157,188``):

* ``EFI/UniXNU/Payloads/scratch-data.img`` -- the packed Data seed, whose digest
  is ``data_ramdisk_sha256``;
* ``EFI/UniXNU/Payloads/kernel.macho.gpuaddon`` -- the GPU addon blob, whose
  digest is ``gpu_addon_sha256`` (``payload.py:145``, and the ADT placeholder
  that makes it mandatory is written by ``make-qemu-adt.py:120``).

On Windows the image is never attached as a drive -- the ESP the guest sees is a
*staged directory* behind QEMU's ``fat:`` block driver
(``unipad/host/qemu.py:444``) -- so everything inside the image is invisible to
the guest, and the staged directory is the only delivery channel.  A caller that
stages something else overwrites the build's own, and nothing on the host can see
that it happened.

Measured on 2026-10-30 with ``bundle7``: the image's own seed is ``6fa3a34a…``,
its volume UUID is ``974BCE86-A03D-40CF-A55A-B0F7F1A6734D``, and the kernel
beside it in the same image carries that UUID.  The seed that was staged instead
was ``784a8507…`` with volume UUID ``E3E8AE2D-…`` -- a different build entirely.
The consequence was ``native_owner_data_stage = 0`` / ``status = 2`` (ENOENT): the
owner Data volume never mounted, so the GPU addon mount's only call site was
never entered.  ``bundle8`` carries both pieces, and both digests below were
re-read out of it and matched against its own ``payload.json``.

So the bytes are already on disk, inside the public boot-inputs bundle, and the
only thing missing is a reader.  This is that reader.  It is stdlib-only, like
the rest of ``unipad.host``.

What it refuses to do
---------------------
It does not guess.  A volume that is not FAT32, a member that is not there, a
cluster chain that loops or ends early, and a digest that does not match the
declared one are each a refusal naming what was found -- because every one of
them is a silent wrong-answer otherwise.  The digest check is the one that
matters most: the caller passes ``payload.json``'s digest for the member and a
wrong extraction cannot pass.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from unipad.errors import ErrorCode, UniPadError
from unipad.host.paths import safe_location

#: The members the payload build writes, relative to the volume root, spelled the
#: way ``unixnu/payload.py`` spells them.  Matched case-insensitively and with
#: either separator, because FAT stores the name twice -- once as a long name and
#: once as an 8.3 short name -- and which of the two a volume carries depends on
#: the tool that formatted it.  ``bundle8``'s volume carries both, so either
#: spelling resolves.
DATA_MEMBER = "EFI/UniXNU/Payloads/scratch-data.img"

#: The GPU addon blob.  ``payload.py:145`` copies it beside the kernel and
#: ``make-qemu-adt.py:120`` writes the ``GpuAddon`` placeholder that makes
#: DarwinBoot refuse a boot carrying one without the other (``DarwinBoot.c:596``).
GPU_ADDON_MEMBER = "EFI/UniXNU/Payloads/kernel.macho.gpuaddon"

#: How the payload volume is laid out, measured on ``bundle7``'s ``payload.img``
#: and written down here so a volume that differs is refused rather than
#: misread.  The values are the FAT32 BPB fields, not assumptions about every
#: FAT32 volume: :func:`read_data_seed` reads them and checks them.
BYTES_PER_SECTOR = 512
SECTORS_PER_CLUSTER = 4
RESERVED_SECTORS = 32
FAT_COUNT = 2
SECTORS_PER_FAT = 765
ROOT_CLUSTER = 2
TOTAL_SECTORS = 393216

#: A FAT32 directory entry is 32 bytes; the 8.3 name occupies the first eleven.
DIR_ENTRY_SIZE = 32
SHORT_NAME_BYTES = 11

#: The attributes byte's offset within an entry, and the values that are not a
#: file: a volume label (0x08) and the long-name marker (0x0F).
ATTR_OFFSET = 11
ATTR_VOLUME_LABEL = 0x08
ATTR_LONG_NAME = 0x0F
#: FAT reserves the top two bits of the attributes byte, so a long-name fragment
#: is recognised by its low six bits.
ATTR_LONG_NAME_MASK = 0x3F
#: A directory.
ATTR_DIRECTORY = 0x10

#: A deleted entry's first byte.  Skipped, not followed: the payload build
#: deletes nothing, and a deleted member is not the build's seed.
DELETED_ENTRY = 0xE5

#: The first byte that ends a directory scan.  Everything after it is not a
#: directory any more.
END_OF_DIRECTORY = 0x00

#: The high nibble of a FAT32 cluster entry is reserved, so a next-cluster value
#: is masked with this.
CLUSTER_MASK = 0x0FFFFFFF

#: At or above this a cluster entry means end-of-chain; at exactly this it means
#: the cluster is bad.  Both end the walk.
END_OF_CHAIN = 0x0FFFFFF8
BAD_CLUSTER = 0x0FFFFFF7

#: The packed Data ramdisk's own ceiling (``packed_ramdisk.MAX_PACKED_BYTES``).
#: A seed larger than this is not a seed this project produces.  It is a
#: *documentation* of what the seed is, not a bound the generic member reader
#: applies: :func:`read_member` bounds a member by the volume's own size instead,
#: which is derived rather than assumed and so cannot be wrong for a volume this
#: module has not seen.
MAX_SEED_BYTES = 256 * 1024 * 1024

#: A directory deeper than this is a corrupt FAT, not a payload volume.  The
#: payload build writes exactly three levels (``EFI/UniXNU/Payloads``).
MAX_DEPTH = 8

#: Bytes read at a time when hashing a member.  Large enough that a 1.5 MB seed
#: is one read and a 192 MiB image is not read whole to find a 1.5 MB member.
CHUNK = 1 << 20


@dataclass(frozen=True)
class PayloadMember:
    """A member a payload image carries, and how it was found.

    ``member`` is the path the reader matched, so a report can say which
    spelling the volume used -- the long name, the 8.3 short name, or both.
    """

    #: The member's path inside the volume, as the volume spells it.
    member: str
    #: The member's bytes, exactly as the volume carries them.
    data: bytes
    #: ``sha256`` of :attr:`data`.
    sha256: str
    #: ``len(data)``, which is the size the directory entry declared.
    size: int
    #: The first cluster of the chain that holds it.
    cluster: int


def _refuse(code: ErrorCode, message: str, **details: object) -> UniPadError:
    return UniPadError(code, message, **details)  # type: ignore[arg-type]


def _normalize(name: str) -> str:
    """A member name as a comparison key: upper case, forward slashes, no root."""
    return name.replace("\\", "/").lstrip("/").upper()


def _mangled(name: str) -> str:
    """The 8.3 short name FAT derives for a long name that does not fit.

    ``scratch-data.img`` becomes ``SCRATC~1.IMG``: the first six usable
    characters of the stem, a tilde and a counter.  A volume formatted with
    ``-s`` (short names only) carries *only* this spelling, so a reader that
    matched the long name alone would report the member absent on a volume that
    has it.
    """
    head, _, extension = name.replace("\\", "/").rpartition("/")[2].partition(".")
    stem = "".join(
        character if character.isalnum() else "_" for character in head.upper()
    )[:6]
    tail = extension.upper()[:3]
    return f"{stem}~1.{tail}" if tail else f"{stem}~1"


def _short_name(entry: bytes) -> str:
    """The 8.3 name an entry carries, or ``""`` when it carries none.

    A leading 0x05 is FAT's escape for a name that really begins with 0xE5; the
    payload build writes no such name, so it is reported as absent rather than
    guessed at.
    """
    raw = entry[:SHORT_NAME_BYTES]
    if not raw or raw[0] in (0x00, 0x20):
        return ""
    if raw[0] == 0x05:
        return ""
    stem = raw[:8].decode("ascii", "replace").rstrip()
    extension = raw[8:11].decode("ascii", "replace").rstrip()
    if not stem:
        return ""
    return f"{stem}.{extension}" if extension else stem


def _long_name_part(entry: bytes) -> tuple[int, str]:
    """One long-name fragment: ``(ordinal, characters)``.

    The ordinal is 1-based and comes from the low five bits of the sequence
    byte; bit 0x40 marks the fragment that is stored first and holds the name's
    tail.  Both are read here because a fragment whose ordinal cannot be placed
    is a fragment that would be joined in the wrong order.
    """
    sequence = entry[0]
    ordinal = (sequence & 0x1F) - 1
    characters = entry[1:11] + entry[14:26] + entry[28:32]
    return ordinal, characters.decode("utf-16-le", "replace")


def _join_long_name(fragments: dict[int, str]) -> str:
    """The long name its fragments spell, cut at the first NUL or 0xFFFF pad."""
    text = "".join(fragments[index] for index in sorted(fragments))
    cut = len(text)
    for index in range(len(text)):
        if text[index] == "\0" or text[index] == "￿":
            cut = index
            break
    return text[:cut]


class _Fat32:
    """One FAT32 volume, opened read-only.

    The whole FAT is held in memory because it is small -- 765 sectors of 512
    bytes is 391 KB for the payload volume -- and holding it is what makes a
    cluster chain walk a series of ``struct.unpack_from`` calls rather than a
    seek per cluster.
    """

    def __init__(self, image: Path) -> None:
        self.image = image
        self._handle = image.open("rb")
        try:
            self._read_bpb()
        except Exception:
            self._handle.close()
            raise

    def _read_bpb(self) -> None:
        sector = self._handle.read(BYTES_PER_SECTOR)
        if len(sector) < BYTES_PER_SECTOR:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} is shorter than one sector, so it "
                "is not a FAT32 volume",
                bytes=len(sector),
                sector_bytes=BYTES_PER_SECTOR,
            )
        (self.bytes_per_sector,) = struct.unpack_from("<H", sector, 11)
        self.sectors_per_cluster = sector[13]
        (self.reserved_sectors,) = struct.unpack_from("<H", sector, 14)
        self.fat_count = sector[16]
        (self.root_entries,) = struct.unpack_from("<H", sector, 17)
        (self.total_sectors_16,) = struct.unpack_from("<H", sector, 19)
        (self.sectors_per_fat_16,) = struct.unpack_from("<H", sector, 22)
        (self.total_sectors_32,) = struct.unpack_from("<I", sector, 32)
        (self.sectors_per_fat_32,) = struct.unpack_from("<I", sector, 36)
        (self.root_cluster,) = struct.unpack_from("<I", sector, 44)

        # FAT32, and not FAT12/FAT16: the 16-bit FAT-size and root-entry-count
        # fields are zero exactly when the 32-bit ones are in use.  A FAT16
        # volume would otherwise be walked as FAT32 and produce garbage.
        if self.sectors_per_fat_16 or self.root_entries:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} is not FAT32: the 16-bit FAT size "
                f"is {self.sectors_per_fat_16} and the root holds "
                f"{self.root_entries} entries, which is FAT12/FAT16",
                sectors_per_fat_16=self.sectors_per_fat_16,
                root_entries=self.root_entries,
            )
        if not self.sectors_per_fat_32:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} is not FAT32: it declares no FAT",
                sectors_per_fat_32=self.sectors_per_fat_32,
            )
        if self.bytes_per_sector not in (512, 1024, 2048, 4096):
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} declares {self.bytes_per_sector} "
                "bytes per sector, which FAT does not",
                bytes_per_sector=self.bytes_per_sector,
            )
        if not self.sectors_per_cluster or (
            self.sectors_per_cluster & (self.sectors_per_cluster - 1)
        ):
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} declares {self.sectors_per_cluster} "
                "sectors per cluster, which is not a power of two",
                sectors_per_cluster=self.sectors_per_cluster,
            )
        if self.fat_count not in (1, 2):
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} declares {self.fat_count} FATs, "
                "which FAT does not",
                fat_count=self.fat_count,
            )
        if not self.reserved_sectors:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} declares no reserved sectors, so "
                "the FAT's offset cannot be established",
                reserved_sectors=self.reserved_sectors,
            )
        # FAT32 always uses the 32-bit count; a 16-bit count is FAT12/16, which
        # the check above has already refused.  Read anyway, because it is the
        # field that says how large the volume claims to be and that is what
        # bounds a member's declared size.
        self.total_sectors = self.total_sectors_32 or self.total_sectors_16
        if not self.total_sectors:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} declares no sectors at all, so it "
                "cannot hold a member",
                total_sectors_16=self.total_sectors_16,
                total_sectors_32=self.total_sectors_32,
            )

        self.cluster_bytes = self.bytes_per_sector * self.sectors_per_cluster
        self.fat_offset = self.reserved_sectors * self.bytes_per_sector
        self.fat_bytes = self.sectors_per_fat_32 * self.bytes_per_sector
        self.data_offset = (
            self.reserved_sectors + self.fat_count * self.sectors_per_fat_32
        ) * self.bytes_per_sector

        self._handle.seek(self.fat_offset)
        fat = self._handle.read(self.fat_bytes)
        if len(fat) != self.fat_bytes:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} ends inside its FAT: "
                f"{len(fat)} of {self.fat_bytes} bytes",
                fat_bytes=self.fat_bytes,
                read=len(fat),
            )
        self.fat = fat
        if self.root_cluster * 4 + 4 > len(fat):
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} names root cluster "
                f"{self.root_cluster}, which its FAT does not describe",
                root_cluster=self.root_cluster,
                fat_entries=len(fat) // 4,
            )

    # -- clusters ----------------------------------------------------------

    def next_cluster(self, cluster: int) -> int:
        return struct.unpack_from("<I", self.fat, cluster * 4)[0] & CLUSTER_MASK

    def chain(self, first: int) -> list[int]:
        """Every cluster of the chain starting at ``first``, in order.

        A loop is a refusal rather than a hang: the FAT is attacker-controlled
        data as far as this reader is concerned, and a chain that revisits a
        cluster would otherwise walk until the process was killed.
        """
        clusters: list[int] = []
        seen: set[int] = set()
        current = first
        while END_OF_CHAIN > current >= 2:
            if current in seen:
                raise _refuse(
                    ErrorCode.CONFIG_INVALID,
                    f"{safe_location(self.image)} has a cluster chain that loops "
                    f"at cluster {current}",
                    first_cluster=first,
                    loop_at=current,
                    walked=len(clusters),
                )
            if current * 4 + 4 > len(self.fat):
                raise _refuse(
                    ErrorCode.CONFIG_INVALID,
                    f"{safe_location(self.image)}'s FAT does not describe cluster "
                    f"{current}",
                    cluster=current,
                    fat_entries=len(self.fat) // 4,
                )
            seen.add(current)
            clusters.append(current)
            current = self.next_cluster(current)
        if current == BAD_CLUSTER:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} marks cluster {first} bad",
                first_cluster=first,
            )
        return clusters

    def read_clusters(self, clusters: list[int], size: int) -> bytes:
        """The first ``size`` bytes of ``clusters``, refusing a short chain.

        The declared size comes from the directory entry, so a chain that ends
        early is a volume that disagrees with itself -- which is a refusal, not
        a truncated seed.
        """
        chunks: list[bytes] = []
        remaining = size
        for cluster in clusters:
            if remaining <= 0:
                break
            self._handle.seek(self.data_offset + (cluster - 2) * self.cluster_bytes)
            block = self._handle.read(self.cluster_bytes)
            if len(block) != self.cluster_bytes:
                raise _refuse(
                    ErrorCode.CONFIG_INVALID,
                    f"{safe_location(self.image)} ends inside cluster {cluster}: "
                    f"{len(block)} of {self.cluster_bytes} bytes",
                    cluster=cluster,
                    cluster_bytes=self.cluster_bytes,
                    read=len(block),
                )
            if remaining < len(block):
                block = block[:remaining]
            chunks.append(block)
            remaining -= len(block)
        if remaining:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{safe_location(self.image)} runs out of clusters "
                f"{remaining} bytes before the member's declared size",
                declared=size,
                missing=remaining,
            )
        return b"".join(chunks)

    # -- directories -------------------------------------------------------

    def directory(self, first: int) -> list[tuple[str, str, int, int, int]]:
        """``(long name, short name, first cluster, size, attributes)`` per entry.

        Long-name fragments are collected across the entries that precede the
        8.3 entry they belong to and joined in ordinal order, because the
        fragments are stored back-to-front and joining them in storage order
        spells the name backwards.
        """
        entries: list[tuple[str, str, int, int, int]] = []
        fragments: dict[int, str] = {}
        for cluster in self.chain(first):
            self._handle.seek(self.data_offset + (cluster - 2) * self.cluster_bytes)
            block = self._handle.read(self.cluster_bytes)
            if len(block) != self.cluster_bytes:
                raise _refuse(
                    ErrorCode.CONFIG_INVALID,
                    f"{safe_location(self.image)} ends inside directory cluster "
                    f"{cluster}",
                    cluster=cluster,
                )
            for offset in range(0, len(block), DIR_ENTRY_SIZE):
                entry = block[offset : offset + DIR_ENTRY_SIZE]
                if len(entry) < DIR_ENTRY_SIZE:
                    break
                if entry[0] == END_OF_DIRECTORY:
                    return entries
                if entry[0] == DELETED_ENTRY:
                    fragments = {}
                    continue
                attributes = entry[ATTR_OFFSET]
                # The low six bits, not all eight: FAT reserves the top two, and
                # a formatter that sets them writes an entry that is still a
                # long-name fragment.  Comparing the whole byte would treat it as
                # a file whose name is the fragment's bytes.
                if attributes & ATTR_LONG_NAME_MASK == ATTR_LONG_NAME:
                    ordinal, characters = _long_name_part(entry)
                    if ordinal >= 0:
                        fragments[ordinal] = characters
                    continue
                if attributes & ATTR_VOLUME_LABEL:
                    fragments = {}
                    continue
                long_name = _join_long_name(fragments) if fragments else ""
                fragments = {}
                (high,) = struct.unpack_from("<H", entry, 20)
                (low,) = struct.unpack_from("<H", entry, 26)
                (size,) = struct.unpack_from("<I", entry, 28)
                entries.append(
                    (long_name, _short_name(entry), (high << 16) | low, size, attributes)
                )
        return entries

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "_Fat32":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def read_member(
    image: Path,
    member: str,
    *,
    expected_sha256: Optional[str] = None,
) -> PayloadMember:
    """A member a payload image carries, verified against its own record.

    Args:
        image: The payload FAT32 volume, read-only.  Never written to and never
            mounted.
        member: Which member to read, relative to the volume root -- for example
            :data:`DATA_MEMBER` or :data:`GPU_ADDON_MEMBER`.
        expected_sha256: ``payload.json``'s digest *for that member*, when the
            caller has it.  Passing it is what turns a wrong extraction into a
            refusal instead of a wrong file on the ESP: the bytes are inside the
            image and the image is never what the guest sees, so nothing else
            would notice.

    Returns:
        The member, its digest, and the spelling the volume used.

    Raises:
        UniPadError: ``artifact_missing`` when the image or the member is not
            there; ``config_invalid`` when the volume is not FAT32, its FAT
            disagrees with itself, or the member's chain ends early;
            ``verification_failed`` when the bytes do not hash to
            ``expected_sha256``.
    """
    path = Path(image)
    if not path.is_file():
        raise _refuse(
            ErrorCode.ARTIFACT_MISSING,
            "the payload image is not at the path given",
            image=safe_location(path),
        )
    if not member or not member.strip():
        raise _refuse(
            ErrorCode.CONFIG_INVALID,
            "no member was named, so there is nothing to read out of the image",
            image=safe_location(path),
            member=member,
        )

    wanted = _normalize(member)
    # The member may be spelled three ways: as a long name, as the exact target,
    # and as the 8.3 mangling of it.  A volume formatted with short names only
    # carries the third and nothing else.
    aliases = {wanted, _normalize(_mangled(member))}
    with _Fat32(path) as volume:
        found: Optional[tuple[str, int, int]] = None
        # The root directory has no prefix of its own: its entries are the
        # volume's top level, so "EFI" is found as "EFI" and not as "EFI/EFI".
        pending: list[tuple[str, int]] = [("", volume.root_cluster)]
        # Breadth-first, and each directory is walked at most once: a FAT can
        # name the same cluster from two entries, and walking it twice would
        # report the same member twice.
        visited: set[int] = set()
        depth = 0
        while pending and depth <= MAX_DEPTH:
            following: list[tuple[str, int]] = []
            for prefix, cluster in pending:
                if cluster in visited:
                    continue
                visited.add(cluster)
                for long_name, short_name, first, size, attributes in volume.directory(cluster):
                    spelled = long_name or short_name
                    if not spelled:
                        continue
                    relative = f"{prefix}/{spelled}" if prefix else spelled
                    if attributes & ATTR_VOLUME_LABEL:
                        continue
                    # Three spellings, because a volume formatted with short
                    # names only carries the mangled one and nothing else.
                    if (_normalize(relative) in aliases
                            or _normalize(short_name) in aliases):
                        found = (relative, first, size)
                        break
                    if size == 0 and not (attributes & ATTR_DIRECTORY):
                        # Not a directory and not a file with content.
                        continue
                    if attributes & ATTR_DIRECTORY:
                        following.append((relative, first))
                if found:
                    break
            if found:
                break
            pending = following
            depth += 1

        if found is None:
            raise _refuse(
                ErrorCode.ARTIFACT_MISSING,
                f"the payload image carries no {member}",
                image=safe_location(path),
                member=member,
                volume="FAT32",
                bytes_per_sector=volume.bytes_per_sector,
                sectors_per_cluster=volume.sectors_per_cluster,
            )
        relative, first, size = found
        if size <= 0:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{relative} in the payload image is empty, so there is nothing "
                "to read",
                image=safe_location(path),
                member=relative,
                size=size,
            )
        # Bounded by the volume's own size rather than by a constant: it is
        # derived from the BPB this reader has already validated, so it cannot be
        # wrong for a volume shaped differently from the one measured here.
        ceiling = volume.total_sectors * volume.bytes_per_sector
        if size > ceiling:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                f"{relative} in the payload image declares {size} bytes, more than "
                f"the {ceiling}-byte volume that is supposed to hold it",
                image=safe_location(path),
                member=relative,
                size=size,
                ceiling=ceiling,
            )
        data = volume.read_clusters(volume.chain(first), size)

    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 and digest != expected_sha256.lower():
        raise _refuse(
            ErrorCode.VERIFICATION_FAILED,
            f"{member} inside the payload image does not hash to the digest the "
            f"payload build recorded: {digest} against {expected_sha256.lower()}",
            image=safe_location(path),
            member=relative,
            extracted=digest,
            declared=expected_sha256.lower(),
            size=len(data),
        )
    return PayloadMember(
        member=relative,
        data=data,
        sha256=digest,
        size=len(data),
        cluster=first,
    )


def read_data_seed(
    image: Path,
    *,
    expected_sha256: Optional[str] = None,
    member: str = DATA_MEMBER,
) -> PayloadMember:
    """The Data seed a payload image carries, verified against its own record.

    The seed-specific spelling of :func:`read_member`, kept because the seed is
    the member the boot path needs and its digest has a name of its own
    (``payload.json``'s ``data_ramdisk_sha256``).  It is a thin wrapper, not a
    second implementation: one walk, one set of refusals, one digest check.

    Args:
        image: The payload FAT32 volume, read-only.  Never written to and never
            mounted.
        expected_sha256: ``payload.json``'s ``data_ramdisk_sha256``, when the
            caller has it.
        member: Which member to read.  Defaults to the payload build's own
            spelling of the seed.

    Returns:
        The seed, its digest, and the spelling the volume used.
    """
    return read_member(image, member, expected_sha256=expected_sha256)
