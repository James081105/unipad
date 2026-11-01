"""The guest's framebuffer as bytes, and as a PPM the Windows surface can blit.

PRD 4.2 item 3 wants a real guest frame on the Windows screen.
:mod:`unipad.host.winsurface` is the half that puts pixels in a window, and it
is validated: 145 of 145 sampled pixels came back byte-identical from the
window's own device context.  What was missing was the other half -- nothing
produced a frame for it to show.  This module is that half, and nothing more.

What was measured, and is therefore not guessed here
----------------------------------------------------
Against a real booted guest (2026-11-01), the boot log prints::

    video    base 0x1BC7A0000  800x600  row 3200 bytes  32 bpp

One QMP ``pmemsave`` of 1,920,000 bytes from guest-physical ``0x1BC7A0000``
takes 0.02 s and returns 1,148,931 non-zero bytes holding 50 distinct values.
Cross-checked against QMP ``screendump`` -- which on this QEMU writes a **P6
PPM** despite the ``.png`` filename -- all 480,000 pixels are byte-identical in
**B, G, R, A** order.  The other three channel orders score 30%, 18% and 18%,
so the order is measured rather than assumed.

The fourth byte is the one fact that decides this module's shape.  It is not a
colour channel: 450,000 of the 480,000 pixels carry ``0x00`` there and the
other 30,000 -- the green bar the bootloader itself printed, 38 rows of 800 --
carry ``0xFF``.  A converter that read it as blue would paint a visible blue
into 93.75% of the frame and would paint a *different* blue into the bar, and a
converter that emitted four bytes per pixel would produce a document
:func:`unipad.host.winsurface.ppm_to_bgr` reads as garbage.  So the fourth byte
is dropped, always, and no caller is offered a way to keep it.

Stride, honestly
----------------
This frame's stride (3200) is exactly its row width (800 x 4), so it carries no
row padding -- which means the padding path cannot be validated against it and
must be handled by contract instead.  ``stride`` is therefore treated as
authoritative and possibly larger than ``width * 4``: :func:`bgra_to_ppm` walks
rows at ``stride`` and takes ``width * 4`` bytes from each, so padding between
rows is dropped rather than assumed absent.  A geometry whose ``stride`` is
*smaller* than one row of pixels is refused, because rows that overlap are a
corrupt geometry and not a layout.

Refusal, not a partial answer
-----------------------------
Both entry points follow the rule :mod:`unipad.host.guestobserve` is built on:
a wrong frame is worse than no frame.  A short read is refused rather than
truncated, because a truncated frame is a frame with the bottom cut off and no
way for the caller to see it; a short buffer is refused rather than padded, for
the reason :func:`unipad.host.winsurface.ppm_to_bgr` already gives.  The codes
match the neighbouring validated code so a caller branches on the same thing
in the same place:

* geometry that cannot describe a framebuffer -> ``config_invalid`` (the caller
  supplied it);
* the guest returning less than was asked for -> ``evidence_incomplete``, the
  code :meth:`unipad.host.guestobserve.GuestMemory.read` uses for exactly this;
* a buffer shorter than its geometry declares -> ``render_failed``, the code
  :func:`unipad.host.winsurface.ppm_to_bgr` uses for exactly this.

What this module does NOT do
----------------------------
It does not decide the geometry.  The four numbers come from the guest's own
boot log and are the caller's to supply; nothing here parses that log, because a
parser that accepted a second ``video`` line or a differently-worded one would
be inventing a geometry the guest never printed.  It does not scale, crop,
rotate or colour-convert: the surface deliberately follows the frame rather
than rescaling it, so that an operator is never shown pixels the guest did not
draw.  And it does not read the guest itself -- it takes a
:class:`~unipad.host.guestobserve.GuestMemory`, which is what keeps it testable
without a boot.

Dependency direction: :mod:`unipad.errors`, :mod:`unipad.host.guestobserve` and
:mod:`unipad.host.winsurface` (for the :class:`~unipad.host.winsurface.FrameSource`
protocol this module implements).  Nothing here touches the filesystem, QEMU or
a socket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from unipad.errors import ErrorCode, UniPadError
from unipad.host.guestobserve import GuestMemory
from unipad.host.winsurface import BYTES_PER_PIXEL as PPM_BYTES_PER_PIXEL
from unipad.host.winsurface import FrameSource

__all__ = [
    "BYTES_PER_PIXEL",
    "PPM_BYTES_PER_PIXEL",
    "FrameGeometry",
    "GuestFrameSource",
    "bgra_to_ppm",
    "read_guest_frame",
]

#: Bytes per pixel in the framebuffer: blue, green, red, then a byte that is
#: not a colour channel and is dropped.  Measured against QMP ``screendump`` on
#: a real booted guest -- 480,000 of 480,000 pixels identical in this order,
#: where the next-best order scored 30%.
BYTES_PER_PIXEL = 4


def _refuse(code: str, message: str, **details: Any) -> UniPadError:
    """A refusal, with enough detail to act on and nothing secret (PRD 9.2)."""
    return UniPadError(code, message, **details)


def _check_geometry(width: int, height: int, stride: int) -> None:
    """Refuse a geometry that cannot describe a framebuffer.

    Called by every entry point that takes the four numbers, so a bad geometry
    is refused identically whether it arrives as keywords or inside a
    :class:`FrameGeometry`.  The stride check is the one that matters: a stride
    shorter than one row of pixels means the rows overlap, and reading such a
    buffer produces a frame that is wrong in a way no length check can see.
    """
    if width <= 0 or height <= 0:
        raise _refuse(
            ErrorCode.CONFIG_INVALID,
            "a framebuffer needs positive dimensions",
            width=int(width), height=int(height),
        )
    if stride < width * BYTES_PER_PIXEL:
        raise _refuse(
            ErrorCode.CONFIG_INVALID,
            "the row stride is shorter than one row of pixels",
            width=int(width), stride=int(stride),
            row_bytes=int(width) * BYTES_PER_PIXEL,
        )


@dataclass(frozen=True)
class FrameGeometry:
    """Where the framebuffer is and how it is laid out, as one value.

    The four numbers the guest's boot log prints.  Binding them together means a
    caller cannot pass a base without also saying what is at it, and the
    validation in ``__post_init__`` means an impossible geometry is refused
    where it is constructed rather than at the first read.

    ``base`` is guest *physical*, because that is the address space
    :meth:`unipad.host.guestobserve.GuestMemory.read` works in -- the guest's
    MMU is on, and a virtual address here would be read as physical and would
    return whatever happens to live at that number.
    """

    base: int
    width: int
    height: int
    stride: int

    def __post_init__(self) -> None:
        if self.base < 0:
            raise _refuse(
                ErrorCode.CONFIG_INVALID,
                "a framebuffer base cannot be negative",
                base=int(self.base),
            )
        _check_geometry(self.width, self.height, self.stride)

    @property
    def size(self) -> int:
        """Bytes the framebuffer occupies: every row's stride, padding included."""
        return self.stride * self.height


def read_guest_frame(memory: GuestMemory, *, base: int, width: int, height: int,
                     stride: int) -> bytes:
    """Read the framebuffer out of guest physical memory, padding included.

    One read of ``stride * height`` bytes, because that is the shape
    :meth:`unipad.host.guestobserve.GuestMemory.read` is validated for and
    because a row at a time would be one QMP round trip per row on a connection
    that holds a single client.  The padding between rows is returned as part of
    the buffer -- :func:`bgra_to_ppm` drops it, and a caller that wants the raw
    bytes gets exactly what the guest has.

    Args:
        memory: The session's :class:`~unipad.host.guestobserve.GuestMemory`.
        base: Guest-physical address of the first byte of the framebuffer.
        width, height: Pixels per row and rows.
        stride: Bytes from the start of one row to the start of the next.  May
            exceed ``width * 4``; it may not be less.

    Returns:
        Exactly ``stride * height`` bytes.

    Raises:
        UniPadError: ``config_invalid`` for a geometry that cannot describe a
            framebuffer; ``evidence_incomplete`` when the guest returns fewer
            bytes than were asked for.  A short read is refused rather than
            returned short -- :meth:`GuestMemory.read` already refuses in that
            case, and this check is what keeps the promise true for any other
            object with a ``read`` method, including the stubs a test hands in.
    """
    _check_geometry(width, height, stride)
    if base < 0:
        raise _refuse(
            ErrorCode.CONFIG_INVALID,
            "a framebuffer base cannot be negative",
            base=int(base),
        )
    size = stride * height
    data = memory.read(int(base), size)
    if len(data) != size:
        raise _refuse(
            ErrorCode.EVIDENCE_INCOMPLETE,
            "the guest did not return the framebuffer that was asked for",
            base=hex(int(base)),
            asked=int(size),
            returned=len(data),
        )
    return data


def bgra_to_ppm(bgra: bytes, *, width: int, height: int, stride: int) -> bytes:
    """A BGRA framebuffer as a P6 PPM :meth:`SurfaceWindow.present` accepts.

    The conversion is a channel drop and a reorder, and it is the only place
    either happens: each pixel's four bytes become three, in R, G, B order,
    which is the order a P6 payload holds and the order
    :func:`unipad.host.winsurface.ppm_to_bgr` turns into GDI's B-G-R.  The rows
    stay top-down, because a P6 PPM is top-down and the surface's negative
    ``biHeight`` reads it that way.

    Rows are walked at ``stride`` and ``width * 4`` bytes are taken from each,
    so padding between rows is dropped rather than assumed absent.  A buffer
    that is longer than ``stride * height`` is not an error -- the extra bytes
    are simply not read, which is the policy
    :func:`unipad.host.winsurface.ppm_to_bgr` already applies to a padded
    producer.

    Args:
        bgra: At least ``stride * height`` bytes of the framebuffer.
        width, height: Pixels per row and rows.
        stride: Bytes from the start of one row to the start of the next.

    Returns:
        A complete P6 document: magic, header, one whitespace byte, then exactly
        ``width * height * 3`` payload bytes.

    Raises:
        UniPadError: ``config_invalid`` for a geometry that cannot describe a
            framebuffer; ``render_failed`` when the buffer is shorter than that
            geometry declares.  A short buffer is refused rather than padded:
            a padded row is a frame with invented pixels in it, and the caller
            cannot tell which ones.
    """
    _check_geometry(width, height, stride)
    needed = stride * height
    if len(bgra) < needed:
        raise _refuse(
            ErrorCode.RENDER_FAILED,
            "the framebuffer is shorter than its geometry declares",
            declared=int(needed),
            present=len(bgra),
        )
    row_bytes = width * BYTES_PER_PIXEL
    parts = [b"P6\n%d %d\n255\n" % (int(width), int(height))]
    for row in range(height):
        start = row * stride
        pixels = bytes(bgra[start:start + row_bytes])
        line = bytearray(width * PPM_BYTES_PER_PIXEL)
        line[0::3] = pixels[2::4]   # red
        line[1::3] = pixels[1::4]   # green
        line[2::3] = pixels[0::4]   # blue -- the fourth byte is never read
        parts.append(bytes(line))
    return b"".join(parts)


class GuestFrameSource(FrameSource):
    """A :class:`~unipad.host.winsurface.FrameSource` that reads the guest.

    Ties the two halves together so a caller can hand one object to anything
    expecting ``grab()`` -- the window's own frame loop, a test, a closure over
    a live QMP client.  Explicitly a subclass of the protocol rather than a
    duck-typed look-alike, so a rename of ``grab`` in the surface is a
    ``TypeError`` at import instead of a frame that stops arriving.

    The frame is re-read on every :meth:`grab`.  Nothing is cached, because the
    guest's framebuffer is live memory: a cached frame is a still image that
    claims to be current, and the caller has no way to tell the difference.
    """

    def __init__(self, memory: GuestMemory, geometry: FrameGeometry) -> None:
        self._memory = memory
        self._geometry = geometry
        self.frames = 0

    @property
    def geometry(self) -> FrameGeometry:
        """The geometry this source reads, as it was given."""
        return self._geometry

    def grab(self) -> bytes:
        """The guest's current framebuffer, as a complete P6 PPM document."""
        raw = read_guest_frame(
            self._memory,
            base=self._geometry.base,
            width=self._geometry.width,
            height=self._geometry.height,
            stride=self._geometry.stride,
        )
        frame = bgra_to_ppm(
            raw,
            width=self._geometry.width,
            height=self._geometry.height,
            stride=self._geometry.stride,
        )
        self.frames += 1
        return frame
