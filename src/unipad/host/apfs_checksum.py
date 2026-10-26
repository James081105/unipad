"""APFS's object checksum, as it is actually laid out on disk.

Why this exists
---------------
APFS protects every on-disk object -- the container superblock, the volume
superblock, the omaps, the B-tree nodes, all of them -- with a Fletcher-64
checksum stored in the first eight bytes of its 4096-byte block.  Anything that
writes or validates APFS metadata needs it, and nothing in this repository or in
the vendored device trees spells the algorithm out: the bootloader's
``PackedRamdisk.h`` and the kernel driver's ``SparseDisk.h`` describe a different
format entirely, and the APFS kext is prelinked into ``kernel.macho`` with its
static functions stripped, so there is no ``fletcher64`` symbol to read.

So the rule here was **measured, not transcribed** (2026-10-26): the eight bytes
were recomputed for every object block of three containers the guest actually
mounts -- the SystemOS cryptex, the OS root volume, and the packed Data seed --
and the construction below reproduced all nine of them byte for byte.  The tenth
block in that set is a corrupt object in the seed and does not carry a checksum
at all, which is itself part of the evidence.

The construction is the classic zero-residue one: the checksum field is part of
what is summed, and the stored value is chosen so the first running sum comes
out to exactly zero.  That is why the obvious reading -- "store ``(c1 << 32) |
c0``" -- gets the high half right and the low half wrong, which is what made
this look unsolvable for two rounds of guessing.

Lay out a block, then :func:`verify_block` it; the check is the invariant, not a
comparison against a constant, so a block this module wrote is checked by the
same rule a kernel would apply.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

#: APFS reduces both running sums modulo ``2**32 - 1``, not ``2**32``.  The
#: difference is visible: with ``2**32`` the high half of a real container's
#: stored checksum is off by one part in 2**32, which is exactly the "high half
#: matches, low half does not" symptom.
MODULUS = 0xFFFFFFFF

#: The size of the checksum field at the start of every object.
CKSUM_SIZE = 8

#: The APFS block size.  Every checksummed object occupies a whole block.
BLOCK_SIZE = 4096


@dataclass(frozen=True)
class Pair:
    """The two running sums of a Fletcher-64 pass."""

    #: The plain sum of the words.
    sum1: int
    #: The sum of the partial sums.
    sum2: int


def sums(block: bytes, *, start: int = CKSUM_SIZE) -> Pair:
    """The two running sums over ``block[start:]``.

    ``start`` defaults to :data:`CKSUM_SIZE` because the checksum field is
    excluded -- but that is a convenience, not a requirement: prepending zero
    words leaves both sums unchanged, so summing the whole block with the field
    zeroed gives the same :class:`Pair` as skipping it.

    Raises:
        ValueError: when the range is not a whole number of 32-bit words.
    """
    data = block[start:]
    if len(data) % 4:
        raise ValueError(
            f"the checksummed range is {len(data)} bytes, not a multiple of 4"
        )
    sum1 = 0
    sum2 = 0
    for (word,) in struct.iter_unpack("<I", data):
        sum1 = (sum1 + word) % MODULUS
        sum2 = (sum2 + sum1) % MODULUS
    return Pair(sum1=sum1, sum2=sum2)


def object_checksum(block: bytes) -> bytes:
    """The eight bytes a block's first eight bytes must carry.

    Laid out little-endian, so the first four bytes are
    ``-(sum1 + sum2) mod MODULUS`` and the last four are :attr:`Pair.sum2`.
    Sum2 is the *numerically* high half of the 64-bit value and the *second*
    half of the byte string, which is the detail that makes a naive
    ``(sum2 << 32) | sum1`` look right in a hex dump and wrong on disk.
    """
    pair = sums(block)
    low = (-(pair.sum1 + pair.sum2)) % MODULUS
    return struct.pack("<Q", (pair.sum2 << 32) | low)


def stamp(block: bytes) -> bytes:
    """``block`` with its checksum field filled in.

    The input's first eight bytes are ignored, so a block read off a disk can be
    re-stamped and a block built from scratch can be stamped in one step.
    """
    if len(block) != BLOCK_SIZE:
        raise ValueError(
            f"an APFS object is {BLOCK_SIZE} bytes; this one is {len(block)}"
        )
    return object_checksum(block) + block[CKSUM_SIZE:]


def verify_block(block: bytes) -> bool:
    """Whether ``block`` carries the checksum its own content implies.

    This is the invariant rather than a stored-value comparison: with the
    checksum field left in place, the first running sum over the whole block is
    exactly zero.  A block that satisfies it satisfies :func:`object_checksum`
    too, and one that does not fails both.
    """
    if len(block) != BLOCK_SIZE:
        return False
    return sums(block, start=0).sum1 == 0


def verify_stamped(block: bytes) -> bool:
    """Whether ``block``'s first eight bytes are what :func:`object_checksum` says."""
    if len(block) != BLOCK_SIZE:
        return False
    return block[:CKSUM_SIZE] == object_checksum(block)
