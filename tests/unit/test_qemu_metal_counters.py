"""The counter list and the kernel's own declarations have to be the same set.

This is the instrument check, and it exists because of a specific cost.  The
``QemuMetalPublish.cpp`` counters were missing from ``measure-display.py``'s
100-name list, so the accelerator sat at stage 0 for days while the instrument
read 94 other globals and reported a complete-looking run -- a measured absence
that was never measured.  The names were then hand-copied into three scratch
instruments, which is the same failure one level further out.

So ``src/unipad/host/qemu_metal.py`` holds the groups, the observation
instruments import them, and this file asserts the two sets are equal in BOTH
directions:

  * a counter the kernel declares and the list does not ask for is the original
    failure -- it reads as absent;
  * a name the list asks for and the kernel does not declare is a typo, and it
    reads as absent too, which is worse because it looks like a finding.

The comparison is against the sources in the device repository, read by
:func:`~unipad.host.qemu_metal.declared_in`, so it is two measurements rather
than two copies of one intention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unipad.host import qemu_metal as qm

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def declared() -> dict[str, int]:
    """Every ``native_*`` counter the sources declare, with its width."""
    for relative in qm.SOURCES:
        path = REPO.joinpath(*relative)
        assert path.is_file(), f"{path.relative_to(REPO)} is missing"
    return qm.declared_everywhere(REPO)


def test_every_counter_the_kernel_claims_is_asked_for(declared: dict[str, int]) -> None:
    """The original failure, as a test.

    A counter nobody asks for is a counter that reads as absent, and a run that
    reports a complete-looking set having never read it is the expensive kind of
    wrong.

    Scoped to :data:`~unipad.host.qemu_metal.CLAIMED_SOURCES` plus
    :data:`~unipad.host.qemu_metal.CLAIMED_NAMES`, because the other direction
    is not a contract anyone can honour: the display probe declares about sixty
    ``native_*`` diagnostics and the display chain reads eleven.  What IS a
    contract is that the QemuMetal device's own counters -- across the two
    translation units that declare them -- are all read, and the test names the
    scope rather than pretending it is everything.
    """
    asked = set(qm.spec())
    claimed = set(qm.CLAIMED_NAMES)
    for relative in qm.CLAIMED_SOURCES:
        claimed |= set(qm.declared_in(REPO, *relative))
    missing = sorted(name for name in claimed if name not in asked)
    assert not missing, (
        f"these counters are claimed and the list does not ask for them: {missing}"
    )
    for name in claimed:
        assert name in declared, f"{name} is claimed but no source in SOURCES declares it"


def test_every_counter_the_list_asks_for_the_kernel_declares(declared: dict[str, int]) -> None:
    """A name that is not in the kernel reads as absent, which looks like a finding.

    ``test_a_counter_the_kernel_does_not_have_is_a_negative_not_a_failure`` in
    ``test_host_guestobserve.py`` makes an absent symbol *reportable*; this makes
    it not happen in the first place for the counters this repository owns.
    """
    asked = set(qm.spec())
    invented = sorted(name for name in asked if name not in declared)
    assert not invented, (
        f"the list asks for counters the kernel does not declare: {invented}"
    )


def test_the_widths_match_the_kernels_own_declarations(declared: dict[str, int]) -> None:
    """Four bytes for a ``uint32_t``, eight for a ``uint64_t``, and nothing else.

    A probe that read a ``uint32_t`` as eight bytes would fold the next counter
    into this one and report a number that is no counter's value --
    ``test_a_counter_the_kernel_has_is_read_at_the_width_it_asked_for`` asserts
    the probe honours the width it is given; this asserts the width it is given.
    """
    for name, width in qm.spec().items():
        assert declared[name] == width, (
            f"{name} is declared {declared[name]} bytes wide in the kernel and "
            f"asked for at {width}"
        )


def test_a_name_asked_for_at_two_widths_is_refused() -> None:
    """A duplicate at a different width is a bug, not a convenience.

    ``spec()`` would otherwise let the last group win silently, and the run
    would read one width and report another.
    """
    seen: dict[str, int] = {}
    for group in qm.GROUPS:
        for name, width in group:
            if name in seen and seen[name] != width:
                with pytest.raises(ValueError, match=name):
                    qm.spec()
                return
            seen[name] = width


def test_the_handoff_group_covers_the_eight_counters_the_kernel_counts() -> None:
    """The hand-off is the ring the 2026-10-30 run located and could not close.

    Eight names, and the three "last" ones are末值 rather than totals -- which is
    the distinction ``test_every_handoff_outcome_is_counted`` guards on the
    kernel side.  If one of them stops being asked for, the reading that says
    "the hand-off never reached the presenter" loses the detail that says why.
    """
    handoff = sorted(
        name for name, _ in qm.QMETAL_COUNTERS if "handoff" in name or "scanout_maps" in name
    )
    assert len(handoff) == 8, f"expected eight hand-off counters, found {len(handoff)}: {handoff}"
    for name in (
        "native_qemu_metal_handoff_opens",
        "native_qemu_metal_handoff_calls",
        "native_qemu_metal_handoff_refusals",
        "native_qemu_metal_scanout_maps",
        "native_qemu_metal_handoff_probes",
        "native_qemu_metal_handoff_last_selector",
        "native_qemu_metal_handoff_last_method",
        "native_qemu_metal_handoff_last_status",
    ):
        assert name in handoff, f"{name} is no longer asked for"
