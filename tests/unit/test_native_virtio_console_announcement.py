"""A driver that bound perfectly and one that never enumerated must not read the same.

Why this exists
---------------
Run 41224396626 booted a kernel carrying ``NativeVirtioConsole`` -- the guest
half of the agent channel -- on the Windows host, and measured:

* the guest FIRMWARE wrote 924 bytes of EDK2/BdsDxe boot-console output into the
  agent channel, so the host side of the transport is receiving;
* the guest reported itself ready through its kernel ring buffer;
* over a 240-second window the KERNEL wrote 0 bytes to that channel;
* ``status.request`` got no reply;
* the ring buffer GREW from 17,110 to 17,156 bytes during the window, so the
  guest was alive, not dead.

``0 bytes`` does not say the driver failed to bind.  It cannot separate three
readings, and only one of them is a defect:

(a) ``probe()`` did not match the transport, so the driver never attached;
(b) the driver attached and nothing in the guest writes to that port -- which is
    the LIVE one, because ``payload.json``'s ``kernel_args`` carries
    ``serial=3``, so the kernel's own ``printf`` goes to the SERIAL port and a
    perfectly working console driver would also produce 0 bytes here;
(c) the driver writes only on demand.

So "0 bytes" proves "no bytes arrived", not "the driver did not enumerate".
Those need different fixes, and a measurement that cannot tell them apart is
worth nothing either way.

The fix is instrumentation, not a feature: the driver announces itself on the
port at the first point the port is genuinely writable, so any boot of a kernel
carrying it produces bytes the host can measure.  That turns (a) versus (b) into
a one-line measurement.

Where that point is, and why it is not ``probe()``
--------------------------------------------------
This is the part that must not be guessed, because a write in the wrong place is
worse than no write: it would panic the guest and destroy the evidence instead
of producing it.  Read out of ``NativeVirtioConsole.cpp``:

* ``probe()`` matches the device and RELEASES the view it mapped.  It sets none
  of ``gate``, ``registers``, ``ownsDevice``, ``online`` or the queue buffers --
  every one of those is a default-initialised member (``gate=nullptr``,
  ``registers=0``, ``online=false``, ``queues[2]`` all null) and is only filled
  in by ``start()``.
* ``send()`` begins with ``IOLockLock(gate)`` BEFORE it consults ``online``.
  Called from ``probe()`` that locks a null ``IOLock`` -- a kernel panic, not a
  silent no-op.  And even past that, ``if(online)`` is false there, so it would
  return ``kIOReturnNotReady`` and put nothing on the wire anyway.
* ``start()`` allocates ``gate``, maps the device, sets ``registers``, and calls
  ``configure()``, which allocates BOTH queue buffers (``configureQueue`` runs
  for ids 0 and 1), publishes both rings and sets ``online``.  Only after
  ``ownsDevice=true;if(!configure())return false;`` is the port writable.
* ``handleOpen`` is NOT required.  The private ``send()`` never consults
  ``owner``; only the ``extern "C"`` ``native_console_send()`` does, and it
  refuses with ``kIOReturnNotOpen`` while ``isOpen(owner)`` is false.  Nothing
  has opened the port at ``start()`` time, so routing the announcement through
  that entry point would make it a guaranteed refusal.

Hence: the announcement goes in ``start()``, after the ``configure()`` gate and
before ``registerService()`` -- which is also before any client could open the
port, so it measures the binding itself rather than a client's arrival.

What this file guards
---------------------
Six properties, each of which a plausible edit would break:

1. the announcement is a fixed, recognisable literal naming the driver and the
   transport it bound -- the host greps for it;
2. it fits the transport's buffer, because ``send()`` refuses anything larger;
3. it is emitted through the driver's own ``send()``, not through
   ``native_console_send()``, which would refuse it;
4. it is emitted AFTER the port is writable -- inside ``start()``, after the
   ``configure()`` gate -- and never from ``probe()``;
5. its length is derived from the array with ``sizeof(...)-1`` rather than
   hand-counted, so editing the string cannot leave a stale count;
6. it is instrumentation: ``start()`` still returns ``true`` unconditionally, so
   a host that never drains the queue cannot stop the driver from starting.

How the patterns are written, and why
-------------------------------------
Every structural pattern below is tolerant of whitespace.  That is not
fastidiousness: the first mutation round carried a control that reformatted the
call's spacing and nothing else, and the guard went red on it.  A guard that
goes red on a reformat is the shape that trains people to ignore a stage, so
the assertions match on WHAT is called and WHERE it sits, never on how it is
spaced.  The one place spacing is asserted exactly is the string's own payload,
because there the bytes ARE the contract -- the host greps for them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.vendor_paths import require

REPO = Path(__file__).resolve().parents[2]
#: The driver the kernel build compiles: ``build-arm-driver.py`` resolves
#: ``NativeVirtioConsole`` to ``drivers/NativeVirtioConsole/NativeVirtioConsole.cpp``
#: and archives it as ``libNativeVirtioConsole.a``.  It lives in the UniXNU pin's
#: ``drivers/NativeVirtioConsole`` submodule (``config/sources.lock.json``'s
#: ``submodules`` array), NOT in the device repository -- which is why this guard
#: reads it out of ``vendor/UniXNU`` rather than out of ``vendor/UniXNU-qemu``.
DRIVER = (
    REPO / "vendor" / "UniXNU" / "drivers" / "NativeVirtioConsole" / "NativeVirtioConsole.cpp"
)
#: The bytes the host is meant to see, with a real newline where the C++ source
#: spells the two characters ``\\n``.
ANNOUNCEMENT = "NativeVirtioConsole: bound virtio,mmio console port 0\n"
#: The same string as the C++ source spells it.
ANNOUNCEMENT_ESCAPED = ANNOUNCEMENT.replace("\n", "\\n")

# --- the shapes this guard matches, written to survive a reformat -----------
#
# ``\\s*`` between tokens rather than an exact spelling.  Each of these was a
# literal at first and each of them went red on a whitespace-only mutant.

#: The declaration, with the payload spelled exactly.
_LITERAL = r'static\s+char\s+const\s+announcement\s*\[\s*\]\s*=\s*"' + re.escape(
    ANNOUNCEMENT_ESCAPED
) + r'"\s*;'
#: The send, with the length derived from the array.
_SEND = (
    r"send\s*\(\s*announcement\s*,\s*sizeof\s*\(\s*announcement\s*\)\s*-\s*1\s*\)\s*;"
)
#: Any send of the announcement, however its length is spelled.
_SEND_ANY = r"send\s*\(\s*announcement\b"
#: A hand-counted length.
_SEND_COUNTED = r"send\s*\(\s*announcement\s*,\s*\d+\s*\)"
#: The shared entry point that would refuse the announcement.
_SHARED_SEND = r"\bnative_console_send\s*\("
#: The three lines of ``start()`` that make the port writable, in order.
_LOCK = r"gate\s*=\s*IOLockAlloc\s*\(\s*\)"
_CONFIGURED = r"ownsDevice\s*=\s*true\s*;\s*if\s*\(\s*!\s*configure\s*\(\s*\)\s*\)\s*return\s+false\s*;"
_REGISTERED = r"registerService\s*\(\s*\)\s*;\s*return\s+true\s*;"


def _text(path: Path) -> str:
    """A source file as text, with the checkout's line endings normalised.

    The pinned checkouts are laid down with ``core.autocrlf=true`` on Windows, so
    every line ends CRLF and a pattern written with ``\\n`` never matches.
    Normalising here is cheaper than teaching every pattern to tolerate an
    optional ``\\r``.
    """
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _function(source: str, header: str) -> str:
    """One member function's body, from its header to its closing brace.

    ``probe()`` and ``start()`` are different bodies in the same file, and the
    whole point of this guard is which of them the write lives in.  Slicing by
    header rather than by "the next occurrence of send(" keeps the two
    distinguishable.
    """
    start = source.index(header)
    return source[start : source.index("\n}\n", start)]


def _start(source: str) -> str:
    return _function(source, "bool NativeVirtioConsole::start(IOService *provider)")


def _probe(source: str) -> str:
    return _function(source, "IOService *NativeVirtioConsole::probe(IOService *provider")


def _code(body: str) -> str:
    """A function body with its ``//`` comments removed.

    The structural assertions ask "is this symbol called here", and a comment
    that names the symbol is not a call.  Measured on this file's own first run:
    the comment explaining why ``native_console_send`` cannot carry the
    announcement contains the text ``native_console_send(``, so the guard went
    red on its own prose.  ``NativeVirtioConsole.cpp`` carries only ``//``
    comments and no string literal containing ``//``, so dropping from the
    marker to end of line is exact here -- and if a block comment ever arrives
    the stripped text simply keeps the ``/*``, which changes no assertion's
    outcome silently.
    """
    return "\n".join(line.split("//", 1)[0] for line in body.split("\n"))


def _at(pattern: str, body: str, what: str) -> int:
    """Where ``pattern`` first matches, failing with ``what`` when it does not."""
    match = re.search(pattern, body)
    assert match, what
    return match.start()


# --- 1. a fixed literal the host can recognise ------------------------------


def test_the_announcement_is_a_literal_the_host_can_recognise() -> None:
    """The host greps for a string, so the string has to be exact and fixed.

    It names the driver AND the transport it bound, because the measurement is
    worthless if it cannot distinguish "the console driver bound on virtio,mmio"
    from some other driver writing to the same channel.  A greeting that drifts
    with a refactor is a greeting the probe stops matching, and a probe that
    matches nothing reports 0 bytes -- which is the reading this change exists
    to disambiguate.

    The declaration's spacing is not asserted; the payload's is, byte for byte.
    """
    source = _text(require(DRIVER))
    assert re.search(_LITERAL, source), (
        "the driver no longer declares the announcement literal with the exact "
        "payload; the host would have nothing to match and the two readings "
        "collapse again"
    )
    assert "NativeVirtioConsole" in ANNOUNCEMENT, "the announcement does not name the driver"
    assert "virtio,mmio" in ANNOUNCEMENT, "the announcement does not name the transport it bound"
    assert ANNOUNCEMENT.endswith("\n"), "the announcement is not newline-terminated"
    assert "\r" not in ANNOUNCEMENT, "the announcement carries a CR, which the host would read as a second record"


def test_the_announcement_fits_the_transports_buffer() -> None:
    """``send()`` refuses anything larger than ``bufferSize``.

    A 4 KiB limit is generous for a greeting, but a "useful" announcement that
    grew into a diagnostic dump would be refused with ``kIOReturnBadArgument``
    and the channel would stay silent -- with a string in the source that looks
    like it should be on the wire.
    """
    source = _text(require(DRIVER))
    match = re.search(r"\bbufferSize\s*=\s*(\d+)", source)
    assert match, "bufferSize is no longer an integer constant"
    buffer_size = int(match.group(1))
    assert buffer_size == 4096, f"bufferSize moved to {buffer_size}; the limit is not the one send() enforces"
    assert len(ANNOUNCEMENT) < buffer_size, (
        f"the announcement is {len(ANNOUNCEMENT)} bytes and send() refuses "
        f"anything over {buffer_size}"
    )


# --- 2. through the driver's own send, not the shared entry point ------------


def test_the_announcement_goes_through_the_drivers_own_send() -> None:
    """``native_console_send()`` would refuse it, and the refusal is silent.

    That entry point checks ``isOpen(owner)`` and returns ``kIOReturnNotOpen``
    when nobody has opened the port -- which is exactly the situation at
    ``start()`` time, because ``handleOpen`` has not run yet.  A routing change
    from ``send`` to ``native_console_send`` therefore compiles, runs, returns
    a status nobody looks at, and puts nothing on the wire.
    """
    source = _text(require(DRIVER))
    body = _code(_start(source))
    assert re.search(_SEND_ANY, body), (
        "start() no longer sends the announcement through the driver's own send()"
    )
    assert not re.search(_SHARED_SEND, body), (
        "the announcement is routed through native_console_send(), which refuses "
        "while isOpen(owner) is false -- and nothing has opened the port yet"
    )
    # And the reason that entry point would refuse is still there to be read.
    assert re.search(r"console->isOpen\s*\(\s*owner\s*\)", source), (
        "native_console_send() no longer checks isOpen(owner); the reason it "
        "cannot carry this announcement has moved"
    )


# --- 3. after the port is writable, never from probe() ----------------------


def test_the_announcement_is_emitted_after_the_port_is_writable() -> None:
    """Order inside ``start()`` is the whole point.

    ``send()`` locks ``gate`` before it looks at ``online``.  Three things have
    to be true when it runs, and each is established by a specific earlier line:
    ``gate`` exists (``IOLockAlloc``), the device is configured (``configure()``
    returns, which is what sets ``online`` and allocates both queue buffers),
    and the register view is mapped.  A write moved above the ``configure()``
    gate returns ``kIOReturnNotReady``; a write moved above ``IOLockAlloc``
    panics the kernel.  Both look like "no bytes arrived".
    """
    source = _text(require(DRIVER))
    body = _code(_start(source))
    send_at = _at(_SEND_ANY, body, "start() does not send the announcement at all")
    lock_at = _at(_LOCK, body, "start() no longer allocates gate with IOLockAlloc()")
    configured_at = _at(_CONFIGURED, body, "start() no longer gates on configure() returning")
    registered_at = _at(_REGISTERED, body, "start() no longer ends with registerService();return true;")
    assert lock_at < send_at, "the announcement is sent before gate is allocated, so send() locks a null IOLock"
    assert configured_at < send_at, (
        "the announcement is sent before configure() returns, so online is "
        "still false and send() returns kIOReturnNotReady with nothing on the wire"
    )
    assert send_at < registered_at, (
        "the announcement is sent after registerService(), which is later than "
        "it needs to be and no longer measures the binding itself"
    )


def test_probe_never_writes_to_the_port() -> None:
    """``probe()`` matches the device; it does not touch the rings.

    This is the mutation that would destroy the evidence rather than fail to
    collect it.  In ``probe()`` every member ``send()`` depends on is still at
    its default: ``gate`` is null, ``registers`` is 0, both queue buffers are
    null and ``online`` is false.  ``send()`` takes ``gate`` first, so the call
    panics.  Asserted here by structure -- no ``send`` call in that body at all --
    because the defect is unreachable by any amount of source reading alone.
    """
    source = _text(require(DRIVER))
    probe = _code(_probe(source))
    assert not re.search(r"\bsend\s*\(", probe), (
        "probe() writes to the port; gate, registers, the queue buffers and "
        "online are all still unset there, so send() locks a null IOLock and "
        "panics the guest instead of producing bytes"
    )
    assert "announcement" not in probe, "the announcement lives in probe() as well as somewhere else"
    # probe() is also where the driver decides it does not match, and that
    # decision has to stay a non-match rather than a half-attached one.
    assert "return nullptr" in probe, "probe() no longer refuses a device it does not match"


def test_the_announcement_is_not_gated_on_a_client_opening_the_port() -> None:
    """The measurement is about the binding, not about a client's arrival.

    ``handleOpen`` sets ``owner`` and nothing else in this driver reads it except
    the ``extern "C"`` entry points.  An announcement behind that gate fires only
    once some other driver opens the port -- which is the very thing the 240 s
    window was waiting for and did not see.
    """
    source = _text(require(DRIVER))
    body = _code(_start(source))
    send_at = _at(_SEND_ANY, body, "start() does not send the announcement at all")
    assert not re.search(r"\bhandleOpen\b", body[:send_at]), (
        "something in start() before the announcement consults handleOpen, so "
        "the announcement is gated on a client that has not arrived"
    )


# --- 4. the length is derived, not counted ---------------------------------


def test_the_announcement_length_is_derived_from_the_array() -> None:
    """A hand-counted length is a defect waiting for the next edit.

    ``send()`` copies exactly the length it is given, so a count that is one
    short truncates the greeting and a count that is one long sends the NUL and
    whatever follows it in rodata.  Both still put bytes on the wire, so the
    host would see a partial or polluted string and the failure would look like
    a transport problem.
    """
    source = _text(require(DRIVER))
    body = _code(_start(source))
    assert re.search(_SEND, body), (
        "the announcement's length is no longer sizeof(announcement)-1, so it "
        "can drift from the string it is supposed to describe"
    )
    assert not re.search(_SEND_COUNTED, body), (
        "the announcement's length is a literal number rather than sizeof()-1"
    )


# --- 5. instrumentation, not a feature -------------------------------------


def test_the_announcement_cannot_stop_the_driver_from_starting() -> None:
    """A host that never drains the queue must not cost the guest its console.

    ``send()`` ends in ``complete()``, which polls for up to 2 seconds and then
    returns ``kIOReturnNotResponding`` while incrementing
    ``native_console_failures``.  If that status were allowed to fail
    ``start()``, a host that accepted the TCP connection and then read nothing
    would leave the guest with no console driver at all -- turning an
    instrumentation line into a boot dependency.
    """
    source = _text(require(DRIVER))
    body = _code(_start(source))
    send_at = _at(_SEND_ANY, body, "start() does not send the announcement at all")
    registered_at = _at(_REGISTERED, body, "start() no longer ends with registerService();return true;")
    assert send_at < registered_at, "the announcement is sent after start() has already returned"
    assert "return false" not in body[send_at:registered_at], (
        "the announcement's status is now tested, so a host that does not drain "
        "the queue can stop the driver from starting"
    )


# --- read the dependency back out of the file it describes -----------------


def test_the_file_this_guard_describes_exists() -> None:
    """A guard whose anchor has moved fails as "not found", not as "passing".

    ``require`` keeps that property while making the clean-checkout case honest:
    when the owning checkout is absent the test skips, and when the checkout is
    present and this file is not it fails -- see ``tests/vendor_paths.py``.
    """
    require(DRIVER)
    assert DRIVER.is_file(), f"{DRIVER.name} is missing from the pinned checkout"
