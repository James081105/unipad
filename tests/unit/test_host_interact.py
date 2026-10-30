"""The input-to-frame probe: what it measures, and the three things it refuses to.

The module's whole value is that a *failed* capture and a *verified
no-change* are different answers.  So most of this file is about the
refusals: a probe that reports ``changed=False`` because its second
screendump failed has measured nothing and looks like it measured
something.

The control is the other half.  ``"the screen changed after the tap"`` is
not ``"the tap changed the screen"``, and the only thing that separates them
is two frames with no input between them.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Optional

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.interact import InteractionProbe, compare_frames

#: A geometry small enough that the byte comparison in the tests is cheap.
WIDTH, HEIGHT = 8, 4


def _ppm(path: Path, pixels: bytes, width: int = WIDTH, height: int = HEIGHT) -> Path:
    """A binary P6 PPM, written the way QMP's screendump writes one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"P6\n" + f"{width} {height}\n255\n".encode("ascii") + pixels
    )
    return path


def _flat(value: int, width: int = WIDTH, height: int = HEIGHT) -> bytes:
    return bytes([value]) * (width * height * 3)


class _FakeScreenshotter:
    """Stands in for :class:`unipad.host.logs.Screenshotter`.

    It writes the PPM the caller hands it, so a test controls exactly what
    each capture contains -- including a capture that fails, which is the case
    the whole module is built around.
    """

    def __init__(self, qmp: Any, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.calls: list[str] = []
        self._script: list[Optional[dict[str, Any]]] = []

    def script(self, *captures: Optional[dict[str, Any]]) -> None:
        self._script = list(captures)

    def capture(self, name: Optional[str] = None) -> dict[str, Any]:
        index = len(self.calls)
        self.calls.append(str(name))
        if index >= len(self._script):
            raise AssertionError(f"no scripted capture for call {index} ({name})")
        planned = self._script[index]
        if planned is None:
            # The failure shape logs.Screenshotter actually returns.
            return {
                "ok": False,
                "path": str(self.run_dir / f"{name}.ppm"),
                "name": f"{name}.ppm",
                "bytes": None,
                "sha256": None,
                "width": None,
                "height": None,
                "timestamp": "2026-10-31T00:00:00+00:00",
                "error_code": ErrorCode.RENDER_FAILED,
                "error_message": "qmp screendump reported success but no file was produced",
            }
        target = _ppm(self.run_dir / f"{name}.ppm", planned["pixels"])
        return {
            "ok": True,
            "path": str(target),
            "name": f"{name}.ppm",
            "bytes": target.stat().st_size,
            "sha256": "0" * 64,
            "width": planned.get("width", WIDTH),
            "height": planned.get("height", HEIGHT),
            "timestamp": planned.get("timestamp", "2026-10-31T00:00:00+00:00"),
            "error_code": None,
            "error_message": None,
        }


class _FakeSink:
    """Records what was sent, and can be made to fail."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.raise_on_tap = False

    def tap(self, x: int, y: int) -> dict[str, Any]:
        if self.raise_on_tap:
            raise UniPadError(ErrorCode.INPUT_FAILED, "the sink refused the tap")
        self.taps.append((x, y))
        return {"move": {}, "press": {}, "release": {}}


def _probe(
    tmp_path: Path, sink: Optional[_FakeSink] = None
) -> tuple[InteractionProbe, _FakeScreenshotter, _FakeSink]:
    qmp = object()
    screenshots = _FakeScreenshotter(qmp, tmp_path)
    sink = sink or _FakeSink()
    probe = InteractionProbe.__new__(InteractionProbe)
    probe.qmp = qmp
    probe.sink = sink
    probe.run_dir = tmp_path
    probe.frames = screenshots
    return probe, screenshots, sink


# --- the delta itself ------------------------------------------------------


def test_two_identical_frames_are_a_delta_of_zero(tmp_path: Path) -> None:
    """Byte-identical is a result, not a failure.

    Fact 165 rested on exactly this: two screendumps with the same sha256 are
    how "the framebuffer did not change" was established.
    """
    a = _ppm(tmp_path / "a.ppm", _flat(7))
    b = _ppm(tmp_path / "b.ppm", _flat(7))
    delta = compare_frames(a, b)
    assert delta["identical"] is True
    assert delta["changed_bytes"] == 0
    assert delta["changed_pixels"] == 0
    assert delta["total_pixels"] == WIDTH * HEIGHT


def test_a_changed_pixel_is_counted_once(tmp_path: Path) -> None:
    pixels = bytearray(_flat(0))
    pixels[0:3] = b"\xff\xff\xff"
    a = _ppm(tmp_path / "a.ppm", _flat(0))
    b = _ppm(tmp_path / "b.ppm", bytes(pixels))
    delta = compare_frames(a, b)
    assert delta["identical"] is False
    assert delta["changed_pixels"] == 1, delta


def test_a_frame_that_cannot_be_read_is_a_refusal_not_a_delta_of_zero(
    tmp_path: Path,
) -> None:
    """The whole point: a missing file must not read as "nothing changed"."""
    a = _ppm(tmp_path / "a.ppm", _flat(1))
    with pytest.raises(UniPadError) as refused:
        compare_frames(a, tmp_path / "absent.ppm")
    assert refused.value.code == ErrorCode.RENDER_FAILED
    assert "no delta exists" in refused.value.message


def test_two_geometries_are_a_different_measurement_not_a_delta_of_zero(
    tmp_path: Path,
) -> None:
    a = _ppm(tmp_path / "a.ppm", _flat(1))
    b = _ppm(tmp_path / "b.ppm", _flat(1), width=4, height=8)
    with pytest.raises(UniPadError) as refused:
        compare_frames(a, b)
    assert refused.value.code == ErrorCode.RENDER_FAILED
    assert "different geometries" in refused.value.message


def test_a_truncated_body_is_a_refusal(tmp_path: Path) -> None:
    """A header promising more than the file holds is not a frame."""
    target = tmp_path / "short.ppm"
    target.write_bytes(b"P6\n8 4\n255\n" + b"\x00" * 10)
    good = _ppm(tmp_path / "good.ppm", _flat(0))
    with pytest.raises(UniPadError) as refused:
        compare_frames(good, target)
    assert refused.value.code == ErrorCode.RENDER_FAILED
    assert "shorter than its header" in refused.value.message


# --- the probe: a failed capture is a refusal ------------------------------


def test_a_capture_that_fails_refuses_rather_than_reporting_no_change(
    tmp_path: Path,
) -> None:
    probe, screenshots, _sink = _probe(tmp_path)
    screenshots.script({"pixels": _flat(3)}, None)  # the second capture fails
    with pytest.raises(UniPadError) as refused:
        probe.tap(4, 2)
    assert refused.value.code == ErrorCode.RENDER_FAILED
    assert "no delta exists" in refused.value.message
    # ...and the input WAS sent, which is why this matters: a caller that
    # treated the refusal as "no change" would believe the tap did nothing.
    assert probe.sink.taps == [(4, 2)]


def test_a_tap_whose_screen_did_not_change_is_reported_as_such(
    tmp_path: Path,
) -> None:
    probe, screenshots, sink = _probe(tmp_path)
    screenshots.script({"pixels": _flat(5)}, {"pixels": _flat(5)})
    result = probe.tap(1, 1)
    assert result["changed"] is False
    assert result["delta"]["identical"] is True
    assert result["before_sha256"] == result["after_sha256"]
    assert sink.taps == [(1, 1)]
    assert result["at"] == [1, 1]


def test_a_tap_whose_screen_did_change_is_reported_with_both_digests(
    tmp_path: Path,
) -> None:
    probe, screenshots, _sink = _probe(tmp_path)
    screenshots.script({"pixels": _flat(0)}, {"pixels": _flat(9)})
    result = probe.tap(2, 3)
    assert result["changed"] is True
    assert result["before_sha256"] != result["after_sha256"]
    assert result["delta"]["changed_pixels"] == WIDTH * HEIGHT


# --- the control, which is what makes the tap mean anything ----------------


def test_the_control_takes_two_frames_with_no_input_between_them(
    tmp_path: Path,
) -> None:
    probe, screenshots, sink = _probe(tmp_path)
    screenshots.script({"pixels": _flat(1)}, {"pixels": _flat(1)})
    control = probe.control()
    assert control["delta"]["identical"] is True
    assert sink.taps == [], "the control must not send anything"
    assert screenshots.calls == ["control-a", "control-b"]


def test_a_guest_that_moves_on_its_own_makes_the_tap_indistinguishable(
    tmp_path: Path,
) -> None:
    """The shape fact 165 would have mis-read without a control.

    The tap changes every pixel -- and so does nothing at all, because the
    guest redraws between any two frames.  Only the control can tell those
    apart, and it says they are the same.
    """
    probe, screenshots, _sink = _probe(tmp_path)
    screenshots.script(
        {"pixels": _flat(0)},   # control-a
        {"pixels": _flat(4)},   # control-b: the guest moved on its own
        {"pixels": _flat(4)},   # tap-before
        {"pixels": _flat(9)},   # tap-after: also moved
    )
    verdict = probe.verdict(5, 5)
    assert verdict["answer"] == "indistinguishable", verdict
    assert verdict["control_changed_bytes"] == verdict["tap_changed_bytes"]


def test_a_tap_that_beats_the_control_is_a_response(tmp_path: Path) -> None:
    probe, screenshots, _sink = _probe(tmp_path)
    screenshots.script(
        {"pixels": _flat(0)},          # control-a
        {"pixels": _flat(0)},          # control-b: nothing moved
        {"pixels": _flat(0)},          # tap-before
        {"pixels": _flat(0xAA)},       # tap-after: everything moved
    )
    verdict = probe.verdict(5, 5)
    assert verdict["answer"] == "responded", verdict
    assert verdict["tap_changed_bytes"] > verdict["control_changed_bytes"]


def test_an_unchanged_screen_is_no_change_even_when_the_control_moved(
    tmp_path: Path,
) -> None:
    """``no_change`` is about the tap's own two verified frames."""
    probe, screenshots, _sink = _probe(tmp_path)
    screenshots.script(
        {"pixels": _flat(0)},
        {"pixels": _flat(3)},   # the guest moved during the control
        {"pixels": _flat(3)},
        {"pixels": _flat(3)},   # the tap changed nothing
    )
    verdict = probe.verdict(5, 5)
    assert verdict["answer"] == "no_change", verdict


# --- the sink's own failure ------------------------------------------------


def test_a_sink_that_refuses_the_tap_propagates_the_refusal(
    tmp_path: Path,
) -> None:
    probe, screenshots, sink = _probe(tmp_path)
    sink.raise_on_tap = True
    screenshots.script({"pixels": _flat(1)}, {"pixels": _flat(2)})
    with pytest.raises(UniPadError) as refused:
        probe.tap(1, 1)
    assert refused.value.code == ErrorCode.INPUT_FAILED


def test_the_probe_does_not_build_its_own_input_path() -> None:
    """A probe that constructs its sink cannot be run without a guest.

    That is why the sink is injected: this whole file exercises the probe with
    no QEMU, no kernel and no guest, which is the only reason the refusals
    above could be written at all.
    """
    import inspect

    signature = inspect.signature(InteractionProbe.__init__)
    assert "sink" in signature.parameters
    assert signature.parameters["sink"].default is inspect.Parameter.empty


def test_the_timestamps_come_from_one_clock(tmp_path: Path) -> None:
    """Both stamps are the captures' own, so a latency is one clock minus itself.

    A probe that stamped the input with its own clock and the frame with the
    capture's would report a latency that is the sum of two offsets.
    """
    probe, screenshots, _sink = _probe(tmp_path)
    screenshots.script(
        {"pixels": _flat(1), "timestamp": "2026-10-31T00:00:00+00:00"},
        {"pixels": _flat(1), "timestamp": "2026-10-31T00:00:02+00:00"},
    )
    result = probe.tap(1, 1)
    assert result["before"]["timestamp"] == "2026-10-31T00:00:00+00:00"
    assert result["after"]["timestamp"] == "2026-10-31T00:00:02+00:00"
    # Two seconds, from the two captures' own stamps -- so the number is the
    # difference of one clock's two readings and nothing else.
    assert round(result["frame_after_at"] - _stamp_seconds(result["before"])) == 2


def _stamp_seconds(capture: dict[str, Any]) -> float:
    """A capture's ISO stamp as epoch seconds, the way the module parses it."""
    from datetime import datetime

    return datetime.fromisoformat(
        str(capture["timestamp"]).replace("Z", "+00:00")
    ).timestamp()
