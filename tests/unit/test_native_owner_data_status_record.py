"""The Data mount's first observable point, and the invariant that keeps it there.

Why this exists
---------------
``native_owner_data_prepare()`` is the first thing ``load_init_program()`` does
after the SystemOS mount, and until now it emitted nothing.  Every failure path
in it is a ``goto done``, and the caller's only response is
``panic("offline scratch Data mount failed: %d")`` -- whose text goes to XNU's
printf console, which on this board is neither the serial port
(``unixnu_boot.py:468-469``, measured) nor the frame buffer.  The ADT carries no
``embedded-panic-log-size``, so there is no panic region to read either.

The consequence, measured on boot 53: the guest channel delivered 17 records
ending at ``system.volume_identity`` and then nothing for the remaining 540 s of
a 900 s budget.  A Data mount failure, a hang in the launchd loop below it, and a
boot that is merely slow on TCG all produce that same silence.  ``diag.owner_data_status``
is the record that separates them, and it is the difference between "the guest
went quiet" and "the guest stopped at stage N".

What is NOT checked here, and cannot be
---------------------------------------
* that the kernel compiles.  This host has no C compiler; CI is the first thing
  that compiles this translation unit.
* that the record arrives on a real boot.  That needs a matched kernel and a
  3600 s capture.
* that the Data mount succeeds.  ``XNU_OWNER_DATA_VOLUME_UUID`` has no
  ``seed.json`` on this host at all
  (``build-macos.sh:2211``, ``:2661``), the desktop builder uses
  ``18AB5557-...`` (``build-cpu-desktop-kernel.py:7``) and the last measured seed
  carried ``18AF0FFB-...`` (``verify-data-mount.py:117-123``), which that file
  itself calls "an open item on the runner side".  Neither literal is in the
  kernel boot 53 ran.  That is a recorded risk, not a finding.

The invariant that is one edit away from silent, and just was
-------------------------------------------------------------
Adding lines to a hunk without updating its ``@@`` header produces a patch
``git apply`` rejects as **corrupt** -- and ``repro-patch-twopass.py`` said
"ALL PASSES CLEAN" anyway, because it staged patches with ``git show HEAD:`` and
was therefore testing the committed bytes rather than the edit.  The same edit
also put 28 lines into the exact context block ``zzy-native-gpu-addon-mount.patch``
needs at ``kern_exec.c:7193``, which is a *different* failure that only appears
once the first one is fixed.  Both are asserted below, on the source, in under a
second -- because both cost a full build-and-boot cycle to discover and neither
produces an error when it does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.vendor_paths import require

REPO = Path(__file__).resolve().parents[2]

PATCH = (
    REPO / "vendor" / "UniXNU" / "patches" / "xnu" / "offline-owner-systemos-mount.patch"
)

#: The defconfig that turns the Data mount on, and the console archive with it.
DEFCONFIG = REPO / "vendor" / "UniXNU-qemu" / "configs" / "qemu_root_defconfig"

#: The event, and the source it claims to come from.
EVENT = "diag.owner_data_status"
SOURCE = "XNU.native_owner_data_prepare"

#: The stage values the function assigns, and what each one means.  Kept here
#: rather than derived from the source because the mapping is the diagnostic: a
#: reader of a record needs to know what "stage 2" cost them.
STAGES = {
    0: "the seed was not found, or its geometry was rejected",
    1: "the copyout of the mount arguments failed",
    2: "mount() failed",
    3: "vnode_lookup(\"/private/var\") failed",
    4: "the mount verified",
}

#: The escape scanner.  Built from chr() so nothing can collapse it: a bash
#: heredoc turns backslash-n into a real newline, and that mangled this
#: verifier's ancestor twice.
BACKSLASH = chr(92)

HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _patch_text() -> str:
    require(PATCH)
    return PATCH.read_text(encoding="utf-8")


def _hunks(text: str) -> list[tuple[int, int, int, int, int]]:
    """Every hunk of *text* as (line, old_count, new_count, body_old, body_new).

    Takes the text rather than reading the file, so a caller can hand it a
    mutated copy.  An earlier version of the teeth checks wrote the mutation to
    the real patch and restored it in a ``finally`` -- which leaves the repo
    mutated if anything interrupts the test, and this repository has already lost
    four edits to a checkout that restored the wrong bytes.
    """
    lines = text.splitlines()
    out: list[tuple[int, int, int, int, int]] = []
    index = 0
    while index < len(lines):
        match = HEADER.match(lines[index])
        if match is None:
            index += 1
            continue
        old_count = int(match.group(2) or 1)
        new_count = int(match.group(4) or 1)
        cursor = index + 1
        seen_old = seen_new = 0
        while cursor < len(lines):
            line = lines[cursor]
            if (line.startswith("@@") or line.startswith("diff --git")
                    or line.startswith("--- ") or line.startswith("+++ ")):
                break
            if line.startswith("+"):
                seen_new += 1
            elif line.startswith("-"):
                seen_old += 1
            elif line.startswith(" "):
                seen_old += 1
                seen_new += 1
            elif line.startswith(BACKSLASH):
                pass
            elif line == "":
                seen_old += 1
                seen_new += 1
            else:
                break
            cursor += 1
        out.append((index + 1, old_count, new_count, seen_old, seen_new))
        index = cursor
    return out


# ---------------------------------------------------------------------------
# The invariant that just broke
# ---------------------------------------------------------------------------

def test_every_hunk_header_matches_the_body_it_has() -> None:
    """A hunk header that names the wrong line count makes the patch corrupt.

    ``git apply`` refuses the whole file with "corrupt patch at line N", which
    is not a message that says "your line count is wrong" -- so it reads like
    file damage rather than an edit that forgot to update a number.
    """
    bad = [h for h in _hunks(_patch_text()) if h[1] != h[3] or h[2] != h[4]]
    assert not bad, (
        "hunk headers whose counts disagree with their bodies: "
        + "; ".join(f"line {h[0]}: -{h[1]}+{h[2]} vs body -{h[3]}+{h[4]}"
                    for h in bad)
    )


def test_the_patch_applies_to_a_pristine_tree() -> None:
    """``git apply --check`` against the pin, so the counts are not the only thing.

    A patch can have every hunk header right and still not apply, because the
    context a *later* patch needs moved.  That is the second failure this edit
    caused and the one the header check cannot see.
    """
    import shutil
    import subprocess
    import tempfile

    require(PATCH)
    xnu = REPO / "local-private" / "upstream" / "xnu"
    if not (xnu / "bsd" / "kern" / "kern_exec.c").is_file():
        pytest.skip("the pristine XNU tree is not on this host")
    with tempfile.TemporaryDirectory(prefix="patch-apply-") as tmp:
        # git apply resolves the patch's a/ b/ paths, so the tree needs the
        # directory, not just the file.
        target = Path(tmp) / "bsd" / "kern"
        target.mkdir(parents=True)
        shutil.copyfile(xnu / "bsd" / "kern" / "kern_exec.c",
                        target / "kern_exec.c")
        run = subprocess.run(["git", "apply", "--check", str(PATCH)],
                             capture_output=True, text=True, cwd=tmp)
        assert run.returncode == 0, (
            f"the patch does not apply to a pristine tree: {run.stderr.strip()[:600]}"
        )


# ---------------------------------------------------------------------------
# The record's shape
# ---------------------------------------------------------------------------

def test_the_record_literal_is_well_formed_json() -> None:
    """The literal, decoded, parses -- and names the event it claims to.

    The record is built by adjacent-literal concatenation, which is how two
    earlier drafts of the mount emitter produced a record that parsed as nothing
    and compiled clean.  So the three parts are reassembled here the way the C
    preprocessor does, and the result has to be JSON with the right keys.
    """
    import json

    text = _patch_text()
    # The C source escapes its quotes, so the spelling to look for is the escaped
    # one -- and a check for the unescaped form passes vacuously against a file
    # whose literals are all escaped.
    for part in (EVENT, SOURCE, 'stage', 'error'):
        assert part in text, f"the patch does not mention {part}"

    # The three static arrays, in the order the emitter appends them.  The
    # continuation line carries the patch's own '+' prefix, which is not
    # whitespace, so the gap between "=" and the literal is [\s+]*.
    parts = re.findall(
        r'static char const data_\w+\[\] =[\s+]*"((?:[^"\\]|\\.)*)"', text)
    assert len(parts) == 3, (
        f"expected the three record parts, found {len(parts)}: {parts}"
    )
    # C string escapes -> real characters, the way the compiler would.
    decoded = "".join(
        p.replace(BACKSLASH + BACKSLASH, BACKSLASH)
         .replace(BACKSLASH + '"', '"')
         .replace(BACKSLASH + "n", chr(10))
         .replace(BACKSLASH + "t", chr(9))
        for p in parts
    )
    # The two numbers are hand-formatted at run time, so the literal leaves the
    # value empty.  A zero stands in for each: the shape being checked is the
    # keys and their order, and a real digit cannot make an invalid record valid.
    template = re.sub(r":(?=,|})", ":0", decoded)
    record = json.loads(template)
    assert record["event"] == EVENT
    assert record["source"] == SOURCE
    assert set(record) == {"event", "stage", "error", "source"}, (
        f"the record carries unexpected keys: {sorted(record)}"
    )


def test_the_record_has_no_unbounded_formatter() -> None:
    """No snprintf: this translation unit does not include its header.

    ``zzz-native-springboard-events.patch`` records the same constraint and works
    around it by hand-formatting digits.  A ``%d`` here would not compile, which
    is the loud failure -- but a ``snprintf`` call that does compile somewhere
    else would silently produce a record whose numbers disagree with the mount.
    """
    text = _patch_text()
    start = text.find("diag.owner_data_status")
    assert start > 0
    window = text[start - 200:start + 3000]
    assert "snprintf" not in window, (
        "the emitter uses snprintf, which this translation unit cannot declare"
    )
    assert "%d" not in window and "%u" not in window, (
        "the emitter uses a format conversion; the digits are hand-formatted"
    )


# ---------------------------------------------------------------------------
# The couplings that make the record TRUE
# ---------------------------------------------------------------------------

def test_the_emitter_is_inside_the_data_mount_guard() -> None:
    """A record outside its ``#if`` is compiled in a config that cannot run it."""
    lines = _patch_text().splitlines()
    emitter = next(i for i, l in enumerate(lines) if EVENT in l)
    guard = None
    for i in range(emitter, -1, -1):
        if lines[i].startswith("+#if defined(XNU_OWNER_DATA_MOUNT)"):
            guard = i
            break
    assert guard is not None, (
        f"the emitter at line {emitter + 1} has no XNU_OWNER_DATA_MOUNT guard above it"
    )


def test_every_stage_the_function_assigns_is_one_the_record_can_report() -> None:
    """A stage the emitter cannot name is a stage that reads as silence.

    ``native_owner_data_stage`` is assigned 0..4 by the paths in the function.
    The record carries it as a decimal, so any value outside that range means a
    path was added without this mapping being updated.
    """
    text = _patch_text()
    assigned = {int(m) for m in re.findall(
        r"native_owner_data_stage\s*=\s*(\d+)", text)}
    assert assigned, "the function assigns no stage at all"
    assert assigned <= set(STAGES), (
        f"stages {sorted(assigned - set(STAGES))} are assigned but not documented"
    )
    assert 4 in assigned, "the success stage is never assigned"


def test_the_defconfig_turns_the_guard_on() -> None:
    """The guard is real only if the booting kernel sets the macro.

    ``CONFIG_XNU_OWNER_DATA_MOUNT=n`` would compile the emitter out and the
    record would never arrive -- a green build with a dead instrument.
    """
    require(DEFCONFIG)
    text = DEFCONFIG.read_text(encoding="utf-8")
    assert re.search(r"^CONFIG_XNU_OWNER_DATA_MOUNT=y$", text, re.M), (
        "qemu_root_defconfig does not set CONFIG_XNU_OWNER_DATA_MOUNT=y, so the "
        "emitter is compiled out of the kernel the guest boots"
    )


def test_the_buffer_can_hold_the_worst_case_record() -> None:
    """160 bytes against the longest record the emitter can build.

    41 prefix + 10 stage digits + 9 middle + 10 error digits + 40 suffix + NUL is
    111, so the buffer is under-filled even at the widest, and every append is
    bounded by ``size - 1`` regardless.
    """
    text = _patch_text()
    match = re.search(r"char record\[(\d+)\]", text)
    assert match, "the emitter declares no record buffer"
    size = int(match.group(1))
    worst = (len('{"event":"' + EVENT + '","stage":') + 10
             + len(',"error":') + 10
             + len(',"source":"' + SOURCE + '"}' + chr(10)) + 1)
    assert size >= worst, f"a {size}-byte buffer cannot hold a {worst}-byte record"


# ---------------------------------------------------------------------------
# Teeth: the checks above must go red on a broken emitter
# ---------------------------------------------------------------------------

def test_the_hunk_check_goes_red_on_a_broken_header() -> None:
    """Mutate a COPY, and the count check must fail.

    A guard that passes on its own defect is worse than no guard.  This is the
    mutation that actually happened: 332 left in the header, 417 in the body.
    """
    text = _patch_text()
    # Derive the anchor rather than hardcode it: the count changes every time the
    # emitter is touched, and a teeth check whose anchor has silently moved is a
    # teeth check that passes because it mutated nothing.
    header = re.search(r'^@@ -(\d+),(\d+) \+(\d+),(\d+) @@$', text, re.M)
    assert header, 'the patch has no hunk header to mutate'
    good = header.group(0)
    broken = text.replace(good, f'@@ -{header.group(1)},{header.group(2)} '
                                f'+{header.group(3)},332 @@', 1)
    assert broken != text, f'the mutation did not apply; the anchor {good!r} moved'
    bad = [h for h in _hunks(broken) if h[1] != h[3] or h[2] != h[4]]
    assert bad, (
        "the hunk-count check passed on a header that disagrees with its body; "
        "it has no teeth"
    )


def test_the_guard_check_goes_red_when_the_guard_is_removed() -> None:
    """Delete the guard above the emitter, and the guard check must fail."""
    lines = _patch_text().splitlines()
    emitter = next(i for i, l in enumerate(lines) if EVENT in l)
    guard = next(i for i in range(emitter, -1, -1)
                 if lines[i].startswith("+#if defined(XNU_OWNER_DATA_MOUNT)"))
    del lines[guard]
    unguarded = chr(10).join(lines)

    with pytest.raises(StopIteration):
        _guard_index(unguarded)
    # And the guard is still there in the real file, so the check is not passing
    # for the wrong reason.
    assert _guard_index(_patch_text()) < emitter


def _guard_index(text: str) -> int:
    lines = text.splitlines()
    emitter = next(i for i, l in enumerate(lines) if EVENT in l)
    return next(i for i in range(emitter, -1, -1)
                if lines[i].startswith("+#if defined(XNU_OWNER_DATA_MOUNT)"))
