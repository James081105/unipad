"""Tests for the Windows surface: a real window, and a readback that proves it.

The interesting thing about testing a window is that most of what could be
wrong with one is invisible to the usual assertions.  ``CreateWindowExW``
returning a handle proves a window exists; it does not prove a pixel arrived.
``StretchDIBits`` returning a non-zero count proves GDI accepted the call; it
does not prove the frame's bytes are in the bitmap.  And a frame presented
upside down is byte-identical to a frame presented correctly, so no comparison
of the two documents can tell them apart.

So these tests use :meth:`SurfaceWindow.displayed_pixel` -- what the window's
own device context holds -- as the instrument, and the first thing they do is
validate that instrument on a frame whose answer is known.  That is the same
rule the QMP client needed when a missing ``qmp_capabilities`` made ``pmemsave``
look absent for days: check the tool against a known-present answer before
believing a negative.

The first version of this suite used :meth:`SurfaceWindow.readback` instead, and
a mutation run told us exactly why that was not enough.  ``readback`` returns
the DIB section's memory, which ``memmove`` wrote -- so it is correct even when
``StretchDIBits`` was never called, and it is orientation-blind, because
flipping a bottom-up DIB flips how GDI *reads* the same bytes rather than the
bytes themselves.  Two mutations survived it: "present reports success without
blitting" and "the DIB is bottom-up, so every frame is presented upside down".
Both are now caught, and both are recorded in the test docstrings so the next
reader does not reintroduce them.

Every test that touches a window creates one and closes it, because a leaked
window is a leaked desktop resource and the suite runs thousands of times.
"""

from __future__ import annotations

import sys

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.winsurface import (
    BYTES_PER_PIXEL,
    SurfaceWindow,
    ppm_to_bgr,
    read_ppm_header_bytes,
    solid_frame,
)

#: A geometry small enough that creating dozens of these in a suite is cheap,
#: and odd enough (not a power of two) that an off-by-one in the row stride
#: shows up rather than hiding behind alignment.
WIDTH = 13
HEIGHT = 7

#: The window the fixture creates.  ``WIDTH * 16`` is 208, which is above the
#: minimum tracking size Windows enforces on a window with a caption -- a window
#: asked to be narrower is silently clamped, so ``client_size()`` comes back
#: wider than requested and the resize test proves nothing.  Measured: a request
#: for 52x28 comes back 120x28.
SURF_W = WIDTH * 16
SURF_H = HEIGHT * 16

#: A second geometry, for the frames that change size mid-test.  Also above the
#: minimum, and a different aspect ratio from :data:`SURF_W`/:data:`SURF_H` so a
#: swap of the two dimensions is visible.
BIG_W = WIDTH * 24
BIG_H = HEIGHT * 20

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="a Windows surface needs Windows",
)


def _frame_with_rows(width: int, height: int,
                     top: tuple[int, int, int],
                     bottom: tuple[int, int, int]) -> bytes:
    """A P6 PPM whose first and last rows are different colours.

    This is the shape that catches an upside-down blit.  A solid frame cannot:
    flipping it produces the same document.
    """
    rows = [bytes(top) * width, bytes(bottom) * width]
    middle = [bytes((0, 0, 0)) * width for _ in range(max(0, height - 2))]
    return b"P6\n%d %d\n255\n" % (width, height) + b"".join(rows[:1] + middle + rows[1:])


# --- the pure conversion, which needs no window at all ---


def test_the_channel_swap_is_the_only_change() -> None:
    """RGB becomes BGR and nothing else moves.

    GDI's 24-bit DIB is B-G-R; a P6 PPM is R-G-B.  The conversion is a swap of
    the first and third bytes, so a frame of one colour must come back with the
    same length and the same middle byte, and only the outer two exchanged.
    """
    frame = solid_frame(WIDTH, HEIGHT, (0x11, 0x22, 0x33))
    width, height, rows = ppm_to_bgr(frame)
    assert (width, height) == (WIDTH, HEIGHT)
    assert len(rows) == WIDTH * HEIGHT * BYTES_PER_PIXEL
    assert len(rows) == len(frame) - len(b"P6\n%d %d\n255\n" % (WIDTH, HEIGHT))
    assert rows[0] == 0x33 and rows[1] == 0x22 and rows[2] == 0x11


def test_a_short_payload_is_refused_rather_than_padded() -> None:
    """A frame whose bytes run out is a refusal, not a frame with invented pixels.

    Padding would produce a document of the right length whose tail is
    whatever was in the buffer, and no caller could tell those pixels from real
    ones.  Refusing is the only honest option.
    """
    header = b"P6\n%d %d\n255\n" % (WIDTH, HEIGHT)
    truncated = header + b"\x00" * (WIDTH * HEIGHT * BYTES_PER_PIXEL - 1)
    with pytest.raises(UniPadError) as caught:
        ppm_to_bgr(truncated)
    assert caught.value.code == ErrorCode.RENDER_FAILED


def test_trailing_bytes_are_dropped_not_presented() -> None:
    """A producer may pad a row; the surface does not blit the padding.

    Dropping rather than refusing because a padded producer is a real shape,
    and refusing it would make a working source unusable.  But the extra bytes
    must not become a partial extra row.
    """
    header = b"P6\n%d %d\n255\n" % (WIDTH, HEIGHT)
    padded = header + b"\x00" * (WIDTH * HEIGHT * BYTES_PER_PIXEL + 5)
    width, height, rows = ppm_to_bgr(padded)
    assert (width, height) == (WIDTH, HEIGHT)
    assert len(rows) == WIDTH * HEIGHT * BYTES_PER_PIXEL


def test_a_non_ppm_document_is_refused_with_its_magic() -> None:
    """The refusal names what it saw, so a caller can tell a bad frame from a
    truncated one without guessing.

    The magic is in ``details`` rather than the message because the message is
    the operator-facing sentence and the magic is the machine-readable fact --
    the same split :mod:`unipad.host.logs` uses for the same refusal.
    """
    with pytest.raises(UniPadError) as caught:
        ppm_to_bgr(b"P5\n13 7\n255\n" + b"\x00" * 91)
    assert caught.value.code == ErrorCode.RENDER_FAILED
    # The detail is the first 16 bytes, the same slice logs.py reports, so it
    # carries the dimension line too -- enough to tell "wrong magic" from
    # "right magic, wrong document".
    assert caught.value.details["magic"].startswith("P5")


def test_the_bytes_parser_and_the_file_parser_agree() -> None:
    """``read_ppm_header_bytes`` must not drift from ``read_ppm_header``.

    The file parser is what accepts a ``screendump``; this one is what accepts a
    frame held in memory.  If they accept different documents, the surface shows
    frames the rest of the project would have refused -- and the divergence is
    silent because nothing compares the two.
    """
    import tempfile
    from pathlib import Path

    from unipad.host.logs import read_ppm_header

    frame = solid_frame(WIDTH, HEIGHT, (1, 2, 3))
    from_bytes = read_ppm_header_bytes(frame)
    assert (from_bytes["width"], from_bytes["height"]) == (WIDTH, HEIGHT)
    # And the file parser accepts the same bytes written to disk.
    with tempfile.TemporaryDirectory(prefix="unipad-surface-") as raw:
        target = Path(raw) / "frame.ppm"
        target.write_bytes(frame)
        from_file = read_ppm_header(target)
    assert from_file["width"] == from_bytes["width"]
    assert from_file["height"] == from_bytes["height"]
    assert from_file["header_bytes"] == from_bytes["header_bytes"]


def test_a_maxval_above_255_is_refused() -> None:
    """A 16-bit PPM cannot be shown on a 24-bit surface, and saying so is better
    than scaling it silently into colours nobody asked for."""
    frame = b"P6\n%d %d\n65535\n" % (WIDTH, HEIGHT) + b"\x00" * (
        WIDTH * HEIGHT * 3 * 2)
    with pytest.raises(UniPadError) as caught:
        ppm_to_bgr(frame)
    assert caught.value.code == ErrorCode.RENDER_FAILED


# --- a real window ---


@pytest.fixture()
def surface():
    """One real window per test, closed on the way out."""
    window = SurfaceWindow("unipad-test", width=SURF_W, height=SURF_H)
    try:
        yield window
    finally:
        window.close()


@windows_only
def test_a_window_is_actually_created(surface: SurfaceWindow) -> None:
    """A handle is not readiness, but its absence is not a window either."""
    assert surface.hwnd is not None
    assert surface.hwnd > 0


@windows_only
def test_the_window_shows_the_frame_it_was_given(surface: SurfaceWindow) -> None:
    """The positive control: a known frame must be readable back from the WINDOW.

    ``readback`` reads the DIB section's memory, and that memory is written by
    ``memmove`` -- so it is correct even when ``StretchDIBits`` was never called.
    A mutation run proved it: "present reports success without blitting"
    survived a suite that only read the DIB back, because the DIB held the right
    bytes while the window showed nothing.

    ``displayed_pixel`` reads the window's device context, which is what an
    operator sees, so it is the only instrument that can catch an unblitted
    frame.
    """
    result = surface.present(solid_frame(SURF_W, SURF_H, (0xFF, 0x00, 0x00)))
    assert result["blitted"] is True
    # BGR in the DIB, but COLORREF is 0x00BBGGRR, so both spell pure red.
    assert surface.displayed_pixel(0, 0) == (0xFF, 0x00, 0x00)
    assert surface.displayed_pixel(SURF_W - 1, SURF_H - 1) == (0xFF, 0x00, 0x00)
    # And the DIB agrees, which is what makes a later mismatch diagnosable.
    back = surface.readback()
    assert len(back) == SURF_W * SURF_H * BYTES_PER_PIXEL
    assert back[0] == 0x00 and back[1] == 0x00 and back[2] == 0xFF


@windows_only
def test_an_off_window_coordinate_is_a_refusal_not_black(
    surface: SurfaceWindow,
) -> None:
    """Black is a real pixel; an off-window coordinate is not one.

    Returning ``(0, 0, 0)`` for an out-of-range coordinate would make a
    coordinate bug look like a black frame, and the two need different fixes.
    """
    assert surface.displayed_pixel(-1, 0) is None
    assert surface.displayed_pixel(0, -1) is None
    assert surface.displayed_pixel(SURF_W, 0) is None
    assert surface.displayed_pixel(0, SURF_H) is None


@windows_only
def test_the_frame_is_not_presented_upside_down(surface: SurfaceWindow) -> None:
    """The row order must survive the blit, and only the window can say so.

    A top-down frame blitted bottom-up is byte-identical to a correct one in the
    DIB: flipping a bottom-up DIB flips how GDI *reads* the same bytes, not the
    bytes.  So ``readback`` cannot catch it -- and a mutation run confirmed that
    "the DIB is bottom-up" survived a suite that only read the DIB back.

    ``displayed_pixel`` asks the window which row it put where, so the upside
    down case is visible.  ``biHeight`` is negative precisely so GDI reads the
    rows in the order the PPM wrote them.
    """
    frame = _frame_with_rows(SURF_W, SURF_H, (0xAA, 0xBB, 0xCC),
                             (0x11, 0x22, 0x33))
    assert surface.present(frame)["blitted"] is True
    assert surface.displayed_pixel(0, 0) == (0xAA, 0xBB, 0xCC), (
        "the frame's first row is not at the top of the window")
    assert surface.displayed_pixel(0, SURF_H - 1) == (0x11, 0x22, 0x33), (
        "the frame's last row is not at the bottom of the window")


@windows_only
def test_a_second_frame_replaces_the_first(surface: SurfaceWindow) -> None:
    """Presenting twice must overwrite, not accumulate.

    A surface that kept the first frame's bytes would show a ghost of it after
    any change, and the only way to see that is to present two different frames
    and ask the window which one it is showing.
    """
    geometry = (SURF_W, SURF_H)
    assert surface.present(solid_frame(*geometry, (0xFF, 0xFF, 0xFF)))["blitted"]
    assert surface.displayed_pixel(0, 0) == (0xFF, 0xFF, 0xFF)
    assert surface.present(solid_frame(*geometry, (0x00, 0x00, 0x00)))["blitted"]
    assert surface.displayed_pixel(0, 0) == (0x00, 0x00, 0x00)


@windows_only
def test_a_geometry_change_rebuilds_the_surface(surface: SurfaceWindow) -> None:
    """A frame of a different size gets its own DIB, and the old one is released."""
    first = surface.present(solid_frame(SURF_W, SURF_H, (1, 2, 3)))
    assert first["blitted"] is True
    second = surface.present(solid_frame(SURF_W, SURF_H, (4, 5, 6)))
    assert second["blitted"] is True
    assert second["width"] == SURF_W and second["height"] == SURF_H
    assert len(surface.readback()) == SURF_W * SURF_H * BYTES_PER_PIXEL
    assert surface.displayed_pixel(0, 0) == (4, 5, 6)


@windows_only
def test_the_window_grows_to_the_frame_it_is_given(surface: SurfaceWindow) -> None:
    """The window follows the frame; the frame is not scaled into the window.

    This is a defect this module's own instrument found.  The first version
    resized only the DIB section when the frame's geometry changed, so
    ``self.width`` grew to the frame's size while the client area stayed at the
    size the window was created with.  The consequence was measured against a
    real 1112x834 guest screendump: four of five sampled pixels matched and the
    bottom-right one returned ``None`` -- the bounds check passed for a
    coordinate the real client area did not have.

    Resizing the window rather than scaling the frame is deliberate: a viewer
    that silently rescales is showing the operator pixels the guest never drew.

    ``client_size`` is the instrument, because ``self.width`` is what was
    *requested* and this is what Windows actually delivered.
    """
    frame_size = (SURF_W, SURF_H)
    oversized = (BIG_W, BIG_H)
    assert surface.present(solid_frame(*frame_size, (1, 2, 3)))["blitted"] is True
    assert surface.client_size() == frame_size

    result = surface.present(solid_frame(*oversized, (4, 5, 6)))
    assert result["blitted"] is True
    assert surface.client_size() == oversized, (
        "the client area did not follow the frame, so displayed_pixel's bounds "
        "check is comparing against the requested size rather than the real one")
    # And the frame really is there at the corner that used to be missing.
    assert surface.displayed_pixel(oversized[0] - 1, oversized[1] - 1) == (4, 5, 6)


@windows_only
def test_the_frame_counter_counts_frames(surface: SurfaceWindow) -> None:
    assert surface.frame_count == 0
    surface.present(solid_frame(SURF_W, SURF_H, (1, 1, 1)))
    surface.present(solid_frame(SURF_W, SURF_H, (2, 2, 2)))
    assert surface.frame_count == 2


@windows_only
def test_the_last_frame_size_is_reported(surface: SurfaceWindow) -> None:
    """``last_frame_bytes`` is the diagnostic an operator reads when a frame
    looks wrong, so it must be the frame's size and not a constant.

    A mutation set it to 0 unconditionally and survived, which is the signature
    of an untested diagnostic rather than a harmless one.
    """
    expected = SURF_W * SURF_H * BYTES_PER_PIXEL
    surface.present(solid_frame(SURF_W, SURF_H, (1, 2, 3)))
    assert surface.last_frame_bytes == expected


@windows_only
def test_the_pump_does_not_block(surface: SurfaceWindow) -> None:
    """A pump that blocks is a program that cannot be interrupted.

    This test would hang rather than fail if ``pump`` used ``GetMessage``
    instead of ``PeekMessage``, which is the point: the property under test is
    that it returns at all.
    """
    for _ in range(3):
        assert isinstance(surface.pump(), int)


@windows_only
def test_presenting_after_close_is_a_refusal_not_a_crash(
    surface: SurfaceWindow,
) -> None:
    """A closed surface refuses.  It does not raise, and it does not blit into a
    handle that no longer exists."""
    surface.close()
    result = surface.present(solid_frame(SURF_W, SURF_H, (9, 9, 9)))
    assert result["blitted"] is False
    assert result["frame"] == 0
    assert surface.displayed_pixel(0, 0) is None


@windows_only
def test_close_is_idempotent(surface: SurfaceWindow) -> None:
    surface.close()
    surface.close()
    assert surface.hwnd is None


@windows_only
def test_the_window_survives_a_context_manager() -> None:
    """The procedure reference is the thing that keeps this alive.

    ctypes holds a ``WINFUNCTYPE`` weakly, so a procedure that is collected
    while the window still points at it makes the next message call into freed
    memory.  Holding it on the instance is the fix; pumping after the block
    would crash without it.
    """
    with SurfaceWindow("unipad-ctx", width=WIDTH * 2, height=HEIGHT * 2) as window:
        assert window.present(solid_frame(SURF_W, SURF_H, (7, 7, 7)))["blitted"]
        window.pump()
    assert window.hwnd is None


@windows_only
def test_closing_releases_the_gdi_objects_it_took() -> None:
    """A leaked DIB section is a leaked GDI object, and only a count sees it.

    The measurement is a full cycle, not an absolute number, because the FIRST
    window in a process has a one-time cost that is never released: measured,
    cycle 0 is ``0 -> 11 -> 9`` and every cycle after it is ``9 -> 11 -> 9``.
    Those two extra objects are paid once per process, not once per window --
    five create/close cycles leave the count exactly where the second one found
    it.  So the baseline is taken after a throwaway window has paid the cost,
    and what is asserted is that a window returns the count to where it started.

    A window that skipped :meth:`SurfaceWindow._drop_bitmap` would leave the DIB
    section and its DC behind, and nothing in the window's own state would show
    it -- the count is the only instrument that can.
    """
    warmup = SurfaceWindow("unipad-leak-warm", width=WIDTH * 2, height=HEIGHT * 2)
    warmup.close()
    # Measured AFTER the warmup is gone: a reading taken while it is still open
    # is two objects high, and the next window lands exactly on it -- so the
    # assertion compares two equal numbers and proves nothing.
    baseline = warmup.gdi_objects_held()

    window = SurfaceWindow("unipad-leak", width=WIDTH * 2, height=HEIGHT * 2)
    try:
        # The DIB section is created at CONSTRUCTION, not on the first frame, so
        # the count rises here rather than in present().  Asserting it after
        # present() instead compares two equal numbers and proves nothing.
        assert window.gdi_objects_held() > baseline, (
            "the surface holds no GDI objects, so it is not holding a DIB "
            "section and the count cannot detect a leak")
        window.present(solid_frame(SURF_W, SURF_H, (5, 5, 5)))
    finally:
        window.close()
    assert window.gdi_objects_held() == baseline, (
        "closing the surface left GDI objects behind")


@windows_only
def test_displayed_pixel_bounds_are_the_client_area_not_the_request(
    surface: SurfaceWindow,
) -> None:
    """``displayed_pixel`` must ask the client area, not ``self.width``.

    ``self.width`` is what was REQUESTED.  They diverge exactly when it matters:
    if ``_resize_window`` could not resize -- a desktop too small, a window style
    that refuses -- the client area stays small while ``self.width`` grew to the
    frame's, and a bounds check against the requested size passes for coordinates
    the window does not have.  That is the same defect ``client_size`` was added
    to expose, and a self-review found it living in ``displayed_pixel`` too.

    The first version of this test asserted the invariant only where the two
    AGREE, so a mutation reverting the bounds check survived it.  The divergence
    is created here deliberately: Windows enforces a minimum tracking width
    (measured: a request for 40 comes back 120), so a frame narrower than that
    leaves the client area wider than the frame -- and a coordinate in the gap
    must be answerable, because the window really does have it.
    """
    surface.present(solid_frame(SURF_W, SURF_H, (0x40, 0x50, 0x60)))
    client = surface.client_size()
    assert client is not None
    assert (surface.width, surface.height) == client, (
        "this test needs the requested and delivered sizes to agree first")

    # A frame narrower than the window's minimum tracking width.  Windows clamps
    # the client area, so the delivered size is now WIDER than the request.
    narrow = 40
    result = surface.present(solid_frame(narrow, 20, (9, 9, 9)))
    assert result["blitted"] is True
    assert surface.width == narrow
    clamped = surface.client_size()
    assert clamped is not None
    assert clamped[0] > narrow, (
        f"Windows did not clamp a {narrow}px window (client={clamped}); this "
        "test cannot create the divergence it needs on this host")

    # The frame's own last pixel is answerable either way.
    assert surface.displayed_pixel(narrow - 1, 19) == (9, 9, 9)
    # This one is the assertion that fails if the bounds check compares against
    # the requested size: it is beyond the frame but inside the client area, and
    # the window really does have it -- it shows the class background, not black.
    gap = surface.displayed_pixel(clamped[0] - 1, 19)
    assert gap is not None, (
        "a coordinate inside the client area was refused, so the bounds check "
        "is comparing against the requested size rather than the delivered one")
    # And one past the client area is still a refusal.
    assert surface.displayed_pixel(clamped[0], 19) is None


@windows_only
def test_a_bad_dimension_is_refused_before_any_window_is_made() -> None:
    """Crash early: a zero-sized surface is a caller bug, not a GDI problem."""
    with pytest.raises(UniPadError) as caught:
        SurfaceWindow("unipad-bad", width=0, height=10)
    assert caught.value.code == ErrorCode.RENDER_FAILED
