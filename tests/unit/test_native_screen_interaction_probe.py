"""M9's two guest-channel records, and the veto that decides whether they are true.

Why this exists
---------------
``M9`` is the first gate on the blocking path that is not waiting on a build.
Its verdict is FAIL, and the reason is the PRD's own veto
(``src/unipad/acceptance/milestones.py:1745-1752``)::

    @_predicate("homescreen_screenshot_only")
    def _homescreen_screenshot_only(snapshot):
        \"\"\"PRD 215: "只展示主屏幕截图".\"\"\"
        return _ok(snapshot, "p.screendump") and not _ok(snapshot, "p.springboard_interaction")

Read that carefully, because it is the whole reason this file exists.  A
screenshot of the homescreen plus a mouse click is NOT evidence of an operable
SpringBoard -- it is a screenshot and a click.  The gate refuses to be
satisfied by the pair, and it is right to.

So ``springboard.interaction`` cannot mean "input was delivered".  It has to
mean the input changed what is on screen.  Those are two different facts and
only one of them is the one the PRD asked for.

What is measurable, and where
-----------------------------
Both halves are kernel-side, which matters because the guest userspace is
Apple's real code and cannot be modified to emit anything:

* **the screen.**  ``NativeGOPPresenter::start()``
  (``vendor/UniXNU/drivers/NativeMobileDisplay/NativeGOPPresenter.cpp:82-98``)
  maps the scanout writable and cross-checks it against ``PE_current_console()``
  -- same width, height, rowBytes and ``v_baseAddr``.  So the kernel holds a
  verified view of exactly the bytes the user is looking at, and a digest over
  them is a measurement rather than an inference.
* **the input.**  ``NativeHidInput`` reaches ``handleReport()`` from
  ``touchSend()`` (:188), ``buttonsAction()`` (:204) and ``tick()`` (:244), and
  the device is attached for real: the staged kernel args carry
  ``offline-hid-input=1 offline-hid-home=1 sheng-touch-hid=1`` and
  ``tests/unit/test_staged_kernel_args_provenance.py`` asserts them.  The
  attach is default-OFF in ``HidInputProbe.cpp:147`` and the boot turns it on.

Put those together and ``springboard.interaction`` becomes: digest the screen,
deliver the input, digest again, report only if the two differ.  That is a
measurement of "mouse input changed the guest interface" from a guest channel,
which is the sentence the milestone asks for.

What is NOT claimed, and must not be
------------------------------------
* that a digest changing proves the user's intent was honoured.  It proves the
  screen changed after input.  A clock redraw would also change it.  The record
  therefore carries both digests and the changed-byte count, so a reader can
  see HOW MUCH changed rather than trusting a boolean.
* that ``springboard.running`` means SpringBoard is usable.  It means the exec
  of a process named SpringBoard succeeded.  The site is ``__mac_execve()``'s
  ``if (!error)`` block -- the same site ``zzz-m8-launchd-daemon-events.patch``
  uses for ``daemon.spawned``, where ``p_comm`` has just been written by
  ``set_proc_name()``.

The two invariants that are one edit away from silent
-----------------------------------------------------
1. **The interaction record is gated on a CHANGE, never on a delivery.**  An
   emitter that fires when the report is handed to the HID stack clears
   ``p.springboard_interaction`` and clears the veto, and the release verdict
   improves, while the guest has been photographed standing still.  That is the
   defect this test's teeth check reproduces.
2. **The digests are of the SCANOUT, not of a report buffer.**  Digesting the
   input report proves nothing about the screen.  The digest function must read
   the presenter's mapping.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VENDOR = REPO / "vendor" / "UniXNU"
DRIVERS = VENDOR / "drivers"
PATCHES = VENDOR / "patches"

RUNNING_EVENT = '"event":"springboard.running"'
INTERACTION_EVENT = '"event":"springboard.interaction"'

# The two driver files that hold the halves.  Named rather than discovered,
# because the coupling between them is the thing under test: the input driver
# cannot emit without the display driver's digest, and a reader needs to see
# which two files must move together.
PRESENTER = "NativeMobileDisplay/NativeGOPPresenter.cpp"
INPUT = "NativeHidInput/NativeHidInput.cpp"

# The symbol the console publisher exposes.  Strong, never null-checked, for the
# reason zzz-m8-launchd-daemon-events.patch records at length: a weak reference
# resolved to null on build 41330622788 and dropped two records behind a green
# build.
PUBLISHER = "native_console_report"


def _vendor_tree() -> list[Path]:
    if not DRIVERS.is_dir():
        pytest.skip(f"{DRIVERS} is not present; the pinned vendor tree is not acquired")
    found = [p for p in DRIVERS.rglob("*.cpp") if "Original" not in p.name]
    found += [p for p in DRIVERS.rglob("*.h") if "Original" not in p.name]
    if PATCHES.is_dir():
        found += sorted(PATCHES.rglob("*.patch"))
    assert found, f"no source anywhere under {DRIVERS}"
    return found


def _added_lines(patch: Path) -> list[str]:
    """The lines a patch ADDS, with their hunk headers, context kept.

    Context is kept because the nesting a call site sits at is decided by the
    conditionals around it -- the same lesson the declaration-visibility test
    records, where all three builds failed on exactly that.
    """
    return [line for line in patch.read_text(encoding="utf-8").splitlines()
            if line.startswith(("+", "@@")) and not line.startswith(("+++", "+diff"))]


def _all_text() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in _vendor_tree()}


def _interaction_sources(tree: dict[Path, str]) -> dict[Path, str]:
    """Files that mention the interaction record at all."""
    return {p: t for p, t in tree.items() if INTERACTION_EVENT in t}


def _digest_sources(tree: dict[Path, str]) -> dict[Path, str]:
    return {p: t for p, t in tree.items() if "screen_digest" in t}


# ---------------------------------------------------------------------------
# springboard.running
# ---------------------------------------------------------------------------


def test_springboard_running_is_emitted_from_a_kernel_site():
    tree = _all_text()
    hits = {p for p, t in tree.items() if RUNNING_EVENT in t}
    assert hits, (
        f"no file under {DRIVERS} or {PATCHES} builds the record {RUNNING_EVENT}. "
        "M9's first requirement (milestones.py:2477-2483) is "
        "'SpringBoard running in the guest, from a guest channel'."
    )


def test_springboard_running_uses_the_exec_site_where_the_name_is_known():
    """The record must name the process, so it must be emitted where p_comm is set.

    ``__mac_execve()``'s ``if (!error)`` block is the only place in that
    function where the exec has succeeded AND the new identity can be named:
    ``p`` is the switched proc and ``p_comm`` was written by ``set_proc_name()``
    inside ``exec_mach_imgact()``.  ``imgp->ip_ndp`` is already freed by then,
    which is why the M8 patch reads ``p_comm`` and not the namei record.
    """
    tree = _all_text()
    hits = {p for p, t in tree.items() if RUNNING_EVENT in t}
    if not hits:
        pytest.skip("no springboard.running emitter exists yet; "
                    "test_springboard_running_is_emitted_from_a_kernel_site owns that failure")
    for path in hits:
        text = tree[path]
        if "kern_exec" not in str(path) and "kern_exec" not in text:
            continue
        assert "p_comm" in text or "SpringBoard" in text, (
            f"{path.name} emits {RUNNING_EVENT} but never reads p_comm or names "
            "SpringBoard, so the record cannot distinguish SpringBoard from any "
            "other exec and would fire on launchd."
        )


# ---------------------------------------------------------------------------
# springboard.interaction -- the veto's half
# ---------------------------------------------------------------------------


def test_springboard_interaction_is_emitted():
    tree = _all_text()
    assert _interaction_sources(tree), (
        f"no file builds the record {INTERACTION_EVENT}.  "
        "M9's second requirement (milestones.py:2484-2492) is "
        "'mouse input changed the guest interface, from a guest channel'."
    )


def test_the_interaction_record_is_gated_on_a_screen_change():
    """THE TEETH.  Input delivered is not input honoured.

    A record emitted when the report reaches the HID stack would clear
    ``p.springboard_interaction`` and clear ``homescreen_screenshot_only``
    (milestones.py:1745-1752), and the release verdict would improve while the
    guest stood still.  So the emit must sit behind a comparison of two screen
    digests taken at different times.
    """
    tree = _all_text()
    sources = _interaction_sources(tree)
    assert sources, "no interaction emitter exists; nothing to gate"
    offenders = []
    for path, text in sources.items():
        if "screen_digest" not in text:
            offenders.append(f"{path.name}: no screen digest is computed")
            continue
        # Two digest VALUES must be compared.  A single digest compared against a
        # constant is not a before/after pair.  The operator is ANY comparison:
        # `before == after -> return` and `before != after -> emit` are the same
        # gate written two ways, and an earlier revision of this rule matched only
        # `!=`/`<`/`>` -- which rejected the honest `== ... return` shape while
        # the teeth check was running, and would have kept it rejected.
        compared = re.search(
            r"(before|after|first|second|previous|prior)\w*\s*"
            r"(==|!=|<=|>=|<|>)[^;\n]*"
            r"(before|after|first|second|previous|prior)",
            text,
        )
        if not compared:
            offenders.append(f"{path.name}: no before/after digest comparison")
    assert not offenders, (
        "the interaction record is not gated on a screen change:\n  "
        + "\n  ".join(offenders)
    )


def test_the_digest_reads_the_scanout_not_a_report_buffer():
    """Digesting the input report would prove the input happened, not that the screen moved.

    ``NativeGOPPresenter::start()`` (NativeGOPPresenter.cpp:82-98) validates the
    mapping against ``PE_current_console()`` -- ``v_baseAddr``, ``v_width``,
    ``v_height``, ``v_rowBytes`` -- so the digest has to be taken over that
    mapping for the record to be about the screen.
    """
    tree = _all_text()
    digests = _digest_sources(tree)
    assert digests, "no screen digest function exists"
    offenders = []
    for path, text in digests.items():
        if path.suffix == ".patch":
            continue          # a patch that adds the definition is checked by its target's own file
        if "getVirtualAddress" not in text and "mapping" not in text:
            offenders.append(f"{path.name}: the digest does not read the presenter's mapping")
    assert not offenders, (
        "the screen digest is not reading the verified scanout:\n  " + "\n  ".join(offenders)
    )


def test_the_input_driver_and_the_display_driver_are_coupled():
    """The two halves must be in the two files that hold them, or neither works.

    ``NativeHidInput`` is the only thing that delivers input to the guest and
    ``NativeGOPPresenter`` is the only thing that holds the scanout.  If the
    emit lives in the input driver while the digest lives nowhere, or the digest
    lives in the display driver while nothing calls it, the record cannot
    arrive -- and it would be absent, which reads exactly like a guest that
    never moved.
    """
    tree = _all_text()
    by_name = {p.name: p for p in tree if p.suffix == ".cpp"}
    presenter = by_name.get("NativeGOPPresenter.cpp")
    inp = by_name.get("NativeHidInput.cpp")
    assert presenter is not None, f"{PRESENTER} is not in the pinned vendor tree"
    assert inp is not None, f"{INPUT} is not in the pinned vendor tree"
    assert INTERACTION_EVENT in tree[inp], (
        f"{inp.name} delivers the input but does not emit {INTERACTION_EVENT}; "
        "the record can only be built where the input is delivered."
    )
    assert "screen_digest" in tree[presenter], (
        f"{presenter.name} holds the scanout but exposes no digest; the input "
        "driver would have to reach into its private mapping."
    )


def test_the_publisher_reference_is_strong_and_never_null_checked():
    """A null check here would make a missing archive look deliberate.

    Measured on build 41330622788: a ``weak_import`` reference resolved to null
    and dropped two records behind a green build with both records byte-level in
    the booting kernel, and the ``if (native_console_report)`` guard made the
    absence look like a decision.
    """
    tree = _all_text()
    sources = _interaction_sources(tree) or {p: t for p, t in tree.items() if RUNNING_EVENT in t}
    assert sources, "no emitter exists to check"
    for path, text in sources.items():
        for match in re.finditer(rf"^\+\s*.*\b{PUBLISHER}\s*\)", text, re.M):
            line = match.group(0)
            assert not re.search(rf"&\s*{PUBLISHER}|if\s*\(\s*{PUBLISHER}\b", line), (
                f"{path.name}: {line.strip()!r} null-checks {PUBLISHER}; "
                "use a strong extern so a missing archive fails the link."
            )


# ---------------------------------------------------------------------------
# The teeth check -- the defect this file names, reproduced
# ---------------------------------------------------------------------------


def test_the_rule_catches_the_defect_it_names():
    """The shape that would pass the gate while proving nothing.

    This is the emitter a reasonable person writes first: the report reached the
    HID stack, so report that the user interacted.  It clears
    ``p.springboard_interaction``, it clears ``homescreen_screenshot_only``, and
    it is false.  If ``test_the_interaction_record_is_gated_on_a_screen_change``
    does not reject this text, that test has no teeth and the gate is being
    satisfied by a photograph.
    """
    defective = (
        'extern int native_console_report(const void *, size_t);\n'
        'static void report_interaction(void) {\n'
        '    static char const rec[] = "{\\"event\\":\\"springboard.interaction\\"}\\n";\n'
        '    native_console_report(rec, sizeof(rec) - 1);\n'
        '}\n'
    )
    honest = defective.replace(
        "static void report_interaction(void) {",
        "static uint64_t before_screen_digest, after_screen_digest;\n"
        "static void report_interaction(void) {\n"
        "    if (before_screen_digest == after_screen_digest) return;",
    )

    def gated(text: str) -> bool:
        if "screen_digest" not in text:
            return False
        return bool(re.search(
            r"(before|after)\w*\s*(==|!=|<=|>=|<|>)[^;\n]*(before|after)", text))

    assert not gated(defective), (
        "the defective shape passed: a record emitted on input delivery is being "
        "accepted as evidence that the screen changed"
    )
    assert gated(honest), (
        "the honest shape failed: the rule rejects an emitter that IS gated on a "
        "screen change, which would make the test wrong rather than the code"
    )
