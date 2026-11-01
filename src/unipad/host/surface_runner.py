"""Put the guest's framebuffer in a real Windows window, in a loop, honestly.

PRD 4.2 item 3 wants a real guest frame on the Windows screen.  Both halves of
that now exist and both were measured separately: :mod:`unipad.host.framebuffer`
reads the guest and produces a P6 PPM, and :mod:`unipad.host.winsurface` blits a
P6 PPM into a window and can read the pixels back out of the window's own device
context.  Nothing joined them.  This module is the join, and it is deliberately
small: it owns a window and a frame source, it pumps the message loop between
frames, it times every frame, and it refuses rather than lies.

What it refuses to claim
------------------------
**A failed capture is not "the screen did not change".**  That is the rule
:mod:`unipad.host.interact` is built on and it is restated here because the
temptation is stronger: a presenter that skipped a frame it could not read would
leave the *previous* frame on screen, and the window would look exactly like a
guest that is not drawing.  So a frame that cannot be read, a frame the window
refuses to blit, and a message loop that cannot be pumped each end the run with a
:class:`RunRefused` naming the stage, the frame index, the cause, and **how many
frames were presented before it** -- a caller that ignores the return value still
cannot mistake the run for a success, and a caller that catches the refusal gets
the partial report on ``exc.report``.

**A still image is not a frame rate.**  Fact 198 established that this guest's
framebuffer is static, so a loop over it produces N identical frames however fast
it runs.  What is measured here is therefore *latency*, not animation: per frame,
the seconds spent reading the guest and the seconds spent handing the frame to
GDI, reported as min/median/max.  That is the number that says whether this is a
debugger or a renderer, and "a debugger, not a renderer" is a number rather than
an opinion.  The two halves are reported separately because they are different
bottlenecks with different fixes: the read is one QMP ``pmemsave`` and a file
read-back, the blit is a ``memmove`` and a ``StretchDIBits``.

Why this module opens no socket
-------------------------------
It takes a frame *source*, not a guest.  :class:`unipad.host.guestobserve.GuestMemory`
documents the reason and it is a property of QEMU rather than of this code: QEMU
takes a single QMP client at a time and is slow to take the next, so a connection
per read would often be refused.  The presenter therefore never constructs a
:class:`~unipad.host.qmp.QmpClient`, never connects and never closes one -- the
caller owns the session's client, hands it to :func:`guest_frame_source` once, and
the presenter reads through it for as long as it runs.  A presenter that opened
its own client would either be refused or would contend with whatever else is
talking to the monitor, and the failure would look like a frame that would not
arrive.

Geometry
--------
The four numbers come from the guest's boot log, and :mod:`unipad.host.framebuffer`
deliberately does not parse it.  :func:`video_geometry_from_serial` is that parse,
and it is a refusal rather than a default: no video line, two lines that describe
*different* framebuffers, or a depth this module does not read each end in a
named refusal.  The one thing it does accept is the same geometry printed twice,
because that is one fact stated twice and not an ambiguity -- the distinction
between those two cases is the whole point of the check.

What this module does NOT do
----------------------------
It does not launch QEMU, read a serial log off disk, or touch a socket.  It does
not create the window -- the caller does, because the caller is the one that can
close it, and a presenter that destroyed a window it did not create would take
the desktop with it.  It does not scale, crop or colour-convert: the window
follows the frame, as :mod:`unipad.host.winsurface` already decided.  And it does
not decide the geometry from anything but the guest's own line.

Dependency direction: :mod:`unipad.errors`, :mod:`unipad.host.framebuffer`,
:mod:`unipad.host.guestobserve` and :mod:`unipad.host.winsurface`.  It imports
:mod:`unipad.host.qmp` nowhere -- not even lazily -- so a test can prove it opens
no client by making the client's constructor refuse.
"""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union

from unipad.errors import ErrorCode, UniPadError
from unipad.host.framebuffer import BYTES_PER_PIXEL, FrameGeometry, GuestFrameSource
from unipad.host.guestobserve import GuestMemory
from unipad.host.winsurface import SurfaceWindow

__all__ = [
    "VIDEO_LINE",
    "WINDOW_CONTRACT",
    "FrameTiming",
    "GuestSurfacePresenter",
    "RunRefused",
    "RunReport",
    "guest_frame_source",
    "video_geometry_from_serial",
]


def _refuse(code: str, message: str, **details: Any) -> UniPadError:
    """A refusal, with enough detail to act on and nothing secret (PRD 9.2)."""
    return UniPadError(code, message, **details)


#: The line the guest's kernel prints for the framebuffer it was handed::
#:
#:     video    base 0x1BC7A0000  800x600  row 3200 bytes  32 bpp
#:
#: Measured in ``local-private/tmp/framebuffer-acquire-evidence/serial.log`` on
#: 2026-11-01.  Anchored on the whole shape rather than on the word ``video``,
#: because the bootloader prints a *differently worded* line about the same
#: framebuffer a few hundred lines earlier::
#:
#:     DARWINBOOT: video base=0x1BC7A0000 display=1 row=3200 width=800 height=600 depth=32
#:
#: A pattern loose enough to match both would read two geometries out of one log
#: and have to choose between them.  The equals sign after ``base`` is what keeps
#: them apart, which is why the whitespace between ``base`` and ``0x`` is required
#: rather than optional.  The address is ``+`` rather than ``{8,}`` -- unlike the
#: kernel load line in :mod:`unipad.host.guestobserve`, a truncated base cannot
#: produce a wrong geometry here, because the width, height, row and depth must
#: all match after it or the whole line does not match at all.
VIDEO_LINE = re.compile(
    rb"video\s+base\s+0x([0-9A-Fa-f]+)\s+(\d+)\s*x\s*(\d+)\s+"
    rb"row\s+(\d+)\s+bytes\s+(\d+)\s+bpp"
)

#: The ``SurfaceWindow`` methods this module calls, and how many positional
#: arguments each is given.  Published so the suite can check it against the real
#: class: a rename or a signature change in :mod:`unipad.host.winsurface` must
#: fail a test here rather than fail on a desktop, which is the one place nobody
#: is watching when it happens.
WINDOW_CONTRACT = {"present": 1, "pump": 0}

#: Which half of a frame failed, and the sentence that says so.  Kept in one
#: place so the three stages cannot drift into three different vocabularies.
_STAGE_PHRASE = {
    "capture": "the guest's framebuffer could not be read",
    "present": "the window could not be given the frame",
    "pump": "the window's message loop could not be pumped",
}


def video_geometry_from_serial(text: Union[str, bytes]) -> FrameGeometry:
    """The framebuffer geometry the guest's serial log printed, or a refusal.

    The log is accepted as bytes as well as ``str`` because it is a boot log: it
    can hold a partial line if the run was cut short, and decoding a truncated
    multibyte sequence would raise on a file that is otherwise good evidence.
    That is the same reason :func:`unipad.host.guestobserve.load_addresses_from_serial`
    takes bytes.

    Args:
        text: The serial log, as text or as raw bytes.

    Returns:
        The :class:`~unipad.host.framebuffer.FrameGeometry` the guest printed.

    Raises:
        UniPadError: ``evidence_incomplete`` when the log prints no video line,
            when it prints two lines describing *different* framebuffers, or
            when the depth it prints is not one this module reads;
            ``config_invalid`` when the line it printed cannot describe a
            framebuffer at all (a row stride shorter than one row of pixels).
            Nothing here defaults: a geometry nobody printed is a geometry the
            guest does not have.
    """
    blob = text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text)
    found = VIDEO_LINE.findall(blob)
    if not found:
        raise _refuse(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "the serial log prints no framebuffer line, so the geometry the "
            "guest was handed is not in it",
            expected="video base 0x... WxH row N bytes B bpp",
        )

    # Every matching line, compared as a whole.  Two lines that agree are one
    # fact stated twice -- a log that wraps, or a bootloader that reprints what
    # the kernel was given.  Two that disagree are a guest whose display mode
    # changed under us, and there is no way to tell which one is current: taking
    # the first would be a guess, and taking the last would be the same guess
    # with more confidence.  So it is a refusal, and the refusal names every
    # geometry it saw so a reader can see the change rather than infer it.
    geometries = sorted(
        {
            (int(base, 16), int(width), int(height), int(stride), int(bpp))
            for base, width, height, stride, bpp in found
        }
    )
    if len(geometries) > 1:
        raise _refuse(
            ErrorCode.EVIDENCE_INCOMPLETE,
            f"the serial log prints {len(geometries)} different framebuffer "
            "geometries, so the guest's display mode is not a single fact this "
            "module can read",
            lines=len(found),
            geometries=[
                {"base": hex(base), "width": width, "height": height,
                 "stride": stride, "bpp": bpp}
                for base, width, height, stride, bpp in geometries
            ],
        )

    base, width, height, stride, bpp = geometries[0]
    if bpp != 8 * BYTES_PER_PIXEL:
        # Not a warning.  framebuffer.py measured the channel order on a 32-bit
        # framebuffer and drops the fourth byte; reading a 16- or 24-bit one
        # through that same path would produce a frame whose every pixel is
        # wrong in a way no length check can see.
        raise _refuse(
            ErrorCode.EVIDENCE_INCOMPLETE,
            f"the guest printed a {bpp}-bit framebuffer, and this module reads "
            f"{8 * BYTES_PER_PIXEL}-bit ones",
            bpp=bpp,
            supported_bpp=8 * BYTES_PER_PIXEL,
        )
    try:
        return FrameGeometry(base=base, width=width, height=height, stride=stride)
    except UniPadError as exc:
        raise _refuse(
            ErrorCode.CONFIG_INVALID,
            "the framebuffer the guest printed cannot describe a framebuffer",
            base=hex(base), width=width, height=height, stride=stride,
            cause=exc.message,
        ) from exc


def guest_frame_source(
    client: Any, geometry: FrameGeometry, scratch: Union[str, Path]
) -> GuestFrameSource:
    """A :class:`~unipad.host.framebuffer.GuestFrameSource` over the caller's client.

    The client is *given*, never made.  :class:`unipad.host.guestobserve.GuestMemory`
    holds one QMP connection for the whole session because QEMU takes a single
    client at a time and is slow to take the next, so the caller who opened the
    connection is the one who keeps it -- a source that opened its own would
    contend with the boot controller's and the failure would read as a frame
    that would not arrive.

    Args:
        client: A connected :class:`~unipad.host.qmp.QmpClient`, or anything
            else with ``execute(command, arguments)``.  An object that publishes
            ``is_connected`` as false is refused; one that does not publish it
            at all cannot be asked, and refusing it would make this helper
            unusable with the stubs a test hands in.
        geometry: The framebuffer's geometry, as a
            :class:`~unipad.host.framebuffer.FrameGeometry`.  Required rather
            than derived, because the four numbers are positional and a tuple in
            the wrong order is a frame from the wrong address.
        scratch: Where QMP ``pmemsave`` writes.  Required rather than defaulted
            to a temporary file: ``pmemsave`` writes a file on every read, and a
            temporary directory nobody removes is one leak per run.

    Raises:
        UniPadError: ``config_invalid`` for a geometry that is not a
            ``FrameGeometry`` or a scratch path that is empty;
            ``qmp_unavailable`` when the client says it is not connected.
    """
    if not isinstance(geometry, FrameGeometry):
        raise _refuse(
            ErrorCode.CONFIG_INVALID,
            "the framebuffer geometry must be a FrameGeometry; the four numbers "
            "are positional and a loose value can put them in the wrong order",
            received_type=type(geometry).__name__,
        )
    if not isinstance(scratch, (str, Path)) or not str(scratch).strip():
        raise _refuse(
            ErrorCode.CONFIG_INVALID,
            "pmemsave writes a file on every read, so a scratch path is required",
            received_type=type(scratch).__name__,
        )
    if getattr(client, "is_connected", True) is False:
        raise _refuse(
            ErrorCode.QMP_UNAVAILABLE,
            "the qmp client is not connected, so no frame can be read through it",
        )
    return GuestFrameSource(GuestMemory(client, Path(scratch)), geometry)


@dataclass(frozen=True)
class FrameTiming:
    """One presented frame, and what it cost.

    ``index`` is the frame's position in the run, zero-based, so a timing can be
    matched against the frame that produced it.  ``grab_seconds`` is the read
    from the guest; ``blit_seconds`` is everything from handing the frame to the
    window to the window reporting it had blitted.  They are separate because
    they are separate bottlenecks: the read is one QMP round trip and a file
    read-back, the blit is a ``memmove`` and a ``StretchDIBits``.

    Every recorded timing is a frame the window blitted.  A frame that could not
    be read or could not be blitted ends the run in a refusal, so there is no
    such thing as a timing for a frame that did not happen -- which is what keeps
    ``len(timings)`` equal to the count of presented frames.
    """

    index: int
    grab_seconds: float
    blit_seconds: float
    width: int
    height: int
    presented_bytes: int

    @property
    def total_seconds(self) -> float:
        """Read plus blit: what one frame cost end to end."""
        return self.grab_seconds + self.blit_seconds


@dataclass(frozen=True)
class RunReport:
    """What a run did, and how long each frame took.

    ``presented`` is the count of frames the window actually blitted, and it is
    the number a refusal carries too -- a run that stopped on frame 7 of 30 has
    presented 6, and saying so is the difference between a measurement and a
    story.  ``stop_reason`` distinguishes the honest ways a run ends:
    ``completed`` (every requested frame), ``window_closed`` (the window's own
    message loop posted ``WM_QUIT``), ``stop_requested`` (the caller's predicate
    fired) and ``refused`` (a frame could not be captured or presented, which is
    the stop_reason on the report a :class:`RunRefused` carries).
    """

    requested: Optional[int]
    presented: int
    stop_reason: str
    seconds: float
    timings: tuple[FrameTiming, ...]
    geometry: Optional[dict[str, Any]] = None

    @property
    def complete(self) -> bool:
        """``True`` when every requested frame was presented."""
        return self.requested is not None and self.presented >= self.requested

    def latency(self) -> dict[str, Any]:
        """min/median/max over the run, in milliseconds, plus the split.

        The median is the number that says what a typical frame cost; the max is
        the one that says what the worst cost, and neither is allowed to hide the
        other.  A run that presented nothing has no measurement, and the fields
        come back ``None`` rather than ``0`` -- zero would read as "free".
        """
        if not self.timings:
            return {"frames": 0, "min_ms": None, "median_ms": None,
                    "max_ms": None, "mean_ms": None, "total_ms": None,
                    "grab_median_ms": None, "blit_median_ms": None}
        totals = [timing.total_seconds for timing in self.timings]
        return {
            "frames": len(self.timings),
            "min_ms": round(min(totals) * 1000.0, 3),
            "median_ms": round(statistics.median(totals) * 1000.0, 3),
            "max_ms": round(max(totals) * 1000.0, 3),
            "mean_ms": round(statistics.fmean(totals) * 1000.0, 3),
            "total_ms": round(sum(totals) * 1000.0, 3),
            "grab_median_ms": round(
                statistics.median([t.grab_seconds for t in self.timings]) * 1000.0, 3),
            "blit_median_ms": round(
                statistics.median([t.blit_seconds for t in self.timings]) * 1000.0, 3),
        }

    def to_dict(self) -> dict[str, Any]:
        """The report as one JSON-shapable object, with no paths in it."""
        return {
            "requested": self.requested,
            "presented": self.presented,
            "complete": self.complete,
            "stop_reason": self.stop_reason,
            "seconds": round(self.seconds, 6),
            "geometry": self.geometry,
            "latency": self.latency(),
            "frames": [
                {
                    "index": timing.index,
                    "grab_ms": round(timing.grab_seconds * 1000.0, 3),
                    "blit_ms": round(timing.blit_seconds * 1000.0, 3),
                    "total_ms": round(timing.total_seconds * 1000.0, 3),
                    "width": timing.width,
                    "height": timing.height,
                    "bytes": timing.presented_bytes,
                }
                for timing in self.timings
            ],
        }


class RunRefused(UniPadError):
    """A run that stopped because a frame could not be captured or presented.

    A subclass of :class:`~unipad.errors.UniPadError` rather than a return value,
    so a caller that ignores what :meth:`GuestSurfacePresenter.run` returns still
    cannot mistake a refusal for a still image.  The ``details`` carry the
    machine-readable facts -- which stage failed, at which frame, how many frames
    were presented before it -- and :attr:`report` carries the whole partial
    report, timings included, for a caller that wants the numbers from the frames
    that did happen.
    """

    def __init__(self, code: str, message: str, *, report: "RunReport",
                 **details: Any) -> None:
        super().__init__(code, message, **details)
        self.report = report


class GuestSurfacePresenter:
    """A window, a frame source, and a loop that times every frame.

    Args:
        window: A :class:`~unipad.host.winsurface.SurfaceWindow`.  Required to be
            one rather than duck-typed: the presenter calls exactly
            :data:`WINDOW_CONTRACT`, and a look-alike would keep working after a
            rename in the real class while every real caller broke.
        source: Anything with ``grab()`` returning a P6 PPM -- in practice a
            :class:`~unipad.host.framebuffer.GuestFrameSource`, which is what
            :func:`guest_frame_source` builds.  The frame is re-read on every
            frame by the source itself; nothing here caches one.

    The presenter does not create the window and does not close it.  It did not
    make it, so it must not destroy it: the caller's ``with`` block or explicit
    ``close()`` is what ends the window's life, and a presenter that tore down a
    window it was handed would take the desktop with it.

    Example:
        >>> with SurfaceWindow("UniPad guest") as window:      # doctest: +SKIP
        ...     source = guest_frame_source(client, geometry, scratch)
        ...     report = GuestSurfacePresenter(window, source).run(60)
        ...     print(report.latency()["median_ms"])            # doctest: +SKIP
    """

    def __init__(self, window: SurfaceWindow, source: Any) -> None:
        if not isinstance(window, SurfaceWindow):
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                "the presenter needs a SurfaceWindow; a look-alike would keep "
                "working after a rename in the real class while every real "
                "caller broke",
                received_type=type(window).__name__,
            )
        if not callable(getattr(source, "grab", None)):
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                "the presenter needs a frame source with grab(); without one the "
                "first frame would be the first failure",
                received_type=type(source).__name__,
            )
        self._window = window
        self._source = source

    # --- what it holds ------------------------------------------------

    @property
    def window(self) -> SurfaceWindow:
        """The window this presenter blits into.  It does not own its lifetime."""
        return self._window

    @property
    def source(self) -> Any:
        """The frame source this presenter reads from, once per frame."""
        return self._source

    # --- the loop ------------------------------------------------------

    def run(
        self,
        frames: Optional[int] = None,
        *,
        stop: Optional[Callable[[int], bool]] = None,
    ) -> RunReport:
        """Present frames until the count is reached, the window closes, or ``stop``.

        The message loop is pumped once per presented frame, immediately after
        the blit.  That is the boundary between two frames: the window gets the
        pixels, then it gets the chance to repaint, resize and dispatch input
        before the next read is asked for.  A pump per frame rather than per
        iteration of some inner loop, so the count is exactly the number of
        frames presented and a caller can check it.

        Args:
            frames: How many frames to present.  ``None`` runs until the window
                closes or ``stop`` fires -- which is what an operator-facing
                presenter wants, and is unbounded on purpose.
            stop: Called with the zero-based frame index before each frame; a
                true answer ends the run as ``stop_requested``.

        Returns:
            A :class:`RunReport`.  ``stop_reason`` is ``completed``,
            ``window_closed`` or ``stop_requested``.

        Raises:
            RunRefused: When a frame could not be read, the window would not
                blit it, or the message loop could not be pumped.  The count of
                frames presented before the failure is in ``details`` and the
                partial report is on ``exc.report``.
            UniPadError: ``config_invalid`` for a frame count that is not
                positive -- a run of zero frames has measured nothing, and
                reporting it as a successful run of zero would be a claim.
        """
        if frames is not None and frames <= 0:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                "a run needs at least one frame; a run of zero has measured "
                "nothing and reporting it as a success would be a claim",
                frames=frames,
            )

        timings: list[FrameTiming] = []
        started = time.perf_counter()
        index = 0
        stop_reason = "completed"
        while frames is None or index < frames:
            if stop is not None and stop(index):
                stop_reason = "stop_requested"
                break

            grab_started = time.perf_counter()
            try:
                frame = self._source.grab()
            except Exception as exc:  # noqa: BLE001 - classified, never a traceback
                raise self._refuse_run("capture", index, frames, timings,
                                       started, exc) from exc
            grab_seconds = time.perf_counter() - grab_started

            blit_started = time.perf_counter()
            try:
                result = self._window.present(frame)
            except Exception as exc:  # noqa: BLE001 - classified, never a traceback
                raise self._refuse_run("present", index, frames, timings,
                                       started, exc) from exc
            blit_seconds = time.perf_counter() - blit_started

            # A window that reports it did not blit has not shown the frame, and
            # a run that carried on would leave the previous frame on screen
            # looking exactly like a guest that is not drawing.
            if not (isinstance(result, dict) and result.get("blitted")):
                raise self._refuse_run(
                    "present", index, frames, timings, started,
                    _refuse(
                        ErrorCode.RENDER_FAILED,
                        "the window did not report that it blitted the frame",
                        reported=result if isinstance(result, dict) else type(result).__name__,
                    ),
                )

            timings.append(FrameTiming(
                index=index,
                grab_seconds=grab_seconds,
                blit_seconds=blit_seconds,
                # A window that does not report a geometry is recorded as zero
                # rather than guessed at: zero is visibly "not reported", where a
                # plausible-looking number is not.
                width=int(result.get("width", 0) or 0),
                height=int(result.get("height", 0) or 0),
                presented_bytes=int(result.get("bytes", 0) or 0),
            ))

            try:
                pumped = self._window.pump()
            except Exception as exc:  # noqa: BLE001 - classified, never a traceback
                raise self._refuse_run("pump", index, frames, timings,
                                       started, exc) from exc
            # A negative return is the window's own way of asking to stop --
            # ``SurfaceWindow.pump`` returns -1 when WM_QUIT arrived.  Anything
            # that is not a number is not that, and guessing would end a healthy
            # run on a window that never asked.
            if isinstance(pumped, int) and pumped < 0:
                # The window posted WM_QUIT: the operator closed it.  That is a
                # way of finishing, not a failure, and the frames before it were
                # really presented.
                stop_reason = "window_closed"
                break
            index += 1

        return self._report(frames, timings, stop_reason, started)

    # --- the report ----------------------------------------------------

    def _report(self, requested: Optional[int], timings: list[FrameTiming],
                stop_reason: str, started: float) -> RunReport:
        # Reported when the source is a GuestFrameSource and null otherwise:
        # a report that cannot say which framebuffer it read is a report about an
        # unknown frame.  MEASURED: the first version computed the local below
        # and then passed ``None`` anyway, and
        # ``test_the_report_names_the_geometry_it_read_and_is_json_shapable``
        # went red on it -- which is that test doing the only job it has.
        geometry = getattr(self._source, "geometry", None)
        described: Optional[dict[str, Any]] = None
        if geometry is not None:
            described = {
                "base": hex(int(geometry.base)),
                "width": int(geometry.width),
                "height": int(geometry.height),
                "stride": int(geometry.stride),
                "bytes": int(geometry.size),
            }
        return RunReport(
            requested=requested,
            presented=len(timings),
            stop_reason=stop_reason,
            seconds=time.perf_counter() - started,
            timings=tuple(timings),
            geometry=described,
        )

    def _refuse_run(self, stage: str, index: int, requested: Optional[int],
                    timings: list[FrameTiming], started: float,
                    cause: BaseException) -> RunRefused:
        """A refusal that says what succeeded before it.

        The cause's own code is kept when it has one -- a short read stays
        ``evidence_incomplete`` and a frame the surface would not take stays
        ``render_failed``, because a caller branches on the code and a normalised
        one would throw away the branch.  Anything without a code becomes
        ``render_failed``: it reached the presenter from somewhere else, and
        "something this module did not expect" is a rendering failure of the run
        rather than a fact about the guest.
        """
        code = getattr(cause, "code", None)
        if not isinstance(code, str) or not code:
            code = ErrorCode.RENDER_FAILED
        report = self._report(requested, timings, "refused", started)
        return RunRefused(
            code,
            f"frame {index} of the run could not be presented: "
            f"{_STAGE_PHRASE[stage]}",
            report=report,
            stage=stage,
            frame_index=index,
            frames_requested=requested,
            frames_presented=len(timings),
            cause_code=getattr(cause, "code", None),
            cause_message=str(cause) or type(cause).__name__,
            cause_type=type(cause).__name__,
        )
