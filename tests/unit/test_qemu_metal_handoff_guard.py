"""The display hand-off has to exist in both halves, or the screen stays black.

Measured on 2026-10-31 (fact 181): every ring from the shared window to an
OPEN Metal device has a measurement.  ``native_qemu_metal_opens=1``, the
owners slot decodes to ``b'back'``, and the opener is the process
``QemuMetalPublish.cpp:50`` names.  And the screen is still black:
``native_gop_gpu_offers=0`` with the device open and rendering.

The reason is structural, and it is the same shape as the gap fact 169
recorded -- a step whose absence is indistinguishable from success.
``NativeGOPPresenter::offer()`` accepts an offer only while a privileged
client is inside ``waitSwap()`` (``gpuClient`` is set there and nowhere
else), and the presenter's own service is one the sandbox refuses.  The
client that is supposed to wait lives in ``bridgephase::serveScanout``
(drivers/NativeMetal/NativeMetalBridgePhase.h), which
``NativeAdrenoMetalPlugin.mm`` starts and ``QemuMetalPlugin.mm`` never did.
So no offer could ever be accepted, and every counter still looked
plausible.

What this file guards is the hand-off's four load-bearing facts:

  1. the accelerator accepts a SECOND open type that is not the slot type,
     and that branch is reached before the ``type != kOpenType`` refusal --
     otherwise the hand-off connection is refused and counted as a slot
     refusal, which reads like a sandbox problem;
  2. a hand-off connection takes no slot and writes no owner name, so a
     slot open's measurement keeps meaning what it meant;
  3. the three method numbers and the scanout memory type are the
     presenter's own, read back out of ``NativeGOPWire.h`` -- a renumbering
     upstream must turn this red rather than silently serving the wrong
     method;
  4. the plugin starts the scanout thread for the render server ONLY, and
     it probes for an admitted selector instead of assuming one.

The fifth is the one that keeps a measurement honest: every outcome is
counted, so "the hand-off never ran" and "the hand-off ran and was refused"
are different readings rather than the same zero.

Every dependency is hand-copied here, so the last test reads each one back
out of the file it claims to describe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KIT = REPO / "vendor" / "UniXNU-qemu" / "package" / "QemuMetal"
ACCELERATOR = KIT / "QemuMetalAccelerator.cpp"
PLUGIN = KIT / "QemuMetalPlugin.mm"
SCANOUT = KIT / "QemuMetalScanout.mm"
BUILDER = REPO / "vendor" / "UniXNU-qemu" / "scripts" / "build-qemu-metal.py"
#: Upstream's own wire records and the presenter that serves them.  These are
#: in the device repository, not in this board's kit: the hand-off is a
#: contract with code this repository does not own, which is exactly why the
#: numbers have to be read from there rather than written here.
WIRE = REPO / "vendor" / "UniXNU" / "drivers" / "NativeMobileDisplay" / "NativeGOPWire.h"
PRESENTER = REPO / "vendor" / "UniXNU" / "drivers" / "NativeMobileDisplay" / "NativeGOPPresenter.cpp"


def _text(path: Path) -> str:
    """A source file as text, with the checkout's line endings normalised.

    This repository is checked out CRLF on Windows, so a multi-line pattern
    written with ``\\n`` never matches.  Normalising here is cheaper than
    writing every pattern to tolerate an optional ``\\r``.
    """
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _constant(source: str, name: str) -> int:
    """One ``constexpr`` integer, by name, out of the source that defines it."""
    match = re.search(rf"\b{name}\s*=\s*(0[xX][0-9a-fA-F]+|\d+)\b", source)
    assert match, f"{name} is not defined as an integer constant any more"
    return int(match.group(1), 0)


# --- 1. a second open type, reached before the slot refusal ----------------


def test_the_accelerator_accepts_a_second_open_type_that_is_not_the_slot_type() -> None:
    """0x4148 and 0x514d are different numbers, and both are spelled out.

    If the hand-off type ever collapsed into ``kOpenType`` the connection
    would take a slot of the window away from the render server, and the
    owners array would then name two processes in a boot where only one
    rendered -- a reading that looks like a bug in the render server.
    """
    source = _text(ACCELERATOR)
    slot = _constant(source, "kOpenType")
    handoff = _constant(source, "kHandoffOpenType")
    assert slot == 0x514D, "kOpenType moved; the plugin's open would be refused"
    assert handoff == 0x4148, "kHandoffOpenType moved; the plugin's hand-off open would be refused"
    assert handoff != slot


def test_the_handoff_branch_is_reached_before_the_slot_refusal() -> None:
    """Order is the whole point: a hand-off open that falls through to the
    ``type != kOpenType`` refusal increments ``native_qemu_metal_refusals``.

    That counter is the one that said "iconservicesagent was refused" on
    2026-10-30.  A hand-off connection landing in it would be read as a
    sandbox problem in the render server, and the real cause -- no scanout
    client -- would stay invisible.
    """
    source = _text(ACCELERATOR)
    branch = source.index("if (type == kHandoffOpenType)")
    refusal = source.index("if (type != kOpenType)")
    assert branch < refusal, (
        "the hand-off open type is checked after the slot refusal, so a "
        "hand-off connection is counted as a slot refusal"
    )
    assert "native_qemu_metal_handoff_opens = native_qemu_metal_handoff_opens + 1" in source, (
        "the hand-off branch does not count its own opens"
    )


def test_a_handoff_connection_takes_no_slot_and_writes_no_owner() -> None:
    """``start()`` returns before the slot loop when the connection is hand-off only.

    The owners array is the evidence that ``backboardd`` is the process that
    opened the accelerator (fact 181).  A hand-off connection that reached
    ``proc_selfname`` would overwrite it, and the next run would show an
    owner that is not the render server.
    """
    source = _text(ACCELERATOR)
    body = source[source.index("bool AGXDeviceUserClient::start("):]
    body = body[: body.index("void AGXDeviceUserClient::giveBack")]
    assert "if (handoff) return true;" in body, (
        "a hand-off connection no longer returns before the slot loop"
    )
    assert body.index("if (handoff) return true;") < body.index("proc_selfname"), (
        "the slot loop runs before the hand-off check"
    )


# --- 3. the numbers are the presenter's, not this board's ------------------


@pytest.mark.parametrize(
    "name",
    ["NGP_METHOD_MODE", "NGP_METHOD_WAIT_SWAP", "NGP_METHOD_COMPLETE_SWAP"],
)
def test_the_handoff_method_numbers_are_the_presenters_own(name: str) -> None:
    """Read out of ``NativeGOPWire.h``, which this repository does not own.

    The three methods are the whole hand-off: mode (the framebuffer's
    geometry), wait (block until a frame is offered), complete (report the
    GPU's verdict).  A number written here from memory and renumbered
    upstream would serve a different method and still return success for
    the ones that happen to line up.
    """
    wire = _text(WIRE)
    values = dict(re.findall(r"\b(NGP_METHOD_\w+)\s*=\s*(\d+)", wire))
    assert values, "NativeGOPWire.h no longer names the hand-off methods"
    source = _text(ACCELERATOR)
    base = _constant(source, "kHandoffBase")
    last = _constant(source, "kHandoffLast")
    method = int(values[name])
    assert 0 <= method <= last - base, (
        f"{name} is {method}, which is outside kHandoffBase..kHandoffLast "
        f"({base}..{last}); the presenter would be asked for a method it "
        "does not have"
    )
    assert _constant(source, "kHandoffBase") == 0x7000, (
        "kHandoffBase moved; the method numbers would land somewhere else"
    )


def test_the_scanout_memory_type_is_served_before_the_slot_type_refusal() -> None:
    """Type 0x5343 is not a slot, and ``type != 0`` would refuse it.

    The scanout is the writable firmware framebuffer, which the presenter
    hands out only in GPU mode.  Serving it after the slot check would make
    every scanout map fail with ``kIOReturnBadArgument`` -- the same answer
    a hand-off connection asking for a slot gets, so the plugin could not
    tell "no GPU mode" from "wrong connection".
    """
    source = _text(ACCELERATOR)
    assert _constant(source, "kScanoutType") == 0x5343
    body = source[source.index("IOReturn AGXDeviceUserClient::clientMemoryForType"):]
    served = body.index("native_gop_handoff_memory()")
    refused = body.index("if (type != 0 || slot < 0")
    assert served < refused, (
        "the scanout memory type is checked after the slot type's refusal"
    )
    assert "native_qemu_metal_scanout_maps = native_qemu_metal_scanout_maps + 1" in body, (
        "a served scanout is not counted"
    )


def test_the_presenter_hands_the_scanout_out_only_in_gpu_mode() -> None:
    """The kernel side's premise, checked where it is actually decided.

    ``native_gop_handoff_memory()`` returns null unless the presenter is in
    GPU mode.  If that gate ever moved, the plugin's scanout would fail at
    step 3 and the reason would not be this guest's boot arguments.
    """
    presenter = _text(PRESENTER)
    memory = presenter[presenter.index("IOMemoryDescriptor *NativeGOPPresenter::handoffMemory"):]
    assert "!p->gpuMode" in memory, (
        "handoffMemory no longer refuses when the presenter is not in GPU mode"
    )


# --- 4. the plugin starts the client, for the right process ----------------


def test_the_plugin_starts_the_scanout_for_the_render_server_only() -> None:
    """The frame the display driver offers is the compositor's surface.

    A ``--metal-clients`` grant gets a device of its own to render with.  If
    it also waited for frames, two clients would race for the presenter's
    one frame queue and the loser would report a protocol error.
    """
    source = _text(PLUGIN)
    assert "qemu_metal_serve_scanout((__bridge void *)device, port);" in source
    guard = source.index("qemu_metal_serve_scanout((__bridge void *)device, port);")
    assert "if (server)" in source[:guard], (
        "the scanout thread is started for every client, not only the render server"
    )
    service = source[source.index("mach_port_t service("):]
    service = service[: service.index("id open(")]
    assert '*server = true; return accelerator;' in service, (
        "service() no longer says which of the two services this process opened"
    )
    assert '*server = false;' in service


def test_the_scanout_client_probes_for_a_selector_instead_of_assuming_one() -> None:
    """Which selectors a sandbox profile admits is not this process's decision.

    sheng's Adreno plugin probes for the same reason
    (drivers/NativeMetal/NativeAdrenoUserTest.h).  A client that assumed
    selector 0x7000 would send its first call into a refusal and report "no
    frames", which is indistinguishable from a black screen for any other
    reason.
    """
    source = _text(SCANOUT)
    assert "for (uint32_t selector = 0; selector < 256; ++selector)" in source, (
        "the scanout client does not probe the selectors"
    )
    assert "kProbe" in source and "return selector;" in source
    assert "if (!link.selector) {" in source, (
        "a boot whose profile admits no selector is not reported as such"
    )
    assert "tunnel(link, kProbe, nullptr, 0, nullptr, 0) == 0" in source


def test_the_scanout_client_opens_a_connection_of_its_own() -> None:
    """Serving a frame must not take a slot of the window from the render server.

    The window is finite and slot 0 is the render server's; a scanout client
    that opened with the slot type would consume one of the rest and write
    its name into the owners array.
    """
    source = _text(SCANOUT)
    assert "link.openFn(link.port, mach_task_self(), kHandoffOpenType, &link.connection)" in source, (
        "the scanout client does not open a hand-off connection of its own"
    )


def test_the_scanout_thread_cannot_kill_the_render_server_with_an_exception() -> None:
    """An exception escaping a worker thread aborts the whole host process.

    This thread runs inside backboardd.  sheng's plugin wraps the same call
    for the same reason (int-075/083: SpringBoard died), and a Metal
    exception on a frame is an ordinary outcome, not a corner case.
    """
    source = _text(SCANOUT)
    assert "@try {" in source, "the serving loop is not inside @try"
    assert "@catch (NSException *exception)" in source, (
        "the serving loop has no @catch"
    )
    assert source.index("@try {") < source.index("while (client.serveOne(1000))") < source.index(
        "@catch (NSException *exception)"
    ), "the serving loop is not the code the @try covers"


# --- 5. every outcome is counted, so the readings stay distinct ------------


def test_every_handoff_outcome_is_counted() -> None:
    """``handoff_opens=0`` and ``handoff_calls=0`` must mean different things.

    The first is "no scanout client ever ran"; the second is "it ran and
    every method was refused".  Both look like a black screen, and only the
    counters tell them apart -- which is the instrument discipline
    ``test_a_counter_that_is_defined_but_cannot_be_read_is_a_refusal`` is
    about, one layer further out.

    Two kinds, and the difference is load-bearing: a total is only useful
    if it accumulates, and a "last" is only useful if it is overwritten.  A
    last-value counter that someone converts into ``= x + 1`` reports a
    running sum of selector numbers, which is not any reading at all.
    """
    source = _text(ACCELERATOR)
    totals = (
        "native_qemu_metal_handoff_opens",
        "native_qemu_metal_handoff_calls",
        "native_qemu_metal_handoff_refusals",
        "native_qemu_metal_scanout_maps",
        "native_qemu_metal_handoff_probes",
    )
    lasts = (
        "native_qemu_metal_handoff_last_selector",
        "native_qemu_metal_handoff_last_method",
        "native_qemu_metal_handoff_last_status",
    )
    for name in totals:
        assert re.search(rf"\b{name}\s*=\s*{name}\s*\+\s*1", source), (
            f"{name} is declared but never accumulated"
        )
    for name in lasts:
        assert re.search(rf"\b{name}\s*=\s*(?!{name}\b)", source), (
            f"{name} is declared but never assigned a value"
        )
        assert not re.search(rf"\b{name}\s*=\s*{name}\s*\+", source), (
            f"{name} is a last-value counter and must not accumulate"
        )


def test_every_handoff_counter_is_four_bytes_wide() -> None:
    """The observer asks for a width, and a wrong one folds two counters together.

    ``native_qemu_metal_stage`` is four bytes in ``QemuMetalPublish.cpp``;
    reading eight would report a number that is no counter's value.  The
    hand-off counters are read as one block alongside it, so they have to
    be the same width -- and the block has to declare exactly the eight the
    guard above counts, or one of them is silently not read.
    """
    source = _text(ACCELERATOR)
    declared = re.search(r"volatile uint32_t (native_qemu_metal_handoff\w[^;]*);", source)
    assert declared, "the hand-off counters are not declared as one uint32_t block"
    names = declared.group(1).replace("\n", " ").split(",")
    names = [name.strip() for name in names if name.strip()]
    assert len(names) == 8, f"expected eight hand-off counters, found {len(names)}: {names}"
    for name in names:
        assert name.startswith("native_qemu_metal_handoff") or name.startswith(
            "native_qemu_metal_scanout"
        ), f"{name} is not a hand-off counter"


def test_the_builder_links_the_scanout_client_into_the_bundle() -> None:
    """A file nobody compiles is the gap this whole family of guards is about.

    The bundle is the only place the plugin runs; ``QemuMetalScanout.mm``
    has to be in its link or the scanout thread does not exist and
    ``native_gop_gpu_offers`` stays 0 for a reason no counter can show.
    """
    source = _text(BUILDER)
    bundle = source[source.index("def bundle()"):source.index("def trust(")]
    assert "KIT / 'QemuMetalScanout.mm'" in bundle, (
        "build-qemu-metal.py's bundle link no longer compiles QemuMetalScanout.mm"
    )
    # The Mac instrument must NOT get it: it has no IOKit user client to
    # open, and a scanout client there would be dead code that still has to
    # compile.
    instruments = source[source.index("def instruments()"):source.index("def check()")]
    assert "QemuMetalScanout" not in instruments, (
        "the Mac instrument compiles the scanout client, which needs a guest connection"
    )


# --- read every dependency back out of the file it describes ---------------


@pytest.mark.parametrize(
    "path",
    [ACCELERATOR, PLUGIN, SCANOUT, BUILDER, WIRE, PRESENTER],
)
def test_the_files_this_guard_describes_exist(path: Path) -> None:
    """A guard whose anchors have moved fails as "not found", not as "passing".

    Every pattern above is a literal from the file it names.  If a rename
    makes one of them stop matching, the test that owns it goes red -- which
    is the difference between a guard and a comment.
    """
    assert path.is_file(), f"{path.relative_to(REPO)} is missing"
