"""Reading the Data seed out of a payload FAT32 image.

Why these tests exist
---------------------
On Windows the payload image is never attached as a drive -- the ESP is a staged
directory behind QEMU's ``fat:`` block driver -- so the seed inside the image is
invisible to the guest and the staged directory is the only delivery channel.
Measured on 2026-10-30 with ``bundle7``: the image carried ``6fa3a34a…`` whose
volume UUID ``974BCE86-…`` is the one its own kernel was built for, and what got
staged was ``784a8507…`` with ``E3E8AE2D-…``.  The guest logged
``native_owner_data_stage = 0`` / ``status = 2`` (ENOENT), so the owner Data
volume never mounted and the GPU addon mount was never entered.

So the reader has to be right, and a reader that is right about the happy path
only is not right.  Every test below builds the volume it reads: a FAT32 volume
is a BPB, a FAT, and a cluster chain, and writing one here is what lets the
tests corrupt it in the specific ways a corrupt volume corrupts -- a non-FAT32
header, a chain that loops, a chain that ends early, a member that is not there,
and a digest that does not match.  A real ``payload.img`` is 192 MiB of
Apple-derived build output and does not belong in the repository (PRD 12).
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import payload_seed as ps
from unipad.host.packed_ramdisk import pack

#: The geometry of ``bundle7``'s payload volume, measured rather than assumed.
BYTES_PER_SECTOR = 512
SECTORS_PER_CLUSTER = 4
RESERVED_SECTORS = 32
FAT_COUNT = 2
SECTORS_PER_FAT = 8
CLUSTER_BYTES = BYTES_PER_SECTOR * SECTORS_PER_CLUSTER

#: The packed Data ramdisk the volume carries.  One page, because ``pack()`` is
#: the same function the payload build's counterpart uses -- a hand-written
#: ``DBRDISK1`` blob would satisfy the format without being one, and PRD 12 keeps
#: a real Apple-derived seed out of the repository.
SEED_DISK = b"APSB" + b"\0" * 60 + bytes(range(0x97, 0x97)) + b"\x97\x4b\xce\x86\xa0\x3d\x40\xcf\xa5\x5a\xb0\xf7\xf1\xa6\x73\x4d" + b"\0" * (4096 - 80)
SEED = pack(SEED_DISK)
SEED_SHA256 = hashlib.sha256(SEED).hexdigest()

#: The clusters the fixture uses, in the order ``unixnu/payload.py:157`` writes
#: them: three directories, then the seed.
ROOT_CLUSTER = 2
EFI_CLUSTER = 3
UNIXNU_CLUSTER = 4
PAYLOADS_CLUSTER = 5
SEED_CLUSTER = 6
#: The packed seed is 4,144 bytes, which is three 2,048-byte clusters -- so the
#: chain is three long, and a reader that stops at the first cluster returns a
#: seed the format check then rejects.
SEED_CLUSTERS = (SEED_CLUSTER, SEED_CLUSTER + 1, SEED_CLUSTER + 2)
LAST_CLUSTER = SEED_CLUSTER + 2
#: The data region: eight clusters, one per directory plus the seed's three.
DATA_CLUSTERS = 8


# ---------------------------------------------------------------------------
# building a FAT32 volume
# ---------------------------------------------------------------------------

def _short(name: str, *, stem: str = "") -> bytes:
    """An 8.3 directory entry's name field: ``NAME    EXT``.

    ``stem`` overrides the eight characters before the extension, which is how a
    mangled name (``SCRATC~1.IMG``) is written -- FAT generates those when a name
    does not fit, and the reader has to match on whatever is actually there.
    """
    head, _, extension = name.partition(".")
    text = (stem or head).upper()[:8].ljust(8)
    return text.encode("ascii") + extension.upper()[:3].ljust(3).encode("ascii")


def _lfn_checksum(short_name: bytes) -> int:
    """The checksum every long-name entry carries, over the 8.3 name.

    Written even though the reader does not check it, because a fixture that
    skips a field the format defines is a fixture that cannot be trusted to
    catch a reader that reads that field wrong.
    """
    total = 0
    for character in short_name:
        total = (((total & 1) << 7) + (total >> 1) + character) & 0xFF
    return total


def _long_name_fragments(name: str, short_name: bytes) -> list[bytes]:
    """The long-name entries that spell ``name``, stored back-to-front.

    FAT stores them in reverse ordinal order with bit 0x40 on the first one
    stored, so a reader that joins them in storage order spells the name
    backwards -- which is the bug this fixture exists to catch.
    """
    encoded = name.encode("utf-16-le") + b"\0\0"
    while len(encoded) % 26:
        encoded += b"\xff\xff"
    pieces = [encoded[index:index + 26] for index in range(0, len(encoded), 26)]
    entries = []
    for index, piece in enumerate(pieces):
        sequence = index + 1
        if index == len(pieces) - 1:
            sequence |= 0x40
        entry = bytearray(32)
        entry[0] = sequence
        entry[1:11] = piece[0:10]          # characters 1-5
        entry[11] = 0x0F                   # ATTR_LONG_NAME
        entry[12] = 0                      # type
        entry[13] = _lfn_checksum(short_name)
        entry[14:26] = piece[10:22]        # characters 6-11
        entry[26:28] = b"\0\0"             # cluster, always zero
        entry[28:32] = piece[22:26]        # characters 12-13
        entries.append(bytes(entry))
    return entries


def _entry(name: str, cluster: int, size: int, *, long_name: bool = True,
           directory: bool = False, stem: str = "") -> bytes:
    """One 8.3 entry, preceded by the long-name entries that spell it.

    The field offsets are written out rather than packed in one expression,
    because the entry has eleven timestamp fields around the two that matter and
    an off-by-one anywhere shifts the cluster or the size -- which is a fixture
    bug that reads as a reader bug.
    """
    short_name = _short(name, stem=stem)
    fragments = list(reversed(_long_name_fragments(name, short_name))) if long_name else []

    entry = bytearray(32)
    entry[0:11] = short_name
    entry[11] = 0x10 if directory else 0x20        # attributes
    entry[12] = 0                                  # NT reserved
    entry[13] = 0                                  # creation time, tenths
    struct.pack_into("<H", entry, 14, 0)           # creation time
    struct.pack_into("<H", entry, 16, 0)           # creation date
    struct.pack_into("<H", entry, 18, 0)           # last access date
    struct.pack_into("<H", entry, 20, cluster >> 16)   # first cluster, high
    struct.pack_into("<H", entry, 22, 0)           # write time
    struct.pack_into("<H", entry, 24, 0)           # write date
    struct.pack_into("<H", entry, 26, cluster & 0xFFFF)  # first cluster, low
    struct.pack_into("<I", entry, 28, size)
    fragments.append(bytes(entry))
    return b"".join(fragments)


def _volume(directories: dict[int, list[bytes]], *, fat32: bool = True,
            root_entries: int = 0, fat_size_16: int = 0,
            seed_clusters: tuple[int, ...] = SEED_CLUSTERS,
            seed_size: int = len(SEED)) -> bytes:
    """A FAT32 volume holding the payload build's own directory tree.

    Two FATs, and a data region of eight clusters: the root, ``EFI``,
    ``UniXNU`` and ``Payloads`` each take one, and the seed takes the rest.  The
    directory entries are rebuilt from the arguments so a test can corrupt one
    field -- a size, a short name, a chain -- without hand-patching bytes.
    """
    total_sectors = RESERVED_SECTORS + FAT_COUNT * SECTORS_PER_FAT + DATA_CLUSTERS * SECTORS_PER_CLUSTER
    boot = bytearray(BYTES_PER_SECTOR)
    struct.pack_into("<H", boot, 11, BYTES_PER_SECTOR)
    boot[13] = SECTORS_PER_CLUSTER
    struct.pack_into("<H", boot, 14, RESERVED_SECTORS)
    boot[16] = FAT_COUNT
    struct.pack_into("<H", boot, 17, root_entries)
    struct.pack_into("<H", boot, 22, fat_size_16)
    struct.pack_into("<I", boot, 32, total_sectors)
    struct.pack_into("<I", boot, 36, SECTORS_PER_FAT if fat32 else 0)
    struct.pack_into("<I", boot, 44, ROOT_CLUSTER)
    boot[82:90] = b"FAT32   "

    fat = bytearray(SECTORS_PER_FAT * BYTES_PER_SECTOR)
    struct.pack_into("<I", fat, 0, 0x0FFFFFF8)      # cluster 0: media + EOC
    struct.pack_into("<I", fat, 4, 0x0FFFFFFF)      # cluster 1: reserved
    for index, cluster in enumerate(seed_clusters):
        following = seed_clusters[index + 1] if index + 1 < len(seed_clusters) else 0x0FFFFFF8
        struct.pack_into("<I", fat, cluster * 4, following)

    data = bytearray(DATA_CLUSTERS * CLUSTER_BYTES)
    for cluster, entries in directories.items():
        # Cluster N lives at data offset (N - 2) * CLUSTER_BYTES, so each
        # directory occupies its own cluster rather than the root's.
        at = (cluster - 2) * CLUSTER_BYTES
        for entry in entries:
            data[at:at + len(entry)] = entry
            at += len(entry)
    seed_at = (seed_clusters[0] - 2) * CLUSTER_BYTES
    data[seed_at:seed_at + seed_size] = SEED[:seed_size]

    volume = bytearray(total_sectors * BYTES_PER_SECTOR)
    volume[0:BYTES_PER_SECTOR] = boot
    fat_at = RESERVED_SECTORS * BYTES_PER_SECTOR
    volume[fat_at:fat_at + len(fat)] = fat
    volume[fat_at + len(fat):fat_at + 2 * len(fat)] = fat       # the second FAT
    data_at = (RESERVED_SECTORS + FAT_COUNT * SECTORS_PER_FAT) * BYTES_PER_SECTOR
    volume[data_at:data_at + len(data)] = data
    return bytes(volume)


def _tree(*, long_name: bool = True, stem: str = "", seed_size: int = len(SEED)
           ) -> dict[int, list[bytes]]:
    """The payload build's own tree, one directory per cluster."""
    return {
        ROOT_CLUSTER: [_entry("EFI", EFI_CLUSTER, 0, long_name=long_name, directory=True)],
        EFI_CLUSTER: [_entry("UniXNU", UNIXNU_CLUSTER, 0, long_name=long_name, directory=True)],
        UNIXNU_CLUSTER: [_entry("Payloads", PAYLOADS_CLUSTER, 0, long_name=long_name, directory=True)],
        PAYLOADS_CLUSTER: [_entry("scratch-data.img", SEED_CLUSTER, seed_size,
                                  long_name=long_name, stem=stem)],
    }


def _write(tmp_path: Path, volume: bytes, name: str = "payload.img") -> Path:
    path = tmp_path / name
    path.write_bytes(volume)
    return path


#: What :func:`_tree` understands, so :func:`_payload_volume` can split its
#: keyword arguments between the directory entries and the volume geometry.
_TREE_KEYS = ("long_name", "stem", "seed_size")


def _payload_volume(tmp_path: Path, *, tree: dict | None = None, **kwargs) -> Path:
    """A volume carrying the seed at the payload build's own member path.

    ``tree`` overrides the directory entries; everything else goes to
    :func:`_volume`, which owns the BPB and the FAT.
    """
    entries = tree if tree is not None else _tree(**{
        key: value for key, value in kwargs.items() if key in _TREE_KEYS
    })
    return _write(tmp_path, _volume(entries, **{
        key: value for key, value in kwargs.items() if key not in _TREE_KEYS
    }))


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------

def test_it_reads_the_seed_the_volume_carries(tmp_path: Path) -> None:
    """Bytes, digest and the spelling the volume used, all three."""
    image = _payload_volume(tmp_path)
    seed = ps.read_data_seed(image)
    assert seed.data == SEED, "the seed read is not the seed the volume carries"
    assert seed.sha256 == SEED_SHA256
    assert seed.size == len(SEED)
    assert seed.member.endswith("scratch-data.img"), seed.member
    assert seed.path == ps.DATA_MEMBER


def test_it_reads_a_volume_that_spells_only_the_short_name(tmp_path: Path) -> None:
    """A volume formatted without long names is read by its 8.3 entry.

    ``mtools`` writes both, but the reader must not depend on which one it gets:
    a volume carrying only ``SCRATC~1.IMG`` has the same seed in it.
    """
    seed = ps.read_data_seed(_payload_volume(tmp_path, long_name=False, stem="SCRATC~1"))
    assert seed.data == SEED
    assert seed.sha256 == SEED_SHA256


def test_the_declared_digest_is_the_free_check(tmp_path: Path) -> None:
    """Passing ``payload.json``'s digest turns a wrong extraction into a refusal.

    This is the whole reason the caller passes it: the seed inside the image is
    invisible to the guest, so a reader that returned the wrong bytes would
    otherwise stage a wrong seed with nothing to notice.
    """
    image = _payload_volume(tmp_path)
    assert ps.read_data_seed(image, expected_sha256=SEED_SHA256).sha256 == SEED_SHA256
    # Upper case, because a digest may be spelled either way and neither is wrong.
    assert ps.read_data_seed(image, expected_sha256=SEED_SHA256.upper()).sha256 == SEED_SHA256


# ---------------------------------------------------------------------------
# the refusals
# ---------------------------------------------------------------------------

def test_a_volume_that_is_not_fat32_is_refused(tmp_path: Path) -> None:
    """FAT16's own fields, read as FAT32, would walk garbage.

    The 16-bit FAT-size and root-entry-count fields are zero exactly when the
    32-bit ones are in use, so a FAT16 volume is refused by those two rather
    than by guessing at the geometry.
    """
    image = _payload_volume(tmp_path, fat_size_16=9)
    error = _refused(ps.read_data_seed, image)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "not FAT32" in error.message, error
    assert error.details["sectors_per_fat_16"] == 9, error.details


def test_a_volume_with_a_fixed_root_directory_is_refused(tmp_path: Path) -> None:
    """The other FAT12/16 marker: a root directory of a fixed entry count."""
    image = _payload_volume(tmp_path, root_entries=512)
    error = _refused(ps.read_data_seed, image)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "not FAT32" in error.message, error


def test_an_image_that_is_not_there_is_refused(tmp_path: Path) -> None:
    error = _refused(ps.read_data_seed, tmp_path / "absent.img")
    assert error.code is ErrorCode.ARTIFACT_MISSING, error


def test_a_volume_that_carries_no_seed_is_refused(tmp_path: Path) -> None:
    """A payload built without a Data ramdisk has no member to read.

    ``unixnu/payload.py:156`` copies the seed only ``if data``, so a payload
    built without one is a volume with no ``scratch-data.img`` -- and a reader
    that returned empty bytes instead would stage a seed that mounts nothing.
    """
    entries = {ROOT_CLUSTER: [_entry("EFI", EFI_CLUSTER, 0, directory=True)]}
    image = _write(tmp_path, _volume(entries))
    error = _refused(ps.read_data_seed, image)
    assert error.code is ErrorCode.ARTIFACT_MISSING, error
    assert "carries no" in error.message, error
    assert error.details["member"] == ps.DATA_MEMBER, error.details


def test_a_seed_that_does_not_hash_to_the_declared_digest_is_refused(tmp_path: Path) -> None:
    """The refusal that makes the extraction trustworthy.

    Both digests are named, because the question an operator has is "which of
    these do I have" and a message that names only one of them does not answer
    it.
    """
    image = _payload_volume(tmp_path)
    error = _refused(ps.read_data_seed, image, expected_sha256="0" * 64)
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert error.details["extracted"] == SEED_SHA256, error.details
    assert error.details["declared"] == "0" * 64, error.details
    assert SEED_SHA256 in error.message and "0" * 64 in error.message, error


def test_a_declared_digest_that_is_not_a_sha256_is_refused(tmp_path: Path) -> None:
    """A caller that passes a truncated digest gets a refusal, not a mismatch.

    Comparing against a value that cannot be a digest would refuse every seed
    forever, which reads as a broken reader rather than a broken argument.
    """
    image = _payload_volume(tmp_path)
    # The reader itself does not parse the digest; that is stage_esp's job via
    # unixnu_boot._declared_seed_digest.  Here the point is that a wrong digest
    # is reported as a mismatch and not as a silent pass.
    error = _refused(ps.read_data_seed, image, expected_sha256="not-a-digest")
    assert error.code is ErrorCode.VERIFICATION_FAILED, error
    assert error.details["declared"] == "not-a-digest", error.details


def test_a_cluster_chain_that_loops_is_refused(tmp_path: Path) -> None:
    """A FAT is attacker-controlled data as far as this reader is concerned.

    Without the cycle check the walk would run until the process was killed,
    which is a hang and not a refusal.
    """
    volume = bytearray(_payload_volume(tmp_path).read_bytes())
    fat_at = RESERVED_SECTORS * BYTES_PER_SECTOR
    # Cluster 6 is the seed's first cluster; point it at itself.
    struct.pack_into("<I", volume, fat_at + 6 * 4, 6)
    image = _write(tmp_path, bytes(volume), "looping.img")
    error = _refused(ps.read_data_seed, image)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "loops" in error.message, error


def test_a_chain_that_ends_before_the_declared_size_is_refused(tmp_path: Path) -> None:
    """A volume that disagrees with itself is refused, not truncated.

    The declared size comes from the directory entry, so a short chain means
    the two halves of the volume disagree -- and a reader that returned what it
    had would stage a seed the format check then rejects for a different reason.
    """
    # Declare one cluster more than the two the chain carries.
    seed_size = len(SEED) + CLUSTER_BYTES
    image = _payload_volume(tmp_path, seed_size=seed_size)
    error = _refused(ps.read_data_seed, image)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "runs out of clusters" in error.message, error
    assert error.details["declared"] == seed_size, error.details
    # What the chain actually carried: the three clusters it has.
    assert error.details["missing"] == seed_size - len(SEED_CLUSTERS) * CLUSTER_BYTES, error.details


def test_an_empty_member_is_refused(tmp_path: Path) -> None:
    """A zero-length ``scratch-data.img`` is not a seed.

    It would pass a presence check and then be staged as a file the packed
    ramdisk format rejects -- a failure that names the format and not the cause.
    """
    image = _payload_volume(tmp_path, seed_size=0)
    error = _refused(ps.read_data_seed, image)
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "empty" in error.message, error


def test_a_volume_that_ends_inside_a_cluster_is_refused(tmp_path: Path) -> None:
    """A truncated image is refused rather than read as a short seed.

    The cut is placed inside the seed's last cluster, not at the end of the
    image: the data region is followed by nothing the reader needs, so cutting
    there would leave a volume that is merely shorter and not truncated.
    """
    image = _payload_volume(tmp_path)
    blob = image.read_bytes()
    data_offset = (RESERVED_SECTORS + FAT_COUNT * SECTORS_PER_FAT) * BYTES_PER_SECTOR
    last = data_offset + (SEED_CLUSTERS[-1] - 2) * CLUSTER_BYTES + 100
    _write(tmp_path, blob[:last], "truncated.img")
    error = _refused(ps.read_data_seed, tmp_path / "truncated.img")
    assert error.code is ErrorCode.CONFIG_INVALID, error
    assert "ends inside cluster" in error.message, error


# ---------------------------------------------------------------------------
# the geometry the reader depends on
# ---------------------------------------------------------------------------

def test_the_documented_geometry_is_the_measured_geometry() -> None:
    """The constants are the payload volume's own, not a generic FAT32 set.

    ``bundle7``'s ``payload.img`` is 512 bytes per sector, 4 sectors per
    cluster, 32 reserved sectors, 2 FATs of 765 sectors and 393,216 sectors in
    all.  If a future payload volume differs, this test is what says the
    constants are stale rather than letting the reader silently misread it.
    """
    assert ps.BYTES_PER_SECTOR == 512
    assert ps.SECTORS_PER_CLUSTER == 4
    assert ps.RESERVED_SECTORS == 32
    assert ps.FAT_COUNT == 2
    assert ps.SECTORS_PER_FAT == 765
    assert ps.ROOT_CLUSTER == 2
    assert ps.TOTAL_SECTORS == 393216


def _refused(call, *args, **kwargs) -> UniPadError:
    """Run ``call`` and return the refusal, failing loudly if it does not refuse."""
    with pytest.raises(UniPadError) as caught:
        call(*args, **kwargs)
    return caught.value
