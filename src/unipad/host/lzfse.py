"""Decode the LZFSE and LZVN block streams an Apple UDIF image is made of.

What this is
------------
A pure-stdlib port of the LZFSE/LZVN *core*: the block magics, the FSE table
construction and backward bit reader, the ``bvx1``/``bvx2`` compressed-block
body, and the ``bvxn`` LZVN block.  Nothing here knows about IM4P, DER, Mach-O
or FAT -- a UDIF chunk is a bare LZFSE block stream, so those layers live
elsewhere and are deliberately not part of this module.

Pure stdlib, and it has to be.  There is no wheel to install: ``pip`` is broken
on this host because the account directory contains non-ASCII characters, so
every attempt to fetch a dependency fails in the path handling before it
reaches the network.  Python's standard library has no LZFSE either, which is
the whole reason this file exists.

Why it exists
-------------
``unipad.host.udif`` refuses a UDIF image whose chunks are LZFSE-compressed
unless the caller supplies a decoder, because Python's standard library has
none.  That refusal is not pedantry: the OS member of the pinned IPSW
(``044-76040-038.dmg``) is 3814 chunks, of which **3415 are LZFSE** (396 raw,
3 zero-fill).  Without a decoder the root volume cannot be produced on this
host at all -- and a half-decoder that silently produced a wrong image would be
worse than refusing, because the GPT CRC only says so after 6 GB of writes.
``udif.default_lzfse()`` is how a caller gets the real one.

Upstream C this is ported from (local copy of the lzfse 0.4.2.post1 sdist,
Apple Inc., BSD-3-Clause):

  F:/iosvirtual/local-private/tmp/pipdl/lzfse-0.4.2.post1/lzfse/src/lzfse_internal.h
  F:/iosvirtual/local-private/tmp/pipdl/lzfse-0.4.2.post1/lzfse/src/lzfse_fse.c
  F:/iosvirtual/local-private/tmp/pipdl/lzfse-0.4.2.post1/lzfse/src/lzfse_fse.h
  F:/iosvirtual/local-private/tmp/pipdl/lzfse-0.4.2.post1/lzfse/src/lzfse_decode_base.c
  F:/iosvirtual/local-private/tmp/pipdl/lzfse-0.4.2.post1/lzfse/src/lzvn_decode_base.c

The comments keep the upstream ``file:line`` citations so that every constant
and every branch can be audited against the C without re-deriving the format.
"""

from __future__ import annotations

import struct

__all__ = ["DecodeError", "decode", "lzfse_decompress"]


class DecodeError(ValueError):
    """Any malformed input, at any stage.

    A :class:`ValueError` subclass, so a caller that already catches
    ``ValueError`` for bad configuration catches this too.  It deliberately
    does *not* inherit from ``unipad.errors.UniPadError``: this module has no
    dependency on the rest of the package, and ``udif.LzfseDecoder.decode``
    already turns any exception from here into a refusal.
    """


# ===========================================================================
# 1. LZFSE constants and L/M/D tables
#    (lzfse_internal.h:92-101, 573-627)
# ===========================================================================

MAGIC_ENDOFSTREAM = 0x24787662   # bvx$
MAGIC_UNCOMPRESSED = 0x2d787662  # bvx-
MAGIC_LZFSE_V1 = 0x31787662      # bvx1
MAGIC_LZFSE_V2 = 0x32787662      # bvx2
MAGIC_LZVN = 0x6e787662          # bvxn

L_SYMBOLS, M_SYMBOLS, D_SYMBOLS, LITERAL_SYMBOLS = 20, 20, 64, 256
L_STATES, M_STATES, D_STATES, LITERAL_STATES = 64, 64, 256, 1024
MATCHES_PER_BLOCK = 10000
LITERALS_PER_BLOCK = 4 * MATCHES_PER_BLOCK          # 40000
N_FREQ_VALUES = L_SYMBOLS + M_SYMBOLS + D_SYMBOLS + LITERAL_SYMBOLS  # 360

L_EXTRA_BITS = (0,) * 16 + (2, 3, 5, 8)
L_BASE_VALUE = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 28, 60)
M_EXTRA_BITS = (0,) * 16 + (3, 5, 8, 11)
M_BASE_VALUE = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 24, 56, 312)
D_EXTRA_BITS = ((0,) * 4 + (1,) * 4 + (2,) * 4 + (3,) * 4 + (4,) * 4 + (5,) * 4 +
                (6,) * 4 + (7,) * 4 + (8,) * 4 + (9,) * 4 + (10,) * 4 + (11,) * 4 +
                (12,) * 4 + (13,) * 4 + (14,) * 4 + (15,) * 4)
D_BASE_VALUE = (
    0, 1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 28, 36, 44, 52,
    60, 76, 92, 108, 124, 156, 188, 220, 252, 316, 380, 444, 508, 636,
    764, 892, 1020, 1276, 1532, 1788, 2044, 2556, 3068, 3580, 4092, 5116,
    6140, 7164, 8188, 10236, 12284, 14332, 16380, 20476, 24572, 28668, 32764,
    40956, 49148, 57340, 65532, 81916, 98300, 114684, 131068, 163836, 196604,
    229372)

#: Bytes of the ``lzvn_compressed_block_header`` before the opcode stream:
#: magic, n_raw_bytes, n_payload_bytes (lzfse_internal.h:284-289).
LZVN_HEADER_SIZE = 12

# v2 freq-table value code: lzfse_decode_base.c:28-48
_FREQ_NBITS = (2, 3, 2, 5, 2, 3, 2, 8, 2, 3, 2, 5, 2, 3, 2, 14,
               2, 3, 2, 5, 2, 3, 2, 8, 2, 3, 2, 5, 2, 3, 2, 14)
_FREQ_VALUE = (0, 2, 1, 4, 0, 3, 1, -1, 0, 2, 1, 5, 0, 3, 1, -1,
               0, 2, 1, 6, 0, 3, 1, -1, 0, 2, 1, 7, 0, 3, 1, -1)

# v1 header field offsets inside a bvx1 block (lzfse_internal.h:291-330).
# The C struct is *not* __packed__, so its sizeof is 772 (770 bytes of fields
# rounded up to the 4-byte alignment of the largest member); the decoder
# advances by that many bytes (lzfse_decode_base.c:400).
_V1 = dict(magic=0, n_raw_bytes=4, n_payload_bytes=8, n_literals=12, n_matches=16,
           n_literal_payload_bytes=20, n_lmd_payload_bytes=24, literal_bits=28,
           literal_state=32, lmd_bits=40, l_state=44, m_state=46, d_state=48,
           l_freq=50, m_freq=90, d_freq=130, literal_freq=258)
_V1_FIELDS_END = 770
_V1_SIZEOF = 772


def _freq_value(bits):
    """lzfse_decode_v1_freq_value(): returns (value, nbits)."""
    b = bits & 31
    n = _FREQ_NBITS[b]
    if n == 8:
        return 8 + ((bits >> 4) & 0xF), 8
    if n == 14:
        return 24 + ((bits >> 4) & 0x3FF), 14
    return _FREQ_VALUE[b], n


def _clz32(x):
    """__builtin_clz for a nonzero uint32."""
    return 32 - x.bit_length()


# ===========================================================================
# 2. FSE table construction
#    (lzfse_fse.c:57-98 fse_init_decoder_table,
#     lzfse_fse.c:109-147 fse_init_value_decoder_table)
# ===========================================================================


def init_decoder_table(nstates, nsymbols, freq):
    """Plain symbol decoder table. t[state] = (k_bits, symbol, delta)."""
    n_clz = _clz32(nstates)
    table = [None] * nstates
    offset = 0
    for i in range(nsymbols):
        f = freq[i]
        if f == 0:
            continue
        offset += f
        if offset > nstates:
            raise DecodeError(f'freq sum {offset} exceeds {nstates} states')
        k = _clz32(f) - n_clz
        j0 = ((2 * nstates) >> k) - f
        for j in range(f):
            if j < j0:
                table[offset - f + j] = (k, i, ((f + j) << k) - nstates)
            else:
                table[offset - f + j] = (k - 1, i, (j - j0) << (k - 1))
    if offset != nstates:
        raise DecodeError(f'freq sum {offset} != {nstates} states')
    return table


def init_value_decoder_table(nstates, nsymbols, freq, vbits, vbase):
    """Value decoder table. t[state] = (total_bits, value_bits, delta, vbase)."""
    n_clz = _clz32(nstates)
    table = [None] * nstates
    offset = 0
    for i in range(nsymbols):
        f = freq[i]
        if f == 0:
            continue
        offset += f
        if offset > nstates:
            raise DecodeError(f'freq sum {offset} exceeds {nstates} states')
        k = _clz32(f) - n_clz
        j0 = ((2 * nstates) >> k) - f
        for j in range(f):
            if j < j0:
                table[offset - f + j] = (k + vbits[i], vbits[i],
                                         ((f + j) << k) - nstates, vbase[i])
            else:
                table[offset - f + j] = ((k - 1) + vbits[i], vbits[i],
                                         (j - j0) << (k - 1), vbase[i])
    if offset != nstates:
        raise DecodeError(f'freq sum {offset} != {nstates} states')
    return table


# ===========================================================================
# 3. FSE backward bit reader
#    (lzfse_fse.h:283-330 fse_in_checked_init64 / fse_in_checked_flush64 /
#     fse_in_pull64)
# ===========================================================================


class InStream:
    """The 64-bit FSE input stream: bits are consumed most-significant-first
    out of a little-endian accumulator that is refilled from bytes *before*
    the current position (the stream is stored back-to-front)."""

    __slots__ = ('buf', 'pos', 'start', 'accum', 'nbits')

    def __init__(self, buf, end_pos, n, start_pos):
        if not 0 <= start_pos <= end_pos <= len(buf):
            raise DecodeError('FSE stream end position out of range')
        self.buf, self.start = buf, start_pos
        if n:
            if end_pos - 8 < start_pos:
                raise DecodeError('FSE init underflow (needs 8 bytes)')
            self.pos = end_pos - 8
            self.accum = int.from_bytes(buf[self.pos:self.pos + 8], 'little')
            self.nbits = n + 64
        else:
            if end_pos - 7 < start_pos:
                raise DecodeError('FSE init underflow (needs 7 bytes)')
            self.pos = end_pos - 7
            self.accum = int.from_bytes(buf[self.pos:self.pos + 7], 'little')
            self.nbits = n + 56
        if not 56 <= self.nbits < 64:
            raise DecodeError(f'FSE init bad accum_nbits {self.nbits}')
        if self.accum >> self.nbits:
            raise DecodeError('FSE init: encoder left nonzero padding bits')

    def flush(self):
        """fse_in_checked_flush64(): top the accumulator back up to 56..63 bits."""
        nbits = (63 - self.nbits) & -8
        if nbits:
            pos = self.pos - (nbits >> 3)
            if pos < self.start:
                raise DecodeError('FSE flush underflow')
            self.pos = pos
            incoming = int.from_bytes(self.buf[pos:pos + 8], 'little')
            self.accum = ((self.accum << nbits) | (incoming & ((1 << nbits) - 1)))
            self.nbits += nbits

    def pull(self, n):
        """fse_in_pull64(): take the top n bits."""
        self.nbits -= n
        if self.nbits < 0:
            raise DecodeError('FSE pull underflow')
        value = self.accum >> self.nbits
        self.accum &= (1 << self.nbits) - 1
        return value


# ===========================================================================
# 4. Block headers
# ===========================================================================


def decode_v2_header(src, at):
    """lzfse_decode_v1() applied to a v2 header (lzfse_decode_base.c:72-145).

    Returns a dict of v1-equivalent fields plus 'header_size' (the absolute
    end of the header inside `src`).
    """
    if at + 32 > len(src):
        raise DecodeError('bvx2 block header truncated')
    magic, n_raw = struct.unpack_from('<II', src, at)
    if magic != MAGIC_LZFSE_V2:
        raise DecodeError(f'block magic {magic:#010x} is not bvx2')
    v0, v1, v2 = struct.unpack_from('<3Q', src, at + 8)

    def field(v, offset, nbits):
        return (v >> offset) & ((1 << nbits) - 1)

    h = {
        'n_raw': n_raw,
        'n_literals': field(v0, 0, 20),
        'n_literal_payload_bytes': field(v0, 20, 20),
        'n_matches': field(v0, 40, 20),
        'literal_bits': field(v0, 60, 3) - 7,
        'literal_state': [field(v1, 10 * i, 10) for i in range(4)],
        'n_lmd_payload_bytes': field(v1, 40, 20),
        'lmd_bits': field(v1, 60, 3) - 7,
        'header_size': field(v2, 0, 32),
        'l_state': field(v2, 32, 10),
        'm_state': field(v2, 42, 10),
        'd_state': field(v2, 52, 10),
    }
    if at + h['header_size'] > len(src):
        raise DecodeError('bvx2 header_size overruns buffer')

    # Compressed freq tables, Huffman-style, in [at+32, at+header_size).
    freq = [0] * N_FREQ_VALUES
    src_end = at + h['header_size']
    pos = at + 32
    accum, accum_nbits = 0, 0
    if pos != src_end:
        for i in range(N_FREQ_VALUES):
            while pos < src_end and accum_nbits + 8 <= 32:
                accum |= src[pos] << accum_nbits
                accum_nbits += 8
                pos += 1
            value, nbits = _freq_value(accum)
            if nbits > accum_nbits:
                raise DecodeError(f'freq table ran out of bits at symbol {i}')
            freq[i] = value
            accum >>= nbits
            accum_nbits -= nbits
        if accum_nbits >= 8 or pos != src_end:
            raise DecodeError('freq table did not end exactly at header end')

    h['l_freq'] = freq[0:L_SYMBOLS]
    h['m_freq'] = freq[L_SYMBOLS:L_SYMBOLS + M_SYMBOLS]
    h['d_freq'] = freq[L_SYMBOLS + M_SYMBOLS:L_SYMBOLS + M_SYMBOLS + D_SYMBOLS]
    h['literal_freq'] = freq[L_SYMBOLS + M_SYMBOLS + D_SYMBOLS:]
    return h


def decode_v1_header(src, at):
    """A bvx1 block header (lzfse_decode_base.c:396-401). Not emitted by
    Apple's compressor, implemented for completeness."""
    if at + _V1_SIZEOF > len(src):
        raise DecodeError('bvx1 block header truncated')
    magic = struct.unpack_from('<I', src, at)[0]
    if magic != MAGIC_LZFSE_V1:
        raise DecodeError(f'block magic {magic:#010x} is not bvx1')
    # Everything except the freq arrays and literal_state is a scalar, but they
    # are not all the same width: l_state/m_state/d_state are uint16_t sitting
    # immediately before l_freq (lzfse_internal.h:320-330).  Reading them as
    # u32 swallows the neighbour -- d_state would come back as
    # d_state | l_freq[0] << 16 -- so they are read separately.
    h = {k: struct.unpack_from('<I', src, at + off)[0]
         for k, off in _V1.items() if k not in ('magic', 'l_freq', 'm_freq', 'd_freq',
                                                'literal_freq', 'literal_state',
                                                'l_state', 'm_state', 'd_state')}
    for key in ('l_state', 'm_state', 'd_state'):
        h[key] = struct.unpack_from('<H', src, at + _V1[key])[0]
    h['literal_bits'] = struct.unpack_from('<i', src, at + _V1['literal_bits'])[0]
    h['lmd_bits'] = struct.unpack_from('<i', src, at + _V1['lmd_bits'])[0]
    h['literal_state'] = list(struct.unpack_from('<4H', src, at + _V1['literal_state']))
    for key, count in (('l_freq', L_SYMBOLS), ('m_freq', M_SYMBOLS),
                       ('d_freq', D_SYMBOLS), ('literal_freq', LITERAL_SYMBOLS)):
        h[key] = list(struct.unpack_from(f'<{count}H', src, at + _V1[key]))
    h['header_size'] = _V1_SIZEOF
    h['n_raw'] = h['n_raw_bytes']
    return h


def check_header_v1(h):
    """lzfse_check_block_header_v1() (lzfse_internal.h:511-568)."""
    bad = []
    if h['n_literals'] > LITERALS_PER_BLOCK:
        bad.append('n_literals')
    if h['n_matches'] > MATCHES_PER_BLOCK:
        bad.append('n_matches')
    for i, s in enumerate(h['literal_state']):
        if s >= LITERAL_STATES:
            bad.append(f'literal_state[{i}]')
    if h['l_state'] >= L_STATES:
        bad.append('l_state')
    if h['m_state'] >= M_STATES:
        bad.append('m_state')
    if h['d_state'] >= D_STATES:
        bad.append('d_state')
    for name, freq, states in (('l', h['l_freq'], L_STATES),
                               ('m', h['m_freq'], M_STATES),
                               ('d', h['d_freq'], D_STATES),
                               ('literal', h['literal_freq'], LITERAL_STATES)):
        if sum(freq) > states:
            bad.append(f'{name}_freq')
    if bad:
        raise DecodeError('block header sanity check failed: ' + ', '.join(bad))


# ===========================================================================
# 5. bvx1 / bvx2 block body
#    (lzfse_decode_base.c:156-330 lzfse_decode_lmd, :441-508 literal decode)
# ===========================================================================


def decode_lzfse_block(src, at, h, out, out_pos):
    """Decode one bvx1/bvx2 block into `out` at `out_pos`. Returns new out_pos."""
    block_start = out_pos
    lit_table = init_decoder_table(LITERAL_STATES, LITERAL_SYMBOLS, h['literal_freq'])
    l_table = init_value_decoder_table(L_STATES, L_SYMBOLS, h['l_freq'],
                                       L_EXTRA_BITS, L_BASE_VALUE)
    m_table = init_value_decoder_table(M_STATES, M_SYMBOLS, h['m_freq'],
                                       M_EXTRA_BITS, M_BASE_VALUE)
    d_table = init_value_decoder_table(D_STATES, D_SYMBOLS, h['d_freq'],
                                       D_EXTRA_BITS, D_BASE_VALUE)

    # ---- literals: 4 interleaved FSE streams (lzfse_decode_base.c:441-484) --
    n_lit = h['n_literals']
    literals = bytearray(n_lit)
    if n_lit:
        if not h['n_literal_payload_bytes']:
            raise DecodeError('n_literals > 0 but the literal payload is empty')
        lit_end = at + h['header_size'] + h['n_literal_payload_bytes']
        # buf_start is src_begin, i.e. offset 0 of the whole stream.
        stream = InStream(src, lit_end, h['literal_bits'], 0)
        buf, pos, accum, nbits = stream.buf, stream.pos, stream.accum, stream.nbits
        s0, s1, s2, s3 = h['literal_state']
        states = (s0, s1, s2, s3)
        i = 0
        while i < n_lit:
            add = (63 - nbits) & -8
            if add:
                pos -= add >> 3
                if pos < 0:
                    raise DecodeError('literal FSE flush underflow')
                accum = ((accum << add) |
                         (int.from_bytes(buf[pos:pos + 8], 'little') & ((1 << add) - 1)))
                nbits += add
            nxt = list(states)
            for si in range(4):
                k, symbol, delta = lit_table[nxt[si]]
                nbits -= k
                if nbits < 0:
                    raise DecodeError('literal FSE pull underflow')
                bits = accum >> nbits
                accum &= (1 << nbits) - 1
                nxt[si] = (delta + bits) & 0xFFFF
                literals[i + si] = symbol
            states = tuple(nxt)
            i += 4

    # ---- L, M, D (lzfse_decode_base.c:490-508 then :156-330) ----------------
    lmd_start = at + h['header_size'] + h['n_literal_payload_bytes']
    lit_pos = 0
    D = -1
    if h['n_matches']:
        if not h['n_lmd_payload_bytes']:
            raise DecodeError('n_matches > 0 but the LMD payload is empty')
        stream = InStream(src, lmd_start + h['n_lmd_payload_bytes'],
                          h['lmd_bits'], lmd_start)
        buf, pos, accum, nbits = stream.buf, stream.pos, stream.accum, stream.nbits
        l_state, m_state, d_state = h['l_state'], h['m_state'], h['d_state']
        # remaining_bytes is dst_end - dst - 32 in C; we use the block's own
        # n_raw so the "careful path" near the end of a block is exercised.
        remaining = h['n_raw'] - 32
        symbols = h['n_matches']

        while symbols > 0:
            # fse_in_flush before L only; flush2 is a no-op on 64-bit streams.
            add = (63 - nbits) & -8
            if add:
                pos -= add >> 3
                # NOTE: buf_start here is src_begin (0), NOT lmd_start: the C
                # code passes s->src_begin (lzfse_decode_base.c:162,193), so
                # the LMD stream may legitimately reach back into the literal
                # payload and the block header.
                if pos < 0:
                    raise DecodeError('LMD FSE flush underflow')
                accum = ((accum << add) |
                         (int.from_bytes(buf[pos:pos + 8], 'little') & ((1 << add) - 1)))
                nbits += add
            total, vbits, delta, vbase = l_table[l_state]
            nbits -= total
            if nbits < 0:
                raise DecodeError('L FSE pull underflow')
            sv = accum >> nbits
            accum &= (1 << nbits) - 1
            l_state = (delta + (sv >> vbits)) & 0xFFFF
            L = vbase + (sv & ((1 << vbits) - 1))

            total, vbits, delta, vbase = m_table[m_state]
            nbits -= total
            if nbits < 0:
                raise DecodeError('M FSE pull underflow')
            sv = accum >> nbits
            accum &= (1 << nbits) - 1
            m_state = (delta + (sv >> vbits)) & 0xFFFF
            M = vbase + (sv & ((1 << vbits) - 1))

            total, vbits, delta, vbase = d_table[d_state]
            nbits -= total
            if nbits < 0:
                raise DecodeError('D FSE pull underflow')
            sv = accum >> nbits
            accum &= (1 << nbits) - 1
            d_state = (delta + (sv >> vbits)) & 0xFFFF
            new_d = vbase + (sv & ((1 << vbits) - 1))
            if new_d:
                D = new_d
            symbols -= 1

            # ---- ExecuteMatch (lzfse_decode_base.c:221-223) ----------------
            if not 0 <= D <= out_pos + L:
                raise DecodeError(f'match distance {D} > {out_pos + L} bytes of history')
            if lit_pos + L > n_lit:
                raise DecodeError('literal stream exhausted')
            # lzfse_decode_base.c:199
            if lit_pos + L >= LITERALS_PER_BLOCK + 64:
                raise DecodeError('literal pointer out of the block literal buffer')

            if L + M <= remaining:
                remaining -= L + M
                out[out_pos:out_pos + L] = literals[lit_pos:lit_pos + L]
                out_pos += L
                lit_pos += L
                # C: `if (D >= 8 || D >= M) copy(dst, dst - D, M); else byte loop`
                # (lzfse_decode_base.c:237-241).  copy() advances src and dst in
                # lock-step 8 bytes at a time, so for D >= 8 it reproduces the
                # byte-by-byte LZ overlap semantics -- NOT memcpy semantics.
                # A plain slice copy is therefore only valid when the source and
                # destination windows cannot overlap, i.e. D >= M.
                if D >= M:
                    out[out_pos:out_pos + M] = out[out_pos - D:out_pos - D + M]
                else:
                    pattern = bytes(out[out_pos - D:out_pos])
                    whole, rest = divmod(M, D)
                    if whole:
                        out[out_pos:out_pos + whole * D] = pattern * whole
                    if rest:
                        out[out_pos + whole * D:out_pos + M] = pattern[:rest]
                out_pos += M
            else:
                remaining += 32
                if L <= remaining:
                    out[out_pos:out_pos + L] = literals[lit_pos:lit_pos + L]
                    out_pos += L
                    lit_pos += L
                    remaining -= L
                    L = 0
                else:
                    out[out_pos:out_pos + remaining] = literals[lit_pos:lit_pos + remaining]
                    out_pos += remaining
                    lit_pos += remaining
                    L -= remaining
                    raise DecodeError('destination buffer full mid-literal')
                if M <= remaining:
                    for i in range(M):
                        out[out_pos + i] = out[out_pos + i - D]
                    out_pos += M
                    remaining -= M
                    M = 0
                else:
                    raise DecodeError('destination buffer full mid-match')
                remaining -= 32

    # The encoder pads n_literals up to a multiple of 4 with zero bytes
    # (lzfse_encode_base.c:191-196), so sum(L) may be up to 3 short of
    # n_literals. That is not an error.
    if n_lit - lit_pos >= 4:
        raise DecodeError(f'consumed {lit_pos} of {n_lit} literals')
    # The block declares how many raw bytes it carries, and the C decoder is
    # driven to exactly that many: `dst_used > n_raw_bytes` is an error and the
    # outer loop only moves on when the block is finished
    # (lzfse_decode_base.c:604-616).  A block that produced fewer would leave a
    # hole the final length check alone cannot locate.
    produced = out_pos - block_start
    if produced != h['n_raw']:
        raise DecodeError(f'block produced {produced} of {h["n_raw"]} raw bytes')
    return out_pos


# ===========================================================================
# 6. LZVN block (bvxn)
#    (lzvn_decode_base.c:37-660)
# ===========================================================================

# Opcode classes, read off the computed-goto jump table of
# lzvn_decode_base.c:42-99.
_LZVN_SML_D = set(range(0, 6)) | set(range(8, 14)) | set(range(16, 22)) | \
    set(range(24, 30)) | set(range(32, 38)) | set(range(40, 46)) | \
    set(range(48, 54)) | set(range(56, 62)) | set(range(64, 70)) | \
    set(range(72, 78)) | set(range(80, 86)) | set(range(88, 94)) | \
    set(range(96, 102)) | set(range(104, 110)) | set(range(128, 134)) | \
    set(range(136, 142)) | set(range(144, 150)) | set(range(152, 158)) | \
    set(range(192, 198)) | set(range(200, 206))
_LZVN_LRG_D = {7, 15, 23, 31, 39, 47, 55, 63, 71, 79, 87, 95, 103, 111,
               135, 143, 151, 159, 199, 207}
_LZVN_PRE_D = {70, 78, 86, 94, 102, 110, 134, 142, 150, 158, 198, 206}
_LZVN_MED_D = set(range(160, 192))
_LZVN_NOP = {14, 22}
_LZVN_EOS = {6}
_LZVN_UDEF = ({30, 38, 46, 54, 62} | set(range(112, 128)) |
              set(range(208, 224)))
_LZVN_LRG_L = {224}
_LZVN_SML_L = set(range(225, 240))
_LZVN_LRG_M = {240}
_LZVN_SML_M = set(range(241, 256))


def lzvn_decode(src, src_at, src_end, dst, dst_at, dst_end, d_prev, dst_begin):
    """lzvn_decode() over a bounded window. Returns (src_used, dst_used, d_prev,
    end_of_stream).

    ``dst_at``/``dst_end`` bound the *window* this call may fill -- always
    exactly ``n_raw_bytes`` long, exactly like lzfse_decode_base.c:584-588
    does.  ``dst_begin`` is where the whole output buffer starts, which is what
    a match distance is measured against (lzvn_decode_base.c:432: ``D > dst_ptr
    - state->dst_begin``).  The two are not the same thing and the difference
    is load-bearing: a distance may reach back into an *earlier block* of the
    same stream, which is the only reason ``d_prev`` is worth carrying across
    blocks at all.  ``lzfse_decompress`` allocates one buffer per stream and
    appends blocks into it, so it passes 0.

    Only the forward path is implemented.  The C saves a partially expanded
    match in ``state`` when the destination runs out mid-opcode and resumes on
    the next call; we always hand it a window exactly ``n_raw_bytes`` long, so
    that path is unreachable and an over-running opcode is a refusal instead.
    """
    pos, out_pos, D = src_at, dst_at, d_prev
    src_len, dst_len = src_end - src_at, dst_end - dst_at
    if src_len <= 0 or dst_len <= 0:
        return 0, 0, d_prev, False
    L = M = 0
    opc_len = 0

    while True:
        if pos >= src_end:
            return pos - src_at, out_pos - dst_at, D, False
        opc = src[pos]

        if opc in _LZVN_EOS:                                    # eos
            opc_len = 8
            if src_len < opc_len:
                return pos - src_at, out_pos - dst_at, D, False
            pos += opc_len
            src_len -= opc_len
            return pos - src_at, out_pos - dst_at, D, True

        elif opc in _LZVN_SML_D:                                # sml_d
            opc_len = 2
            L = (opc >> 6) & 3
            M = ((opc >> 3) & 7) + 3
            if src_len <= opc_len + L:
                return pos - src_at, out_pos - dst_at, D, False
            D = ((opc & 7) << 8) | src[pos + 1]

        elif opc in _LZVN_LRG_D:                                # lrg_d
            opc_len = 3
            L = (opc >> 6) & 3
            M = ((opc >> 3) & 7) + 3
            if src_len <= opc_len + L:
                return pos - src_at, out_pos - dst_at, D, False
            D = src[pos + 1] | (src[pos + 2] << 8)

        elif opc in _LZVN_MED_D:                                # med_d
            opc_len = 3
            L = (opc >> 3) & 3
            if src_len <= opc_len + L:
                return pos - src_at, out_pos - dst_at, D, False
            opc23 = src[pos + 1] | (src[pos + 2] << 8)
            M = (((opc & 7) << 2) | (opc23 & 3)) + 3
            D = (opc23 >> 2) & 0x3FFF

        elif opc in _LZVN_PRE_D:                                # pre_d
            opc_len = 1
            L = (opc >> 6) & 3
            M = ((opc >> 3) & 7) + 3
            if src_len <= opc_len + L:
                return pos - src_at, out_pos - dst_at, D, False

        elif opc in _LZVN_SML_L:                                # sml_l
            opc_len = 1
            L = opc & 0xF
            M = 0

        elif opc in _LZVN_LRG_L:                                # lrg_l
            opc_len = 2
            if src_len <= 2:
                return pos - src_at, out_pos - dst_at, D, False
            L = src[pos + 1] + 16
            M = 0

        elif opc in _LZVN_SML_M:                                # sml_m
            opc_len = 1
            if src_len <= opc_len:
                return pos - src_at, out_pos - dst_at, D, False
            M = opc & 0xF
            L = 0

        elif opc in _LZVN_LRG_M:                                # lrg_m
            opc_len = 2
            if src_len <= opc_len:
                return pos - src_at, out_pos - dst_at, D, False
            M = src[pos + 1] + 16
            L = 0

        elif opc in _LZVN_NOP:                                  # nop
            opc_len = 1
            if src_len <= opc_len:
                return pos - src_at, out_pos - dst_at, D, False
            pos += 1
            src_len -= 1
            continue

        else:
            raise DecodeError(f'LZVN undefined opcode {opc:#04x} at {pos}')

        # ---- copy_literal_and_match / copy_literal / copy_match ------------
        pos += opc_len
        src_len -= opc_len
        if L:
            # lzvn_decode_base.c:392-397 (copy_literal) and :418-423
            # (copy_literal_and_match): the source must hold the opcode, the
            # whole literal, *and* the first byte of the next opcode, because a
            # valid stream is terminated by an eos token.  sml_d / lrg_d / med_d
            # / pre_d already checked this before extracting the distance;
            # sml_l and lrg_l check it here.  Without it a truncated stream
            # yields a short literal, and assigning a short slice into the
            # middle of a bytearray *shrinks* it -- which would corrupt every
            # later block's offsets rather than refuse.
            if src_len <= L:
                raise DecodeError(f'LZVN source truncated: {src_len} bytes left '
                                  f'where {L} literal bytes plus the next opcode '
                                  f'are needed')
            if L > dst_len:
                raise DecodeError('LZVN destination truncated mid-literal')
            dst[out_pos:out_pos + L] = src[pos:pos + L]
            out_pos += L
            dst_len -= L
            pos += L
            src_len -= L
        if M:
            if D == 0 or D > out_pos - dst_begin:
                raise DecodeError(f'LZVN invalid match distance {D}')
            if M > dst_len:
                raise DecodeError('LZVN destination truncated mid-match')
            # C: `if (dst_len >= M + 7 && D >= 8) 8-byte chunks; else byte loop`
            # (lzvn_decode_base.c:437-449).  The 8-byte path advances src and dst
            # together, so for D >= 8 it is LZ overlap semantics, not memcpy.
            if D >= M:
                dst[out_pos:out_pos + M] = dst[out_pos - D:out_pos - D + M]
            else:
                pattern = bytes(dst[out_pos - D:out_pos])
                whole, rest = divmod(M, D)
                if whole:
                    dst[out_pos:out_pos + whole * D] = pattern * whole
                if rest:
                    dst[out_pos + whole * D:out_pos + M] = pattern[:rest]
            out_pos += M
            dst_len -= M


def decode_lzvn_block(src, at, n_raw, n_payload, out, out_pos, d_prev=0):
    """lzfse_decode_base.c:571-619. Returns (new out_pos, d_prev)."""
    # The opcode stream starts *after* the 12-byte
    # lzvn_compressed_block_header: the C advances s->src past it before it
    # builds dstate (lzfse_decode_base.c:585-586).
    src_end = min(at + LZVN_HEADER_SIZE + n_payload, len(src))
    dst_end = out_pos + n_raw
    src_used, dst_used, d_prev, eos = lzvn_decode(
        src, at + LZVN_HEADER_SIZE, src_end, out, out_pos, dst_end, d_prev, 0)
    if not eos:
        raise DecodeError('LZVN block did not reach end-of-stream')
    # lzfse_decode_base.c:600-616: both the payload and the raw bytes must be
    # exactly consumed.  A block that stops early would otherwise leave
    # whatever `out` was initialised with -- zeros -- in the tail, which is a
    # silently wrong image rather than a refusal.
    if dst_used != n_raw:
        raise DecodeError(f'LZVN block produced {dst_used} of {n_raw} bytes')
    if src_used != n_payload:
        raise DecodeError(f'LZVN block consumed {src_used} of {n_payload} '
                          f'payload bytes')
    return out_pos + n_raw, d_prev


# ===========================================================================
# 7. Whole-stream driver
#    (lzfse_decode_base.c:332-630 lzfse_decode)
# ===========================================================================


def _scan(src, max_output=None):
    """Walk the block chain once, returning (blocks, total_output_size).

    Every block's declared extent is checked against the buffer *here*, not left
    to the final "the chain must land on the end of the buffer" test.  That test
    alone is not enough: a raw block declaring nine bytes over five bytes of
    payload and a four-byte ``bvx$`` consumes the terminator as data and still
    lands exactly on the end -- a silently wrong image of exactly the right
    length.  The C refuses the same input, because it clamps the copy to the
    source that exists and then reports that the stream never terminated
    (lzfse_decode_base.c:541-553).

    ``max_output`` bounds the declared output before anything is allocated: a
    corrupt ``n_raw_bytes`` can otherwise name a multi-gigabyte buffer inside
    the one function whose job is to report the corruption.
    """
    blocks = []
    at, total = 0, 0
    ended = False
    while at < len(src):
        if at + 4 > len(src):
            raise DecodeError('truncated block magic')
        magic = struct.unpack_from('<I', src, at)[0]
        if magic == MAGIC_ENDOFSTREAM:
            at += 4
            ended = True
            break
        if magic == MAGIC_UNCOMPRESSED:
            if at + 8 > len(src):
                raise DecodeError('truncated uncompressed block header')
            n_raw = struct.unpack_from('<I', src, at + 4)[0]
            if at + 8 + n_raw > len(src):
                raise DecodeError('uncompressed block overruns the buffer')
            blocks.append(('raw', at, n_raw))
            at += 8 + n_raw
            total += n_raw
        elif magic == MAGIC_LZVN:
            if at + 12 > len(src):
                raise DecodeError('truncated lzvn block header')
            n_raw, n_payload = struct.unpack_from('<II', src, at + 4)
            if at + 12 + n_payload > len(src):
                raise DecodeError('lzvn block overruns the buffer')
            blocks.append(('lzvn', at, n_raw, n_payload))
            at += 12 + n_payload
            total += n_raw
        elif magic in (MAGIC_LZFSE_V1, MAGIC_LZFSE_V2):
            if at + 32 > len(src):
                raise DecodeError('truncated bvx block header')
            n_raw = struct.unpack_from('<I', src, at + 4)[0]
            if magic == MAGIC_LZFSE_V2:
                header_size = struct.unpack_from('<I', src, at + 24)[0]
                v0, v1 = struct.unpack_from('<2Q', src, at + 8)
                n_lit_payload = (v0 >> 20) & ((1 << 20) - 1)
                n_lmd_payload = (v1 >> 40) & ((1 << 20) - 1)
            else:
                # A v1 header is a plain struct, not the packed v2 one: its
                # payload sizes are u32 fields at their own offsets, and its
                # extent is sizeof() rather than a field read out of the
                # header (lzfse_decode_base.c:396-401).  Reading it with the
                # v2 layout above would walk the chain by the wrong amount --
                # and offset 24 in v1 is n_lmd_payload_bytes, so a block whose
                # match payload is empty would declare a zero-sized header and
                # stall the scan forever.
                header_size = _V1_SIZEOF
                n_lit_payload = struct.unpack_from('<I', src, at + 20)[0]
                n_lmd_payload = struct.unpack_from('<I', src, at + 24)[0]
            if at + header_size > len(src):
                raise DecodeError('bvx header_size overruns buffer')
            if at + header_size + n_lit_payload + n_lmd_payload > len(src):
                raise DecodeError('bvx block payload overruns the buffer')
            if header_size + n_lit_payload + n_lmd_payload == 0:
                # Not a check the C needs: it reads its own sizeof() for v1, and
                # a v2 header_size of 0 would spin its outer loop too.  It is a
                # check a Python decoder needs, because the block chain of a
                # multi-gigabyte image is attacker-controlled input and a block
                # that does not advance is an unbounded loop, not a refusal.
                raise DecodeError(f'block at offset {at} declares a zero extent')
            blocks.append(('v2' if magic == MAGIC_LZFSE_V2 else 'v1', at, n_raw,
                           header_size, n_lit_payload, n_lmd_payload))
            at += header_size + n_lit_payload + n_lmd_payload
            total += n_raw
        else:
            raise DecodeError(f'unknown block magic {magic:#010x} at {at}')
        if max_output is not None and total > max_output:
            raise DecodeError(f'the block chain declares {total} output bytes, '
                              f'more than the {max_output} expected')
    if not ended:
        # lzfse_decode() only returns LZFSE_STATUS_OK on the end-of-stream
        # block; anything else is SRC_EMPTY and lzfse_decode_buffer reports a
        # zero-length failure (lzfse_decode.c:37-41).  A stream that merely runs
        # out of bytes is truncated, not finished.
        raise DecodeError('the block chain has no end-of-stream block')
    if at != len(src):
        raise DecodeError(f'{len(src) - at} trailing bytes after the last block, '
                          f'at offset {at}')
    return blocks, total


def lzfse_decompress(src, progress=None, max_output=None):
    """Decode a whole LZFSE block stream (what `compression_tool -a lzfse`
    reads). Returns the raw bytes."""
    src = bytes(src)
    if not src:
        # lzfse_decode() returns LZFSE_STATUS_SRC_EMPTY for an empty buffer,
        # which lzfse_decode_buffer_with_scratch reports as a 0-byte failure.
        raise DecodeError('empty LZFSE source')
    blocks, total = _scan(src, max_output)
    out = bytearray(total)
    out_pos = 0
    d_prev = 0
    for index, block in enumerate(blocks):
        kind = block[0]
        if kind == 'raw':
            _, at, n_raw = block
            out[out_pos:out_pos + n_raw] = src[at + 8:at + 8 + n_raw]
            out_pos += n_raw
        elif kind == 'lzvn':
            _, at, n_raw, n_payload = block
            out_pos, d_prev = decode_lzvn_block(src, at, n_raw, n_payload,
                                                out, out_pos, d_prev)
        else:
            _, at, n_raw, header_size, n_lit_payload, n_lmd_payload = block
            h = (decode_v2_header(src, at) if kind == 'v2'
                 else decode_v1_header(src, at))
            check_header_v1(h)
            out_pos = decode_lzfse_block(src, at, h, out, out_pos)
        if progress is not None and index % 100 == 0:
            progress(index + 1, len(blocks), out_pos)
    if progress is not None:
        progress(len(blocks), len(blocks), out_pos)
    if out_pos != total:
        raise DecodeError(f'decoded {out_pos} of {total} expected bytes')
    return bytes(out)


def decode(compressed: bytes, expected: int) -> bytes:
    """Decode one LZFSE/LZVN chunk and return exactly ``expected`` bytes.

    This is the contract ``unipad.host.udif.LzfseDecoder`` calls: the compressed
    bytes of one UDIF chunk and the number of bytes it expands to, which for a
    chunk is ``sector_count * 512``.  Anything that does not add up -- a stream
    that produces fewer or more bytes than the image declares, a truncated
    stream, an unknown block magic -- is a :class:`DecodeError`, because an
    image that decoded wrong is indistinguishable from one that decoded right
    until the GPT CRC is checked, and by then 6 GB has been written.
    """
    if expected < 0:
        raise DecodeError(f'expected size {expected} is negative')
    produced = lzfse_decompress(compressed, max_output=expected)
    if len(produced) != expected:
        raise DecodeError(f'the stream produced {len(produced)} bytes, '
                          f'not the {expected} the image declares')
    return produced
