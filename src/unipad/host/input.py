"""Input sinks for the guest display (PRD 3.1 FR04, 4.2, 6 M12).

Two layers live here:

1. **Pure coordinate math** -- :func:`scale_point` maps a point in host window
   space onto the guest display, and :func:`guest_to_qemu_abs` maps a guest
   pixel onto QEMU's input-layer absolute axis range.  Both are side-effect
   free and unit-testable, which is what makes M12 debuggable.
2. **Sinks** -- :class:`InputSink` is the protocol the controller codes
   against, :class:`QmpInputSink` drives a real guest over QMP, and
   :class:`NullInputSink` records calls for tests and headless diagnostics.

Coordinate spaces::

    host window (pixels)  --scale_point-->  guest display (pixels)
    guest display (pixels) --guest_to_qemu_abs-->  QEMU abs axis (0..0x7fff)

M12 UNVERIFIED: which guest input device the pinned board exposes (virtio
tablet vs. virtio mouse) decides whether absolute events land where expected,
and the Home affordance has no confirmed QEMU key mapping.  Both are recorded
as UNVERIFIED rather than assumed; see :meth:`QmpInputSink.home`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from unipad.errors import ErrorCode, UniPadError
from unipad.host.qmp import ABS_AXIS_MAX, keycode_names

__all__ = [
    "InputSink",
    "QmpInputSink",
    "NullInputSink",
    "SerialInputSink",
    "scale_point",
    "guest_to_qemu_abs",
    "qcode_for_char",
]

#: QEMU QCode names for the printable ASCII characters we can type.
_CHAR_QCODES: dict[str, str] = {
    " ": "spc",
    "\n": "ret",
    "\r": "ret",
    "\t": "tab",
    ".": "dot",
    ",": "comma",
    "-": "minus",
    "=": "equal",
    "/": "slash",
    ";": "semicolon",
    "'": "apostrophe",
    "`": "grave_accent",
    "[": "bracket_left",
    "]": "bracket_right",
    "\\": "backslash",
}


def scale_point(
    point: tuple[int, int],
    host_size: tuple[int, int],
    guest_size: tuple[int, int],
) -> tuple[int, int]:
    """Map a point from host window space onto the guest display.

    Each axis is scaled independently and the result is clamped to the guest
    display bounds, so a click outside the guest area still produces a valid
    guest coordinate instead of an out-of-range event.

    Args:
        point: ``(x, y)`` in host window pixels.
        host_size: ``(width, height)`` of the host window in pixels.
        guest_size: ``(width, height)`` of the guest display in pixels.

    Returns:
        ``(x, y)`` in guest display pixels, ``0 <= x < guest_width`` and
        ``0 <= y < guest_height``.

    Raises:
        ValueError: When any size component is not positive, or ``point`` is not
            a 2-tuple of integers.  Failing loudly beats silently clicking the
            wrong pixel.

    Note:
        Letterbox/pillar-box black bars are not modelled: the host window is
        assumed to show the guest framebuffer edge to edge.  If the launcher
        later adds aspect-preserving scaling, this is the single function to
        change.
    """
    if len(point) != 2:
        raise ValueError(f"point must be an (x, y) pair, got {point!r}")
    px, py = point
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (px, py)):
        raise ValueError(f"point coordinates must be integers, got {point!r}")
    host_w, host_h = host_size
    guest_w, guest_h = guest_size
    if host_w <= 0 or host_h <= 0:
        raise ValueError(f"host_size must be positive, got {host_size!r}")
    if guest_w <= 0 or guest_h <= 0:
        raise ValueError(f"guest_size must be positive, got {guest_size!r}")
    gx = int(round(px * guest_w / host_w))
    gy = int(round(py * guest_h / host_h))
    return (min(max(gx, 0), guest_w - 1), min(max(gy, 0), guest_h - 1))


def guest_to_qemu_abs(
    point: tuple[int, int],
    guest_size: tuple[int, int],
) -> tuple[int, int]:
    """Map a guest pixel onto QEMU's absolute input axis range.

    Args:
        point: ``(x, y)`` in guest display pixels.
        guest_size: ``(width, height)`` of the guest display.

    Returns:
        ``(x, y)`` in ``0..ABS_AXIS_MAX`` (QEMU's ``INPUT_EVENT_ABS_MAX``).

    Raises:
        ValueError: When ``guest_size`` is not positive.
    """
    gx, gy = point
    guest_w, guest_h = guest_size
    if guest_w <= 0 or guest_h <= 0:
        raise ValueError(f"guest_size must be positive, got {guest_size!r}")
    span_x = max(guest_w - 1, 1)
    span_y = max(guest_h - 1, 1)
    return (
        min(max(int(round(gx * ABS_AXIS_MAX / span_x)), 0), ABS_AXIS_MAX),
        min(max(int(round(gy * ABS_AXIS_MAX / span_y)), 0), ABS_AXIS_MAX),
    )


def qcode_for_char(char: str) -> Optional[list[str]]:
    """Return the QCode name(s) needed to type one character.

    Args:
        char: A single character.

    Returns:
        A list of QCode names to send (``["shift", "a"]`` for uppercase), or
        ``None`` when the character has no known mapping.  Returning ``None``
        lets the caller report the exact character instead of guessing.
    """
    if len(char) != 1:
        return None
    if char.isascii() and char.isalpha():
        return ["shift", char.lower()] if char.isupper() else [char.lower()]
    if char.isascii() and char.isdigit():
        return [char]
    name = _CHAR_QCODES.get(char)
    return [name] if name else None


@runtime_checkable
class InputSink(Protocol):
    """The input surface the controller programs against.

    Implementations must be safe to call in any order and must raise
    :class:`~unipad.errors.UniPadError` (code ``input_failed``) rather than
    silently dropping an event: a dropped tap looks exactly like an
    unresponsive guest.
    """

    def pointer_move(self, x: int, y: int) -> Any:
        """Move the pointer to guest coordinates ``(x, y)``."""

    def tap(self, x: int, y: int) -> Any:
        """Tap at guest coordinates ``(x, y)``."""

    def drag(self, x1: int, y1: int, x2: int, y2: int, **kwargs: Any) -> Any:
        """Drag from ``(x1, y1)`` to ``(x2, y2)``."""

    def home(self) -> Any:
        """Trigger the guest Home affordance."""

    def key(self, names: str | Sequence[str]) -> Any:
        """Send one key or chord."""

    def text(self, text: str, **kwargs: Any) -> Any:
        """Type a string, one character at a time."""


class QmpInputSink:
    """Drive a guest pointer/keyboard through QMP.

    Args:
        qmp: A connected :class:`unipad.host.qmp.QmpClient`.
        display: The machine display mapping (``width``, ``height``,
            ``format``) from ``config/machine.json``.
        host_size: Optional host window size.  When set, incoming coordinates
            are treated as host-window pixels and scaled with
            :func:`scale_point`; when unset they are treated as guest pixels.
        key_delay: Seconds to wait between keys in :meth:`text` so the guest can
            keep up with a burst of events.
        home_keys: Key chord used by :meth:`home`.

    Note:
        M12 UNVERIFIED.  Absolute pointer events require a guest input device
        that accepts them; if the pinned board only provides a relative mouse
        the pointer will drift instead of landing on the target pixel.  The
        launcher must record which device the guest actually exposed.
    """

    def __init__(
        self,
        qmp: Any,
        display: Mapping[str, Any],
        *,
        host_size: Optional[tuple[int, int]] = None,
        key_delay: float = 0.02,
        home_keys: Sequence[str] = ("ctrl", "alt", "home"),
    ) -> None:
        self.qmp = qmp
        self.display = dict(display or {})
        self.host_size = host_size
        self.key_delay = max(float(key_delay), 0.0)
        self.home_keys = tuple(home_keys)
        self._log: list[dict[str, Any]] = []

    # -- geometry --------------------------------------------------------

    @property
    def guest_size(self) -> tuple[int, int]:
        """Return the guest display size as ``(width, height)``."""
        try:
            width = int(self.display.get("width", 0))
            height = int(self.display.get("height", 0))
        except (TypeError, ValueError) as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "machine display width/height must be integers",
                display=self.display,
            ) from exc
        if width <= 0 or height <= 0:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "machine display must declare a positive width and height",
                display=self.display,
            )
        return (width, height)

    def set_host_window(self, width: int, height: int) -> None:
        """Set the host window size used to scale incoming coordinates."""
        if width <= 0 or height <= 0:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "host window size must be positive",
                width=width,
                height=height,
            )
        self.host_size = (int(width), int(height))

    def to_guest(self, x: int, y: int) -> tuple[int, int]:
        """Convert ``(x, y)`` into guest pixels, scaling from host space if set."""
        guest = self.guest_size
        if self.host_size is None:
            return (min(max(int(x), 0), guest[0] - 1), min(max(int(y), 0), guest[1] - 1))
        return scale_point((int(x), int(y)), self.host_size, guest)

    # -- InputSink -------------------------------------------------------

    def pointer_move(self, x: int, y: int) -> dict[str, Any]:
        """Move the pointer to ``(x, y)`` (host or guest space, see the class)."""
        guest_x, guest_y = self.to_guest(x, y)
        axis_x, axis_y = guest_to_qemu_abs((guest_x, guest_y), self.guest_size)
        result = self.qmp.pointer_abs(axis_x, axis_y)
        self._record("pointer_move", {"x": x, "y": y, "guest": [guest_x, guest_y], "abs": [axis_x, axis_y]})
        return result

    def tap(self, x: int, y: int) -> dict[str, Any]:
        """Tap at ``(x, y)``: move, press, release."""
        guest_x, guest_y = self.to_guest(x, y)
        axis_x, axis_y = guest_to_qemu_abs((guest_x, guest_y), self.guest_size)
        move = self.qmp.pointer_abs(axis_x, axis_y)
        press = self.qmp.mouse_button("left", True)
        release = self.qmp.mouse_button("left", False)
        self._record(
            "tap",
            {"x": x, "y": y, "guest": [guest_x, guest_y], "abs": [axis_x, axis_y]},
        )
        return {"move": move, "press": press, "release": release}

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        steps: int = 8,
        delay: float = 0.01,
    ) -> dict[str, Any]:
        """Drag from ``(x1, y1)`` to ``(x2, y2)``.

        Args:
            x1, y1: Start point.
            x2, y2: End point.
            steps: Number of intermediate move events (>= 1).
            delay: Seconds to wait between steps.

        Returns:
            ``{"press", "moves", "release"}`` with the move count actually
            emitted, so a caller can tell a real drag from a click.
        """
        if steps < 1:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "drag steps must be at least 1",
                steps=steps,
            )
        start = self.to_guest(x1, y1)
        end = self.to_guest(x2, y2)
        press = self.qmp.mouse_button("left", True)
        moves = 0
        for index in range(1, steps + 1):
            ratio = index / steps
            gx = int(round(start[0] + (end[0] - start[0]) * ratio))
            gy = int(round(start[1] + (end[1] - start[1]) * ratio))
            axis_x, axis_y = guest_to_qemu_abs((gx, gy), self.guest_size)
            self.qmp.pointer_abs(axis_x, axis_y)
            moves += 1
            if delay > 0:
                time.sleep(delay)
        release = self.qmp.mouse_button("left", False)
        self._record(
            "drag",
            {"from": [x1, y1], "to": [x2, y2], "guest_from": list(start), "guest_to": list(end), "moves": moves},
        )
        return {"press": press, "moves": moves, "release": release}

    def home(self) -> dict[str, Any]:
        """Trigger the guest Home affordance.

        M12 UNVERIFIED: there is no confirmed mapping from a host key chord to
        the iPadOS Home button on the pinned board.  The default chord is a
        placeholder recorded in the experiment journal, and a board that exposes
        a dedicated Home GPIO/virtio button will need its own path here.  Do not
        treat a successful QMP reply as proof the guest went home.
        """
        result = self.qmp.send_key(self.home_keys)
        self._record("home", {"keys": list(self.home_keys)})
        return result

    def key(self, names: str | Sequence[str]) -> dict[str, Any]:
        """Send one key or a chord (all names pressed and released together)."""
        resolved = keycode_names(names)
        result = self.qmp.send_key(resolved)
        self._record("key", {"keys": resolved})
        return result

    def text(self, text: str, *, delay: Optional[float] = None) -> dict[str, Any]:
        """Type ``text`` one character at a time.

        Args:
            text: The string to type.  ASCII letters, digits and the
                punctuation in ``_CHAR_QCODES`` are supported.
            delay: Override for ``key_delay``.

        Returns:
            ``{"typed": n, "skipped": [...]}`` -- the skipped list names every
            character that had no QCode mapping so the caller can report it
            instead of pretending the text was entered.

        Raises:
            UniPadError: ``input_failed`` when ``text`` is not a string.
        """
        if not isinstance(text, str):
            raise UniPadError(
                ErrorCode.INPUT_FAILED,
                "text must be a string",
                received_type=type(text).__name__,
            )
        pause = self.key_delay if delay is None else max(float(delay), 0.0)
        typed = 0
        skipped: list[str] = []
        for char in text:
            names = qcode_for_char(char)
            if names is None:
                skipped.append(char)
                continue
            self.qmp.send_key(names)
            typed += 1
            if pause > 0:
                time.sleep(pause)
        self._record("text", {"length": len(text), "typed": typed, "skipped": skipped})
        return {"typed": typed, "skipped": skipped}

    # -- introspection ---------------------------------------------------

    def event_log(self) -> list[dict[str, Any]]:
        """Return the events this sink has issued (no guest data)."""
        return [dict(entry) for entry in self._log]

    def _record(self, action: str, details: Mapping[str, Any]) -> None:
        """Append an action to the in-memory event log."""
        self._log.append({"action": action, "at_epoch": round(time.time(), 3), **dict(details)})


class NullInputSink:
    """An input sink that records calls and touches nothing.

    Used for headless diagnostics, unit tests, and any code path that must not
    claim it interacted with a guest it never reached.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def pointer_move(self, x: int, y: int) -> dict[str, Any]:
        """Record a pointer move."""
        self.calls.append(("pointer_move", (x, y), {}))
        return {"action": "pointer_move", "x": x, "y": y, "delivered": False}

    def tap(self, x: int, y: int) -> dict[str, Any]:
        """Record a tap."""
        self.calls.append(("tap", (x, y), {}))
        return {"action": "tap", "x": x, "y": y, "delivered": False}

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        steps: int = 8,
        delay: float = 0.01,
    ) -> dict[str, Any]:
        """Record a drag."""
        self.calls.append(("drag", (x1, y1, x2, y2), {"steps": steps, "delay": delay}))
        return {"action": "drag", "from": [x1, y1], "to": [x2, y2], "moves": steps, "delivered": False}

    def home(self) -> dict[str, Any]:
        """Record a Home request."""
        self.calls.append(("home", (), {}))
        return {"action": "home", "delivered": False}

    def key(self, names: str | Sequence[str]) -> dict[str, Any]:
        """Record a key event."""
        resolved = keycode_names(names)
        self.calls.append(("key", (resolved,), {}))
        return {"action": "key", "keys": resolved, "delivered": False}

    def text(self, text: str, *, delay: Optional[float] = None) -> dict[str, Any]:
        """Record a text request without typing anything."""
        if not isinstance(text, str):
            raise UniPadError(
                ErrorCode.INPUT_FAILED,
                "text must be a string",
                received_type=type(text).__name__,
            )
        self.calls.append(("text", (text,), {"delay": delay}))
        return {"action": "text", "length": len(text), "typed": 0, "skipped": list(text), "delivered": False}

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the recorded calls."""
        return {
            "calls": [
                {"action": action, "args": list(args), "kwargs": kwargs}
                for action, args, kwargs in self.calls
            ]
        }


class SerialInputSink:
    """Type into the guest's serial console, over the UART's TCP chardev.

    This is the only host-to-guest input channel the pinned board has actually
    opened, and the reason it was never usable is worth stating precisely:
    ``-serial file:`` is **one-way**.  QEMU writes what the guest emits and
    discards anything a host might send.  Measured: the guest's kernel log says
    ``Serial keyboard started`` at t=43.75s, and XNU's ``serial_keyboard_init``
    (``osfmk/console/serial_general.c:51-68``) only prints that when
    ``serialmode & SERIALMODE_INPUT`` -- which the board's ``serial=3`` sets
    (``SERIALMODE_OUTPUT|SERIALMODE_INPUT``, ``serial_protos.h:52,55``).  The
    thread it starts polls ``_serial_getc(false)`` every 16 ms and hands each
    byte to ``cons_cinput`` (``serial_general.c:79-97``), which feeds
    ``km_tty[0]``'s line discipline (``bsd/dev/arm/km.c:397-407``).  So the guest
    is listening -- to a port nothing could write to.

    With ``unixnu_boot.build_argv(serial_port=...)`` the UART becomes a TCP
    chardev and this class writes to it.

    **What it can and cannot do, and why it is written this way.**  This channel
    carries *console characters*, not HID events: the bytes land in the kernel
    console tty, readable by a process that opens ``/dev/console``.  So
    :meth:`text` and :meth:`key` are real, and :meth:`tap`, :meth:`drag` and
    :meth:`home` are **refusals** rather than approximations.  A sink that
    accepted a tap and sent a space would produce a run whose ``input_journal``
    claims a gesture that never happened -- which is the failure mode the
    ``InputSink`` contract exists to prevent ("a dropped tap looks exactly like
    an unresponsive guest", and a faked one looks like a working one).

    Args:
        port: The TCP port ``build_argv`` bound the chardev to.
        host: Loopback by default; a chardev is only ever on this machine.
        timeout: Connect and send timeout in seconds.
        connect: Inject the socket factory for tests.
    """

    #: Why the pointer-shaped methods refuse.  Stated once so every refusal
    #: carries the same reason rather than four hand-written variants.
    _NO_HID = (
        "the serial console carries characters, not HID events: bytes written "
        "here reach the kernel console tty (km_tty[0]) via cons_cinput, which "
        "no IOHIDDevice consumes. A tap needs a device the board has not "
        "exposed; see QmpInputSink for the pointer path."
    )

    def __init__(
        self,
        port: int,
        *,
        host: str = "127.0.0.1",
        timeout: float = 10.0,
        connect: Optional[Any] = None,
    ) -> None:
        self.port = int(port)
        self.host = host
        self.timeout = float(timeout)
        self._connect = connect
        self._socket: Optional[Any] = None
        self.sent: list[str] = []

    # -- connection ------------------------------------------------------

    def _open(self) -> Any:
        """The socket, connected once and reused.

        One connection rather than one per keystroke: XNU's poller reads
        whatever is buffered whenever it wakes, so a persistent connection is
        both cheaper and closer to what a real terminal does.
        """
        if self._socket is not None:
            return self._socket
        factory = self._connect or _default_connect
        try:
            self._socket = factory(self.host, self.port, self.timeout)
        except OSError as exc:
            raise UniPadError(
                ErrorCode.INPUT_FAILED,
                "cannot reach the guest's serial chardev; is the run using "
                "build_argv(serial_port=...)?",
                host=self.host,
                port=self.port,
                reason=f"{type(exc).__name__}: {exc}",
            ) from exc
        return self._socket

    def close(self) -> None:
        """Drop the connection.  Safe to call more than once."""
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    # -- the channel's actual capability ---------------------------------

    def text(self, text: str, *, delay: Optional[float] = None) -> dict[str, Any]:
        """Write ``text`` to the console, one character at a time.

        ``delay`` is accepted for signature compatibility with
        :class:`QmpInputSink` and honoured as a pause between characters; the
        default is no pause, because the console buffers and XNU drains it.
        """
        if not isinstance(text, str):
            raise UniPadError(
                ErrorCode.INPUT_FAILED,
                "text must be a string",
                received_type=type(text).__name__,
            )
        encoded = text.encode("utf-8", errors="replace")
        try:
            self._open().sendall(encoded)
        except OSError as exc:
            raise UniPadError(
                ErrorCode.INPUT_FAILED,
                "the serial chardev stopped accepting characters",
                port=self.port,
                reason=f"{type(exc).__name__}: {exc}",
            ) from exc
        self.sent.append(text)
        if delay:
            time.sleep(max(float(delay), 0.0))
        return {"action": "text", "length": len(text), "bytes": len(encoded),
                "delivered": True}

    def key(self, names: str | Sequence[str]) -> dict[str, Any]:
        """Send one key, as the character it stands for.

        Named keys (``ctrl``, ``alt``, ``f1``) have no console representation --
        the tty line discipline takes bytes, not key codes -- so they are
        refusals rather than silently dropped chords.
        """
        wanted = [names] if isinstance(names, str) else list(names)
        literal = "".join(part for part in wanted if len(part) == 1)
        if not literal or any(len(part) != 1 for part in wanted):
            raise UniPadError(
                ErrorCode.INPUT_FAILED,
                "the serial console takes single characters, not named keys or "
                "chords; a key with no character has nothing to send",
                requested=wanted,
            )
        return self.text(literal)

    # -- what this channel is not ----------------------------------------

    def _refused(self, action: str) -> UniPadError:
        return UniPadError(ErrorCode.INPUT_FAILED, self._NO_HID, action=action)

    def pointer_move(self, x: int, y: int) -> Any:
        raise self._refused("pointer_move")

    def tap(self, x: int, y: int) -> Any:
        raise self._refused("tap")

    def drag(self, x1: int, y1: int, x2: int, y2: int, **kwargs: Any) -> Any:
        raise self._refused("drag")

    def home(self) -> Any:
        raise self._refused("home")


def _default_connect(host: str, port: int, timeout: float) -> Any:
    """A TCP socket to the chardev, or an ``OSError`` the caller turns into a
    refusal.  A module-level function rather than ``socket.create_connection``
    inline so tests can substitute a recorder without patching ``socket``."""
    import socket

    return socket.create_connection((host, port), timeout=timeout)
