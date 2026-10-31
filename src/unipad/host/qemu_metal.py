"""The QemuMetal counters a run has to ask for, in one place.

Why this module exists
----------------------
The instrument family under ``local-private/tmp`` reads guest kernel globals by
name, and a counter nobody asks for is a counter that reads as absent --
``measure-display.py``'s own note records the cost: its 100-name list did not
carry the seven ``QemuMetalPublish.cpp`` counters, so the accelerator sat at
stage 0 for days while the run read 94 other globals and looked complete.  The
names were then hand-copied into three scratch instruments, which is the same
divergence class one level further out.

So the groups live here, next to the code that declares them, and the
observation instruments import them rather than restating them.  The widths are
part of the contract: every one of these is a ``volatile uint32_t`` in the
kernel, and a probe that read eight bytes would fold the next counter into this
one and report a number that is no counter's value.

The names are not invented here either.  :func:`declared_in` reads them back out
of ``QemuMetalAccelerator.cpp`` and ``LegacyDisplayProbe.cpp``, and
``tests/unit/test_qemu_metal_counters.py`` asserts the two sets are equal in
both directions -- so a counter added to the kernel and not asked for here fails
a test, and a name asked for here and not in the kernel fails the same test.

Nothing in this module touches the guest, QEMU or the filesystem.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Tuple

#: ``vendor/UniXNU-qemu/package/QemuMetal/QemuMetalAccelerator.cpp``, which
#: declares the accelerator's and the hand-off's counters in one ``extern "C"``
#: block.  In the device repository, not this one: it is the source of truth.
ACCELERATOR_SOURCE = ("vendor", "UniXNU-qemu", "package", "QemuMetal", "QemuMetalAccelerator.cpp")

#: ``vendor/UniXNU-qemu/package/LegacyDisplayProbe.cpp``, which declares the
#: display probe's counters -- the ones that say WHERE the frame path stopped.
PROBE_SOURCE = ("vendor", "UniXNU-qemu", "package", "LegacyDisplayProbe.cpp")

#: ``QemuMetalPublish.cpp``, which declares the publish counters
#: (``native_qemu_metal_stage``/``_status``/``_grants``) in a different
#: translation unit from the accelerator's own.  Two files, one counter family,
#: which is exactly why the list cannot be derived from either alone.
PUBLISH_SOURCE = ("vendor", "UniXNU-qemu", "package", "QemuMetal", "QemuMetalPublish.cpp")

#: ``SharedWindow.cpp``, which declares the gate the whole accelerator is
#: gated on (``native_shared_window_stage``).
WINDOW_SOURCE = ("vendor", "UniXNU-qemu", "package", "SharedWindow.cpp")

#: ``drivers/NativeMobileDisplay/NativeGOPPresenter.cpp`` in the device
#: repository, which declares the presenter's offer family.
PRESENTER_SOURCE = ("vendor", "UniXNU", "drivers", "NativeMobileDisplay", "NativeGOPPresenter.cpp")

#: Every source that declares a counter this list asks for.  Adding a group
#: without adding its source makes ``test_qemu_metal_counters.py`` fail, which
#: is the point: the list is only as good as its claim about where the names
#: come from.
SOURCES = (ACCELERATOR_SOURCE, PROBE_SOURCE, PUBLISH_SOURCE, WINDOW_SOURCE, PRESENTER_SOURCE)

#: The sources whose EVERY ``native_*`` counter this list claims to ask for.
#:
#: Scoped deliberately, because the other direction cannot be: the display
#: probe alone declares about sixty ``native_*`` diagnostics and the display
#: chain reads eleven of them.  "Every counter in the tree is asked for" is not
#: a contract anyone can honour, so the claim is the one that is true and
#: checkable -- the QemuMetal device's own counters, across the two translation
#: units that declare them, are all read.
CLAIMED_SOURCES = (ACCELERATOR_SOURCE, PUBLISH_SOURCE)

#: The named counters claimed outside :data:`CLAIMED_SOURCES`.  Each of these
#: lives in a source that declares much more than the display chain reads, so
#: the claim is made name by name rather than by file.
CLAIMED_NAMES = (
    "native_shared_window_stage",
    "native_gop_ready",
    "native_gop_gpu_offers",
    "native_gop_gpu_completions",
    "native_gop_gpu_failures",
    "native_gop_gpu_abandoned",
)

#: The shared window's stage, the accelerator's publish counters and the
#: hand-off's own.  All ``volatile uint32_t``.
#:
#: The hand-off group is the ring the 2026-10-30 measurement located but could
#: not close: the Metal device was open and rendering (fact 181), and
#: ``native_gop_gpu_offers`` stayed 0 because nothing ever called the
#: presenter's ``waitSwap`` -- which is the only place its ``gpuClient`` is set,
#: and an offer is refused outright without it.  These are the counters that say
#: whether the client ran, whether the sandbox admitted a selector for it, and
#: what the presenter answered.
QMETAL_COUNTERS: Tuple[Tuple[str, int], ...] = (
    ("native_shared_window_stage", 4),
    ("native_shared_window_base", 8),
    ("native_shared_window_size", 8),
    ("native_qemu_metal_stage", 4),
    ("native_qemu_metal_status", 4),
    ("native_qemu_metal_opens", 4),
    ("native_qemu_metal_refusals", 4),
    ("native_qemu_metal_grants", 4),
    ("native_qemu_metal_owners", 4),
    ("native_qemu_metal_handoff_opens", 4),
    ("native_qemu_metal_handoff_calls", 4),
    ("native_qemu_metal_handoff_refusals", 4),
    ("native_qemu_metal_scanout_maps", 4),
    ("native_qemu_metal_handoff_probes", 4),
    ("native_qemu_metal_handoff_last_selector", 4),
    ("native_qemu_metal_handoff_last_method", 4),
    ("native_qemu_metal_handoff_last_status", 4),
)

#: The GOP presenter's offer family.  ``native_gop_transfers`` is deliberately
#: *not* here: ``NativeGOPPresenter::transfer()`` returns
#: ``kIOReturnUnsupported`` as its first statement when ``gpuMode``, so it is 0
#: by design.  The offers are what say whether the presenter was asked for a
#: frame at all.
#:
#: EIGHT bytes, and that is a correction rather than a preference.  The
#: observation instrument asked for these four at four, while
#: ``NativeGOPPresenter.cpp:15`` declares them ``volatile uint64_t`` -- so every
#: reading so far was the low half of a 64-bit counter.  It happened to be right
#: at the values measured (0 and 1), which is exactly what makes a wrong width
#: dangerous: it is invisible until it wraps.  ``test_qemu_metal_counters.py``
#: is what caught it, by comparing the list against the declaration rather than
#: against a previous list.
GOP_COUNTERS: Tuple[Tuple[str, int], ...] = (
    ("native_gop_ready", 4),
    ("native_gop_gpu_offers", 8),
    ("native_gop_gpu_completions", 8),
    ("native_gop_gpu_failures", 8),
    ("native_gop_gpu_abandoned", 8),
)

#: Whether anything is drawing, ordered from the earliest gate to the latest.
#:
#: ``native_legacy_frame_count`` is NOT "a frame was presented", and reading it
#: that way cost a wrong conclusion once already (fact 183): its two increment
#: sites are the blank fill and the CPU copy, both AFTER the offer, and the
#: accepted offer returns before either.  A working hand-off leaves it at 0 by
#: design.  What says where the path stopped is ``transfer_calls`` -- it is
#: incremented as the first statement of ``native_legacy_transfer_observed()``,
#: which the driver calls on every way out of ``kickOnce()`` including the four
#: early returns, so 0 means the driver never got a validated swap at all.
DRAWING_COUNTERS: Tuple[Tuple[str, int], ...] = (
    ("native_legacy_stage", 4),
    ("native_legacy_swap_validation_calls", 4),
    ("native_legacy_swap_accepted_count", 4),
    ("native_legacy_swap_rejected_count", 4),
    ("native_legacy_swap_validation_reason", 4),
    ("native_legacy_transfer_calls", 4),
    ("native_legacy_frame_count", 4),
    ("native_legacy_copy_bytes_sum", 8),
    ("native_legacy_gpu_fallbacks", 4),
    ("native_legacy_prepare_calls", 4),
    ("native_legacy_retirement_calls", 4),
)

#: Every group, so an instrument can ask for all of them without naming three
#: tuples and risking one.
GROUPS = (QMETAL_COUNTERS, GOP_COUNTERS, DRAWING_COUNTERS)


def spec() -> Dict[str, int]:
    """Every name above with its width, later groups overriding earlier ones.

    A duplicate would otherwise be read twice for no reason, and a name that
    appears in two groups at two widths is a bug this surfaces rather than
    hides.
    """
    merged: Dict[str, int] = {}
    for group in GROUPS:
        for name, width in group:
            if name in merged and merged[name] != width:
                raise ValueError(f"{name} is asked for at {merged[name]} and {width} bytes")
            merged[name] = width
    return merged


#: A ``volatile uint32_t name;`` or ``volatile uint32_t a, b, c;`` declaration.
_DECLARATION = re.compile(r"volatile\s+uint32_t\s+([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*;")
#: A ``volatile uint64_t name;`` declaration.
_WIDE_DECLARATION = re.compile(r"volatile\s+uint64_t\s+([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*;")
#: ``volatile int name;`` -- the one signed counter in the family.
#: ``native_qemu_metal_status`` is an ``IOReturn``, so it is declared ``int`` and
#: not ``uint32_t``; it is still four bytes, and a list that only knew
#: ``uint32_t`` would silently stop asking for the one counter that says WHY the
#: publish stopped.
_SIGNED_DECLARATION = re.compile(r"volatile\s+int\s+([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*;")
#: ``volatile char name[...]`` -- the owners array, which is not a scalar.
#: ``native_qemu_metal_owners`` is ``char[QM_SLOTS][17]``: one 17-byte NUL-padded
#: process name per slot of the window.  It is read as FOUR bytes, which is the
#: first four characters of slot 0 -- that is how fact 181 decoded ``b'back'``
#: out of 1801675106, and a wider read would fold slot 0's tail into slot 1's
#: head and name a process that does not exist.
_CHAR_ARRAY = re.compile(r"volatile\s+char\s+(native_[A-Za-z0-9_]+)\s*\[")


def declared_in(repo_root: Path, *relative: str) -> Dict[str, int]:
    """Every ``native_*`` counter a source declares, with the width a read uses.

    Read from the source rather than restated, so the test that compares this
    with :func:`spec` is comparing two measurements rather than two copies of
    the same intention.  Only ``native_*`` names are returned: the same files
    declare plenty of unrelated globals, and a set comparison over all of them
    would fail on every unrelated addition.

    The width is the width a READ of that name uses, which for one name is not
    the width it is declared at: see :data:`_CHAR_ARRAY`.
    """
    text = (repo_root.joinpath(*relative)).read_text(encoding="utf-8").replace("\r\n", "\n")
    found: Dict[str, int] = {}
    for pattern, width in ((_DECLARATION, 4), (_WIDE_DECLARATION, 8), (_SIGNED_DECLARATION, 4)):
        for group in pattern.findall(text):
            for name in group.split(","):
                name = name.strip()
                if name.startswith("native_"):
                    found[name] = width
    for name in _CHAR_ARRAY.findall(text):
        found[name] = 4
    return found


def declared_everywhere(repo_root: Path) -> Dict[str, int]:
    """Every ``native_*`` counter in :data:`SOURCES`, with its width.

    A name declared in two sources at two widths is a contradiction and raises,
    rather than letting the second file win by dict order.
    """
    found: Dict[str, int] = {}
    for relative in SOURCES:
        for name, width in declared_in(repo_root, *relative).items():
            if name in found and found[name] != width:
                raise ValueError(f"{name} is {found[name]} and {width} bytes wide across SOURCES")
            found[name] = width
    return found
