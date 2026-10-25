"""``unipad.host.lzfse`` against vectors built by hand on this host.

Why the vectors are hand-built
------------------------------
There is no LZFSE encoder available here -- ``pip`` is broken on this host
because the account directory contains non-ASCII characters -- so nothing can
round-trip.  Every expected output below is written out literally as a bytes
literal and derived by hand from the opcode semantics the decoder's own
comments cite (``lzvn_decode_base.c``, ``lzfse_fse.c``), so the tests assert
real decode behaviour rather than echoing the implementation.

The one thing a hand-built vector cannot prove is that this decoder agrees with
Apple's on Apple's bytes, and PRD 12 forbids committing anything decoded from
the IPSW, the DMG or the kernelcache.  :func:`test_the_real_images_chunks_decode`
closes that gap by decoding two real chunks and asserting *structural* facts
about them -- that the sector-0 chunk is a protective MBR and the sector-1 chunk
is a GPT header -- and it skips when ``local-private`` is absent.  A skipped
test is invisible in CI, so it is recorded here and in the module docstring as
what it is: not coverage.

What is deliberately not here
-----------------------------
No fixture decoded from the IPSW, the DMG, or the kernelcache.  Every byte
string in this file was authored here.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from unipad.host import lzfse
from unipad.host.lzfse import (
    DecodeError,
    InStream,
    decode,
    init_decoder_table,
    init_value_decoder_table,
)

REPO = Path(__file__).resolve().parents[2]
REAL_IMAGE = REPO / "local-private/out/components/044-76040-038.dmg"


# ---------------------------------------------------------------------------
# building a block stream
# ---------------------------------------------------------------------------
def _end() -> bytes:
    """``bvx$``: the end-of-stream block, 4 bytes and nothing else."""
    return struct.pack("<I", lzfse.MAGIC_ENDOFSTREAM)


def _raw(payload: bytes) -> bytes:
    """``bvx-``: magic, n_raw_bytes, then the bytes verbatim."""
    return struct.pack("<II", lzfse.MAGIC_UNCOMPRESSED, len(payload)) + payload


def _lzvn(payload: bytes, n_raw: int) -> bytes:
    """``bvxn``: magic, n_raw_bytes, n_payload_bytes, then the opcode stream."""
    return struct.pack("<III", lzfse.MAGIC_LZVN, n_raw, len(payload)) + payload


def _eos() -> bytes:
    """The LZVN end-of-stream opcode: ``0x06`` plus 7 bytes, 8 in total.

    ``lzvn_decode_base.c:644-655`` consumes all 8, so the padding is part of
    the token and not decoration.
    """
    return b"\x06" + b"\x00" * 7


def _push_bits(codes: list[tuple[int, int]]) -> bytes:
    """Pack ``(value, width)`` codes LSB-first into bytes.

    This is the spelling ``lzfse_decode_v1_freq_value`` reads: the accumulator
    is filled from the low bits of each byte upward (lzfse_decode_base.c:139-142),
    so the first code occupies the lowest bits of the first byte.
    """
    out = bytearray()
    acc = nbits = 0
    for value, width in codes:
        acc |= value << nbits
        nbits += width
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits:
        out.append(acc & 0xFF)
    return bytes(out)


def _freq_code(value: int) -> tuple[int, int]:
    """The v2 freq-table code for ``value`` (lzfse_decode_base.c:28-48).

    The table is indexed by the *low 5 bits* of the code, so a code is the table
    index in bits 0-4 plus the payload above it: 8-bit codes carry 4 payload
    bits, 14-bit codes carry 10.

    Values 1..7 are encodable too, on the 2-, 3- and 5-bit paths; the codes below
    are the first slot in ``_FREQ_VALUE`` holding each value, which is what
    ``lzfse_encode_v1_freq_value`` picks.
    """
    if value <= 7:
        return _FREQ_CODE_SMALL[value]
    if value < 24:
        return ((value - 8) << 4) | 7, 8             # 8 + 4 payload bits
    return ((value - 24) << 4) | 15, 14              # 24 + 10 payload bits


#: (bits, width) for the values the 2-, 3- and 5-bit codes can carry, taken from
#: the first slot of lzfse_freq_value_table holding each value.
_FREQ_CODE_SMALL = ((0, 2), (2, 2), (1, 3), (5, 3), (3, 5), (11, 5), (19, 5), (27, 5))


def _v2_freq_bytes() -> bytes:
    """A compressed freq table that sums to the state count of every stream.

    The literal table is split over two codes -- a 16 on the 8-bit path and a
    1008 on the 14-bit path -- so both of the table's non-trivial widths are
    exercised; the other three tables use a single 14-bit code each and the
    remaining 355 slots are zero, a 2-bit code each.

    774 bits is 97 bytes, and that is what makes this a *valid* header: the
    decoder has to finish with fewer than 8 bits left in its accumulator and
    with every byte consumed (lzfse_decode_base.c:158-161).
    """
    literal_base = lzfse.L_SYMBOLS + lzfse.M_SYMBOLS + lzfse.D_SYMBOLS
    singles = {
        0: lzfse.L_STATES,                                    # l_freq[0]
        lzfse.L_SYMBOLS: lzfse.M_STATES,                      # m_freq[0]
        lzfse.L_SYMBOLS + lzfse.M_SYMBOLS: lzfse.D_STATES,    # d_freq[0]
        literal_base: 16,                                     # literal_freq[0], 8-bit
        literal_base + 1: lzfse.LITERAL_STATES - 16,          # literal_freq[1], 14-bit
    }
    assert sum(singles.values()) == (
        lzfse.L_STATES + lzfse.M_STATES + lzfse.D_STATES
        + lzfse.LITERAL_STATES), singles
    return _push_bits([_freq_code(singles.get(i, 0))
                       for i in range(lzfse.N_FREQ_VALUES)])


def _v2_block(n_raw: int = 0) -> bytes:
    """A ``bvx2`` block that carries no literals and no matches.

    The three packed 64-bit fields are all zero except ``header_size`` in the
    low 32 bits of the third, and the header is 32 bytes of fixed part plus the
    96-byte compressed freq table.  Apple's compressor never emits a block this
    degenerate, but it is the shape that exercises ``decode_v2_header`` -- the
    packed fields and the freq decoder -- without an FSE payload to construct
    by hand.
    """
    freq = _v2_freq_bytes()
    head = struct.pack("<II", lzfse.MAGIC_LZFSE_V2, n_raw)
    head += struct.pack("<QQQ", 0, 0, 32 + len(freq))
    assert len(head) == 32, len(head)
    return head + freq


def _v1_block(n_raw: int = 0) -> bytes:
    """A ``bvx1`` block with the same degenerate shape, tables uncompressed.

    The freq arrays are plain ``uint16_t`` here rather than a packed bit stream,
    and the header is ``sizeof()`` long -- 772, the 770 bytes of fields rounded
    up to the struct's 4-byte alignment (lzfse_internal.h:291-330).
    """
    blob = bytearray(lzfse._V1_SIZEOF)
    struct.pack_into("<I", blob, 0, lzfse.MAGIC_LZFSE_V1)
    struct.pack_into("<I", blob, 4, n_raw)               # n_raw_bytes
    struct.pack_into("<I", blob, 12, 0)                  # n_literals
    struct.pack_into("<I", blob, 16, 0)                  # n_matches
    struct.pack_into("<H", blob, 50, lzfse.L_STATES)     # l_freq[0]
    struct.pack_into("<H", blob, 90, lzfse.M_STATES)     # m_freq[0]
    struct.pack_into("<H", blob, 130, lzfse.D_STATES)    # d_freq[0]
    struct.pack_into("<H", blob, 258, lzfse.LITERAL_STATES)   # literal_freq[0]
    return bytes(blob)


# ---------------------------------------------------------------------------
# (a) hand-built LZVN
# ---------------------------------------------------------------------------
def test_a_lzvn_block_decodes_every_opcode_class() -> None:
    """One ``bvxn`` block exercising lrg_l, sml_d, lrg_d, pre_d, sml_m and eos.

    The stream, and what each opcode produces:

    ``E0 04``     lrg_l  L=20   "abcdefghijklmnopqrst"        -> out[0..19]
    ``90 04``     sml_d  L=2 M=5 D=4, literal "XY"
                  D < M, so the copy overlaps and repeats: out[22..26] = stXYs
    ``9F 12 00``  lrg_d  L=2 M=6 D=18, literal "PQ"
                  D >= M, a plain copy: out[29..34] = out[11..16] = lmnopq
    ``4E``        pre_d  L=1 M=4, distance carried over (18), literal "W"
                  out[36..39] = out[18..21] = stXY
    ``F5``        sml_m  M=5, the same carried distance
                  out[40..44] = out[22..26] = stXYs
    ``06 00*7``   eos

    45 bytes out of a 42-byte payload.
    """
    payload = (
        b"\xe0\x04" + b"abcdefghijklmnopqrst"
        + b"\x90\x04" + b"XY"
        + b"\x9f\x12\x00" + b"PQ"
        + b"\x4e" + b"W"
        + b"\xf5"
        + _eos()
    )
    assert len(payload) == 42, len(payload)
    assert decode(_lzvn(payload, 45) + _end(), 45) == (
        b"abcdefghijklmnopqrst"      # lrg_l
        b"XYstXYs"                   # sml_d, D=4 < M=5 so it repeats
        b"PQlmnopq"                  # lrg_d, D=18
        b"WstXY"                     # pre_d, D carried over
        b"stXYs"                     # sml_m, D carried over
    )


def test_a_lzvn_match_may_reach_back_into_the_previous_block() -> None:
    """A match distance is measured from the buffer start, not the block start.

    ``lzvn_decode_base.c:432`` checks ``D > dst_ptr - state->dst_begin``, and
    ``dst_begin`` is the whole output buffer -- not the ``n_raw_bytes`` window
    this block is confined to.  So block two's very first opcode can be a
    ``pre_d`` whose distance was established in block one, which is the only
    reason ``d_prev`` is worth carrying across blocks at all.

    Block one: lrg_l L=20, then ``sml_d`` L=0 M=4 D=5, so ``d_prev`` leaves as 5
    and the block has written 24 bytes.  Block two's first opcode is ``pre_d``
    L=1 M=3 with that carried distance 5: it reads out[20..22], which is *block
    one's* output.  Measured from the block's own start the distance would be
    5 > 1 and the block would be refused.
    """
    first = b"\xe0\x04" + b"abcdefghijklmnopqrst" + b"\x08\x05" + _eos()
    second = b"\x46" + b"Z" + _eos()          # pre_d L=1 M=3, D carried = 5
    stream = _lzvn(first, 24) + _lzvn(second, 4) + _end()
    assert decode(stream, 28) == (
        b"abcdefghijklmnopqrst"               # block one, lrg_l
        b"pqrs"                               # block one, sml_d D=5
        b"Z"                                  # block two, pre_d literal
        b"pqr"                                # block two, D=5 into block one
    )


def test_a_lzvn_small_distance_uses_its_three_high_bits() -> None:
    """``sml_d`` is not limited to distances under 256.

    ``D = ((opc & 7) << 8) | src[pos + 1]`` -- the opcode's low three bits are
    the *high* bits of the distance, so a ``sml_d`` reaches 2047.  Dropping them
    turns this stream's match into a distance-5 copy of the run of ``O``s, which
    is still a valid match and still the right length, so only the bytes say so.

    The stream: ``sml_l`` L=15, then ``sml_d`` L=0 M=4 D=1 to establish a
    distance, then ``lrg_m`` M=251 carrying it, then ``sml_d`` L=0 M=3 D=261.
    The last match reads out[9..11] -- the ``JKL`` of the literal -- while a
    distance-5 reading would read the ``O`` run instead.
    """
    payload = (b"\xef" + b"ABCDEFGHIJKLMNO"       # sml_l L=15
               + b"\x08\x01"                 # sml_d L=0 M=4 D=1
               + b"\xf0\xeb"                 # lrg_m M=0xEB+16=251, D carried
               + b"\x01\x05"                 # sml_d L=0 M=3 D=(1<<8)|5=261
               + _eos())
    expected = b"ABCDEFGHIJKLMNO" + b"O" * 255 + b"JKL"
    assert len(expected) == 273, len(expected)
    assert decode(_lzvn(payload, 273) + _end(), 273) == expected


def test_a_lzvn_zero_distance_is_refused() -> None:
    """``D == 0`` is the same class of corruption as a distance past the start.

    ``sml_d`` L=1 M=3 D=0 encodes as ``40 00`` plus one literal byte.
    """
    with pytest.raises(DecodeError) as caught:
        decode(_lzvn(b"\x40\x00" + b"Q" + _eos(), 4) + _end(), 4)
    assert "invalid match distance 0" in str(caught.value), caught.value


def test_a_lzvn_distance_past_the_output_is_refused() -> None:
    """A distance larger than everything written so far reads out of bounds.

    ``sml_d`` L=0 M=3 D=100 encodes as ``00 64``.
    """
    with pytest.raises(DecodeError) as caught:
        decode(_lzvn(b"\x00\x64" + _eos(), 3) + _end(), 3)
    assert "invalid match distance 100" in str(caught.value), caught.value


def test_a_lzvn_undefined_opcode_is_refused() -> None:
    """The jump table has 41 undefined slots and they must not be guessed at.

    ``0x1e`` is one of them (lzvn_decode_base.c:60-61).
    """
    with pytest.raises(DecodeError) as caught:
        decode(_lzvn(b"\x1e" + _eos(), 1) + _end(), 1)
    assert "undefined opcode 0x1e" in str(caught.value), caught.value


def test_a_lzvn_nop_is_skipped() -> None:
    """``nop`` is a real opcode that consumes a byte and produces nothing.

    ``0x0e`` and ``0x16`` are the two (lzvn_decode_base.c:52,59).  Without
    handling them the decoder would read the *next* opcode's byte as an opcode
    and produce a wrong image rather than a refusal.
    """
    payload = b"\xe6" + b"abcdef" + b"\x0e" + b"\x0e" + _eos()
    assert decode(_lzvn(payload, 6) + _end(), 6) == b"abcdef"


# The whole dispatch table, one character per opcode, laid out eight per row the
# way ``opc_tbl`` in lzvn_decode_base.c:50-99 is.  Transcribed from that array
# by hand, not derived from anything in ``lzfse``:
#
#   s sml_d   L lrg_d   M med_d   P pre_d   l sml_l   G lrg_l
#   m sml_m   H lrg_m   n nop     e eos     u udef
#
# This is the table the confrontation tried and failed to break in one place:
# shifting ``sml_d``'s first three rows by one opcode moves 0x08 out of ``sml_d``
# entirely, so a stream that starts ``00 64`` -- literal-free, match-only, which
# is what a run of identical bytes compresses to -- stops decoding and starts
# refusing.  Every other row was already pinned by a test that exercised it; the
# first three were not, because no hand-built vector needed D's high bits with
# L=0 and M=3.
_LZVN_DISPATCH = (
    "sssssseL"
    "ssssssnL"
    "ssssssnL"
    "ssssssuL"
    "ssssssuL"
    "ssssssuL"
    "ssssssuL"
    "ssssssuL"
    "ssssssPL"
    "ssssssPL"
    "ssssssPL"
    "ssssssPL"
    "ssssssPL"
    "ssssssPL"
    "uuuuuuuu"
    "uuuuuuuu"
    "ssssssPL"
    "ssssssPL"
    "ssssssPL"
    "ssssssPL"
    "MMMMMMMM"
    "MMMMMMMM"
    "MMMMMMMM"
    "MMMMMMMM"
    "ssssssPL"
    "ssssssPL"
    "uuuuuuuu"
    "uuuuuuuu"
    "Glllllll"
    "llllllll"
    "Hmmmmmmm"
    "mmmmmmmm"
)

_LZVN_LETTER = {
    "s": "_LZVN_SML_D", "L": "_LZVN_LRG_D", "M": "_LZVN_MED_D",
    "P": "_LZVN_PRE_D", "l": "_LZVN_SML_L", "G": "_LZVN_LRG_L",
    "m": "_LZVN_SML_M", "H": "_LZVN_LRG_M", "n": "_LZVN_NOP",
    "e": "_LZVN_EOS", "u": "_LZVN_UDEF",
}


@pytest.mark.parametrize("opcode", range(256))
def test_the_lzvn_dispatch_table_matches_upstream(opcode: int) -> None:
    """Every one of the 256 opcodes lands in the class the C puts it in.

    The classes must also partition the byte: no opcode in two classes, none in
    none.  A gap would show up as an ``undefined opcode`` refusal on a stream the
    C decodes, which is a silent image corruption rather than a clean failure.
    """
    want = _LZVN_DISPATCH[opcode]
    found = [name for name in (
        "_LZVN_SML_D", "_LZVN_LRG_D", "_LZVN_MED_D", "_LZVN_PRE_D", "_LZVN_SML_L",
        "_LZVN_LRG_L", "_LZVN_SML_M", "_LZVN_LRG_M", "_LZVN_NOP", "_LZVN_EOS",
        "_LZVN_UDEF") if opcode in getattr(lzfse, name)]
    assert found == [_LZVN_LETTER[want]], (
        f"opcode 0x{opcode:02x} should be {want}, got {found}")


def test_a_lzvn_literal_cut_off_by_the_end_of_the_payload_is_refused() -> None:
    """``lrg_l`` L=20 with 16 bytes of payload left after the opcode.

    The C checks the source can hold the opcode, the literal and the first byte
    of the next opcode before copying (lzvn_decode_base.c:392-397).  A missing
    check here is not a tidy refusal: assigning a short slice into the middle of
    a ``bytearray`` *shrinks* it, which would corrupt every later block's
    offsets.
    """
    payload = b"\xe0\x04" + b"abcdefghijklmnop"
    with pytest.raises(DecodeError) as caught:
        decode(_lzvn(payload, 20) + _end(), 20)
    assert "source truncated" in str(caught.value), caught.value


def test_a_lzvn_small_literal_cut_off_by_the_end_of_the_payload_is_refused() -> None:
    """The same for ``sml_l``, whose length lives in the opcode's low nibble.

    ``0xef`` is L=15 with five literal bytes behind it.
    """
    with pytest.raises(DecodeError) as caught:
        decode(_lzvn(b"\xef" + b"abcde", 15) + _end(), 15)
    assert "source truncated" in str(caught.value), caught.value


def test_a_lzvn_stream_that_stops_short_of_eos_is_refused() -> None:
    """The opcode stream runs out before the end-of-stream token.

    The block still declares 45 raw bytes and the chain still ends exactly where
    it says it does, so only the decoder's own bookkeeping can catch it.
    """
    good = (
        b"\xe0\x04" + b"abcdefghijklmnopqrst"
        + b"\x90\x04" + b"XY"
        + b"\x9f\x12\x00" + b"PQ"
        + b"\x4e" + b"W"
        + b"\xf5"
        + _eos()
    )
    with pytest.raises(DecodeError) as caught:
        decode(_lzvn(good[:30], 45) + _end(), 45)
    assert "end-of-stream" in str(caught.value), caught.value


def test_a_lzvn_block_that_produces_fewer_bytes_than_it_declares_is_refused() -> None:
    """The dangerous one: a block that hits eos early would leave zeros.

    ``decode_lzvn_block`` advances by the *declared* ``n_raw_bytes``, so a block
    that stops one byte short would leave a zero the image never declared -- a
    silently wrong image, exactly what the GPT CRC can only see after the fact.
    ``lzfse_decode_base.c:604-616`` rejects it; so does this.
    """
    with pytest.raises(DecodeError) as caught:
        decode(_lzvn(b"\xe6" + b"abcdef" + _eos(), 7) + _end(), 7)
    assert "produced 6 of 7" in str(caught.value), caught.value


def test_a_lzvn_block_with_bytes_after_the_eos_token_is_refused() -> None:
    """The payload has to be consumed exactly, not merely started.

    ``n_payload_bytes`` is what advances the chain, so leftover bytes after the
    end-of-stream token would otherwise be skipped in silence.  That is a
    stream the encoder never wrote, and the one place it shows up is a corrupt
    image -- which is precisely when "the length is right" stops being evidence
    of anything.  ``lzfse_decode_base.c:604-616`` checks it; so does this.
    """
    payload = b"\xe4" + b"abcd" + _eos() + b"XY"
    assert len(payload) == 15, len(payload)
    with pytest.raises(DecodeError) as caught:
        decode(_lzvn(payload, 4) + _end(), 4)
    assert "consumed 13 of 15" in str(caught.value), caught.value


def test_a_lzvn_match_that_overruns_its_blocks_window_is_refused() -> None:
    """A match longer than the bytes the block still owes.

    The destination window is the block's own ``n_raw_bytes``; a match that runs
    past it would land in the *next* block's bytes, which still totals the right
    length -- so this is a wrong image that passes every length check.  Three
    blocks, the middle one overrunning by two:

    * ``bvxn`` n_raw=4, ``e4`` lrg_l L=4, literal "abcd", eos
    * ``bvxn`` n_raw=6, ``28 01`` sml_d L=0 M=8 D=1 -- eight bytes where six are
      owed
    * ``bvx-`` n_raw=20, so the whole stream is 30 bytes and the overrun fits
      inside the buffer rather than past it
    """
    stream = (_lzvn(b"\xe4" + b"abcd" + _eos(), 4)
              + _lzvn(b"\x28\x01" + _eos(), 6)
              + _raw(b"Z" * 20) + _end())
    assert len(stream) > 0
    with pytest.raises(DecodeError) as caught:
        decode(stream, 30)
    assert "destination truncated mid-match" in str(caught.value), caught.value


def test_a_v2_block_that_declares_no_extent_is_refused() -> None:
    """A ``bvx2`` block whose header and payloads are all zero.

    ``header_size`` is a 32-bit field in a header this decoder is validating, so
    it can say 0.  Upstream would spin its outer loop forever on that; here it is
    a refusal, because the block chain of a multi-gigabyte image is
    attacker-controlled input and an unbounded loop is not an acceptable answer
    to 40 bytes of garbage.
    """
    block = bytearray(_v2_block())
    struct.pack_into("<QQQ", block, 8, 0, 0, 0)          # header_size and payloads
    with pytest.raises(DecodeError) as caught:
        decode(bytes(block) + _raw(b"HELLO") + _end(), 5)
    assert "zero extent" in str(caught.value), caught.value


def test_a_v1_block_whose_payload_sizes_are_zero_still_advances() -> None:
    """``bvx1`` has no ``header_size`` field, so it can never declare a zero extent.

    Offset 24 in a v1 header is ``n_lmd_payload_bytes``, not a header size.  A v1
    reading that reused the v2 layout would take 0 from there, fail to advance,
    and append blocks until the process ran out of memory -- a hang dressed up as
    a ``MemoryError``.  The extent is ``sizeof()`` (772) instead
    (lzfse_decode_base.c:400).
    """
    assert len(_v1_block()) == lzfse._V1_SIZEOF == 772
    assert decode(_v1_block() + _raw(b"HELLO") + _end(), 5) == b"HELLO"


def test_a_v1_block_with_no_literal_payload_still_decodes_its_literals() -> None:
    """``n_literal_payload_bytes == 0`` with ``n_literals > 0`` is LEGAL.

    This is the block shape that reddened
    ``tests/acceptance/test_root_volume_probe_decoder.py`` on the real OS image
    (``local-private/out/components/044-76040-038.dmg``): a ``bvx2`` block with
    ``n_literals=4``, ``n_literal_payload_bytes=0``, ``n_matches=446``,
    ``n_lmd_payload_bytes=627``, ``header_size=128`` -- and 128+0+627+4 is its 759
    compressed bytes exactly, so it is one block plus the ``bvx$`` terminator.

    The reference has no guard for it.  ``fse_in_checked_init64``
    (lzfse_fse.h:309-316) takes its 7-byte path when the stored bit count is 0,
    and reads them from immediately before the LMD payload -- which, with no
    literal payload, is the tail of the freq table.  So the literals are seeded
    from the header's own last bytes, and that is what the encoder writes.

    An earlier version of this module refused the shape outright
    ("n_literals > 0 but the literal payload is empty"), inherited from the
    private kernelcache decoder it was ported from -- which never met it, because
    all 939 of that file's blocks carry a literal payload.  The guard was not
    merely redundant: it made 19 of the first 80 real OS chunks undecodable.

    Built here as a ``bvx1`` block rather than a ``bvx2`` one so the freq tables
    are plain ``uint16_t`` arrays and the whole block is spelled out instead of
    packed.  What it decodes to is four zero bytes: the literal table puts symbol
    0 in every state, and the seed bytes are zero.
    """
    blob = bytearray(lzfse._V1_SIZEOF)

    def field(offset: int, fmt: str, value: int) -> None:
        struct.pack_into(fmt, blob, offset, value)

    field(lzfse._V1["magic"], "<I", lzfse.MAGIC_LZFSE_V1)
    field(lzfse._V1["n_raw_bytes"], "<I", 4)               # 4 literals, no match bytes
    field(lzfse._V1["n_payload_bytes"], "<I", 7)           # 0 literal + 7 LMD
    field(lzfse._V1["n_literals"], "<I", 4)
    field(lzfse._V1["n_matches"], "<I", 1)
    field(lzfse._V1["n_literal_payload_bytes"], "<I", 0)   # <-- the case under test
    field(lzfse._V1["n_lmd_payload_bytes"], "<I", 7)
    field(lzfse._V1["literal_bits"], "<i", 0)
    field(lzfse._V1["lmd_bits"], "<i", 0)
    field(lzfse._V1["l_state"], "<H", 0)
    field(lzfse._V1["m_state"], "<H", 0)
    field(lzfse._V1["d_state"], "<H", 0)
    # l_freq[4] owns every L state, so the L decode returns L_BASE_VALUE[4] == 4
    # with no extra bits -- the one triple that flushes exactly four literals.
    field(lzfse._V1["l_freq"] + 2 * 4, "<H", lzfse.L_STATES)
    field(lzfse._V1["m_freq"], "<H", lzfse.M_STATES)       # M == 0
    field(lzfse._V1["d_freq"] + 2 * 1, "<H", lzfse.D_STATES)  # D == 1, the first
    #                                                          match's only legal D
    field(lzfse._V1["literal_freq"], "<H", 512)            # symbols 0 and 1 share
    field(lzfse._V1["literal_freq"] + 2, "<H", 512)        # the 1024 literal states

    block = bytes(blob) + b"\x00" * 7                      # the LMD payload, all zero
    assert len(block) == lzfse._V1_SIZEOF + 7
    assert decode(block + _end(), 4) == b"\x00\x00\x00\x00"


def test_a_v1_block_with_matches_but_no_lmd_payload_is_still_refused() -> None:
    """The mirror of the test above, and the reason the guard was not simply deleted.

    An empty literal payload is legal; an empty LMD payload is not.  The
    reference decides both in ``fse_in_init``, and for the LMD stream it passes
    ``s->src`` as ``buf_start`` (lzfse_decode_base.c:500) -- the start of the LMD
    payload, not of the block.  So with no payload the 7 bytes it needs would
    come from before the payload begins, which is a range it refuses.
    """
    block = bytearray(_v1_block())
    struct.pack_into("<I", block, lzfse._V1["n_matches"], 1)
    struct.pack_into("<I", block, lzfse._V1["n_lmd_payload_bytes"], 0)
    with pytest.raises(DecodeError) as caught:
        decode(bytes(block) + _end(), 0)
    assert "underflow" in str(caught.value), caught.value


# ---------------------------------------------------------------------------
# (b) uncompressed and end-of-stream blocks
# ---------------------------------------------------------------------------
def test_an_uncompressed_block_is_copied_verbatim() -> None:
    """``bvx-`` plus ``bvx$``: the dispatcher and the two trivial codecs."""
    assert decode(_raw(b"HELLO") + _end(), 5) == b"HELLO"


def test_several_blocks_are_concatenated_in_order() -> None:
    """The block chain is a sequence, and a chunk can hold more than one."""
    stream = (_raw(b"AB") + _lzvn(b"\xe4" + b"cdef" + _eos(), 4) + _raw(b"GH")
              + _end())
    assert decode(stream, 8) == b"ABcdefGH"


def test_an_empty_stream_is_refused() -> None:
    """``lzfse_decode`` reports SRC_EMPTY for a zero-length buffer."""
    with pytest.raises(DecodeError) as caught:
        decode(b"", 0)
    assert "empty LZFSE source" in str(caught.value), caught.value


def test_a_stream_of_nothing_but_the_end_magic_is_empty() -> None:
    """``bvx$`` on its own is a well-formed zero-byte stream."""
    assert decode(_end(), 0) == b""


def test_an_uncompressed_block_running_past_the_chunk_is_refused() -> None:
    """A block that declares more raw bytes than the buffer carries.

    ``_scan`` checks every block's declared extent against the buffer, not just
    that the chain lands on the end of it.  The latter alone is not enough: this
    block declares ten bytes over five bytes of payload plus a four-byte
    ``bvx$``, so it would eat the terminator as data and still land exactly on
    the end -- a silently wrong image of exactly the right length.  The C
    refuses the same input by clamping the copy to the source that exists and
    then reporting the stream never terminated (lzfse_decode_base.c:541-553).
    """
    block = struct.pack("<II", lzfse.MAGIC_UNCOMPRESSED, 10) + b"HELLO"
    assert len(block) == 13, len(block)          # declares 10 raw bytes, carries 5
    with pytest.raises(DecodeError) as caught:
        decode(block + _end(), 10)
    assert "overruns the buffer" in str(caught.value), caught.value


def test_an_lzvn_block_running_past_the_chunk_is_refused() -> None:
    """The same check on the LZVN path, where the payload is the whole body."""
    block = struct.pack("<III", lzfse.MAGIC_LZVN, 4, 99) + b"\xe4" + b"cdef"
    with pytest.raises(DecodeError) as caught:
        decode(block + _end(), 4)
    assert "overruns the buffer" in str(caught.value), caught.value


def test_a_bvx_block_payload_running_past_the_chunk_is_refused() -> None:
    """And on the v2 path, where header and payload sizes are separate fields."""
    block = bytearray(_v2_block())
    # n_lmd_payload_bytes is bits 40-59 of the second packed field.
    struct.pack_into("<Q", block, 16, 1 << 59)
    with pytest.raises(DecodeError) as caught:
        decode(bytes(block) + _raw(b"HELLO") + _end(), 5)
    assert "overruns the buffer" in str(caught.value), caught.value


def test_a_stream_with_no_end_of_stream_block_is_refused() -> None:
    """Running out of bytes is not the same as finishing.

    ``lzfse_decode`` returns OK only on the ``bvx$`` block, and
    ``lzfse_decode_buffer`` turns anything else into a zero-length failure
    (lzfse_decode.c:37-41).  A chain that simply stops is truncated.
    """
    with pytest.raises(DecodeError) as caught:
        decode(_raw(b"HELLO"), 5)
    assert "no end-of-stream block" in str(caught.value), caught.value


def test_trailing_bytes_after_the_end_of_stream_block_are_refused() -> None:
    """The chain has to land exactly on the end of the buffer."""
    with pytest.raises(DecodeError) as caught:
        decode(_raw(b"HELLO") + _end() + b"\x00\x00\x00\x00", 5)
    assert "trailing bytes after the last block" in str(caught.value), caught.value


def test_a_truncated_block_header_is_refused() -> None:
    """Four bytes of magic and nothing after them."""
    with pytest.raises(DecodeError) as caught:
        decode(b"bvx-", 0)
    assert "truncated" in str(caught.value), caught.value


def test_a_v2_block_that_produces_fewer_bytes_than_it_declares_is_refused() -> None:
    """The per-block check, asserted by its own message rather than by the total.

    ``_scan``'s final ``out_pos != total`` would also refuse this stream, so a
    test that only asked "does it raise" could not tell the two apart -- and the
    per-block check is the one that names *which* block is wrong, which is the
    difference between a diagnosis and a shrug.  ``_v2_block(5)`` declares five
    raw bytes and carries no literals and no matches, so it produces none.
    """
    with pytest.raises(DecodeError) as caught:
        decode(_v2_block(5) + _end(), 5)
    assert "block produced 0 of 5 raw bytes" in str(caught.value), caught.value


def test_the_v2_freq_value_table_decodes_every_code_width() -> None:
    """``lzfse_decode_v1_freq_value`` against both of its tables, hand-derived.

    ``_FREQ_NBITS``/``_FREQ_VALUE`` are indexed by the low 5 bits of the code, so
    the 2-, 3- and 5-bit codes all decode straight out of the table and the 8-
    and 14-bit ones add a payload above bit 4.  The expectations below are
    transcribed from lzfse_decode_base.c:28-48, not computed:

    * 8-bit: b in {7, 15, 23, 31}, value = 8 + ((bits >> 4) & 0xf), so with the
      payload zeroed b=7 gives 8 and b=23 (whose bit 4 is set) gives 9.
    * 14-bit: b in {15, 31}, value = 24 + ((bits >> 4) & 0x3ff), so b=15 gives 24
      and b=31 gives 25.
    * everything else: value = _FREQ_VALUE[b] with its own width.

    The 5-bit slots are the ones a table of mostly-zero frequencies never
    reaches, and they are where a wrong ``_FREQ_VALUE`` entry hides.
    """
    expected = [
        (0, 2), (2, 3), (1, 2), (4, 5), (0, 2), (3, 3), (1, 2), (8, 8),
        (0, 2), (2, 3), (1, 2), (5, 5), (0, 2), (3, 3), (1, 2), (24, 14),
        (0, 2), (2, 3), (1, 2), (6, 5), (0, 2), (3, 3), (1, 2), (9, 8),
        (0, 2), (2, 3), (1, 2), (7, 5), (0, 2), (3, 3), (1, 2), (25, 14),
    ]
    assert len(expected) == 32
    for bits, want in enumerate(expected):
        got = lzfse._freq_value(bits)
        assert got == want, f"code {bits:#07b}: {got}, wanted {want}"


@pytest.mark.parametrize("value", range(8))
def test_the_v2_freq_code_round_trips_every_small_value(value: int) -> None:
    """The encoder side of the same table, for the values 1..7 in particular.

    ``_freq_code`` is how the hand-built vectors spell a frequency, so if it and
    ``_freq_value`` disagree the vectors are lying.  Each pair below is the first
    slot of ``lzfse_freq_value_table`` holding the value.
    """
    bits, width = _freq_code(value)
    assert lzfse._freq_value(bits) == (value, width), value
    assert (bits >> width) == 0 or width == 8 or width == 14, (value, bits, width)


# ---------------------------------------------------------------------------
# (c) FSE table construction
# ---------------------------------------------------------------------------
def test_init_decoder_table_spreads_the_frequencies() -> None:
    """The standard FSE spread, derived by hand for nstates=8, freq=[3,3,2].

    ``n_clz = clz(8) = 28``.  For each symbol ``k = clz(f) - n_clz`` and
    ``j0 = (2*nstates >> k) - f``, and states ``offset-f .. offset-1`` belong to
    the symbol:

    * f=3: k = 30-28 = 2, j0 = (16>>2)-3 = 1.  j=0 < j0, so
      ``(k, sym, ((f+j)<<k) - nstates)`` = (2, sym, 12-8) = (2, sym, 4);
      j=1,2 >= j0, so ``(k-1, sym, (j-j0) << (k-1))`` = (1, sym, 0) and (1, sym, 2).
    * f=2: k = 30-28 = 2, j0 = (16>>2)-2 = 2.  Both j are below j0, so
      (2, sym, ((2+j)<<2)-8) = (2, sym, 0) and (2, sym, 4).

    Both branches of the loop are exercised, which is the point: the ``j >= j0``
    branch is the one a transcription slip would drop.
    """
    assert init_decoder_table(8, 3, [3, 3, 2]) == [
        (2, 0, 4), (1, 0, 0), (1, 0, 2),
        (2, 1, 4), (1, 1, 0), (1, 1, 2),
        (2, 2, 0), (2, 2, 4),
    ]


def test_init_value_decoder_table_adds_the_value_bits() -> None:
    """The same spread with ``vbits``/``vbase`` folded in.

    Same freq vector, ``vbits = (1, 2, 3)`` and ``vbase = (10, 20, 30)``: the
    total is ``k + vbits[i]`` on the first branch and ``k - 1 + vbits[i]`` on the
    second, the delta is unchanged, and ``vbase`` rides along untouched.
    """
    assert init_value_decoder_table(8, 3, [3, 3, 2], (1, 2, 3), (10, 20, 30)) == [
        (3, 1, 4, 10), (2, 1, 0, 10), (2, 1, 2, 10),
        (4, 2, 4, 20), (3, 2, 0, 20), (3, 2, 2, 20),
        (5, 3, 0, 30), (5, 3, 4, 30),
    ]


def test_a_single_symbol_fills_every_state() -> None:
    """freq == nstates gives k=0 and delta = j, one entry per state."""
    assert init_decoder_table(8, 2, [8, 0]) == [(0, 0, j) for j in range(8)]


def test_the_l_m_d_base_and_extra_bits_tables_match_upstream() -> None:
    """The six L/M/D tables, transcribed from lzfse_internal.h:589-627.

    A decoded L, M or D is ``base[symbol] + (extra_bits[symbol] raw bits)``.  No
    hand-built vector reaches these tables -- building one needs an FSE *encoder*
    to produce the L/M/D bit stream, and there is no encoder here -- so the
    constants are pinned directly instead.  A shifted ``D_BASE_VALUE[0]`` in
    particular turns a distance of 0 into a distance of 1, which is the
    difference between refusing a corrupt block and silently reading the wrong
    byte; that is exactly the kind of error a length check cannot see.
    """
    assert lzfse.L_EXTRA_BITS == (0,) * 16 + (2, 3, 5, 8)
    assert lzfse.M_EXTRA_BITS == (0,) * 16 + (3, 5, 8, 11)
    assert lzfse.D_EXTRA_BITS == tuple(i // 4 for i in range(64))
    assert lzfse.L_BASE_VALUE == (
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 28, 60)
    assert lzfse.M_BASE_VALUE == (
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 24, 56, 312)
    assert lzfse.D_BASE_VALUE == (
        0, 1, 2, 3, 4, 6, 8, 10, 12, 16,
        20, 24, 28, 36, 44, 52, 60, 76, 92, 108,
        124, 156, 188, 220, 252, 316, 380, 444, 508, 636,
        764, 892, 1020, 1276, 1532, 1788, 2044, 2556, 3068, 3580,
        4092, 5116, 6140, 7164, 8188, 10236, 12284, 14332, 16380, 20476,
        24572, 28668, 32764, 40956, 49148, 57340, 65532, 81916, 98300, 114684,
        131068, 163836, 196604, 229372)
    # The three tables are 20, 20 and 64 symbols; D's is four values per width.
    assert len(lzfse.D_BASE_VALUE) == lzfse.D_SYMBOLS == 64
    assert len(lzfse.L_BASE_VALUE) == lzfse.L_SYMBOLS == 20
    assert len(lzfse.M_BASE_VALUE) == lzfse.M_SYMBOLS == 20


def test_a_freq_vector_that_overruns_the_states_is_refused() -> None:
    """``sum(freq) > nstates`` is a corrupt header, not a rounding problem."""
    with pytest.raises(DecodeError) as caught:
        init_decoder_table(8, 3, [3, 3, 3])
    assert "exceeds 8 states" in str(caught.value), caught.value


def test_a_freq_vector_that_underfills_the_states_is_refused() -> None:
    """``sum(freq) != nstates`` leaves states with no symbol at all.

    The C's ``fse_check_freq`` only rejects an overrun, so the decoder would read
    an uninitialised entry; this module refuses instead, and the encoder never
    emits one.
    """
    with pytest.raises(DecodeError) as caught:
        init_decoder_table(8, 3, [3, 3, 1])
    assert "!= 8 states" in str(caught.value), caught.value


def test_zero_frequency_symbols_are_absent_from_the_table() -> None:
    """A symbol with frequency 0 never appears, however many symbols there are."""
    table = init_decoder_table(8, 5, [3, 0, 3, 0, 2])
    assert sorted({symbol for _k, symbol, _d in table}) == [0, 2, 4]


# ---------------------------------------------------------------------------
# the FSE backward bit reader
# ---------------------------------------------------------------------------
def test_in_stream_reads_seven_bytes_when_there_is_no_leftover() -> None:
    """``n == 0`` takes the 7-byte path and leaves exactly 56 bits.

    A 7-byte little-endian read is at most 56 bits, so the value is already in
    range and the "encoder left nonzero padding" check is an invariant here
    rather than a test -- it only has teeth on the 8-byte path, where the
    accumulator holds ``n + 64`` bits and the bits above that are padding.
    """
    stream = InStream(b"0123456\x00", 8, 0, 0)
    assert (stream.pos, stream.nbits) == (1, 56), (stream.pos, stream.nbits)


def test_in_stream_rejects_nonzero_padding_bits() -> None:
    """The 8-byte path's top bits are padding and the encoder had to zero them.

    ``n`` is the encoder's leftover bit count (``literal_bits`` and ``lmd_bits``
    are stored as ``field - 7``, so they arrive as -7..0), and a nonzero ``n``
    reads 8 bytes and leaves ``n + 64`` of them (lzfse_fse.h:302-316).
    """
    good = InStream(b"0123456\x00", 8, -3, 0)
    assert (good.pos, good.nbits) == (0, 61), (good.pos, good.nbits)
    with pytest.raises(DecodeError) as caught:
        InStream(b"0123456\xff", 8, -3, 0)
    assert "nonzero padding bits" in str(caught.value), caught.value


def test_in_stream_rejects_an_init_that_underruns_the_buffer() -> None:
    """The 7-byte and 8-byte paths each need their bytes to exist."""
    with pytest.raises(DecodeError) as caught:
        InStream(b"012345", 6, 0, 0)
    assert "needs 7 bytes" in str(caught.value), caught.value
    with pytest.raises(DecodeError) as caught:
        InStream(b"0123", 4, -3, 0)
    assert "needs 8 bytes" in str(caught.value), caught.value


# ---------------------------------------------------------------------------
# (d) the length contract
# ---------------------------------------------------------------------------
def test_a_stream_that_declares_more_than_the_chunk_is_refused() -> None:
    """The chunk table says how many bytes this chunk expands to.

    A chain that declares more is refused before anything is allocated, which is
    the difference between a refusal and a ``MemoryError``: a corrupt
    ``n_raw_bytes`` can name a multi-gigabyte buffer.
    """
    with pytest.raises(DecodeError) as caught:
        decode(_raw(b"HELLO") + _end(), 4)
    assert "more than the 4 expected" in str(caught.value), caught.value


def test_a_stream_that_declares_fewer_than_the_chunk_is_refused() -> None:
    """The other direction: the image promised bytes the stream does not carry."""
    with pytest.raises(DecodeError) as caught:
        decode(_raw(b"HELLO") + _end(), 6)
    assert "produced 5 bytes, not the 6" in str(caught.value), caught.value


def test_a_negative_expected_size_is_refused() -> None:
    with pytest.raises(DecodeError) as caught:
        decode(_end(), -1)
    assert "negative" in str(caught.value), caught.value


def test_an_unknown_block_magic_is_refused() -> None:
    """The dispatcher's default branch, and the reason it exists."""
    with pytest.raises(DecodeError) as caught:
        decode(b"bvx?" + _end(), 0)
    assert "unknown block magic" in str(caught.value), caught.value


@pytest.mark.parametrize("magic", [
    lzfse.MAGIC_ENDOFSTREAM,
    lzfse.MAGIC_UNCOMPRESSED,
    lzfse.MAGIC_LZFSE_V1,
    lzfse.MAGIC_LZFSE_V2,
    lzfse.MAGIC_LZVN,
])
def test_the_five_magics_are_the_five_apple_uses(magic: int) -> None:
    """lzfse_internal.h:276-281.  A port that renamed one would decode nothing."""
    assert struct.pack("<I", magic) in (
        b"bvx$", b"bvx-", b"bvx1", b"bvx2", b"bvxn",
    ), struct.pack("<I", magic)


# ---------------------------------------------------------------------------
# the bvx1 / bvx2 header paths
# ---------------------------------------------------------------------------
def test_a_v1_block_header_is_walked_by_its_own_layout() -> None:
    """A ``bvx1`` block that produces nothing, then a raw block that does.

    This pins two things a v2-shaped reading would get wrong.  The v1 header is
    a plain struct, so its extent is ``sizeof()`` (772) and its payload sizes are
    ``uint32_t`` fields at their own offsets -- reading them with the v2 packed
    layout walks the chain by the wrong amount and lands mid-block.  And
    ``l_state``/``m_state``/``d_state`` are ``uint16_t`` sitting immediately
    before ``l_freq``, so reading them as ``uint32_t`` makes ``d_state`` come
    back as ``d_state | l_freq[0] << 16`` -- which for a table whose first entry
    is 64 is a state index of 4,194,304 and a refusal.
    """
    assert len(_v1_block()) == lzfse._V1_SIZEOF == 772
    assert decode(_v1_block() + _raw(b"HELLO") + _end(), 5) == b"HELLO"


def test_a_v2_block_header_decodes_its_compressed_freq_table() -> None:
    """The packed fields and the 768-bit freq table, without an FSE payload.

    The freq table is the fiddly part of the v2 header: 360 variable-length
    codes packed LSB-first, and the decoder has to finish with fewer than 8 bits
    left and every byte consumed.  Getting the packing wrong by one bit
    desynchronises the whole table.
    """
    block = _v2_block()
    assert len(block) == 129, len(block)      # 32 fixed + 97 freq
    assert decode(block + _raw(b"HELLO") + _end(), 5) == b"HELLO"


def test_a_v2_freq_table_that_does_not_end_cleanly_is_refused() -> None:
    """A freq section whose codes do not fill it exactly.

    Flipping a byte in the zero-code tail changes the code widths, so the table
    either runs out of bits or leaves 8 or more unconsumed -- both are the
    "this is not a table the encoder wrote" signal
    (lzfse_decode_base.c:158-161).
    """
    block = bytearray(_v2_block())
    block[32 + 90] ^= 0xFF                    # inside the freq table
    with pytest.raises(DecodeError) as caught:
        decode(bytes(block) + _raw(b"HELLO") + _end(), 5)
    assert "freq table" in str(caught.value), caught.value


def test_a_v2_header_size_that_overruns_the_buffer_is_refused() -> None:
    """``header_size`` is a 32-bit field in a header we are validating."""
    block = bytearray(_v2_block())
    struct.pack_into("<I", block, 24, 0xFFFFFF)
    with pytest.raises(DecodeError) as caught:
        decode(bytes(block) + _raw(b"HELLO") + _end(), 5)
    assert "overruns" in str(caught.value), caught.value


def test_a_compressed_block_header_sanity_check_still_fires() -> None:
    """``check_header_v1`` bounds the states a header can name.

    ``d_state`` is a 10-bit field; ``lzfse_internal.h:511-568`` rejects one at or
    above the state count rather than indexing a table out of range.
    """
    block = bytearray(_v2_block())
    # v2 packs d_state into bits 52-61 of the third 64-bit field.
    struct.pack_into("<Q", block, 24, 32 + len(_v2_freq_bytes())
                     | (lzfse.D_STATES << 52))
    with pytest.raises(DecodeError) as caught:
        decode(bytes(block) + _raw(b"HELLO") + _end(), 5)
    assert "d_state" in str(caught.value), caught.value


# ---------------------------------------------------------------------------
# the real image
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not REAL_IMAGE.is_file(), reason="local-private is absent by design")
def test_the_real_images_chunks_decode() -> None:
    """Two real LZFSE chunks, checked for structure rather than against bytes.

    PRD 12 keeps IPSW content in ``local-private/`` and out of the repository, so
    nothing here is a fixture copied from the image.  What is asserted is what
    the *format* says the first two sectors of a UDIF image have to be: sector 0
    is a protective MBR (``0xEE`` partition type at 450 -- the partition entry
    starts at 446 and the type is its fourth byte -- with ``55 AA`` at 510) and
    sector 1 is the GPT primary header (``EFI PART``).  Both of the first two
    LZFSE chunks of the pinned image are exactly those, and neither can be
    produced by a decoder that is wrong in a way that still returns the right
    number of bytes.

    This skips when ``local-private/`` is absent, which is the normal case in a
    clean checkout -- so it is **not** coverage, and the module docstring says
    so.
    """
    from unipad.host.udif import CHUNK_LZFSE, LzfseDecoder, UdifImage

    image = UdifImage(REAL_IMAGE)
    chunks = [c for c in image.chunks() if c.type == CHUNK_LZFSE]
    assert chunks, "the pinned image carries no LZFSE chunks"
    decoder = LzfseDecoder(decode)

    mbr = image.chunk_bytes(chunks[0], decoder)
    assert chunks[0].sector == 0, chunks[0].sector
    assert len(mbr) == 512, len(mbr)
    # A protective MBR's single partition entry starts at 446: one status byte,
    # three CHS bytes, then the partition *type* at 450.
    assert mbr[450] == 0xEE, mbr[450]              # protective partition type
    assert mbr[510:512] == b"\x55\xaa", mbr[510:512]

    gpt = image.chunk_bytes(chunks[1], decoder)
    assert chunks[1].sector == 1, chunks[1].sector
    assert gpt[:8] == b"EFI PART", gpt[:8]
