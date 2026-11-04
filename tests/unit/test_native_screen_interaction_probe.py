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
  therefore carries BOTH digests rather than a boolean, so a reader can see
  exactly which two states were compared instead of trusting a claim.  There is
  deliberately no changed-byte count: computing one would mean holding a second
  copy of a ~24 MB frame, and a number that could not be produced would be worse
  than a number that is not there.
* that ``springboard.running`` means SpringBoard is usable.  It means the exec
  of a process named SpringBoard succeeded.  The site is ``__mac_execve()``'s
  ``if (!error)`` block -- the same site ``zzz-m8-launchd-daemon-events.patch``
  uses for ``daemon.spawned``, where ``p_comm`` has just been written by
  ``set_proc_name()``.

Where the two halves live, and why there and not in the drivers
---------------------------------------------------------------
The emitter is in ``vendor/UniXNU-qemu/package/HidInputProbe.cpp`` -- the DEVICE
repository, not the UniXNU fork.  Three reasons, all measured:

* that file already owns ``native_hid_input_observed()``, the hook every
  delivered report passes through (``NativeHidInput.cpp:190,206,236,240,246``),
  so arming needs no new driver code;
* it already includes ``<pexpert/pexpert.h>``, which ``PE_current_console()``
  needs, and the presenter exports ``native_gop_ready`` / ``native_gop_virtual``
  / ``native_gop_physical`` as ``extern "C"`` globals -- so the digest reads a
  verified scanout without touching ``NativeMobileDisplay`` at all;
* ``NativeHidInput`` and ``NativeMobileDisplay`` are SEPARATE pinned submodules
  (``28f9b463b48e`` and ``eb9c94916482``), so putting the emitter in either would
  have bumped two more pins for no gain.

The linkage is safe by configuration rather than by luck, and that is asserted in
the source rather than assumed: the only defconfig that sets
``CONFIG_XNU_NATIVE_LEGACY_DISPLAY`` -- whose archive force-loads this object --
is ``qemu_root_defconfig``, and that same config sets
``CONFIG_XNU_NATIVE_VIRTIO_CONSOLE``, which is what puts the console archive on
the kernel link.  ``qemu_defconfig`` sets NEITHER.

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

import json
import re
from pathlib import Path

import pytest

from tests.source_text import strip_comments

REPO = Path(__file__).resolve().parents[2]
VENDOR = REPO / "vendor" / "UniXNU"
DRIVERS = VENDOR / "drivers"
PATCHES = VENDOR / "patches"
# The device repository.  `board.toml:11` -- `kit = "qemu"  # package/ of this
# repository appears as kits/qemu in the build tree` -- so package/ is the source
# of truth the build tree stages, and it is where the interaction emitter lives:
# HidInputProbe.cpp already owns native_hid_input_observed(), the hook every
# delivered report passes through, and already includes <pexpert/pexpert.h>, which
# is what PE_current_console() needs.  Scanning only vendor/UniXNU would report
# "no emitter exists" against a file that has one -- the same class of mistake as
# the escaped-literal one below.
DEVICE = REPO / "vendor" / "UniXNU-qemu" / "package"

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

# A C string literal's body.  Raw, so the backslashes survive the trip through
# this file -- and so that a future edit does not quietly change what matches.
_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')

# The only escapes the kernel record vocabulary uses.  Anything else in a record
# is a mistake loud enough to fail here rather than silently emit different bytes.
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}


def _decode(body: str) -> str:
    """Decode one C string literal body into the bytes the compiler will emit."""
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] != "\\":
            out.append(body[i])
            i += 1
            continue
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if nxt not in _ESCAPES:
            raise AssertionError(
                f"unsupported escape \\{nxt!r} in a record literal; the vocabulary "
                "does not need it and guessing produces a different record")
        out.append(_ESCAPES[nxt])
        i += 2
    return "".join(out)


def _added(patch: Path) -> list[str]:
    return [line[1:] for line in patch.read_text(encoding="utf-8").splitlines()
            if line.startswith("+") and not line.startswith("+++")]


def _vendor_tree() -> list[Path]:
    if not DRIVERS.is_dir():
        pytest.skip(f"{DRIVERS} is not present; the pinned vendor tree is not acquired")
    found = [p for p in DRIVERS.rglob("*.cpp") if "Original" not in p.name]
    found += [p for p in DRIVERS.rglob("*.h") if "Original" not in p.name]
    if PATCHES.is_dir():
        found += sorted(PATCHES.rglob("*.patch"))
    if DEVICE.is_dir():
        found += sorted(DEVICE.rglob("*.cpp"))
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


def _strip_comments(text: str) -> str:
    """Delegate to :func:`tests.source_text.strip_comments`.

    The scan that uses this must read code, not prose -- a call site or a
    record inside a comment is neither.  Four copies of the stripper were
    written independently in this repository; this is the one
    implementation, and the call sites here delegate to it.
    """
    return strip_comments(text)
def _all_text() -> dict[Path, str]:
    """Every source file's text, with each C source's literals decoded.

    THE DECODING IS LOAD-BEARING.  A source carries the C it will produce, so its
    string literals contain ``\\"event\\":\\"springboard.running\\"`` -- with the
    backslashes.  A raw search for ``"event":"springboard.running"`` therefore
    never matches, and the first revision of this test reported "no emitter
    exists" against a patch that had one.  So a file contributes its raw text AND
    the decoded concatenation of its string literals, which is what the
    preprocessor will actually hand the port.
    """
    out: dict[Path, str] = {}
    for path in _vendor_tree():
        raw = path.read_text(encoding="utf-8")
        if path.suffix in (".patch", ".cpp", ".c", ".h", ".mm"):
            bodies = _LITERAL.findall(_strip_comments(raw))
            decoded: list[str] = []
            for body in bodies:
                try:
                    decoded.append(_decode(body))
                except AssertionError:
                    pass          # a literal with an odd escape is not a record
            out[path] = raw + "\n" + "\n".join(decoded)
        else:
            out[path] = raw
    return out


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

    ``NativeGOPPresenter::start()`` (NativeGOPPresenter.cpp:82-98) maps the scanout
    writable and validates the mapping against ``PE_current_console()`` --
    ``v_baseAddr``, ``v_width``, ``v_height``, ``v_rowBytes`` -- then publishes
    ``native_gop_ready`` and ``native_gop_virtual`` as ``extern "C"`` globals
    (:14, :96-97).  So the digest does not need to reach into the driver's private
    ``IOMemoryMap``; it needs to read those exported values and prove they still
    describe the screen it is about to hash.
    """
    tree = _all_text()
    digests = _digest_sources(tree)
    assert digests, "no screen digest function exists"
    for path, text in digests.items():
        assert "native_gop_virtual" in text, (
            f"{path.name} claims a screen digest but never reads the presenter's "
            "exported scanout address"
        )
        assert "PE_current_console" in text, (
            f"{path.name} hashes an address without cross-checking it against "
            "PE_current_console(), so nothing proves the bytes are the screen"
        )


def test_the_input_hook_and_the_digest_are_in_the_file_that_owns_them():
    """The emit must live where the input hook is, and the digest must read the scanout.

    THE ARCHITECTURE CHANGED WHILE THIS TEST WAS BEING WRITTEN, and the first
    version named the wrong file.  It demanded that `NativeHidInput.cpp` emit the
    record, on the theory that the input driver is where input is delivered.  That
    is true of the delivery, but the emitter belongs one level up:
    `HidInputProbe.cpp` already owns `native_hid_input_observed()` -- the hook
    every delivered report passes through, called from `touchSend()` (:188),
    `buttonsAction()` (:204) and `tick()` (:244) -- and it already includes
    `<pexpert/pexpert.h>`, which `PE_current_console()` needs.  Putting the digest
    in `NativeHidInput` would have meant bumping two more submodule pins for no
    gain, and reaching into a mapping that driver does not own.

    What is actually asserted is the coupling, not the filename: the file that
    receives the input must be the file that emits, and the digest must read the
    presenter's verified scanout.  If either half moves on its own the record
    cannot arrive, and it would be absent -- which reads exactly like a guest that
    never moved.
    """
    tree = _all_text()
    by_name = {p.name: p for p in tree if p.suffix == ".cpp"}
    probe = by_name.get("HidInputProbe.cpp")
    assert probe is not None, (
        "HidInputProbe.cpp is not in the pinned device tree; it owns "
        "native_hid_input_observed(), the hook every delivered report passes through"
    )
    text = tree[probe]
    assert INTERACTION_EVENT in text, (
        f"{probe.name} receives every input report but does not emit {INTERACTION_EVENT}"
    )
    # The digest must read the presenter's mapping AND prove it is the one the
    # presenter validated.  Both halves: a digest over an address nobody checked
    # is a digest of whatever happens to be there.
    assert "native_gop_virtual" in text, (
        f"{probe.name} does not read the presenter's exported scanout address"
    )
    assert "PE_current_console" in text, (
        f"{probe.name} does not cross-check the scanout against PE_current_console()"
    )
    assert "native_gop_physical" in text and "v_baseAddr" in text, (
        f"{probe.name} does not prove the mapping it reads is the one the presenter "
        "validated (native_gop_physical == video.v_baseAddr)"
    )
    # The input hook must actually be armed by a delivery, not by probe lifecycle.
    assert re.search(r"event==4\|\|event==5\|\|event==6", text), (
        f"{probe.name} does not arm on a delivered report (events 4/5/6); arming on "
        "lifecycle events would make the record about the driver's own bookkeeping"
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


# ---------------------------------------------------------------------------
# The record must parse.  Two drafts of the mount emitter did not.
# ---------------------------------------------------------------------------


def _literals(patch: Path) -> list[str]:
    return [_decode(body) for body in _LITERAL.findall("\n".join(_added(patch)))]


def _assemble(patch: Path, event: str) -> tuple[str, str]:
    """The two literals the pid is spliced between, as the preprocessor joins them."""
    lits = _literals(patch)
    prefix = next((t for t in lits if event in t), None)
    tail = next((t for t in lits if t.endswith("}\n")), None)
    assert prefix is not None, (
        f"{patch.name}: no added literal contains {event!r}; the literals present "
        f"are {[t[:40] for t in lits]}"
    )
    assert tail is not None, f"{patch.name}: no added literal ends with a newline-terminated object"
    return prefix, tail


SPRINGBOARD_PATCH = PATCHES / "xnu" / "zzz-native-springboard-events.patch"


@pytest.mark.parametrize("pid", [0, 1, 42, 9999999])
def test_the_springboard_running_record_assembles_to_json(pid):
    """Two drafts of the mount emitter carried ``system.mount``.  Both compiled clean.

    Adjacent-string-literal concatenation is invisible to the compiler, so a
    malformed record costs a build-and-boot cycle to discover and produces no
    error when it does -- just a missing event, which reads exactly like a guest
    that never did the thing.  So the record is reassembled here, the way the
    preprocessor will, and parsed.  pid=0 is in the list on purpose: it is the one
    value that takes the ``if (!pid) text[digits++] = '0'`` branch.
    """
    if not SPRINGBOARD_PATCH.is_file():
        pytest.skip(f"{SPRINGBOARD_PATCH.name} is not in the pinned vendor tree")
    prefix, tail = _assemble(SPRINGBOARD_PATCH, "springboard.running")
    record = prefix + str(pid) + tail
    obj = json.loads(record)          # raises with the record in the message
    assert obj["event"] == "springboard.running"
    assert obj["comm"] == "SpringBoard"
    assert obj["pid"] == pid
    assert obj["source"] == "XNU.__mac_execve"


def test_the_record_defect_this_names_is_caught():
    """The teeth: a literal that parses as nothing must not pass."""
    defective = '{"event":"springboard.running" system.mount }\n'
    with pytest.raises(Exception):
        json.loads(defective)
    assert json.loads('{"event":"springboard.running"}\n')["event"] == "springboard.running", (
        "the honest shape failed: the JSON check rejects a record that IS well formed"
    )
