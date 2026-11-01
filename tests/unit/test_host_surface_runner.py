"""Tests for the presenter that puts a guest frame in a real window, in a loop.

The governing rule in this project is that **a guard test that cannot go red
where the defect lives is worse than no test**, so every test here names the
defect it would catch rather than describing its assertion, and the four that
matter most are these:

* a failed frame must refuse the run, not leave the previous frame on screen
  looking like a guest that is not drawing (:func:`test_a_frame_that_cannot_be_read_refuses_the_run_and_reports_what_succeeded`,
  :func:`test_a_frame_the_window_refuses_to_blit_is_a_refusal_not_a_still_image`);
* a rename in :class:`~unipad.host.winsurface.SurfaceWindow` must fail here rather
  than on a desktop nobody is watching
  (:func:`test_the_presenter_calls_only_what_the_real_surface_publishes`);
* the message loop must actually be pumped between frames
  (:func:`test_the_message_loop_is_pumped_once_between_frames`);
* a slow frame must be recorded, not averaged away
  (:func:`test_a_slow_frame_is_recorded_rather_than_averaged_away`).

Nothing here touches the network, QEMU or a real window.  The window is a
:class:`RecordingWindow` -- a *subclass* of the real ``SurfaceWindow`` with GDI
taken out -- and the frame source is the real
:class:`~unipad.host.framebuffer.GuestFrameSource` reading a stub memory, so the
conversion under test is the production one.  The only fakes are the two things
that need a desktop and the two that need a failure injected.

The serial-log lines and the framebuffer slice are embedded as literals on
purpose: they came from
``local-private/tmp/framebuffer-acquire-evidence/``, which is gitignored and
absent from a clean checkout.  A test that opened it would fail on a fresh clone
before reaching a single assertion -- exactly what
``test_clean_checkout_selfsufficiency.py`` exists to catch.
"""

from __future__ import annotations

import inspect
import json
import time

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.framebuffer import BYTES_PER_PIXEL, FrameGeometry, GuestFrameSource
from unipad.host.surface_runner import (
    WINDOW_CONTRACT,
    GuestSurfacePresenter,
    RunRefused,
    guest_frame_source,
    video_geometry_from_serial,
)
from unipad.host.winsurface import SurfaceWindow, ppm_to_bgr

#: The base the guest's own kernel printed:
#: ``video    base 0x1BC7A0000  800x600  row 3200 bytes  32 bpp``.
FB_BASE = 0x1BC7A0000

#: The video line itself, verbatim from
#: ``local-private/tmp/framebuffer-acquire-evidence/serial.log`` (2026-11-01).
REAL_VIDEO_LINE = b"video    base 0x1BC7A0000  800x600  row 3200 bytes  32 bpp"

#: The bootloader's own, differently-worded line about the same framebuffer,
#: which appears a few hundred lines earlier in the same log.  It must not be
#: read as a second geometry: it names the same numbers in a different shape,
#: and a parser loose enough to match both would have two geometries and have to
#: choose between them.
BOOTLOADER_VIDEO_LINE = (
    b"DARWINBOOT: video base=0x1BC7A0000 display=1 row=3200 width=800 "
    b"height=600 depth=32"
)

#: A realistic excerpt: the bootloader's line, the hand-off, and then the
#: kernel's line.  The parser has to find the one it can read in the middle of
#: the noise, which is the situation a real log is in.
SERIAL_EXCERPT = (
    b"UEFI firmware (version edk2-stable202408-prebuilt.qemu.org)\n"
    b"BdsDxe: starting Boot0001 \"UEFI Misc Device\" from PciRoot(0x0)/Pci(0x1,0x0)\n"
    + BOOTLOADER_VIDEO_LINE + b"\n"
    b"DARWINBOOT: inputs validated; exiting boot services\n"
    b"?DARWINBOOT: hand-off to the kernel\n"
    b"\n"
    b"kernel   va 0xFFFFFFF00427C000  pa 0x4807C000\n"
    + REAL_VIDEO_LINE + b"\n"
    b"CurrentEL 1  SCTLR_EL1 0x30D0198D  TCR_EL1 0x280803518\n"
)

# --- a small real frame, embedded so a clean checkout needs nothing on disk ---

#: Three rows of a 4x3 frame, laid out the way the guest lays one out: four bytes
#: per pixel, then eight bytes of padding this test owns.  Provenance:
#: ``local-private/tmp/framebuffer-acquire-evidence/fb.bin`` -- the green bar the
#: bootloader printed (rows 0 and 1) and the black-plus-orange-accent row that
#: follows it (row 2).  The padding is poison, exactly as it is in
#: ``test_host_framebuffer.py``: no real pixel holds ``0xEE``, so a row walked at
#: ``width * 4`` instead of ``stride`` shows up in the output.
FRAME_BGRA = (
    b"\x00\xff\x00\xff" * 4 + b"\xee" * 8 +
    b"\x00\xff\x00\xff" * 4 + b"\xee" * 8 +
    b"\x00\x00\x00\x00" + b"\xd7\x5b\x2a\x00" * 3 + b"\xee" * 8
)

#: A geometry with padding in it, so the stride path is exercised by real pixels
#: rather than by a synthetic buffer.
WIDTH = 4
HEIGHT = 3
STRIDE = WIDTH * BYTES_PER_PIXEL + 8

#: How many payload bytes the window is handed for one frame.
FRAME_BYTES = WIDTH * HEIGHT * 3


class RecordingMemory:
    """A stand-in for :class:`~unipad.host.guestobserve.GuestMemory`.

    Matching ``read(physical, size)`` positionally, because that is the
    signature production calls and a stub with a different shape would pass while
    proving nothing about the real call.
    """

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)
        self.calls: list[tuple[int, int]] = []

    def read(self, physical: int, size: int) -> bytes:
        self.calls.append((physical, size))
        if size <= 0:
            return b""
        return self._data[:size]


class RecordingClient:
    """A ``QmpClient`` stand-in that records the commands it was given.

    ``is_connected`` is published because the real client publishes it and
    :func:`guest_frame_source` refuses a client that says it is not connected --
    a stub without the attribute could not be asked that question at all.
    """

    def __init__(self) -> None:
        self.is_connected = True
        self.commands: list[tuple[str, dict]] = []

    def execute(self, command: str, arguments=None) -> dict:
        self.commands.append((command, dict(arguments or {})))
        return {"return": {}}


class RecordingWindow(SurfaceWindow):
    """A ``SurfaceWindow`` with GDI taken out.

    A subclass rather than a look-alike, for two reasons.  First, the presenter
    refuses anything that is not a ``SurfaceWindow``, so a duck-typed stub would
    be testing a promise nothing checks.  Second -- and this is the one that
    matters -- the members this class does *not* override are the real ones:
    :func:`~unipad.host.winsurface.ppm_to_bgr`, ``read_ppm_header_bytes``,
    ``solid_frame``.  A stub that reimplemented them would be testing itself.

    What is replaced is exactly what needs a desktop: ``__init__`` (it registers
    a window class and creates a window), ``present`` (it reaches GDI) and
    ``pump`` (it drains the thread's message queue).  ``present`` still runs the
    REAL :func:`~unipad.host.winsurface.ppm_to_bgr`, so a frame the surface would
    refuse is refused here too -- the fake cannot be made to accept a document
    the window would not, which is what makes
    :func:`test_a_source_that_hands_the_window_something_that_is_not_a_frame_refuses_the_run`
    a test of the presenter rather than of this stub.
    """

    def __init__(self, *, blit_ok: bool = True, quit_after: int | None = None,
                 present_delay: float = 0.0) -> None:
        # Deliberately does not call super().__init__(): that is the part that
        # registers a class and creates a window.  ``_hwnd`` is set to None so
        # the inherited accessors that check it degrade instead of raising
        # AttributeError from a state this class never built.
        self._hwnd = None
        self.presented: list[tuple[int, int, int]] = []
        self.frames_received: list[bytes] = []
        self.events: list[str] = []
        self.pump_calls = 0
        self.closed = False
        self._blit_ok = blit_ok
        self._quit_after = quit_after
        self._present_delay = present_delay

    def present(self, ppm: bytes) -> dict:
        width, height, rows = ppm_to_bgr(ppm)
        if self._present_delay:
            time.sleep(self._present_delay)
        self.frames_received.append(bytes(ppm))
        self.presented.append((width, height, len(rows)))
        self.events.append("present")
        return {"width": width, "height": height, "bytes": len(rows),
                "frame": len(self.presented), "blitted": self._blit_ok}

    def pump(self) -> int:
        self.events.append("pump")
        self.pump_calls += 1
        if self._quit_after is not None and self.pump_calls >= self._quit_after:
            return -1
        return 0

    def close(self) -> None:
        self.closed = True


class RefusingWindow(RecordingWindow):
    """A window that accepts every frame and reports it blitted none of them."""

    def __init__(self) -> None:
        super().__init__(blit_ok=False)


class FailingFrameSource(GuestFrameSource):
    """The real source, with one frame made unreadable.

    A subclass of the real thing rather than a fresh ``grab()``, so the frames
    that *do* arrive come from the production conversion -- otherwise a test that
    asserts the run refused would also be asserting against its own idea of what
    a frame looks like.
    """

    def __init__(self, memory: RecordingMemory, geometry: FrameGeometry,
                 *, fail_on: int) -> None:
        super().__init__(memory, geometry)
        self._fail_on = fail_on

    def grab(self) -> bytes:
        if self.frames == self._fail_on:
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "the guest did not return the framebuffer that was asked for",
                asked=STRIDE * HEIGHT,
                returned=0,
            )
        return super().grab()


class SlowFrameSource(GuestFrameSource):
    """The real source, with one frame made slow on purpose."""

    def __init__(self, memory: RecordingMemory, geometry: FrameGeometry,
                 *, slow_on: int, seconds: float) -> None:
        super().__init__(memory, geometry)
        self._slow_on = slow_on
        self._seconds = seconds

    def grab(self) -> bytes:
        if self.frames == self._slow_on:
            time.sleep(self._seconds)
        return super().grab()


class NotAFrameSource:
    """A source that hands back something that is not a PPM at all."""

    def grab(self) -> bytes:
        return b"this is not a framebuffer"


def _geometry(**overrides: object) -> FrameGeometry:
    fields: dict[str, object] = {"base": FB_BASE, "width": WIDTH,
                                 "height": HEIGHT, "stride": STRIDE}
    fields.update(overrides)
    return FrameGeometry(**fields)  # type: ignore[arg-type]


def _source(memory: RecordingMemory | None = None) -> GuestFrameSource:
    return GuestFrameSource(memory or RecordingMemory(FRAME_BGRA), _geometry())


def _run(frames, *, window: RecordingWindow | None = None,
         source=None, stop=None, **window_kwargs):
    window = window if window is not None else RecordingWindow(**window_kwargs)
    presenter = GuestSurfacePresenter(window, source or _source())
    return presenter.run(frames, stop=stop)


# --- the geometry, from the guest's own line ---------------------------------


def test_the_real_video_line_becomes_the_geometry_the_guest_printed() -> None:
    """The parse, against the line a real booted guest actually printed.

    The expected values are not this module's output: they are the numbers in
    the log, which is the only authority for where the framebuffer is.  A parser
    that accepted a differently-worded line, or that derived the stride from the
    width instead of reading it, would produce a plausible geometry the guest
    never printed -- and every frame read through it would come from the wrong
    place with nothing to show it.
    """
    geometry = video_geometry_from_serial(REAL_VIDEO_LINE)
    assert geometry == FrameGeometry(base=FB_BASE, width=800, height=600,
                                     stride=3200)
    assert geometry.size == 3200 * 600


def test_the_bootloaders_own_video_line_is_not_the_kernels() -> None:
    """Two lines, one framebuffer, one parse.

    The bootloader prints ``video base=0x... display=1 row=... width=...`` and
    the kernel prints ``video base 0x... WxH row N bytes B bpp`` about the same
    framebuffer.  A pattern loose enough to match both reads two geometries out
    of one log; this test pins the distinction, and the second half proves the
    kernel's line is still found in the middle of a log that carries the
    bootloader's.
    """
    with pytest.raises(UniPadError) as caught:
        video_geometry_from_serial(BOOTLOADER_VIDEO_LINE)
    assert caught.value.code == ErrorCode.EVIDENCE_INCOMPLETE

    from_real_log = video_geometry_from_serial(SERIAL_EXCERPT)
    assert from_real_log == FrameGeometry(base=FB_BASE, width=800, height=600,
                                          stride=3200)


def test_a_log_with_no_video_line_is_refused_rather_than_defaulted() -> None:
    """A geometry nobody printed is a geometry the guest does not have.

    Refused rather than defaulted to the shipped panel's 1376x1024, because a
    default would produce a frame from an address the guest never named and a
    length it never claimed -- and the read would either fail or, worse, succeed
    against whatever happens to live there.
    """
    with pytest.raises(UniPadError) as caught:
        video_geometry_from_serial(
            b"DARWINBOOT: hand-off to the kernel\n"
            b"kernel   va 0xFFFFFFF00427C000  pa 0x4807C000\n"
            b"CurrentEL 1  SCTLR_EL1 0x30D0198D\n"
        )
    assert caught.value.code == ErrorCode.EVIDENCE_INCOMPLETE


def test_two_framebuffers_in_one_log_is_ambiguous_and_is_not_resolved_by_picking_one() -> None:
    """A guest whose display mode changed is not a single fact.

    The refusal names every geometry it saw, so a reader can see the change
    rather than infer it.  Picking the first would be a guess; picking the last
    would be the same guess with more confidence.  This is the test that goes red
    on "take the first match", which is what a naive ``search`` does.
    """
    two = (REAL_VIDEO_LINE + b"\n"
           b"video    base 0x0DEADBEEF  1024x768  row 4096 bytes  32 bpp\n")
    with pytest.raises(UniPadError) as caught:
        video_geometry_from_serial(two)
    assert caught.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert caught.value.details["lines"] == 2
    assert sorted(row["width"] for row in caught.value.details["geometries"]) == [800, 1024]
    assert {row["base"] for row in caught.value.details["geometries"]} == {
        "0x1bc7a0000", "0xdeadbeef"}


def test_the_same_geometry_printed_twice_is_one_fact_not_an_ambiguity() -> None:
    """A log that repeats itself is not a guest that changed its mind.

    The line above this one is the ambiguity check; this is its other half.  If
    both were refusals, a log that wrapped -- or a bootloader that reprinted what
    the kernel was handed -- could never be read at all, and the honest answer
    would be a refusal for a log that states one fact twice.
    """
    geometry = video_geometry_from_serial(REAL_VIDEO_LINE + b"\n" + REAL_VIDEO_LINE)
    assert geometry == FrameGeometry(base=FB_BASE, width=800, height=600,
                                     stride=3200)


def test_a_depth_this_module_does_not_read_is_refused() -> None:
    """A 16-bit framebuffer read through the 32-bit path is wrong in every pixel.

    ``framebuffer.py`` measured the channel order on a 32-bit framebuffer and
    drops the fourth byte.  Reading a 16-bit one through that path would produce
    a frame of the right length whose every pixel is wrong, and no length check
    anywhere would see it -- so the depth is refused where it is parsed.
    """
    with pytest.raises(UniPadError) as caught:
        video_geometry_from_serial(
            b"video    base 0x1000  800x600  row 1600 bytes  16 bpp\n")
    assert caught.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert caught.value.details["bpp"] == 16


def test_a_row_stride_the_log_prints_that_cannot_describe_a_framebuffer_is_refused() -> None:
    """Rows that overlap are a corrupt geometry, not a layout.

    The log is the guest's own text, so it can be wrong -- a truncated run, a
    driver that printed a different unit.  ``FrameGeometry`` refuses it, and the
    refusal here adds which line produced it, because "the geometry you built is
    bad" and "the geometry the guest printed is bad" need different fixes.
    """
    with pytest.raises(UniPadError) as caught:
        video_geometry_from_serial(
            b"video    base 0x1000  800x600  row 100 bytes  32 bpp\n")
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert caught.value.details["stride"] == 100


# --- the loop, against the real frame source and a window with GDI taken out --


def test_the_embedded_frame_is_the_size_its_geometry_declares() -> None:
    """The fixture is the frame it claims to be.

    :class:`RecordingMemory` slices to whatever is asked for, so a literal of the
    wrong length would satisfy every other test in this file -- and the real
    ``GuestMemory`` refuses a short read, which is how the first draft of this
    fixture (an accent row of eight bytes per pixel) was caught.  Asserting the
    size here means a mistyped literal fails at the fixture rather than three
    tests later.
    """
    assert len(FRAME_BGRA) == STRIDE * HEIGHT
    assert FRAME_BYTES == WIDTH * HEIGHT * 3


def test_a_run_presents_every_frame_it_was_asked_for_and_reads_the_guest_each_time() -> None:
    """One read per presented frame, and no frame skipped or cached.

    The guest's framebuffer is live memory, so a presenter that grabbed once and
    presented the same bytes N times would look identical on this guest -- whose
    framebuffer is static per fact 198 -- and would start lying the moment the
    guest drew anything.  The read count is the instrument that tells the two
    apart, and it is asserted against the memory's own record rather than against
    the window's frame count, which a caching presenter could fake.
    """
    memory = RecordingMemory(FRAME_BGRA)
    window = RecordingWindow()
    report = GuestSurfacePresenter(window, _source(memory)).run(4)

    assert report.presented == 4
    assert report.complete is True
    assert report.stop_reason == "completed"
    assert len(report.timings) == 4
    assert memory.calls == [(FB_BASE, STRIDE * HEIGHT)] * 4
    assert window.presented == [(WIDTH, HEIGHT, FRAME_BYTES)] * 4


def test_the_window_is_handed_the_frames_the_guest_drew_unmodified() -> None:
    """The presenter is a pipe, not a converter.

    ``framebuffer.py`` owns the conversion and it was validated against QMP
    ``screendump`` byte for byte.  A presenter that reordered a channel, dropped
    a row or rescaled here would undo that, and the window would show pixels the
    guest never drew -- so the documents handed over are compared against what
    the source produced, exactly, and not merely against their length.
    """
    window = RecordingWindow()
    source = _source()
    GuestSurfacePresenter(window, source).run(2)

    expected = source.grab()
    assert source.frames == 3, "the source was not asked for a third frame"
    assert window.frames_received[0] == expected
    assert window.frames_received[1] == expected
    assert window.presented == [(WIDTH, HEIGHT, FRAME_BYTES)] * 2


def test_the_message_loop_is_pumped_once_between_frames() -> None:
    """A window that is never pumped stops repainting while the caller thinks it is alive.

    The count and the order are both asserted: one pump per presented frame, and
    each one *after* its frame's blit.  A loop that pumped only once at the end,
    or only on the first iteration, would leave the window unresponsive for the
    whole run -- and ``present`` would still report every frame blitted, so
    nothing else here would see it.
    """
    window = RecordingWindow()
    report = _run(3, window=window)

    assert window.pump_calls == 3
    assert window.events == ["present", "pump"] * 3
    assert report.presented == 3


def test_a_frame_that_cannot_be_read_refuses_the_run_and_reports_what_succeeded() -> None:
    """A failed capture is not "the screen did not change".

    The defect this exists to catch: a presenter that caught the failure and
    carried on would leave frame 0 on screen, and the window would look exactly
    like a guest that stopped drawing.  So the run refuses, and it says how many
    frames were presented first -- one, not four, and not zero.  The cause's own
    code is kept rather than normalised, because a caller branches on the code
    and ``evidence_incomplete`` is a different branch from ``render_failed``.
    """
    memory = RecordingMemory(FRAME_BGRA)
    window = RecordingWindow()
    source = FailingFrameSource(memory, _geometry(), fail_on=1)

    with pytest.raises(RunRefused) as caught:
        GuestSurfacePresenter(window, source).run(4)

    assert isinstance(caught.value, UniPadError)
    assert caught.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert caught.value.details["stage"] == "capture"
    assert caught.value.details["frame_index"] == 1
    assert caught.value.details["frames_presented"] == 1
    assert caught.value.details["frames_requested"] == 4
    # The window really holds one frame -- the run stopped rather than replaying
    # the last good one for the remaining three.
    assert window.presented == [(WIDTH, HEIGHT, FRAME_BYTES)]
    assert caught.value.report.presented == 1
    assert len(caught.value.report.timings) == 1
    assert caught.value.report.stop_reason == "refused"


def test_a_frame_the_window_refuses_to_blit_is_a_refusal_not_a_still_image() -> None:
    """``blitted: False`` is the window saying it did not show the frame.

    ``SurfaceWindow.present`` returns that when the window is gone or GDI refused
    the call -- a refusal, not a crash.  A presenter that ignored it would report
    four successful frames over a window showing none of them, which is the same
    lie as the skipped frame above wearing a different coat.
    """
    window = RefusingWindow()

    with pytest.raises(RunRefused) as caught:
        _run(3, window=window)

    assert caught.value.code == ErrorCode.RENDER_FAILED
    assert caught.value.details["stage"] == "present"
    assert caught.value.details["frames_presented"] == 0
    assert caught.value.report.presented == 0


def test_a_source_that_hands_the_window_something_that_is_not_a_frame_refuses_the_run() -> None:
    """The window is given real frames, and refuses anything else.

    This is also the test that proves :class:`RecordingWindow` is not a stub that
    accepts anything: its ``present`` runs the REAL
    :func:`~unipad.host.winsurface.ppm_to_bgr`, so a source producing garbage is
    refused by the converter the production window uses.  A fake with a
    hand-written ``present`` would have taken the bytes and reported success.
    """
    with pytest.raises(RunRefused) as caught:
        _run(2, source=NotAFrameSource())

    assert caught.value.code == ErrorCode.RENDER_FAILED
    assert caught.value.details["stage"] == "present"
    assert caught.value.details["frames_presented"] == 0


def test_a_slow_frame_is_recorded_rather_than_averaged_away() -> None:
    """"A debugger, not a renderer" is a number, and the number is per frame.

    The defect: a presenter that accumulated one total and divided by the frame
    count would report a mean that hides a 50 ms stall among sub-millisecond
    frames.  The slow frame's own timing is asserted directly, and then that it
    is the maximum -- so it is in the max *and* the median is below it, which is
    what "not averaged away" means.  The median being lower than the max is the
    property a mean-only report cannot express.
    """
    memory = RecordingMemory(FRAME_BGRA)
    source = SlowFrameSource(memory, _geometry(), slow_on=2, seconds=0.05)
    report = GuestSurfacePresenter(RecordingWindow(), source).run(5)

    assert report.presented == 5
    assert len(report.timings) == 5
    slow = report.timings[2]
    assert slow.grab_seconds >= 0.045, "the slow frame was not recorded"
    assert slow.total_seconds == max(t.total_seconds for t in report.timings)

    latency = report.latency()
    assert latency["frames"] == 5
    assert latency["max_ms"] >= 45.0
    assert latency["median_ms"] < latency["max_ms"], (
        "the slow frame was averaged away into the median")
    # The split is the point of reporting the two halves separately: the sleep
    # was in the read, so the read's median is an ordinary frame and the blit's
    # median never saw the stall at all.  A presenter that recorded one number
    # per frame could not say which half the time went to, which is the
    # difference between "the guest is slow" and "GDI is slow".
    assert latency["grab_median_ms"] < latency["max_ms"]
    assert latency["blit_median_ms"] < 45.0


def test_a_window_that_closes_ends_the_run_without_refusing_it() -> None:
    """``WM_QUIT`` is a way of finishing, not a failure.

    The operator closed the window; the frames before that were really
    presented.  Refusing here would report a broken run for a normal exit, and
    the caller would not be able to tell the two apart.  ``stop_reason`` is what
    tells them apart.
    """
    window = RecordingWindow(quit_after=2)
    report = _run(10, window=window)

    assert report.presented == 2
    assert report.stop_reason == "window_closed"
    assert report.complete is False
    assert window.presented == [(WIDTH, HEIGHT, FRAME_BYTES)] * 2


def test_a_run_with_no_frame_count_ends_when_the_window_does() -> None:
    """``frames=None`` is the operator-facing case, and it is bounded by the window.

    An unbounded run is what a presenter on a desktop wants, so the bound has to
    come from something that can actually end it -- the window's own quit, or the
    caller's predicate.  Without one of those it would never return, which is
    why the two are the only ways this path stops.
    """
    report = _run(None, window=RecordingWindow(quit_after=3))

    assert report.requested is None
    assert report.presented == 3
    assert report.stop_reason == "window_closed"


def test_a_stop_predicate_ends_the_run_before_the_frame_it_names() -> None:
    """The caller's own bound, checked before each frame rather than after.

    ``stop(2)`` on a ten-frame run presents two: indices 0 and 1.  A predicate
    checked after the frame would present three, and the extra frame is one the
    caller said it did not want.
    """
    report = _run(10, stop=lambda index: index >= 2)

    assert report.presented == 2
    assert report.stop_reason == "stop_requested"
    assert report.complete is False


def test_a_run_of_zero_frames_is_refused() -> None:
    """A run of zero has measured nothing, and reporting it as a success is a claim."""
    for frames in (0, -1):
        with pytest.raises(UniPadError) as caught:
            _run(frames)
        assert caught.value.code == ErrorCode.CONFIG_INVALID


# --- construction, where a bad collaborator is cheapest to catch ------------


def test_a_source_with_no_grab_is_refused_before_the_first_frame() -> None:
    """Refused at construction, not on frame one of thirty.

    A presenter that discovered this inside the loop would refuse a run it had
    already started, and the caller would have to work out whether any frames
    happened.  At construction the answer is free.
    """
    with pytest.raises(UniPadError) as caught:
        GuestSurfacePresenter(RecordingWindow(), object())
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_a_window_that_is_not_a_surface_is_refused_before_the_first_frame() -> None:
    """The window is the real class, not a shape that looks like it.

    This is the other half of the rename guard below.  The structural test proves
    the presenter's calls exist on ``SurfaceWindow``; this proves the presenter
    will not accept anything else -- so a look-alike that survives a rename in
    the real class cannot reach the presenter at all.
    """
    class LookAlike:
        def present(self, ppm):  # noqa: D102 - a deliberately wrong collaborator
            return {"blitted": True}

        def pump(self):
            return 0

    with pytest.raises(UniPadError) as caught:
        GuestSurfacePresenter(LookAlike(), _source())  # type: ignore[arg-type]
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_the_presenter_never_closes_a_window_it_did_not_create() -> None:
    """The caller made the window, so the caller ends its life.

    A presenter that closed a window it was handed would take the desktop with it
    on the way out of a run that had merely finished -- and the caller's own
    ``close()`` or ``with`` block would then be closing something already gone.
    """
    window = RecordingWindow()
    _run(2, window=window)
    assert window.closed is False


# --- the contract with the real surface -------------------------------------


def test_the_presenter_calls_only_what_the_real_surface_publishes() -> None:
    """A rename in ``SurfaceWindow`` must fail here, not on a desktop.

    This is the test that makes the fake safe.  A stub shaped by hand goes stale
    silently: rename ``present`` to ``blit`` and every stub still works, every
    test still passes, and the first real window on a real desktop is where it
    breaks.  So the contract is published by the module
    (:data:`~unipad.host.surface_runner.WINDOW_CONTRACT`) and checked here against
    the real class's own signature.

    The parameter *names* are deliberately not asserted.  The presenter calls
    positionally, so renaming ``ppm`` to ``frame`` breaks nothing -- and a test
    that went red on it would be a test the next maintainer relaxes rather than
    fixes.  What is asserted is the number of *required* positional parameters,
    which catches a rename of the method and a change to what it needs, and
    tolerates a new optional one.
    """
    published = {name for name in vars(SurfaceWindow) if not name.startswith("_")}
    assert published, "SurfaceWindow publishes nothing public, which cannot be right"
    for name, arity in WINDOW_CONTRACT.items():
        assert name in published, (
            f"the presenter calls SurfaceWindow.{name}() and the real class no "
            "longer publishes it -- every real caller is broken and only this "
            "test is watching")
        declared = list(
            inspect.signature(getattr(SurfaceWindow, name)).parameters.values())
        # The signature is read off the class rather than off an instance, so the
        # first parameter is ``self`` and is not one the presenter passes.
        if declared and declared[0].name == "self":
            declared = declared[1:]
        required = [
            parameter for parameter in declared
            if parameter.default is inspect.Parameter.empty
            and parameter.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                   inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        assert len(required) == arity, (
            f"SurfaceWindow.{name}() now takes {len(required)} required "
            f"arguments and the presenter passes {arity}")


def test_the_recording_window_is_the_real_class_with_gdi_taken_out() -> None:
    """The fake inherits from the real thing, so it cannot drift into a different shape.

    ``isinstance`` is the promise the presenter makes at construction, and this is
    the test that the fake satisfies it honestly rather than by being handed a
    pass.  The un-overridden members are asserted to be the real ones, which is
    what stops the fake from quietly becoming the thing under test.
    """
    window = RecordingWindow()
    assert isinstance(window, SurfaceWindow)
    assert RecordingWindow.present is not SurfaceWindow.present
    assert RecordingWindow.pump is not SurfaceWindow.pump
    # Not overridden, therefore the real one.  This is what stops the fake from
    # quietly becoming the thing under test: everything it does not replace is
    # inherited from the class production uses.
    assert RecordingWindow.readback is SurfaceWindow.readback
    assert RecordingWindow.client_size is SurfaceWindow.client_size


# --- the guest side: one client for the session -----------------------------


def test_the_presenter_never_opens_a_qmp_client_of_its_own(monkeypatch) -> None:
    """QEMU takes one QMP client at a time, so a second one is a contention bug.

    ``GuestMemory`` holds a single connection for the whole session because QEMU
    is slow to take the next.  A presenter that opened its own client would
    either be refused or would starve whatever else is talking to the monitor,
    and the symptom would be a frame that would not arrive -- which looks like a
    guest problem.  The guard is to make constructing a client an assertion and
    then run a whole loop.
    """
    from unipad.host import qmp

    def refuse(self, *args, **kwargs):
        raise AssertionError(
            "the presenter constructed a QmpClient; the caller owns the session's")

    monkeypatch.setattr(qmp.QmpClient, "__init__", refuse)
    report = _run(3)
    assert report.presented == 3


def test_the_frame_source_is_built_around_the_callers_client(tmp_path) -> None:
    """One client, every read, for the life of the source.

    ``guest_frame_source`` wraps the client it is given instead of making one, so
    two reads go through the same object -- asserted on the client's own command
    record rather than on anything the source says about itself.  The ``pmemsave``
    arguments are checked too, because that is the one place the geometry becomes
    a request: a base in the wrong address space would read the wrong bytes and
    still succeed.

    The scratch file is written first because ``pmemsave`` writes to a file
    rather than returning bytes -- there is no QMP command that does -- so the
    stub client's reply is not what produces the frame; the file is.
    """
    scratch = tmp_path / "pmem.bin"
    scratch.write_bytes(FRAME_BGRA)
    client = RecordingClient()
    source = guest_frame_source(client, _geometry(), scratch)

    first = source.grab()
    second = source.grab()

    assert [command for command, _ in client.commands] == ["pmemsave"] * 2
    for _command, arguments in client.commands:
        assert arguments["val"] == FB_BASE
        assert arguments["size"] == STRIDE * HEIGHT
    assert first == second
    assert source.frames == 2


def test_a_client_that_says_it_is_not_connected_is_refused() -> None:
    """A dead client is refused before the first read, not discovered on frame one."""
    client = RecordingClient()
    client.is_connected = False
    with pytest.raises(UniPadError) as caught:
        guest_frame_source(client, _geometry(), "scratch.bin")
    assert caught.value.code == ErrorCode.QMP_UNAVAILABLE


def test_a_geometry_that_is_not_a_frame_geometry_is_refused() -> None:
    """The four numbers are positional, so a loose value can put them in the wrong order.

    ``FrameGeometry(base, width, height, stride)`` in any other order is a frame
    read from the wrong address at the wrong size -- and a 1,920,000-byte read
    from a wrong base succeeds often enough to be dangerous.
    """
    with pytest.raises(UniPadError) as caught:
        guest_frame_source(RecordingClient(), (800, 600, 3200, FB_BASE),  # type: ignore[arg-type]
                           "scratch.bin")
    assert caught.value.code == ErrorCode.CONFIG_INVALID


# --- what the run reports ---------------------------------------------------


def test_the_report_names_the_geometry_it_read_and_is_json_shapable() -> None:
    """A report that cannot say which framebuffer it read is a report about nothing.

    The geometry is reported when the source carries one and null otherwise, and
    the whole document is asserted to survive a JSON round trip with no absolute
    path in it -- a scratch path is the one place a user directory could arrive,
    and PRD 9.2 is about it not arriving.
    """
    report = _run(2)
    document = report.to_dict()

    assert document["presented"] == 2
    assert document["complete"] is True
    assert document["stop_reason"] == "completed"
    assert document["geometry"] == {"base": hex(FB_BASE), "width": WIDTH,
                                    "height": HEIGHT, "stride": STRIDE,
                                    "bytes": STRIDE * HEIGHT}
    assert set(document) == {"requested", "presented", "complete", "stop_reason",
                             "seconds", "geometry", "latency", "frames"}
    round_tripped = json.loads(json.dumps(document))
    assert round_tripped == document
    assert len(document["frames"]) == 2
    assert document["frames"][0]["index"] == 0
    assert document["frames"][0]["bytes"] == FRAME_BYTES


def test_a_report_with_no_frames_reports_no_measurement_rather_than_zero() -> None:
    """Zero milliseconds would read as "free".

    A run that presented nothing has measured nothing, and the latency fields
    come back ``None`` so a caller cannot average a number that was never taken
    into one that was.
    """
    report = _run(2, stop=lambda index: True)
    latency = report.latency()
    assert report.presented == 0
    assert latency["frames"] == 0
    assert latency["median_ms"] is None
    assert latency["max_ms"] is None
