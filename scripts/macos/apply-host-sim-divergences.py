#!/usr/bin/env python3
"""Apply the recorded divergences the pinned driver tree needs to build its own host simulation.

Why this exists
---------------
``check-turnip-darwin-host.py`` compiles the REAL driver sources
(``drivers/NativeAdreno/*.cpp``) against a stub IOKit that the simulation
provides, and links them into a dylib.  In the pinned tree that no longer works,
and the gaps are all in the simulation rather than in the driver:

  * ``tests/XnuMemoryFixture.h`` declares a hand-picked subset of the IOKit
    vocabulary.  It declares ``kIODirectionOut`` and ``kIODirectionInOut`` as
    constants but never the ``IODirection`` type, and it has no
    ``kIOMemoryPersistent``.  On a real build both come from ``<IOKit/IOTypes.h>``
    via ``NativeAdrenoMemory.h:4``.  MEASURED on run 40932083150 as two compile
    errors in ``NativeAdrenoMemory.cpp:167,170``.
  * ``tests/hostsim/host_iokit_shim.cpp`` answers the IOKit entry points the
    Turnip backend uses, but nothing in the simulation defines
    ``proc_selfpid``, which ``NativeAdrenoUserClient.cpp:14`` declares and
    ``:237`` calls.  On a real kernel build XNU provides it.  MEASURED on run
    40939171888 as a link error:
    ``Undefined symbols for architecture arm64: "_proc_selfpid"``.

Every one of these is in a file the tablet never sees -- the shim says so in its
own header ("Never built for the tablet").  But they are still changes to a
PINNED tree, so they are applied HERE, by name, with the anchor each one extends
verified before anything is written, rather than being edited into the checkout:
the workflow's acquisition step resets ``vendor/UniXNU`` to its pin on every run,
so an in-tree edit would vanish and the next run would fail identically with
nothing in this repository saying why.

What this script guarantees
---------------------------
Each divergence names its anchor.  If the anchor is not present the script
REFUSES and changes nothing -- an edit applied to a file it has never seen is how
a two-declaration change becomes a corrupted header.  A divergence whose result
is already present is skipped, so a second application cannot stack.  Both
directions are tested in tests/unit/test_host_sim_divergences.py.

Usage:
    apply-host-sim-divergences.py DRIVER_ROOT [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

#: The divergences, in the order they must be applied.  Each is
#:   (path, anchor, replacement, what it fixes, why the value is safe)
#: The anchor must appear EXACTLY ONCE; a second occurrence means the upstream
#: file changed shape and the fix has to be re-derived by hand.
DIVERGENCES = [
    (
        "drivers/NativeAdreno/tests/XnuMemoryFixture.h",
        "constexpr unsigned kIODirectionOut=2,kIODirectionInOut=3,kIOMemoryMapperNone=0x800,kIOMemoryPhysicallyContiguous=0x10;",
        "using IODirection=unsigned;\n"
        "constexpr unsigned kIODirectionOut=2,kIODirectionInOut=3,kIOMemoryMapperNone=0x800,"
        "kIOMemoryPhysicallyContiguous=0x10,kIOMemoryPersistent=0x40000;",
        "NativeAdrenoMemory.cpp:167 needs the IODirection type and :170 needs kIOMemoryPersistent",
        "0x40000 shares no bit with kIOMemoryMapperNone (0x800) or "
        "kIOMemoryPhysicallyContiguous (0x10), the only two the fixture's "
        "withAddressRange branches on, so the bit is inert in the simulation",
    ),
    (
        "drivers/NativeAdreno/tests/hostsim/host_iokit_shim.cpp",
        'extern "C" {\nint host_gpu_open();',
        'extern "C" {\n'
        "/* XNU provides proc_selfpid; the simulation links the driver sources into a\n"
        " * dylib, so it has to answer it here.  The host process's own pid is the\n"
        " * faithful stand-in: NativeAdrenoUserClient.cpp:237 only packs it into a\n"
        " * diagnostic word. */\n"
        "int proc_selfpid(void);\n"
        "int host_gpu_open();",
        "NativeAdrenoUserClient.cpp:14 declares proc_selfpid and :237 calls it",
        "getpid() is what the symbol answers on any UNIX; the shim already "
        "includes <unistd.h> for its own tracing",
    ),
]

#: The definition that turns the declaration above into a symbol the linker can
#: find.  Kept separate because it belongs after the shim's includes, not inside
#: its declaration block.
SHIM_DEFINITION = (
    "\nextern \"C\" int proc_selfpid(void) { return getpid(); }\n"
)


def _apply_one(root: Path, relative: str, anchor: str, replacement: str) -> str:
    """Apply one divergence, or say why it did not.  Returns what happened."""
    path = root / relative
    if not path.is_file():
        return f"MISSING {relative}"
    text = path.read_text(encoding="utf-8")

    if replacement.splitlines()[0] in text and text.count(anchor) == 0:
        # The result is already there and the anchor is gone: applied by an
        # earlier run in this same job, or by an upstream that fixed it.
        return f"PRESENT {relative}"

    if text.count(anchor) != 1:
        return (
            f"REFUSED {relative}: the anchor appears {text.count(anchor)} times, "
            f"not exactly once -- the upstream file changed shape and this "
            f"divergence must be re-derived by hand"
        )

    path.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")
    return f"APPLIED {relative}"


def _add_shim_definition(root: Path) -> str:
    """Give the shim's ``proc_selfpid`` declaration a body.

    Appended rather than inserted at a fixed line, because the shim's tail is
    where its other ``extern "C"`` definitions live and an insertion point is one
    more thing to keep in sync.
    """
    path = root / "drivers/NativeAdreno/tests/hostsim/host_iokit_shim.cpp"
    if not path.is_file():
        return "MISSING host_iokit_shim.cpp"
    text = path.read_text(encoding="utf-8")
    if "int proc_selfpid(void) { return getpid(); }" in text:
        return "PRESENT host_iokit_shim.cpp definition"
    path.write_text(text + SHIM_DEFINITION, encoding="utf-8")
    return "APPLIED host_iokit_shim.cpp definition"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: apply-host-sim-divergences.py DRIVER_ROOT [--dry-run]", file=sys.stderr)
        return 2
    root = Path(argv[0]).resolve()
    dry = "--dry-run" in argv[1:]
    if not (root / "scripts" / "check-turnip-darwin-host.py").is_file():
        print(f"no driver checkout at {root}", file=sys.stderr)
        return 2

    applied = 0
    for relative, anchor, replacement, fixes, why in DIVERGENCES:
        path = root / relative
        if dry:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            state = "already-applied" if replacement.splitlines()[0] in text else (
                f"would-apply (anchor x{text.count(anchor)})" if text.count(anchor) == 1 else "REFUSED"
            )
            print(f"{relative}: {state}")
            print(f"    fixes: {fixes}")
            print(f"    safe : {why}")
            continue
        outcome = _apply_one(root, relative, anchor, replacement)
        print(outcome)
        if outcome.startswith("REFUSED"):
            return 1
        if outcome.startswith("APPLIED"):
            applied += 1
            print(f"    fixes: {fixes}")
            print(f"    safe : {why}")

    if not dry:
        # The shim's declaration needs a body; only the shim divergence has one.
        outcome = _add_shim_definition(root)
        print(outcome)
        if outcome.startswith("REFUSED"):
            return 1
        if outcome.startswith("APPLIED"):
            applied += 1

    print(f"{'would apply' if dry else 'applied'} {applied} divergence(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
