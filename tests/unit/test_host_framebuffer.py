"""Tests for the guest-framebuffer transport: bytes in, a PPM the surface blits.

The governing rule in this project is that **a guard test that cannot go red
where the defect lives is worse than no test**, so every test here is written
against a defect it can actually see, and the docstrings name the defect rather
than describing the assertion.

The fixture is the thing that makes that possible.  It is a 72-byte slice of a
frame acquired from a real booted guest on 2026-11-01 by QMP ``pmemsave``, and
the expected output beside it was **not** produced by this module: it was read
out of QMP ``screendump`` for the same frame, which on this QEMU writes a P6
PPM despite its ``.png`` name.  So the expectation is an independent
instrument's answer, and a conversion that is wrong in any way -- a swapped
channel, the fourth byte read as a colour, a row walked at the wrong stride --
disagrees with QEMU rather than merely disagreeing with itself.  That matters
more here than anywhere else in the suite, because the whole module is a
conversion and a test that checks a conversion against its own output proves
nothing.

The bytes are embedded as a literal on purpose.  The frame they came from lives
under ``local-private/tmp/framebuffer-acquire-evidence/``, which is gitignored
and absent from a clean checkout; a test that opened it would fail on a fresh
clone before reaching a single assertion, which is the exact failure
``test_clean_checkout_selfsufficiency.py`` exists to catch.  Nothing in this
file touches the filesystem at all.

Three rows of the real frame were chosen for what they contain, because a frame
of one colour cannot catch a channel swap and a frame of greys cannot catch an
orientation error:

* rows 0 and 37 are the green bar the bootloader printed, whose fourth byte is
  ``0xFF`` -- the bar is 30,400 of the 480,000 pixels that carry it;
* row 316 column 279 is black, whose fourth byte is ``0x00``;
* row 316 columns 280-282 are the orange accent, whose three colour channels
  are all different, so a swap of any two of them is visible in one pixel.

The eight ``0xEE`` bytes between rows are this test's own, not the guest's: the
acquired frame's stride (3200) is exactly its row width (800 x 4) and carries
no padding, so the padding path cannot be validated against it.  Those bytes
are poison -- if a row is walked at ``width * 4`` instead of ``stride``, or if
padding is copied into the output, ``0xEE`` appears where no real pixel has it.
"""

from __future__ import annotations

from typing import Any

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.framebuffer import (
    BYTES_PER_PIXEL,
    FrameGeometry,
    GuestFrameSource,
    bgra_to_ppm,
    read_guest_frame,
)
from unipad.host.winsurface import FrameSource, ppm_to_bgr, read_ppm_header_bytes

#: The base the guest's own boot log printed:
#: ``video    base 0x1BC7A0000  800x600  row 3200 bytes  32 bpp``.
FB_BASE = 0x1BC7A0000

# --- the real frame, embedded so a clean checkout needs nothing on disk ---

#: Columns 279-282 of the guest's 800-wide framebuffer, from rows 0, 37 and 316
#: -- the green bar, the bar again, and the first row of text.  Provenance:
#: ``local-private/tmp/framebuffer-acquire-evidence/fb.bin``.
REAL_FRAME_BGRA = (
    b"\x00\xff\x00\xff\x00\xff\x00\xff\x00\xff\x00\xff\x00\xff\x00\xff"
    b"\xee\xee\xee\xee\xee\xee\xee\xee"
    b"\x00\xff\x00\xff\x00\xff\x00\xff\x00\xff\x00\xff\x00\xff\x00\xff"
    b"\xee\xee\xee\xee\xee\xee\xee\xee"
    b"\x00\x00\x00\x00\xd7\x5b\x2a\x00\xd7\x5b\x2a\x00\xd7\x5b\x2a\x00"
    b"\xee\xee\xee\xee\xee\xee\xee\xee"
)

#: The same pixels as QMP ``screendump`` rendered them, R-G-B, with no padding
#: -- a P6 payload has none.  Provenance:
#: ``local-private/tmp/framebuffer-acquire-evidence/screendump.png``.
#: Row 2 is black, then the orange accent three times -- the guest holds the
#: accent as B=0xd7, G=0x5b, R=0x2a, which is why the PPM reads 0x2a,0x5b,0xd7.
REAL_FRAME_PPM_PAYLOAD = (
    b"\x00\xff\x00\x00\xff\x00\x00\xff\x00\x00\xff\x00"
    b"\x00\xff\x00\x00\xff\x00\x00\xff\x00\x00\xff\x00"
    b"\x00\x00\x00\x2a\x5b\xd7\x2a\x5b\xd7\x2a\x5b\xd7"
)

#: Four columns, at a stride eight bytes wider than the row, so the padding
#: path is exercised by the real pixels rather than by a synthetic buffer.
REAL_WIDTH = 4
REAL_HEIGHT = 3
REAL_STRIDE = REAL_WIDTH * BYTES_PER_PIXEL + 8


def _real_geometry(**overrides: Any) -> FrameGeometry:
    fields: dict[str, Any] = {"base": FB_BASE, "width": REAL_WIDTH,
                              "height": REAL_HEIGHT, "stride": REAL_STRIDE}
    fields.update(overrides)
    return FrameGeometry(**fields)


class RecordingMemory:
    """A stand-in for :class:`GuestMemory` that records what it was asked for.

    Matching ``read(physical, size)`` positionally, because that is the
    signature production calls and a stub with a different shape would pass
    while proving nothing about the real call.
    """

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)
        self.calls: list[tuple[int, int]] = []

    def read(self, physical: int, size: int) -> bytes:
        self.calls.append((physical, size))
        if size <= 0:
            return b""
        return self._data[:size]


class TruncatingMemory:
    """A ``GuestMemory`` that returns one byte less than it was asked for.

    ``GuestMemory.read`` refuses in this case, so the real object cannot
    produce it -- which is exactly why :func:`read_guest_frame` checks the
    length itself.  Without that check the promise "exactly ``stride * height``
    bytes" would rest on a collaborator's behaviour rather than on this
    module's, and any other object with a ``read`` method -- a stub, a future
    transport -- could hand back a short frame that was silently truncated.
    """

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)

    def read(self, physical: int, size: int) -> bytes:
        return self._data[:max(0, size - 1)]


def present_first_frame(source: Any) -> bytes:
    """A consumer typed against ``FrameSource`` -- it knows ``grab()`` and nothing else.

    Deliberately not written against :class:`GuestFrameSource`: the claim under
    test is that a caller holding the protocol can drive this object, and a
    consumer that reached for a method only this class has would pass even if
    ``grab`` were renamed.
    """
    return source.grab()


# --- the conversion, against QEMU's own answer for the same frame ---


def test_a_real_guest_frame_becomes_the_pixels_qemu_itself_drew() -> None:
    """The whole document equals what QMP ``screendump`` produced for this frame.

    This is the test that would go red on every conversion defect at once: a
    swapped channel, the fourth byte read as a colour, a row walked at the
    wrong stride, padding copied into the output, or a payload of the wrong
    length.  It is written against an independent instrument rather than
    against this module's own output, because a conversion checked against
    itself cannot fail.
    """
    ppm = bgra_to_ppm(REAL_FRAME_BGRA, width=REAL_WIDTH, height=REAL_HEIGHT,
                      stride=REAL_STRIDE)
    expected = b"P6\n%d %d\n255\n" % (REAL_WIDTH, REAL_HEIGHT) + REAL_FRAME_PPM_PAYLOAD
    assert ppm == expected


def test_the_real_accent_pixel_keeps_its_channels_in_order() -> None:
    """The orange accent names the defect a grey pixel would hide.

    The guest holds it as B=0xd7, G=0x5b, R=0x2a; the PPM must hold R, G, B.
    The green bar cannot catch a swap -- its red and blue are both zero -- and
    the black pixel cannot catch anything, so this is the pixel the channel
    order is actually proven on.  A test that only asserted "some bytes came
    out" would pass with any of the six orderings.
    """
    header = read_ppm_header_bytes(
        bgra_to_ppm(REAL_FRAME_BGRA, width=REAL_WIDTH, height=REAL_HEIGHT,
                    stride=REAL_STRIDE))
    payload_start = header["header_bytes"]
    ppm = bgra_to_ppm(REAL_FRAME_BGRA, width=REAL_WIDTH, height=REAL_HEIGHT,
                      stride=REAL_STRIDE)[payload_start:]
    # Row 2, third pixel: the accent, at offset (2 rows * 4 + 2) * 3 bytes.
    accent = ppm[(2 * REAL_WIDTH + 2) * 3:(2 * REAL_WIDTH + 3) * 3]
    assert accent == bytes((0x2A, 0x5B, 0xD7))
    # Row 0, first pixel: the green bar, whose red and blue are both zero.
    assert ppm[0:3] == bytes((0x00, 0xFF, 0x00))


def test_the_channel_order_is_exact_on_values_no_swap_can_mimic() -> None:
    """R, G, B in, R, G, B out -- asserted byte for byte, not by length.

    The four channels are given four different values so that every wrong
    ordering produces a different document: swapping red and blue, swapping
    green with either neighbour, or emitting the fourth byte as a colour all
    fail here.  A frame whose channels happened to be equal would pass under
    all of them.
    """
    red, green, blue, alpha = 0x11, 0x22, 0x33, 0x44
    # Laid out the way the guest lays a pixel out: blue, green, red, then a
    # byte that is not a colour channel.
    framebuffer = bytes((blue, green, red, alpha))
    ppm = bgra_to_ppm(framebuffer, width=1, height=1, stride=BYTES_PER_PIXEL)
    assert ppm == b"P6\n1 1\n255\n" + bytes((red, green, blue))


def test_the_same_rgb_with_a_different_alpha_is_the_same_frame() -> None:
    """The measured fact that decides the module's shape, as a property.

    The fourth byte is not constant: 450,000 of the 480,000 pixels in the real
    frame carry ``0x00`` there and the 30,000 in the green bar carry ``0xFF``.
    So a converter that read it as a colour channel would not produce a
    uniformly wrong frame -- it would produce a frame whose 93.75% is wrong in
    one way and whose bar is wrong in another, which is the kind of defect a
    single-pixel value check can miss.  Asserting the property instead: same
    three colour channels, same output, whatever the fourth byte holds.
    """
    in_the_bar = bytes((0x00, 0xFF, 0x00, 0xFF))
    elsewhere = bytes((0x00, 0xFF, 0x00, 0x00))
    assert (bgra_to_ppm(in_the_bar, width=1, height=1, stride=BYTES_PER_PIXEL)
            == bgra_to_ppm(elsewhere, width=1, height=1, stride=BYTES_PER_PIXEL))


def test_padding_between_rows_is_dropped_not_presented() -> None:
    """A stride wider than the row is walked, not assumed away.

    ``0xEE`` is poison: no real pixel in this frame holds it, so if the
    conversion walked rows at ``width * 4`` instead of ``stride``, or copied
    the padding into the output, it would appear in the result.  Each row is a
    different colour so a row that is read from the wrong offset is also
    visible, not just the padding.
    """
    width, height, pad = 3, 3, 5
    stride = width * BYTES_PER_PIXEL + pad
    rows = [bytes((0x10, 0x20, 0x30, 0xFF)),
            bytes((0x40, 0x50, 0x60, 0x00)),
            bytes((0x70, 0x80, 0x90, 0xFF))]
    framebuffer = b"".join(row * width + b"\xee" * pad for row in rows)
    ppm = bgra_to_ppm(framebuffer, width=width, height=height, stride=stride)
    header = read_ppm_header_bytes(ppm)
    payload = ppm[header["header_bytes"]:]
    assert len(payload) == width * height * 3
    assert b"\xee" not in payload
    assert payload == (bytes((0x30, 0x20, 0x10)) * width
                       + bytes((0x60, 0x50, 0x40)) * width
                       + bytes((0x90, 0x80, 0x70)) * width)


def test_bytes_beyond_the_geometry_are_ignored_not_refused() -> None:
    """A buffer longer than the geometry declares is not an error.

    The policy :func:`unipad.host.winsurface.ppm_to_bgr` already applies to a
    padded producer, restated here so the two cannot drift: the extra bytes are
    simply not read.  It is also what makes a sub-rectangle viewable -- hand in
    a whole framebuffer with the geometry of the region of interest.
    """
    padded = REAL_FRAME_BGRA + b"\xaa" * 64
    assert (bgra_to_ppm(padded, width=REAL_WIDTH, height=REAL_HEIGHT,
                        stride=REAL_STRIDE)
            == bgra_to_ppm(REAL_FRAME_BGRA, width=REAL_WIDTH, height=REAL_HEIGHT,
                           stride=REAL_STRIDE))


def test_a_buffer_one_byte_short_is_refused_rather_than_truncated() -> None:
    """A frame whose bytes run out is a refusal, not a frame with the bottom cut off.

    Padding would produce a document of the right length whose tail is whatever
    was in the buffer, and no caller could tell those pixels from real ones --
    the same reason ``ppm_to_bgr`` refuses a short payload.  The detail fields
    are asserted because a caller branches on the code, and a refusal that
    names neither the declared nor the present size cannot be acted on.
    """
    with pytest.raises(UniPadError) as caught:
        bgra_to_ppm(REAL_FRAME_BGRA[:-1], width=REAL_WIDTH, height=REAL_HEIGHT,
                    stride=REAL_STRIDE)
    assert caught.value.code == ErrorCode.RENDER_FAILED
    assert caught.value.details["declared"] == REAL_STRIDE * REAL_HEIGHT
    assert caught.value.details["present"] == len(REAL_FRAME_BGRA) - 1


# --- the read, with no QEMU anywhere near it ---


def test_read_guest_frame_asks_for_one_read_of_the_whole_framebuffer() -> None:
    """One read of exactly ``stride * height`` bytes, from the base given.

    The size is the contract the guest was measured against -- 1,920,000 bytes
    for the 800x600 frame -- and the single call matters as much as the size:
    ``GuestMemory`` holds one QMP client for the session, so a row at a time
    would be one round trip per row on a connection that is slow to take the
    next.  An off-by-one in either direction changes what the guest is asked
    for and produces a frame that is silently the wrong shape.
    """
    memory = RecordingMemory(REAL_FRAME_BGRA)
    data = read_guest_frame(memory, base=FB_BASE, width=REAL_WIDTH,
                            height=REAL_HEIGHT, stride=REAL_STRIDE)
    assert memory.calls == [(FB_BASE, REAL_STRIDE * REAL_HEIGHT)]
    assert data == REAL_FRAME_BGRA


def test_read_guest_frame_refuses_when_the_guest_returns_less_than_asked() -> None:
    """A short read is a refusal, not a short frame.

    ``GuestMemory.read`` already refuses in this case, so the real transport
    cannot produce it -- which is why this uses a stub.  The check under test is
    :func:`read_guest_frame`'s own: without it the promise of exactly
    ``stride * height`` bytes would rest on the collaborator's behaviour, and
    an object with a ``read`` method that returned less would hand the caller a
    truncated frame with nothing to show it had happened.
    """
    with pytest.raises(UniPadError) as caught:
        read_guest_frame(TruncatingMemory(REAL_FRAME_BGRA), base=FB_BASE,
                         width=REAL_WIDTH, height=REAL_HEIGHT, stride=REAL_STRIDE)
    assert caught.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert caught.value.details["asked"] == REAL_STRIDE * REAL_HEIGHT
    assert caught.value.details["returned"] == REAL_STRIDE * REAL_HEIGHT - 1


def test_a_stride_shorter_than_one_row_of_pixels_is_refused() -> None:
    """Rows that overlap are a corrupt geometry, not a layout.

    Nothing in the length check can see this: a stride of ``width * 4 - 1``
    over three rows still asks for fewer bytes than the buffer holds, and the
    frame it produces is a sheared image with no error anywhere.  Refusing is
    the only way it becomes visible.
    """
    with pytest.raises(UniPadError) as caught:
        bgra_to_ppm(REAL_FRAME_BGRA, width=REAL_WIDTH, height=REAL_HEIGHT,
                    stride=REAL_WIDTH * BYTES_PER_PIXEL - 1)
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_a_non_positive_dimension_is_refused() -> None:
    """A framebuffer with no rows, or no columns, is not an empty frame."""
    for width, height in ((0, REAL_HEIGHT), (REAL_WIDTH, 0), (-1, REAL_HEIGHT)):
        with pytest.raises(UniPadError) as caught:
            bgra_to_ppm(REAL_FRAME_BGRA, width=width, height=height,
                        stride=REAL_STRIDE)
        assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_a_negative_base_is_refused() -> None:
    """Guest physical addresses are unsigned; a negative one is a caller's bug."""
    with pytest.raises(UniPadError) as caught:
        read_guest_frame(RecordingMemory(REAL_FRAME_BGRA), base=-1,
                         width=REAL_WIDTH, height=REAL_HEIGHT, stride=REAL_STRIDE)
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_an_impossible_geometry_is_refused_where_it_is_constructed() -> None:
    """``FrameGeometry`` validates itself, so the bad value is caught at its source.

    The functions each check the geometry too -- a caller can pass the four
    numbers as keywords and skip the dataclass entirely -- but a geometry that
    survives construction and fails at the first read is one the caller built
    and then had to debug two steps later.
    """
    with pytest.raises(UniPadError) as caught:
        _real_geometry(stride=1)
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert _real_geometry().size == REAL_STRIDE * REAL_HEIGHT


# --- the two halves, tied together ---


def test_the_frame_source_hands_the_surface_a_document_it_accepts() -> None:
    """The whole path, against the consumer that was already validated.

    ``SurfaceWindow.present`` takes a PPM and :func:`ppm_to_bgr` is what turns
    it into the window's B-G-R.  So the frame is pushed through that converter
    rather than through a private expectation: if the surface ever stopped
    accepting what this module produces, this test fails rather than the window
    silently showing nothing.

    The consumer is :func:`present_first_frame`, which knows ``grab()`` and
    nothing else, so a caller typed against the protocol can drive this object
    -- and the protocol base is asserted on the class rather than assumed.
    """
    memory = RecordingMemory(REAL_FRAME_BGRA)
    source = GuestFrameSource(memory, _real_geometry())
    assert FrameSource in GuestFrameSource.__mro__

    frame = present_first_frame(source)
    assert memory.calls == [(FB_BASE, REAL_STRIDE * REAL_HEIGHT)]
    assert source.frames == 1

    header = read_ppm_header_bytes(frame)
    assert (header["width"], header["height"]) == (REAL_WIDTH, REAL_HEIGHT)
    assert header["maxval"] == 255
    width, height, rows = ppm_to_bgr(frame)
    assert (width, height) == (REAL_WIDTH, REAL_HEIGHT)
    # ppm_to_bgr returns GDI's B-G-R, which is the frame's R-G-B with the outer
    # two channels exchanged -- the orange accent comes back as 0xd7,0x5b,0x2a.
    expected = bytearray(REAL_FRAME_PPM_PAYLOAD)
    expected[0::3] = REAL_FRAME_PPM_PAYLOAD[2::3]
    expected[1::3] = REAL_FRAME_PPM_PAYLOAD[1::3]
    expected[2::3] = REAL_FRAME_PPM_PAYLOAD[0::3]
    assert rows == bytes(expected)
    assert rows[(2 * REAL_WIDTH + 2) * 3:(2 * REAL_WIDTH + 3) * 3] == bytes(
        (0xD7, 0x5B, 0x2A))


def test_every_grab_rereads_the_guest_rather_than_replaying_the_last_frame() -> None:
    """A cached frame is a still image that claims to be current.

    The guest's framebuffer is live memory, so the only honest answer to
    ``grab()`` is what the guest holds now.  A source that returned its first
    frame forever would look identical on a static framebuffer -- which is what
    this guest has, per fact 198 -- and would start lying the moment the guest
    drew anything, with nothing in the window to show it.
    """
    memory = RecordingMemory(REAL_FRAME_BGRA)
    source = GuestFrameSource(memory, _real_geometry())
    first = source.grab()
    second = source.grab()
    assert first == second
    assert memory.calls == [(FB_BASE, REAL_STRIDE * REAL_HEIGHT)] * 2
    assert source.frames == 2
