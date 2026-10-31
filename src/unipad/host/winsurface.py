"""A real Windows surface for the guest's framebuffer (PRD 4.2, item 3).

Every display path in this repository so far has been *configuration*:
:mod:`unipad.host.display` decides which ``-fw_cfg`` items to emit, and
:mod:`unipad.host.qemu_metal` reads the QMTL counters out of guest memory.
Neither one puts a pixel in front of an operator.  This module is the piece
that does: it takes the guest's framebuffer and blits it into a window on the
Windows desktop.

Why GDI, and why not the Metal/QMTL shared window
-------------------------------------------------
The QMTL wire (``QMControl``/``QM_REQUEST``/``QM_REPLY``/``QM_COMPLETIONS`` at
fixed slot offsets in guest memory) is the zero-copy path and it is the one
PRD item 3's acceptance names.  It is not available on this host: the shared
window needs ``memory-backend-file``, which the w64 build does not register
because ``util/mmap-alloc.c`` guards it behind ``CONFIG_POSIX`` (fact 197), and
QMP has no command that writes guest physical memory at all.  So the host side
of that wire has nothing to attach to.

What *is* available, and is measured rather than assumed: QMP ``screendump``
returns the guest's framebuffer as a P6 PPM.  Fact 198 used it to establish
that the framebuffer is static and ignores taps, and the instrument's positive
control proved the differ could see a single flipped byte.  So the frame source
is real; only the presentation was missing.  GDI's ``StretchDIBits`` takes
24-bit BGR, which is a P6 PPM with two channels swapped -- the conversion is a
swap, not a reformat, and it needs no dependency this host cannot install
(``pip`` is broken here, so anything requiring a wheel is not an option).

Every GDI entry point below declares ``argtypes``.  That is not decoration:
ctypes converts a Python ``int`` to a C ``int`` by default, which is 32 bits,
so an undeclared ``HDC`` or ``HBITMAP`` is silently truncated on x64 and the
call fails in a way that looks like a GDI refusal rather than a Python bug.
The same applies to the return types -- ``GetDC`` returns a pointer-sized
value that must not be narrowed to ``int``.

What this module does NOT claim
-------------------------------
That the window is the guest's display.  A window that shows a frame proves the
frame reached the desktop; it says nothing about who drew it.  Fact 191/198
already established that the framebuffer is static, so a window built on this
source will show a still image until the guest starts drawing -- which is a
true statement about the guest, not a defect of the window.

Dependency direction: :mod:`ctypes`, :mod:`unipad.errors` and
:mod:`unipad.host.logs` only.  It must never import :mod:`unipad.host.qemu` or
:mod:`unipad.host.qmp` -- the frame arrives as bytes from whatever source the
caller plugged in, so this module stays testable without a boot.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Any, Optional, Protocol

from unipad.errors import ErrorCode, UniPadError
from unipad.host.logs import read_ppm_header

#: P6, the only PPM variant ``screendump`` produces.
PPM_MAGIC = b"P6"

#: Bytes per pixel in the GDI 24-bit format.  P6 is also 3 bytes per pixel, so
#: the payload size is unchanged by the conversion -- only the channel order is.
BYTES_PER_PIXEL = 3

#: ``DIB_RGB_COLORS``.  The other value (``DIB_PAL_COLORS``) is for palettised
#: displays, which a 24-bit frame is not.
DIB_RGB_COLORS = 0

#: ``SRCCOPY``.  The only raster op this surface uses; anything else would mean
#: the window was compositing, which it is not.
SRCCOPY = 0x00CC0020

#: ``BI_RGB``, an uncompressed DIB.
BI_RGB = 0

#: ``PM_REMOVE`` for ``PeekMessageW``.  A pump that leaves messages queued is a
#: window that stops repainting while the caller thinks it is alive.
PM_REMOVE = 0x0001

#: ``WM_QUIT``.  Returned by :meth:`SurfaceWindow.pump` so a caller's loop can
#: end on the window's own terms rather than a counter it invented.
WM_QUIT = 0x0012

#: ``WM_DESTROY``.  The one message the window procedure handles itself, by
#: posting the quit that ends the pump.
WM_DESTROY = 0x0002

#: ``CS_HREDRAW | CS_VREDRAW``.  Without them a resize leaves the old pixels
#: behind, which looks like a torn frame rather than a stale one.
CS_HREDRAW_VREDRAW = 0x0003

#: ``COLOR_WINDOW`` (the system colour index plus one, as ``hbrBackground``
#: expects).
COLOR_WINDOW = 5

#: ``WS_OVERLAPPEDWINDOW``: a normal window with a caption, a border, and the
#: minimize/maximize/close box.  A borderless fullscreen window would be a
#: different product decision, and this is not the place to make it.
WS_OVERLAPPEDWINDOW = 0x00CF0000

#: ``WS_VISIBLE``.  Set at creation rather than by a later ``ShowWindow`` so
#: there is no window that exists but is not shown -- a state no caller can
#: distinguish from a failed creation by looking at the handle.
WS_VISIBLE = 0x10000000

#: ``SWP_NOZORDER | SWP_NOMOVE`` -- resize in place without reordering the
#: window in the z-order, because moving a window to the top while it is showing
#: a frame is a behaviour change nobody asked for.
SWP_NOZORDER_NOMOVE = 0x0002 | 0x0004

#: ``SW_SHOW`` for the initial ``ShowWindow`` call.
SW_SHOW = 5

#: ``CW_USEDEFAULT``, so the window lands wherever Windows prefers.
CW_USEDEFAULT = 0x80000000

#: ``GR_GDIOBJECTS`` for ``GetGuiResources`` -- the count of GDI objects this
#: process holds.  A window that leaks a DIB section leaks one of these, and the
#: only way to see it is to count.
GR_GDIOBJECTS = 0

#: A ``COLORREF`` is 0x00BBGGRR, which is NOT the DIB's B-G-R order.  Reading a
#: pixel back and comparing it against a frame byte means decoding this, and a
#: comparison that ignores the difference fails in a way that looks like GDI
#: dropped the pixel.
COLORREF_SHIFT_RED = 0
COLORREF_SHIFT_GREEN = 8
COLORREF_SHIFT_BLUE = 16

#: ``IDI_APPLICATION`` / ``IDC_ARROW`` as ``MAKEINTRESOURCE`` values.
IDI_APPLICATION = 32512
IDC_ARROW = 32512

#: Default client size.  The shipped panel's geometry (``config/machine.json``
#: ``display`` and :attr:`unipad.host.display.DEFAULT_GEOMETRY`) is 1376x1024,
#: but a window defaults to something smaller because the desktop is not
#: guaranteed to be that large and a window that cannot be created is worse
#: than a window that must be resized.
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 768

#: Whether this process can create a window at all.  A Windows surface is
#: Windows-only by nature, but this MODULE must import everywhere:
#: ``unipad.host.__init__`` pulls it in, and the macOS runner executes that
#: import in the pin cross-check.  MEASURED: declaring ``ctypes.WINFUNCTYPE``
#: at module level made run 41238555718 die at
#:   AttributeError: module 'ctypes' has no attribute 'WINFUNCTYPE'
#: before it reached a single build step -- a module-level platform assumption
#: took the whole package down.  The house pattern (``unipad.host.httpsvc``)
#: reaches ``ctypes.windll`` lazily inside a function for the same reason.
_WINDOWS = sys.platform == "win32"

#: The window procedure signature, declared once so both the structure field
#: and the callback use the same one.  Off Windows it is a placeholder: the
#: structure still has to be definable, and :class:`SurfaceWindow` refuses to
#: construct before anything would use it.
if _WINDOWS:
    _WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)
else:  # pragma: no cover - exercised only off Windows
    _WNDPROC = ctypes.c_void_p


class WNDCLASSEXW(ctypes.Structure):
    """``WNDCLASSEXW``, the wide ``RegisterClassExW`` argument."""

    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", wintypes.INT),
        ("cbWndExtra", wintypes.INT),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    """``BITMAPINFOHEADER`` for a 24-bit uncompressed DIB.

    ``biHeight`` is negative for a top-down DIB, which is what a P6 PPM is.  A
    positive height would make GDI read the rows bottom-up and present the
    frame upside down -- a defect that is invisible in a byte comparison and
    obvious on screen, which is the kind this module exists to make testable.
    """

    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    """``BITMAPINFO``: the header plus the colour table a 24-bit DIB omits."""

    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class MSG(ctypes.Structure):
    """``MSG`` for ``PeekMessageW``."""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("point_x", wintypes.LONG),
        ("point_y", wintypes.LONG),
    ]


class FrameSource(Protocol):
    """Anything that can hand back one frame as P6 PPM bytes.

    A protocol rather than a base class so a caller can hand in a closure over
    a live QMP client without subclassing anything, and so the tests can hand
    in a generator of known frames.
    """

    def grab(self) -> bytes:
        """The next frame, as a complete ``P6`` PPM document."""
        ...


# --- the GDI entry points, with the signatures ctypes will not guess ---

if not _WINDOWS:  # pragma: no cover - exercised only off Windows
    # Nothing below may touch ctypes.windll: it does not exist off
    # Windows, and reaching it at import time is what broke the runner.
    _USER32 = _GDI32 = _KERNEL32 = None
else:
    _USER32 = ctypes.windll.user32
    _GDI32 = ctypes.windll.gdi32
    _KERNEL32 = ctypes.windll.kernel32

    _USER32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    _USER32.RegisterClassExW.restype = wintypes.ATOM
    _USER32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _USER32.UnregisterClassW.restype = wintypes.BOOL
    _USER32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _USER32.CreateWindowExW.restype = wintypes.HWND
    _USER32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
    _USER32.DefWindowProcW.restype = wintypes.LPARAM
    _USER32.DestroyWindow.argtypes = [wintypes.HWND]
    _USER32.DestroyWindow.restype = wintypes.BOOL
    _USER32.ShowWindow.argtypes = [wintypes.HWND, wintypes.INT]
    _USER32.ShowWindow.restype = wintypes.BOOL
    _USER32.UpdateWindow.argtypes = [wintypes.HWND]
    _USER32.UpdateWindow.restype = wintypes.BOOL
    _USER32.AdjustWindowRectEx.argtypes = [ctypes.POINTER(wintypes.RECT),
                                           wintypes.DWORD, wintypes.BOOL,
                                           wintypes.DWORD]
    _USER32.AdjustWindowRectEx.restype = wintypes.BOOL
    _USER32.GetDC.argtypes = [wintypes.HWND]
    _USER32.GetDC.restype = wintypes.HDC
    _USER32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _USER32.ReleaseDC.restype = wintypes.INT
    _USER32.GetSysColorBrush.argtypes = [wintypes.INT]
    _USER32.GetSysColorBrush.restype = wintypes.HBRUSH
    _USER32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    _USER32.LoadIconW.restype = wintypes.HICON
    _USER32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    _USER32.LoadCursorW.restype = wintypes.HANDLE
    _USER32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                     wintypes.UINT, wintypes.UINT, wintypes.UINT]
    _USER32.PeekMessageW.restype = wintypes.BOOL
    _USER32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    _USER32.TranslateMessage.restype = wintypes.BOOL
    _USER32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    _USER32.DispatchMessageW.restype = wintypes.LPARAM
    _USER32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _USER32.GetGuiResources.restype = wintypes.DWORD
    _USER32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.INT,
                                      wintypes.INT, wintypes.INT, wintypes.INT,
                                      wintypes.UINT]
    _USER32.SetWindowPos.restype = wintypes.BOOL
    _USER32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _USER32.GetClientRect.restype = wintypes.BOOL
    _USER32.IsWindow.argtypes = [wintypes.HWND]
    _USER32.IsWindow.restype = wintypes.BOOL
    _USER32.PostQuitMessage.argtypes = [wintypes.INT]
    _USER32.PostQuitMessage.restype = None

    _GDI32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _GDI32.CreateCompatibleDC.restype = wintypes.HDC
    _GDI32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    _GDI32.CreateDIBSection.restype = wintypes.HBITMAP
    _GDI32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _GDI32.SelectObject.restype = wintypes.HGDIOBJ
    _GDI32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _GDI32.DeleteObject.restype = wintypes.BOOL
    _GDI32.DeleteDC.argtypes = [wintypes.HDC]
    _GDI32.DeleteDC.restype = wintypes.BOOL
    _GDI32.StretchDIBits.argtypes = [
        wintypes.HDC, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT,
        wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT,
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT, wintypes.DWORD]
    _GDI32.GetPixel.argtypes = [wintypes.HDC, wintypes.INT, wintypes.INT]
    _GDI32.GetPixel.restype = wintypes.DWORD
    _GDI32.StretchDIBits.restype = wintypes.INT

    _KERNEL32.GetCurrentProcess.argtypes = []
    _KERNEL32.GetCurrentProcess.restype = wintypes.HANDLE
    _KERNEL32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _KERNEL32.GetModuleHandleW.restype = wintypes.HMODULE


def _header_for(width: int, height: int) -> BITMAPINFO:
    """A ``BITMAPINFO`` for a top-down 24-bit DIB of ``width x height``."""
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # negative: top-down, like a P6 PPM
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 8 * BYTES_PER_PIXEL
    info.bmiHeader.biCompression = BI_RGB
    return info


def read_ppm_header_bytes(ppm: bytes) -> dict[str, int]:
    """``read_ppm_header`` for bytes rather than a path.

    ``unipad.host.logs.read_ppm_header`` reads a file because that is what
    ``screendump`` produces.  This surface holds frames in memory -- a frame
    that has to be written to disk to be displayed is a frame that can fail to
    be written -- so the same parse is needed over bytes.  The magic and the
    ``render_failed`` code are shared with the file version so the two cannot
    drift into accepting different documents.
    """
    if not ppm.startswith(PPM_MAGIC):
        raise UniPadError(
            ErrorCode.RENDER_FAILED,
            "the frame is not a binary PPM (P6)",
            magic=ppm[:16].decode("ascii", errors="replace"),
        )
    fields: list[int] = []
    cursor = len(PPM_MAGIC)
    while len(fields) < 3:
        while cursor < len(ppm) and ppm[cursor:cursor + 1].isspace():
            cursor += 1
        if cursor < len(ppm) and ppm[cursor:cursor + 1] == b"#":
            newline = ppm.find(b"\n", cursor)
            cursor = len(ppm) if newline < 0 else newline + 1
            continue
        start = cursor
        while cursor < len(ppm) and not ppm[cursor:cursor + 1].isspace():
            cursor += 1
        if start == cursor:
            raise UniPadError(
                ErrorCode.RENDER_FAILED,
                "the PPM header ended before width, height and maxval",
                parsed=fields,
            )
        try:
            fields.append(int(ppm[start:cursor]))
        except ValueError as error:
            raise UniPadError(
                ErrorCode.RENDER_FAILED,
                "the PPM header holds a non-numeric field",
                field=ppm[start:cursor].decode("ascii", errors="replace"),
            ) from error
    if fields[0] <= 0 or fields[1] <= 0 or fields[2] <= 0:
        raise UniPadError(
            ErrorCode.RENDER_FAILED,
            "the PPM header declares a non-positive dimension or maxval",
            width=fields[0], height=fields[1], maxval=fields[2],
        )
    if fields[2] > 255:
        raise UniPadError(
            ErrorCode.RENDER_FAILED,
            "the PPM maxval exceeds 255, which a 24-bit surface cannot hold",
            maxval=fields[2],
        )
    # Exactly one whitespace byte separates the header from the payload.
    cursor += 1
    if cursor > len(ppm):
        raise UniPadError(
            ErrorCode.RENDER_FAILED,
            "the PPM has a header and no payload separator",
        )
    return {"width": fields[0], "height": fields[1],
            "maxval": fields[2], "header_bytes": cursor}


def ppm_to_bgr(ppm: bytes) -> tuple[int, int, bytes]:
    """A ``P6`` PPM as ``(width, height, rows)`` in GDI's 24-bit BGR order.

    Pure, and the only place channel order is decided.  The rows stay top-down:
    :class:`BITMAPINFOHEADER`'s negative ``biHeight`` tells GDI to read them
    that way, so doing the flip here instead would flip the image twice.

    Args:
        ppm: A complete P6 document -- magic, whitespace-separated header, one
            whitespace byte, then at least ``width * height * 3`` payload bytes.

    Returns:
        ``(width, height, payload)`` where ``payload`` is exactly
        ``width * height * 3`` bytes and holds B-G-R triplets.

    Raises:
        UniPadError: ``render_failed`` when the document is not a P6 PPM, when
            the header is unreadable, or when the payload is short.  A short
            payload is refused rather than padded: a padded row is a frame with
            invented pixels in it, and the caller cannot tell which ones.
    """
    header = read_ppm_header_bytes(ppm)
    width, height = header["width"], header["height"]
    start = header["header_bytes"]
    expected = width * height * BYTES_PER_PIXEL
    payload = ppm[start:]
    if len(payload) < expected:
        raise UniPadError(
            ErrorCode.RENDER_FAILED,
            "the frame's payload is shorter than its header declares",
            declared=expected,
            present=len(payload),
        )
    # Trailing bytes are not an error -- a producer may pad a row -- but they
    # are not presented either, so they are dropped rather than blitted as a
    # partial extra row.
    payload = payload[:expected]
    rows = bytearray(expected)
    rows[0::3] = payload[2::3]
    rows[1::3] = payload[1::3]
    rows[2::3] = payload[0::3]
    return width, height, bytes(rows)


class SurfaceWindow:
    """One real Windows window that blits 24-bit frames into itself.

    The frame is copied into a DIB section this window owns, and
    ``StretchDIBits`` moves it to the window's device context.  A DIB section
    rather than a direct ``SetDIBitsToDevice`` because it survives a repaint:
    ``WM_PAINT`` is handled by the default procedure, and without a bitmap to
    redraw from, a resize would blank the surface.

    The window procedure is kept as an instance attribute on purpose.  ctypes
    holds only a weak reference to a ``WINFUNCTYPE`` built from a bound method,
    so a procedure that goes out of scope is collected while the window still
    points at it -- and the next message to arrive calls into freed memory.
    That is the classic way this kind of window dies, and it dies silently.

    Args:
        title: Caption text.
        width, height: Client size in pixels.  ``AdjustWindowRectEx`` turns
            these into the outer size, so the *client area* is what was asked
            for rather than whatever is left after the frame and border.
    """

    def __init__(self, title: str = "UniPad", *, width: int = DEFAULT_WIDTH,
                 height: int = DEFAULT_HEIGHT) -> None:
        if not _WINDOWS:
            # Refused here rather than at import, so the module stays importable
            # everywhere and the pure helpers stay usable everywhere.  A caller
            # that reaches this off Windows gets a named refusal instead of an
            # AttributeError from inside ctypes.
            raise UniPadError(
                ErrorCode.RENDER_FAILED,
                "a Windows surface needs Windows; this host is "
                f"{sys.platform}",
                platform=sys.platform,
            )
        if width <= 0 or height <= 0:
            raise UniPadError(
                ErrorCode.RENDER_FAILED,
                "a surface needs positive client dimensions",
                width=width, height=height,
            )
        self.title = title
        self.width = int(width)
        self.height = int(height)
        self._class_name = f"UniPadSurface{id(self):x}"
        self._hwnd: Optional[int] = None
        self._memory_dc: Optional[int] = None
        self._bitmap: Optional[int] = None
        self._old_bitmap: Optional[int] = None
        self._bits: Optional[int] = None      # address GDI allocated
        self._bits_size = 0
        self._frame_count = 0
        self._last_frame_bytes = 0
        # Held for the life of the window; see the class docstring.
        self._wndproc: Optional[Any] = _WNDPROC(self._procedure)
        self._register()

    # --- lifecycle ---

    def _register(self) -> None:
        module = _KERNEL32.GetModuleHandleW(None)
        klass = WNDCLASSEXW()
        klass.cbSize = ctypes.sizeof(WNDCLASSEXW)
        klass.style = CS_HREDRAW_VREDRAW
        klass.lpfnWndProc = self._wndproc
        klass.hInstance = module
        klass.hIcon = _USER32.LoadIconW(module, wintypes.LPCWSTR(IDI_APPLICATION))
        klass.hCursor = _USER32.LoadCursorW(module, wintypes.LPCWSTR(IDC_ARROW))
        klass.hbrBackground = _USER32.GetSysColorBrush(COLOR_WINDOW)
        klass.lpszClassName = self._class_name
        klass.hIconSm = klass.hIcon
        if not _USER32.RegisterClassExW(ctypes.byref(klass)):
            raise UniPadError(
                ErrorCode.RENDER_FAILED,
                "RegisterClassExW refused the surface class",
                last_error=ctypes.get_last_error(),
                class_name=self._class_name,
            )
        outer = wintypes.RECT(0, 0, self.width, self.height)
        if not _USER32.AdjustWindowRectEx(ctypes.byref(outer), WS_OVERLAPPEDWINDOW,
                                          False, 0):
            raise UniPadError(
                ErrorCode.RENDER_FAILED,
                "AdjustWindowRectEx could not size the surface",
                last_error=ctypes.get_last_error(),
            )
        self._hwnd = int(_USER32.CreateWindowExW(
            0, self._class_name, self.title, WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            CW_USEDEFAULT, CW_USEDEFAULT,
            outer.right - outer.left, outer.bottom - outer.top,
            None, None, module, None))
        if not self._hwnd:
            raise UniPadError(
                ErrorCode.RENDER_FAILED,
                "CreateWindowExW did not produce a surface",
                last_error=ctypes.get_last_error(),
                class_name=self._class_name,
            )
        _USER32.ShowWindow(self._hwnd, SW_SHOW)
        _USER32.UpdateWindow(self._hwnd)
        # The DIB section is created here, not on the first frame: a window that
        # exists with no bitmap has a first frame it cannot show, and the caller
        # sees ``blitted: False`` on a frame it just handed over -- which reads
        # as a GDI refusal rather than a missing surface.
        self._resize_bitmap(self.width, self.height)

    def _procedure(self, hwnd: int, message: int, wparam: int,
                   lparam: int) -> int:
        """The window procedure.  ``WM_DESTROY`` ends the message loop."""
        if message == WM_DESTROY:
            _USER32.PostQuitMessage(0)
            return 0
        return int(_USER32.DefWindowProcW(wintypes.HWND(hwnd),
                                          wintypes.UINT(message),
                                          wintypes.WPARAM(wparam),
                                          wintypes.LPARAM(lparam)))

    # --- presentation ---

    @property
    def hwnd(self) -> Optional[int]:
        """The window handle, or ``None`` after :meth:`close`."""
        return self._hwnd

    @property
    def frame_count(self) -> int:
        """How many frames :meth:`present` has blitted.  Not a frame rate."""
        return self._frame_count

    @property
    def last_frame_bytes(self) -> int:
        """Payload size of the most recently blitted frame, in bytes."""
        return self._last_frame_bytes

    def present(self, ppm: bytes) -> dict[str, Any]:
        """Blit one P6 frame into the window, and report what happened.

        Returns:
            ``{"width", "height", "bytes", "frame", "blitted"}``.  ``blitted``
            is ``False`` only when the window is gone or GDI refused the call,
            which is a refusal and not a crash.

        Raises:
            UniPadError: ``render_failed`` when the frame is not a usable P6.
        """
        if self._hwnd is None:
            return {"width": 0, "height": 0, "bytes": 0,
                    "frame": self._frame_count, "blitted": False}
        width, height, rows = ppm_to_bgr(ppm)
        if (width, height) != (self.width, self.height):
            # Both the window AND the bitmap change.  Resizing only the bitmap
            # is the defect this module's own instrument found: the client area
            # stayed at the size it was created with while ``self.width`` grew
            # to the frame's, so :meth:`displayed_pixel`'s bounds check passed
            # for coordinates the real client area did not have -- and the
            # bottom-right pixel of every oversized frame came back ``None``
            # with the window otherwise looking perfect.
            self._resize_window(width, height)
            self._resize_bitmap(width, height)
        if self._bits is None or len(rows) > self._bits_size:
            return {"width": width, "height": height, "bytes": len(rows),
                    "frame": self._frame_count, "blitted": False}
        ctypes.memmove(self._bits, rows, len(rows))
        if not self._stretch():
            return {"width": width, "height": height, "bytes": len(rows),
                    "frame": self._frame_count, "blitted": False}
        self._frame_count += 1
        self._last_frame_bytes = len(rows)
        return {"width": width, "height": height, "bytes": len(rows),
                "frame": self._frame_count, "blitted": True}

    def _resize_window(self, width: int, height: int) -> None:
        """Resize the window's CLIENT area to exactly ``width x height``.

        The window follows the frame rather than the frame being scaled into the
        window, because a framebuffer viewer that silently rescales is showing
        the operator pixels the guest never drew.  ``AdjustWindowRectEx`` turns
        the requested client size into the outer size, so what is asked for is
        what is delivered.

        A refusal here is not fatal on its own -- the frame is still blitted,
        just clipped -- so the caller's ``present`` proceeds and reports what it
        managed.  :meth:`client_size` is the instrument that tells the two
        apart.
        """
        if self._hwnd is None:
            return
        outer = wintypes.RECT(0, 0, width, height)
        if not _USER32.AdjustWindowRectEx(ctypes.byref(outer), WS_OVERLAPPEDWINDOW,
                                          False, 0):
            return
        _USER32.SetWindowPos(
            self._hwnd, None, 0, 0,
            outer.right - outer.left, outer.bottom - outer.top,
            SWP_NOZORDER_NOMOVE)

    def client_size(self) -> Optional[tuple[int, int]]:
        """The window's ACTUAL client area, as Windows measures it.

        This is the check that :meth:`displayed_pixel`'s own bounds test cannot
        do for itself: that test compares against ``self.width``/``self.height``,
        which are what was requested.  When the two disagree -- a window that
        could not be resized, a DPI setting that scaled it -- the bounds check
        passes for coordinates the client area does not have, and
        ``displayed_pixel`` returns ``None`` for a pixel the frame really
        contains.  Comparing this against the requested size is the only way to
        see that.
        """
        if self._hwnd is None:
            return None
        rect = wintypes.RECT()
        if not _USER32.GetClientRect(self._hwnd, ctypes.byref(rect)):
            return None
        return rect.right - rect.left, rect.bottom - rect.top

    def _resize_bitmap(self, width: int, height: int) -> None:
        """Drop the current DIB section and make one of the frame's size."""
        self._drop_bitmap()
        self.width, self.height = width, height
        self._bits_size = width * height * BYTES_PER_PIXEL
        info = _header_for(width, height)
        screen_dc = _USER32.GetDC(None)
        if not screen_dc:
            raise UniPadError(
                ErrorCode.RENDER_FAILED,
                "GetDC(NULL) refused a screen device context",
                last_error=ctypes.get_last_error(),
            )
        try:
            bits = ctypes.c_void_p()
            self._bitmap = int(_GDI32.CreateDIBSection(
                wintypes.HDC(screen_dc), ctypes.byref(info), DIB_RGB_COLORS,
                ctypes.byref(bits), None, 0))
            if not self._bitmap or not bits.value:
                raise UniPadError(
                    ErrorCode.RENDER_FAILED,
                    "CreateDIBSection refused the frame's geometry",
                    width=width, height=height,
                    last_error=ctypes.get_last_error(),
                )
            # GDI allocated the pixels; this is the address to write into.
            self._bits = int(bits.value)
            self._memory_dc = int(_GDI32.CreateCompatibleDC(wintypes.HDC(screen_dc)))
            if not self._memory_dc:
                raise UniPadError(
                    ErrorCode.RENDER_FAILED,
                    "CreateCompatibleDC refused a memory device context",
                    last_error=ctypes.get_last_error(),
                )
            self._old_bitmap = int(_GDI32.SelectObject(wintypes.HDC(self._memory_dc),
                                                       self._bitmap))
        finally:
            _USER32.ReleaseDC(None, wintypes.HDC(screen_dc))

    def _stretch(self) -> bool:
        """``StretchDIBits`` the DIB section onto the window's DC."""
        if self._hwnd is None or self._bits is None:
            return False
        window_dc = _USER32.GetDC(self._hwnd)
        if not window_dc:
            return False
        try:
            info = _header_for(self.width, self.height)
            result = _GDI32.StretchDIBits(
                wintypes.HDC(window_dc), 0, 0, self.width, self.height,
                0, 0, self.width, self.height,
                ctypes.c_void_p(self._bits), ctypes.byref(info),
                DIB_RGB_COLORS, SRCCOPY)
            return result != 0
        finally:
            _USER32.ReleaseDC(self._hwnd, wintypes.HDC(window_dc))

    def readback(self) -> bytes:
        """The pixels the DIB section now holds, as B-G-R triplets.

        This exists so a test can prove a blit happened.  A window that reports
        ``blitted=True`` has proved that GDI accepted the call, not that the
        frame's bytes are in the bitmap -- and "the surface was updated" is
        exactly the kind of claim this project requires an instrument to check
        rather than assume.
        """
        if self._bits is None or self._bits_size == 0:
            return b""
        return ctypes.string_at(self._bits, self._bits_size)

    def displayed_pixel(self, x: int, y: int) -> Optional[tuple[int, int, int]]:
        """The pixel the WINDOW is showing at ``(x, y)``, as ``(r, g, b)``.

        This is the instrument :meth:`readback` cannot be.  ``readback`` returns
        the DIB section's memory, which is written by ``memmove`` and is
        therefore correct even when ``StretchDIBits`` was never called -- and it
        is orientation-blind, because flipping a bottom-up DIB flips how GDI
        *reads* the same bytes rather than the bytes themselves.  A mutation run
        proved both: "the DIB is bottom-up, so every frame is presented upside
        down" and "present reports success without blitting" both survived a
        suite that only read the DIB back.

        ``GetPixel`` reads the window's device context, which is what an
        operator sees, so an upside-down frame and an unblitted one are both
        visible here and neither is visible in ``readback``.

        Returns ``None`` when the window is gone or the coordinate is outside
        it -- a refusal, not ``(0, 0, 0)``, because black is a real pixel and
        conflating the two makes an off-screen coordinate look like a black
        frame.
        """
        if self._hwnd is None:
            return None
        # The bounds check is against the client area Windows actually
        # delivered, NOT against ``self.width``.  ``self.width`` is what was
        # requested, and the two diverge exactly when it matters: if
        # :meth:`_resize_window` could not resize -- a desktop too small, a
        # window style that refuses -- the client area stays small while
        # ``self.width`` grew to the frame's, and a bounds check against the
        # requested size passes for coordinates the window does not have.
        # That is the same defect ``client_size`` was added to expose, and it
        # was living here too.
        client = self.client_size()
        if client is None or not (0 <= x < client[0] and 0 <= y < client[1]):
            return None
        window_dc = _USER32.GetDC(self._hwnd)
        if not window_dc:
            return None
        try:
            colour = int(_GDI32.GetPixel(wintypes.HDC(window_dc), x, y))
            if colour == 0xFFFFFFFF:  # CLR_INVALID: the coordinate is off-window
                return None
            return (
                (colour >> COLORREF_SHIFT_RED) & 0xFF,
                (colour >> COLORREF_SHIFT_GREEN) & 0xFF,
                (colour >> COLORREF_SHIFT_BLUE) & 0xFF,
            )
        finally:
            _USER32.ReleaseDC(self._hwnd, wintypes.HDC(window_dc))

    def gdi_objects_held(self) -> int:
        """How many GDI objects this process currently holds.

        Not this window's objects -- the process's.  That is the useful scope: a
        window that leaks a DIB section leaks one, and the leak is invisible in
        any single window's state.  Take a reading before creating a window and
        the same reading after closing it; if they differ, something leaked.
        """
        return int(_USER32.GetGuiResources(
            wintypes.HANDLE(_KERNEL32.GetCurrentProcess()), GR_GDIOBJECTS))

    def pump(self) -> int:
        """Process queued messages without blocking.

        Returns the number of messages processed, or ``-1`` when ``WM_QUIT``
        arrived.  Non-blocking on purpose: a ``GetMessage`` loop here would
        hand control of the caller's thread to the window and make every
        caller a GUI program whether it wanted to be one or not.
        """
        if self._hwnd is None:
            return -1
        message = MSG()
        processed = 0
        while _USER32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
            if message.message == WM_QUIT:
                return -1
            _USER32.TranslateMessage(ctypes.byref(message))
            _USER32.DispatchMessageW(ctypes.byref(message))
            processed += 1
        return processed

    def _drop_bitmap(self) -> None:
        """Release the DIB section and its DC, in the order GDI requires."""
        if self._memory_dc and self._old_bitmap:
            _GDI32.SelectObject(wintypes.HDC(self._memory_dc),
                                wintypes.HGDIOBJ(self._old_bitmap))
            self._old_bitmap = None
        if self._bitmap:
            _GDI32.DeleteObject(wintypes.HGDIOBJ(self._bitmap))
            self._bitmap = None
        if self._memory_dc:
            _GDI32.DeleteDC(wintypes.HDC(self._memory_dc))
            self._memory_dc = None
        self._bits = None
        self._bits_size = 0

    def close(self) -> None:
        """Destroy the window and its class.  Idempotent."""
        if self._hwnd is not None:
            _USER32.DestroyWindow(self._hwnd)
            self._hwnd = None
        self._drop_bitmap()
        if self._class_name:
            _USER32.UnregisterClassW(self._class_name,
                                     _KERNEL32.GetModuleHandleW(None))
            self._class_name = ""
        self._wndproc = None

    def __enter__(self) -> "SurfaceWindow":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def solid_frame(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A P6 PPM of one colour, for tests and for a window's first paint.

    A first frame of a flat colour is not a fake guest: it is what proves the
    surface can show something before a boot has produced anything, and it is
    the positive control a caller needs before believing a later frame.
    """
    if not (0 <= rgb[0] <= 255 and 0 <= rgb[1] <= 255 and 0 <= rgb[2] <= 255):
        raise UniPadError(
            ErrorCode.RENDER_FAILED,
            "a frame colour channel must be in 0..255",
            rgb=list(rgb),
        )
    return b"P6\n%d %d\n255\n" % (width, height) + bytes(rgb) * (width * height)


def frame_geometry(path: Any) -> tuple[int, int]:
    """The geometry a PPM file on disk declares.

    A thin pass-through to :func:`unipad.host.logs.read_ppm_header` so a caller
    holding a ``screendump`` file can learn its geometry without reimplementing
    the parse -- and so the file-based reader and this surface accept exactly
    the same documents.
    """
    header = read_ppm_header(path)
    return header["width"], header["height"]
