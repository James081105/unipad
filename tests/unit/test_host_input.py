"""Unit tests for :mod:`unipad.host.input` (PRD 3.1 FR04, 4.2, 6 M12, 5.2).

M12 is the interaction gate: "点击、拖动、Home、文本用例通过" -- and its
"insufficient" case is "宿主窗口收到按键但客体无响应".  The host side of that
contract is coordinate fidelity: a click must land on the pixel the user aimed
at, which is why :func:`scale_point` and :func:`guest_to_qemu_abs` are pure and
unit-testable, and why a dropped event is a hard ``input_failed`` rather than
silence.

PRD 4.2 lists the input route as "复用 VirtIO tablet/Home，再补文字" with
"坐标缩放、拖动、释放事件与实际文字输入" as the前置验证 items.  These tests pin:

- host-window -> guest-display scaling, per axis, with out-of-range clamping;
- guest pixel -> QEMU absolute axis (``0..INPUT_EVENT_ABS_MAX``);
- :class:`NullInputSink` recording what a headless run *would* have sent;
- :class:`QmpInputSink` driving the real QMP client against the real fake QEMU
  process, with the scaled coordinates visible in the fake's input journal.

M12 stays UNVERIFIED for real hardware: whether the pinned board exposes a
virtio-tablet (absolute) or virtio-mouse (relative) device decides whether these
events land where expected.  A green run here proves the host sent the right
numbers (docs/testing-strategy.md D2/D3).
"""

from __future__ import annotations

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.input import (
    SerialInputSink,

    ABS_AXIS_MAX,
    InputSink,
    NullInputSink,
    QmpInputSink,
    guest_to_qemu_abs,
    qcode_for_char,
    scale_point,
)
from unipad.host.qmp import QmpClient
from tests.fakes import fake_qemu as fq

#: The iPad-class framebuffer from ``config/machine.json``.
GUEST_SIZE = (1112, 834)


# ---------------------------------------------------------------------------
# scale_point: host window -> guest display
# ---------------------------------------------------------------------------


def test_scale_point_is_the_identity_when_the_sizes_match():
    """PRD 4.2: a 1:1 window must not move the click."""
    assert scale_point((100, 200), GUEST_SIZE, GUEST_SIZE) == (100, 200)
    assert scale_point((0, 0), GUEST_SIZE, GUEST_SIZE) == (0, 0)
    assert scale_point((1111, 833), GUEST_SIZE, GUEST_SIZE) == (1111, 833)


def test_scale_point_scales_each_axis_independently():
    """PRD 4.2: non-uniform scaling is per axis, never a single factor."""
    # A window twice as wide as the guest: x halves, y passes through.
    assert scale_point((100, 50), (200, 100), (100, 50)) == (50, 25)
    # A window twice as tall as the guest: x passes through, y halves.
    assert scale_point((50, 100), (100, 200), (50, 100)) == (25, 50)


def test_scale_point_maps_an_upscaled_window_onto_the_guest():
    """PRD 4.2: a 2x host window maps back onto guest pixels exactly."""
    host = (2224, 1668)

    assert scale_point((1112, 417), host, GUEST_SIZE) == (556, 208)
    assert scale_point((2224, 1668), host, GUEST_SIZE) == (1111, 833)
    assert scale_point((0, 0), host, GUEST_SIZE) == (0, 0)


def test_scale_point_clamps_out_of_range_points():
    """PRD 4.2: a click outside the guest area still yields a valid coordinate."""
    host = (2224, 1668)

    assert scale_point((-50, -50), host, GUEST_SIZE) == (0, 0)
    assert scale_point((99999, 99999), host, GUEST_SIZE) == (1111, 833)


def test_scale_point_never_returns_a_coordinate_outside_the_display():
    """PRD 4.2: the invariant every caller relies on."""
    host = (1373, 911)

    for x in range(-100, 1500, 97):
        for y in range(-100, 1000, 83):
            gx, gy = scale_point((x, y), host, GUEST_SIZE)
            assert 0 <= gx < GUEST_SIZE[0]
            assert 0 <= gy < GUEST_SIZE[1]


@pytest.mark.parametrize("host", [(0, 100), (100, 0), (-1, 100)])
def test_scale_point_rejects_a_non_positive_host_size(host):
    """PRD 4.2: failing loudly beats silently clicking the wrong pixel."""
    with pytest.raises(ValueError):
        scale_point((10, 10), host, GUEST_SIZE)


@pytest.mark.parametrize("guest", [(0, 100), (100, 0), (-5, -5)])
def test_scale_point_rejects_a_non_positive_guest_size(guest):
    """PRD 4.2: a zero-sized display cannot be mapped onto."""
    with pytest.raises(ValueError):
        scale_point((10, 10), (100, 100), guest)


@pytest.mark.parametrize("point", [(1.5, 2), (1, 2.0), (True, 3), (1, 2, 3), (5,)])
def test_scale_point_rejects_a_malformed_point(point):
    """PRD 4.2: floats and bools are refused, not silently truncated."""
    with pytest.raises(ValueError):
        scale_point(point, (100, 100), GUEST_SIZE)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# guest_to_qemu_abs: guest pixel -> QEMU absolute axis
# ---------------------------------------------------------------------------


def test_guest_to_qemu_abs_maps_the_display_corners():
    """PRD 4.2: the axis range is QEMU's ``INPUT_EVENT_ABS_MAX`` at the far edge."""
    assert guest_to_qemu_abs((0, 0), GUEST_SIZE) == (0, 0)
    assert guest_to_qemu_abs((1111, 833), GUEST_SIZE) == (ABS_AXIS_MAX, ABS_AXIS_MAX)
    assert ABS_AXIS_MAX == 0x7FFF


def test_guest_to_qemu_abs_is_monotonic_in_both_axes():
    """PRD 4.2: a larger guest pixel must never map to a smaller axis value."""
    for fixed in (0, 417, 833):
        xs = [guest_to_qemu_abs((x, fixed), GUEST_SIZE)[0] for x in range(0, GUEST_SIZE[0], 37)]
        assert xs == sorted(xs), f"x axis is not monotonic at y={fixed}"
        assert len(set(xs)) == len(xs), f"x axis stalls at y={fixed}"
    for fixed in (0, 556, 1111):
        ys = [guest_to_qemu_abs((fixed, y), GUEST_SIZE)[1] for y in range(0, GUEST_SIZE[1], 37)]
        assert ys == sorted(ys), f"y axis is not monotonic at x={fixed}"
        assert len(set(ys)) == len(ys), f"y axis stalls at x={fixed}"


def test_guest_to_qemu_abs_clamps_values_beyond_the_display():
    """PRD 4.2: an out-of-range guest pixel still yields an in-range event."""
    assert guest_to_qemu_abs((-10, -10), GUEST_SIZE) == (0, 0)
    assert guest_to_qemu_abs((99999, 99999), GUEST_SIZE) == (ABS_AXIS_MAX, ABS_AXIS_MAX)


@pytest.mark.parametrize("guest", [(0, 100), (100, 0), (-1, -1)])
def test_guest_to_qemu_abs_rejects_a_non_positive_guest_size(guest):
    """PRD 4.2: an unusable display size is a caller bug."""
    with pytest.raises(ValueError):
        guest_to_qemu_abs((5, 5), guest)


# ---------------------------------------------------------------------------
# qcode_for_char
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        ("a", ["a"]),
        ("A", ["shift", "a"]),
        ("7", ["7"]),
        (" ", ["spc"]),
        ("\n", ["ret"]),
        ("\t", ["tab"]),
        (".", ["dot"]),
        ("-", ["minus"]),
        ("/", ["slash"]),
    ],
)
def test_qcode_for_char_maps_the_supported_characters(char, expected):
    """PRD 4.2: text input is expressed as QCode names."""
    assert qcode_for_char(char) == expected


@pytest.mark.parametrize("char", ["!", "€", "中", "ab", ""])
def test_qcode_for_char_reports_an_unmappable_character(char):
    """PRD 4.2: an unsupported character is named, never silently dropped."""
    assert qcode_for_char(char) is None


# ---------------------------------------------------------------------------
# NullInputSink
# ---------------------------------------------------------------------------


def test_null_input_sink_records_every_call():
    """PRD 9.2: a headless run records what it *would* have sent."""
    sink = NullInputSink()

    assert sink.pointer_move(10, 20)["action"] == "pointer_move"
    assert sink.tap(30, 40)["action"] == "tap"
    assert sink.drag(1, 2, 3, 4)["action"] == "drag"
    assert sink.home()["action"] == "home"
    assert sink.key("ctrl-alt-f1")["action"] == "key"
    assert sink.text("hello")["action"] == "text"

    assert [call[0] for call in sink.calls] == [
        "pointer_move",
        "tap",
        "drag",
        "home",
        "key",
        "text",
    ]
    assert sink.calls[0][1] == (10, 20)
    assert sink.calls[2][1] == (1, 2, 3, 4)
    assert sink.calls[2][2]["steps"] == 8
    assert sink.calls[4][1] == (["ctrl", "alt", "f1"],)


def test_null_input_sink_never_claims_delivery():
    """PRD 7.4: a sink that touched nothing must not report an interaction."""
    sink = NullInputSink()

    for result in (
        sink.pointer_move(1, 2),
        sink.tap(1, 2),
        sink.drag(1, 2, 3, 4),
        sink.home(),
        sink.key("f1"),
        sink.text("abc"),
    ):
        assert result["delivered"] is False


def test_null_input_sink_text_skips_every_character():
    """PRD 9.2: nothing typed means nothing typed, not a partial claim."""
    sink = NullInputSink()

    result = sink.text("abc")

    assert result["typed"] == 0
    assert result["skipped"] == ["a", "b", "c"]


def test_null_input_sink_rejects_a_non_string_text():
    """PRD 9.1: bad input is reported with a stable code."""
    sink = NullInputSink()

    with pytest.raises(UniPadError) as excinfo:
        sink.text(123)  # type: ignore[arg-type]

    assert excinfo.value.code == ErrorCode.INPUT_FAILED


def test_null_input_sink_to_dict_is_json_serialisable():
    """PRD 9.2: the record is machine-readable evidence."""
    import json

    sink = NullInputSink()
    sink.tap(5, 6)
    sink.key("f1")

    document = json.loads(json.dumps(sink.to_dict()))

    assert document["calls"][0] == {"action": "tap", "args": [5, 6], "kwargs": {}}


def test_null_input_sink_satisfies_the_input_sink_protocol():
    """PRD 4.2: the protocol is what the controller codes against."""
    assert isinstance(NullInputSink(), InputSink)
    assert not isinstance(object(), InputSink)


# ---------------------------------------------------------------------------
# QmpInputSink against the real QMP client + real fake QEMU process
# ---------------------------------------------------------------------------


@pytest.fixture
def qmp_sink(tmp_path):
    """A :class:`QmpInputSink` wired to a live fake QEMU QMP server."""
    with fq.fake_qemu_process(workdir=tmp_path) as fake:
        assert fake.wait_for_port(timeout=10.0), fake.stderr_text()
        client = QmpClient(port=fake.port, timeout=5.0)
        client.connect()
        try:
            yield QmpInputSink(
                client,
                {"width": GUEST_SIZE[0], "height": GUEST_SIZE[1], "format": "ramfb"},
                host_size=(2224, 1668),
                key_delay=0.0,
            ), fake
        finally:
            client.close()


def _abs_events(events: list[dict]) -> list[tuple[int, int]]:
    """Extract the ``(x, y)`` absolute values from journalled input events."""
    axes: list[tuple[int, int]] = []
    for event in events:
        if event.get("channel") != "input-send-event":
            continue
        values = {item["data"].get("axis"): item["data"].get("value") for item in event["events"]}
        if "x" in values and "y" in values:
            axes.append((values["x"], values["y"]))
    return axes


def test_qmp_input_sink_sends_the_scaled_pointer_position(qmp_sink):
    """PRD 4.2: a host click lands on the guest pixel the user aimed at."""
    sink, fake = qmp_sink

    sink.pointer_move(1112, 417)  # half of a 2x window

    journal = fake.input_events()
    assert _abs_events(journal) == [(16398, 8182)]


def test_qmp_input_sink_tap_is_move_press_release(qmp_sink):
    """PRD 4.2: a tap is a move plus a press/release pair."""
    sink, fake = qmp_sink

    result = sink.tap(0, 0)

    channels = [event["channel"] for event in fake.input_events()]
    assert channels == ["input-send-event"] * 3
    buttons = [
        event["events"][0]["data"]
        for event in fake.input_events()
        if event["events"][0]["type"] == "btn"
    ]
    assert buttons == [{"button": "left", "down": True}, {"button": "left", "down": False}]
    assert set(result) == {"move", "press", "release"}


def test_qmp_input_sink_drag_emits_intermediate_moves(qmp_sink):
    """PRD 4.2: a drag is press, N moves, release -- not a click."""
    sink, fake = qmp_sink

    result = sink.drag(0, 0, 2224, 1668, steps=3, delay=0)

    journal = fake.input_events()
    assert result["moves"] == 3
    assert len(_abs_events(journal)) == 3
    # The drag travels from the guest origin to the guest far corner.
    assert _abs_events(journal)[-1] == (ABS_AXIS_MAX, ABS_AXIS_MAX)
    kinds = [event["events"][0]["type"] for event in journal]
    assert kinds == ["btn"] + ["abs"] * 3 + ["btn"]


def test_qmp_input_sink_home_uses_the_configured_chord(qmp_sink):
    """PRD 4.2: the Home affordance is a key chord (M12 UNVERIFIED on hardware)."""
    sink, fake = qmp_sink

    sink.home()

    journal = fake.input_events()
    assert [event["channel"] for event in journal] == ["send-key"]
    assert journal[0]["keys"] == ["ctrl", "alt", "home"]


def test_qmp_input_sink_key_normalises_the_chord(qmp_sink):
    """PRD 4.2: friendly key names become QCode names on the wire."""
    sink, fake = qmp_sink

    sink.key("Enter")

    assert fake.input_events()[0]["keys"] == ["ret"]


def test_qmp_input_sink_text_types_supported_characters_and_names_the_rest(qmp_sink):
    """PRD 4.2: text input reports what it could not type instead of pretending."""
    sink, fake = qmp_sink

    result = sink.text("Hi!")

    keys = [event["keys"] for event in fake.input_events() if event["channel"] == "send-key"]
    assert keys == [["shift", "h"], ["i"]]
    assert result == {"typed": 2, "skipped": ["!"]}


def test_qmp_input_sink_event_log_records_what_it_issued(qmp_sink):
    """PRD 9.2: the sink's own log carries no guest data, only coordinates."""
    import json

    sink, _ = qmp_sink
    sink.tap(1112, 417)


    log = sink.event_log()
    assert [entry["action"] for entry in log] == ["tap"]
    assert log[0]["guest"] == [556, 208]
    assert log[0]["abs"] == [16398, 8182]
    assert "at_epoch" in log[0]
    json.dumps(log)  # must be serialisable for the evidence set


# ---------------------------------------------------------------------------
# the whole interaction sequence, and the human-monitor fallback
# ---------------------------------------------------------------------------


def test_the_full_interaction_sequence_reaches_the_journal(qmp_sink):
    """PRD 5.2 cases 2/6/7 and 6 M12: move, tap, drag, Home and text all land.

    The assertion is on the *order* of the recorded channels, so a sink that
    dropped the release, or emitted Home as a pointer event, fails here.
    """
    sink, fake = qmp_sink

    sink.pointer_move(556, 417)
    sink.tap(556, 417)
    sink.drag(0, 0, 1112, 834, steps=2, delay=0)
    sink.home()
    sink.text("ab")

    assert [entry["action"] for entry in sink.event_log()] == [
        "pointer_move",
        "tap",
        "drag",
        "home",
        "text",
    ]
    channels = [event["channel"] for event in fake.input_events()]
    # 1 move + 3 tap events + (press + 2 moves + release) drag + Home + 2 keys.
    assert channels == ["input-send-event"] * 8 + ["send-key"] * 3


def test_human_monitor_input_commands_are_journaled_as_hmp_strings(tmp_path):
    """PRD 4.2: HMP shares the QMP socket, and each input keeps its command text.

    ``config/machine.json`` keeps the human monitor off the serial/stdio path, so
    ``human-monitor-command`` is the diagnostic route for input.  The journal
    records the exact command line, which is what makes an input failure
    debuggable instead of "the guest did not respond".
    """
    with fq.fake_qemu_process(workdir=tmp_path) as fake:
        assert fake.wait_for_port(timeout=10.0), fake.stderr_text()
        client = QmpClient(port=fake.port, timeout=5.0)
        client.connect()
        try:
            client.human_monitor("mouse_move 100 200")
            client.human_monitor("mouse_button 1")
            client.human_monitor("sendkey ctrl-alt-home")
        finally:
            client.close()
        journal = fake.input_events()

    assert [event["channel"] for event in journal] == ["human-monitor-command"] * 3
    assert [event["command_line"] for event in journal] == [
        "mouse_move 100 200",
        "mouse_button 1",
        "sendkey ctrl-alt-home",
    ]
    assert [event["kind"] for event in journal] == ["mouse_move", "mouse_button", "send_key"]
    assert (journal[0]["x"], journal[0]["y"]) == (100, 200)
    assert journal[1]["button"] == 1
    assert journal[2]["keys"] == ["ctrl-alt-home"]


def test_qmp_input_sink_honours_a_host_window_change(qmp_sink):
    """PRD 4.2: resizing the host window re-scales subsequent coordinates."""
    sink, fake = qmp_sink
    sink.set_host_window(1112, 834)  # 1:1 with the guest display

    sink.pointer_move(556, 208)

    assert _abs_events(fake.input_events()) == [guest_to_qemu_abs((556, 208), GUEST_SIZE)]


def test_qmp_input_sink_treats_coordinates_as_guest_pixels_without_a_host_size(tmp_path):
    """PRD 4.2: without a host window the coordinates are already guest pixels."""
    with fq.fake_qemu_process(workdir=tmp_path) as fake:
        assert fake.wait_for_port(timeout=10.0), fake.stderr_text()
        client = QmpClient(port=fake.port, timeout=5.0)
        client.connect()
        try:
            sink = QmpInputSink(
                client, {"width": GUEST_SIZE[0], "height": GUEST_SIZE[1]}, key_delay=0.0
            )
            sink.tap(556, 208)
        finally:
            client.close()
        journal = fake.input_events()

    assert _abs_events(journal)[0] == guest_to_qemu_abs((556, 208), GUEST_SIZE)


def test_qmp_input_sink_clamps_a_host_point_outside_the_window(qmp_sink):
    """PRD 4.2: an out-of-range click is clamped, not sent as an invalid axis."""
    sink, fake = qmp_sink

    sink.pointer_move(99999, 99999)

    assert _abs_events(fake.input_events()) == [(ABS_AXIS_MAX, ABS_AXIS_MAX)]


@pytest.mark.parametrize("display", [{"width": 0, "height": 834}, {"width": 1112}, {}, {"width": "x", "height": 1}])
def test_qmp_input_sink_rejects_an_unusable_display(tmp_path, display):
    """PRD 4.2: a display that cannot be mapped onto is a configuration error."""
    with fq.fake_qemu_process(workdir=tmp_path, wait_for_port=False) as fake:
        client = QmpClient(port=fake.port, timeout=0.2)
        sink = QmpInputSink(client, display)

        with pytest.raises(UniPadError) as excinfo:
            sink.to_guest(1, 1)

        assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_qmp_input_sink_rejects_a_non_positive_host_window(qmp_sink):
    """PRD 4.2: a zero-sized window cannot scale anything."""
    sink, _ = qmp_sink

    with pytest.raises(UniPadError) as excinfo:
        sink.set_host_window(0, 100)

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_qmp_input_sink_rejects_a_drag_without_steps(qmp_sink):
    """PRD 4.2: a zero-step drag would be indistinguishable from nothing."""
    sink, _ = qmp_sink

    with pytest.raises(UniPadError) as excinfo:
        sink.drag(0, 0, 10, 10, steps=0)

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# the serial console: the one channel the guest has actually opened
# ---------------------------------------------------------------------------
class _Recorder:
    """A socket stand-in that keeps every byte and nothing else."""

    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.written += data

    def close(self) -> None:
        self.closed = True


def _serial_sink(recorder: _Recorder, port: int = 14544) -> SerialInputSink:
    return SerialInputSink(port, connect=lambda host, prt, timeout: recorder)


def test_text_reaches_the_console_as_the_characters_that_were_typed():
    """The bytes on the wire are the string, unchanged.

    This is the whole point of the class: the guest polls ``_serial_getc`` every
    16 ms and hands each byte to ``cons_cinput``, so what is written here is
    literally what the guest reads.  No escape sequence, no protocol, no
    encoding beyond UTF-8 -- which is also why the channel is a *console* and
    not a HID device.
    """
    recorder = _Recorder()
    result = _serial_sink(recorder).text("Hello, UniPad")
    assert recorder.written == b"Hello, UniPad", recorder.written
    assert result["delivered"] is True, result
    assert result["bytes"] == len(b"Hello, UniPad"), result


def test_a_single_character_key_is_the_character_itself():
    """``key('a')`` sends ``a``; there is no key-code layer to translate to."""
    recorder = _Recorder()
    _serial_sink(recorder).key("q")
    assert recorder.written == b"q", recorder.written


def test_a_named_key_or_chord_is_a_refusal_not_a_dropped_event():
    """``ctrl-a`` has no console representation, so it must not be sent as ``a``.

    The tty line discipline takes bytes, not key codes.  Sending the printable
    half of a chord would deliver a keystroke the caller never asked for, and a
    run whose ``input_journal`` claims ``ctrl-a`` while the guest received ``a``
    is worse than one that refused.
    """
    sink = _serial_sink(_Recorder())
    for requested in (["ctrl", "a"], ["f1"], ["ctrl"]):
        with pytest.raises(UniPadError) as caught:
            sink.key(requested)
        assert caught.value.code == ErrorCode.INPUT_FAILED, caught.value


@pytest.mark.parametrize("action", ["tap", "drag", "home", "pointer_move"])
def test_the_pointer_shaped_methods_refuse_because_the_channel_has_no_hid(action):
    """A tap cannot be expressed on a console, so it is refused.

    Asserted per method rather than once, because a sink that accepted a tap and
    sent a space would produce evidence of a gesture that never happened.  The
    refusal names the reason, so a caller sees *why* and can go to
    :class:`QmpInputSink` for the pointer path.
    """
    sink = _serial_sink(_Recorder())
    with pytest.raises(UniPadError) as caught:
        if action == "tap":
            sink.tap(100, 200)
        elif action == "drag":
            sink.drag(1, 2, 3, 4)
        elif action == "home":
            sink.home()
        else:
            sink.pointer_move(5, 6)
    assert caught.value.code == ErrorCode.INPUT_FAILED, caught.value
    assert "km_tty" in caught.value.message, caught.value.message


def test_a_chardev_nobody_is_listening_on_is_a_refusal_not_a_silent_drop():
    """The connect failure is named, with the port.

    A dropped tap "looks exactly like an unresponsive guest", so a sink that
    swallowed the connection error would make an unrunnable channel look like a
    working one.  The message also says the one thing that fixes it: the run has
    to use ``build_argv(serial_port=...)``, because ``-serial file:`` cannot be
    written to at all.
    """
    def refused(host: str, port: int, timeout: float) -> None:
        raise ConnectionRefusedError("nothing is bound there")

    sink = SerialInputSink(14544, connect=refused)
    with pytest.raises(UniPadError) as caught:
        sink.text("hello")
    assert caught.value.code == ErrorCode.INPUT_FAILED, caught.value
    assert "14544" in str(caught.value.details), caught.value.details
    assert "serial_port" in caught.value.message, caught.value.message


def test_the_socket_is_opened_once_and_reused_across_calls():
    """One connection, not one per keystroke.

    XNU's poller drains whatever is buffered whenever it wakes, so a persistent
    connection is both cheaper and closer to a real terminal.  Counted because
    the obvious implementation -- connect, send, close -- would also pass every
    behavioural test above.
    """
    recorder = _Recorder()
    opened: list[int] = []

    def counting(host: str, port: int, timeout: float) -> _Recorder:
        opened.append(port)
        return recorder

    sink = SerialInputSink(14544, connect=counting)
    sink.text("a")
    sink.text("b")
    sink.key("c")
    assert opened == [14544], opened
    assert recorder.written == b"abc", recorder.written
    assert recorder.closed is False, "the socket was closed mid-run"


def test_close_is_idempotent_and_silences_the_channel():
    """Closing twice is not an error, and the second close does nothing."""
    recorder = _Recorder()
    sink = _serial_sink(recorder)
    sink.text("x")
    sink.close()
    sink.close()
    assert recorder.closed is True, recorder.closed


def test_close_drops_the_socket_so_the_next_call_reconnects():
    """``close()`` clears the handle, not just the connection.

    The mutation this closes is replacing ``self._socket = None`` with ``pass``:
    the recorder still reports ``closed``, so the idempotence test above stays
    green, and every later ``text()`` writes into a socket the host already
    closed.  That is the worst of both shapes -- the caller believes the channel
    is finished and the sink believes it is still open, and the bytes go nowhere.
    """
    first = _Recorder()
    second = _Recorder()
    made: list[_Recorder] = [first, second]

    sink = SerialInputSink(14544, connect=lambda host, port, timeout: made.pop(0))
    sink.text("a")
    sink.close()
    sink.text("b")

    assert first.closed is True, first
    assert first.written == b"a", first.written
    assert second.written == b"b", second.written
    assert not made, "the second write reused the closed socket"


def test_a_send_after_the_peer_went_away_is_a_refusal_not_a_crash():
    """A broken pipe mid-run is named, with the reason.

    ``sendall`` on a socket the guest closed raises ``OSError``, and a sink that
    let it through would surface as an unhandled exception from an input call --
    the caller's journal would record an action it cannot know failed.
    """
    class Gone:
        def sendall(self, data: bytes) -> None:
            raise BrokenPipeError("the guest is not listening any more")

        def close(self) -> None:
            pass

    sink = SerialInputSink(14544, connect=lambda host, port, timeout: Gone())
    with pytest.raises(UniPadError) as caught:
        sink.text("after the peer left")
    assert caught.value.code == ErrorCode.INPUT_FAILED, caught.value
    assert "stopped accepting" in caught.value.message, caught.value.message
    # The journal stays empty.  Recording the text before the write is the
    # mutation this closes: it survives every assertion above and produces a
    # journal that claims a string the guest never received -- the same
    # "a dropped tap looks exactly like an unresponsive guest" failure the
    # ``InputSink`` contract exists to prevent, one layer down.
    assert sink.sent == [], sink.sent
