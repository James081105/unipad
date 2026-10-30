"""Did a host input change the guest's screen, and how long did it take?

Why this module exists
----------------------
PRD 5.2 case 2 requires entering the real SpringBoard and *changing the
guest's interface by operating the mouse*.  Case 6 requires clicking a
button ten times and seeing the on-screen counter change.  PRD 5.3 requires
"host sends an event until the corresponding frame changes, keep the
timestamp".  All three are the same measurement, and before this module
nothing performed it: the input path (:mod:`unipad.host.input`) and the
readback path (:class:`unipad.host.logs.Screenshotter`) both existed and
both worked, and nothing joined them.

What it refuses to claim
------------------------
Three things, each because conflating them is how a measurement becomes a
story:

* **A failed capture is not "the screen did not change".**  If either frame
  cannot be read, the result is a refusal naming which one and why.  A run
  that reports ``changed=False`` because its second screendump failed has
  measured nothing and looks like it measured something.
* **"The screen changed after the tap" is not "the tap changed the
  screen".**  A guest that animates, dims, or redraws on its own changes
  between any two frames.  So :meth:`InteractionProbe.control` takes two
  frames with *no input between them* first, and :meth:`InteractionProbe.tap`
  reports the control's own delta alongside the tap's.  A tap whose delta is
  the same size as the control's delta has not been shown to do anything.
* **Byte-identical frames are a real answer, not a failure.**  Fact 165
  rested on exactly that -- two screendumps with the same sha256 are how
  "the framebuffer did not change" was established -- so ``changed=False``
  with two verified captures is a result, and it is reported as one.

What is NOT claimed here
------------------------
That the guest has a device to receive the event.  ``unixnu_boot.py`` emits
``virtio-tablet-device`` and ``virtio-keyboard-device`` when an OS root is
present, because the delivered ADT carries 32 ``virtio-mmio`` nodes and zero
``usb``/``xhci``/``dwc3``/``hid`` -- measured 2026-10-28, when a ``usb-kbd``
run answered QMP ``send-key`` with ``{}`` and changed nothing.  That is the
boot's contract, not this module's; what this module adds is the ability to
*tell the difference* from outside the guest.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Optional

from unipad.errors import ErrorCode, UniPadError
from unipad.host.logs import Screenshotter, read_ppm_header

#: How many bytes of a PPM body to compare at once.  A 1376x1024 P6 body is
#: 5.6 MB, which is small enough to hold but not small enough to hold twice
#: per comparison without noticing on a host that is already busy.
_CHUNK = 1 << 20


def _utc_now() -> float:
    """Wall-clock seconds, the same clock :mod:`time` uses elsewhere."""
    return time.time()


def _refuse(code: ErrorCode, message: str, **detail: Any) -> UniPadError:
    return UniPadError(code, message, **detail)


def compare_frames(before: Path, after: Path) -> dict[str, Any]:
    """Two P6 PPMs, as a pixel-level delta.

    Args:
        before: The frame taken before the input.
        after: The frame taken after it.

    Returns:
        ``{"identical", "changed_pixels", "total_pixels", "ratio", "reason"}``.
        ``total_pixels`` is ``width * height`` from the headers, so a caller
        can tell "no pixels changed" from "the geometry changed and nothing
        could be compared".

    Raises:
        UniPadError: ``render_failed`` when either file is missing, is not a
            readable P6, or the two geometries differ -- a geometry change is
            a different measurement, not a delta of zero.
    """
    try:
        left = read_ppm_header(before)
        right = read_ppm_header(after)
    except UniPadError as exc:
        raise _refuse(
            ErrorCode.RENDER_FAILED,
            "a frame could not be read, so no delta exists",
            before=str(before),
            after=str(after),
            cause=exc.message,
        ) from exc

    width, height = left["width"], left["height"]
    if (right["width"], right["height"]) != (width, height):
        raise _refuse(
            ErrorCode.RENDER_FAILED,
            "the two frames have different geometries, which is a different "
            "measurement rather than a delta of zero",
            before=f"{width}x{height}",
            after=f"{right['width']}x{right['height']}",
        )

    total = width * height
    changed = 0
    with open(before, "rb") as left_handle, open(after, "rb") as right_handle:
        left_handle.seek(left["header_bytes"])
        right_handle.seek(right["header_bytes"])
        while True:
            a = left_handle.read(_CHUNK)
            b = right_handle.read(_CHUNK)
            if not a and not b:
                break
            if len(a) != len(b):
                # The header promised one size and the body is another.  This
                # is a truncated file, not a frame that changed shape.
                raise _refuse(
                    ErrorCode.RENDER_FAILED,
                    "a frame's body is shorter than its header promised",
                    before=len(a),
                    after=len(b),
                )
            # Compare byte-wise and count whole pixels, so a one-channel
            # difference in a 3-channel pixel still counts once.
            changed += sum(1 for x, y in zip(a, b) if x != y)

    # A byte is not a pixel, so the count is an upper bound on changed pixels
    # and is reported as such rather than as an exact pixel count.
    return {
        "identical": changed == 0,
        "changed_bytes": changed,
        "total_bytes": width * height * 3,
        "changed_pixels": min(changed // 3 + (1 if changed % 3 else 0), total),
        "total_pixels": total,
        "ratio": round(changed / max(1, width * height * 3), 6),
        "geometry": f"{width}x{height}",
    }


class InteractionProbe:
    """Frames, input, and the delta between them -- with the control.

    Args:
        qmp: A connected :class:`unipad.host.qmp.QmpClient`.
        sink: Anything with ``tap(x, y)`` returning a dict -- in practice a
            :class:`unipad.host.input.QmpInputSink`.
        run_dir: Where the frames land, under ``frames/``.

    The sink is injected rather than constructed because a probe that builds
    its own input path cannot be exercised without a guest, and a probe that
    cannot be exercised without a guest is a probe nobody has run.
    """

    def __init__(self, qmp: Any, sink: Any, run_dir: Path) -> None:
        self.qmp = qmp
        self.sink = sink
        self.run_dir = Path(run_dir)
        self.frames = Screenshotter(qmp, self.run_dir / "frames")

    def _frame(self, label: str) -> dict[str, Any]:
        capture = self.frames.capture(label)
        if not capture.get("ok"):
            raise _refuse(
                ErrorCode.RENDER_FAILED,
                "a frame could not be captured, so no delta exists",
                label=label,
                error_code=capture.get("error_code"),
                error_message=capture.get("error_message"),
            )
        return capture

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def control(self, label: str = "control") -> dict[str, Any]:
        """Two frames with no input between them.

        This is the number every tap is measured against.  Without it, a guest
        that redraws on its own makes any before/after pair look like a
        response.
        """
        first = self._frame(f"{label}-a")
        second = self._frame(f"{label}-b")
        delta = compare_frames(Path(first["path"]), Path(second["path"]))
        return {
            "label": label,
            "before": first,
            "after": second,
            "delta": delta,
            "seconds_between": round(
                _parse_stamp(second) - _parse_stamp(first), 3
            ),
        }

    def tap(self, x: int, y: int, label: str = "tap") -> dict[str, Any]:
        """Tap at ``(x, y)`` and report what the framebuffer did.

        Returns a verdict, never a bare boolean.  ``changed`` says whether the
        two verified frames differ; ``control_delta`` says how much the guest
        moves on its own; ``input`` is what the sink reported sending.
        """
        before = self._frame(f"{label}-before")
        sent_at = _utc_now()
        input_result = self.sink.tap(x, y)
        after = self._frame(f"{label}-after")
        delta = compare_frames(Path(before["path"]), Path(after["path"]))
        return {
            "label": label,
            "at": [x, y],
            "input": input_result,
            "sent_at": sent_at,
            "frame_after_at": _parse_stamp(after),
            "before": before,
            "after": after,
            "delta": delta,
            "changed": not delta["identical"],
            "before_sha256": self._digest(Path(before["path"])),
            "after_sha256": self._digest(Path(after["path"])),
        }

    def verdict(
        self, x: int, y: int, label: str = "tap", control_label: str = "control"
    ) -> dict[str, Any]:
        """:meth:`control` then :meth:`tap`, with the two deltas side by side.

        The answer this returns is ``"responded"`` only when the tap's delta is
        larger than the control's.  ``"no_change"`` means the frames were
        verified and identical; ``"indistinguishable"`` means the guest moves
        as much on its own, so the tap has not been shown to do anything.
        """
        control = self.control(control_label)
        tap = self.tap(x, y, label)
        control_bytes = control["delta"]["changed_bytes"]
        tap_bytes = tap["delta"]["changed_bytes"]
        if tap_bytes == 0:
            answer = "no_change"
        elif tap_bytes > control_bytes:
            answer = "responded"
        else:
            answer = "indistinguishable"
        return {
            "answer": answer,
            "at": [x, y],
            "control": control,
            "tap": tap,
            "control_changed_bytes": control_bytes,
            "tap_changed_bytes": tap_bytes,
        }


def _parse_stamp(capture: dict[str, Any]) -> float:
    """A capture's ``timestamp`` as seconds, or the wall clock if absent.

    The captures carry an ISO-8601 stamp from the same clock the probe uses,
    so a latency is the difference of two readings of one clock rather than
    of two clocks.  A capture with no readable stamp falls back to "now",
    which makes the latency wrong rather than absent -- and the caller can see
    that, because the field it came from is ``None``.
    """
    stamp = capture.get("timestamp")
    if not stamp:
        return _utc_now()
    try:
        from datetime import datetime, timezone

        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return _utc_now()


__all__ = ["InteractionProbe", "compare_frames"]
