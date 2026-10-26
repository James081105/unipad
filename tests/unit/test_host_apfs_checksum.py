"""``unipad.host.apfs_checksum`` against synthetic blocks built on this host.

No real container is used: one is Apple-derived material that does not belong in
the repository (PRD 12).  What the tests pin instead is the *invariant* -- with
the checksum field in place the first running sum is exactly zero -- plus the
self-consistency of stamping and verifying, because that pair is what any caller
depends on.  A regression in the modulus or the field offset breaks both, and
the modulus in particular is the part a reader would be tempted to "correct" to
``2**32``.
"""

from __future__ import annotations

import struct

import pytest

from unipad.host import apfs_checksum as ac

MODULUS = 0xFFFFFFFF


def _block(seed: int = 0) -> bytes:
    """A deterministic 4096-byte block that is not all zeroes."""
    blob = bytearray(ac.BLOCK_SIZE)
    for index in range(0, ac.BLOCK_SIZE, 4):
        struct.pack_into("<I", blob, index, (index * 2654435761 + seed) & MODULUS)
    return bytes(blob)


# ---------------------------------------------------------------------------
# the invariant
# ---------------------------------------------------------------------------
def test_a_stamped_block_sums_to_zero_over_its_whole_self() -> None:
    """The zero-residue property, which is the whole construction.

    This is the check a kernel applies, so it is the one pinned here: a block
    this module stamps must satisfy it without this module being consulted.
    """
    block = _block()
    stamped = ac.stamp(block)
    assert ac.verify_block(stamped), stamped[:8].hex()
    assert ac.sums(stamped, start=0).sum1 == 0


def test_stamping_is_idempotent() -> None:
    """Stamping an already-stamped block changes nothing."""
    block = _block()
    once = ac.stamp(block)
    assert ac.stamp(once) == once
    assert ac.verify_stamped(once)


def test_an_unstamped_block_is_not_verified() -> None:
    """A block whose field is zeroes does not satisfy the invariant.

    ``sums`` over the whole block with a zeroed field is just ``sums`` over the
    body, which is zero only for an empty body -- so this is the case that would
    silently pass if the check were "is the field zero".
    """
    block = _block()
    assert not ac.verify_block(block)
    assert not ac.verify_stamped(block)


@pytest.mark.parametrize("seed", range(4))
def test_the_last_four_bytes_are_the_second_sum(seed: int) -> None:
    """The second half of the byte string is :attr:`Pair.sum2`.

    Pinned because it is the half a naive implementation gets right, so a change
    to the modulus or the field offset shows up here rather than only in the
    first half.  Note the byte order: sum2 is the numerically high half of the
    64-bit value and the *second* half of the bytes.
    """
    block = _block(seed)
    pair = ac.sums(block)
    first, second = struct.unpack("<II", ac.object_checksum(block))
    assert second == pair.sum2, (hex(second), hex(pair.sum2))
    assert first == (-(pair.sum1 + pair.sum2)) % MODULUS, (hex(first), hex(pair.sum1))


def test_the_first_four_bytes_are_the_negated_total() -> None:
    """``-(sum1 + sum2)`` is what makes the whole-block residue zero."""
    block = _block()
    pair = ac.sums(block)
    first, _ = struct.unpack("<II", ac.object_checksum(block))
    assert (first + pair.sum1 + pair.sum2) % MODULUS == 0


# ---------------------------------------------------------------------------
# the shape of the input
# ---------------------------------------------------------------------------
def test_a_block_of_the_wrong_size_is_refused_rather_than_truncated() -> None:
    """Every checksummed object is a whole 4096-byte block."""
    with pytest.raises(ValueError, match="4096"):
        ac.stamp(b"\0" * 512)
    assert not ac.verify_block(b"\0" * 512)
    assert not ac.verify_stamped(b"\0" * 512)


def test_a_range_that_is_not_whole_words_is_refused() -> None:
    """``struct.iter_unpack`` would raise anyway; the message says why."""
    with pytest.raises(ValueError, match="multiple of 4"):
        ac.sums(b"\0" * 6, start=0)


def test_the_all_zero_block_stamps_to_the_identity() -> None:
    """An empty body has both sums zero, so the field is zero and still verifies.

    That is the one block where "the field is zero" and "the checksum is right"
    coincide, which is why :func:`verify_block` is written as the residue rather
    than as a comparison against zeroes.
    """
    empty = bytes(ac.BLOCK_SIZE)
    assert ac.object_checksum(empty) == bytes(8)
    assert ac.verify_block(empty)
    assert ac.verify_stamped(empty)


# ---------------------------------------------------------------------------
# the modulus
# ---------------------------------------------------------------------------
def test_the_modulus_is_2_pow_32_minus_1_not_2_pow_32() -> None:
    """Pinned because it is the single easiest thing to get wrong.

    Reducing modulo ``2**32`` instead leaves the high half of a real container's
    stored checksum off, and the low half off by a different amount -- which
    reads as "the algorithm is unknown" rather than as an off-by-one in the
    modulus.  Two rounds of guessing went there before the residue property
    made it obvious.
    """
    assert ac.MODULUS == 0xFFFFFFFF
    assert ac.MODULUS != 1 << 32


def test_summing_with_the_field_skipped_equals_summing_it_zeroed() -> None:
    """The equivalence the module's default ``start`` relies on."""
    block = _block()
    zeroed = bytes(8) + block[8:]
    assert ac.sums(block) == ac.sums(zeroed, start=0)
