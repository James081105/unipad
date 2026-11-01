"""Every patch in the pinned tree is well-formed and applies.

Why this exists
---------------
A unified diff's hunk header carries the line counts of both sides.  Adding
lines to the ``+`` side without updating the count produces a file that reads
perfectly well as text and that ``git apply`` refuses with "corrupt patch".

That is not hypothetical.  On 2026-10-30 an edit to
``vendor/UniXNU/patches/xnu/zzy-native-gpu-addon-mount.patch`` grew one comment
by thirteen lines and left the header at its old count.  Nothing locally
noticed: the patch is not applied on Windows, the file parses, and the text
reads exactly as intended.  The failure surfaced on the runner at
``build-xnu-4k-baseline.sh:44``'s ``git apply --check`` -- after the acquisition
step, after the Mesa bootstrap, and after the entire owner root chain had run.
One runner cycle, spent on a check that takes milliseconds here.

So the check lives here.  Two levels, because they catch different things:

* :func:`scripts.shared.check_patch_hunks` compares every hunk's declared counts
  against what it actually carries.  Pure Python, no git, no target files.  This
  is the one that would have caught the 2026-10-30 defect.
* :func:`scripts.shared.verify_patch_applies` reconstructs the ``-`` side of
  every hunk into a throwaway git repository and runs the real
  ``git apply --check``.  Slower, and it needs git, but it is the authority.

Both are skipped when the pinned vendor checkout is absent, which is the normal
state on a host that has not acquired it.

What is NOT skipped, and why
---------------------------
The pinned tree carries four hunks whose new-side start is unreachable -- the
two sides of the header disagree about where the hunk is.  ``git apply``
tolerates that by searching for the context, which is why those four patches
still build.  They are recorded here by name rather than ignored, and the test
fails if the set changes: a *new* unreachable hunk is the defect this check
exists for, and a silently tolerated one is how the next runner cycle gets
spent.

Correcting those four is deliberately not done here.  Fixing one shifts the
next -- they are a systematic offset, not four independent typos -- so the fix
is a cascade through three unrelated patches in a pinned tree, which is exactly
the scope creep that breaks a build that works.  That is a change to propose on
its own, with its own runner cycle, not one to fold into an unrelated fix.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHARED = REPO / "scripts" / "shared"
PATCH_DIR = REPO / "vendor" / "UniXNU" / "patches"
#: The second patch directory the build applies, and the one this test's first
#: revision missed.  ``build-xnu-4k-baseline.sh:44`` walks
#: ``patches/xnu/*.patch`` AND ``kits/NativePrelink/patches/*.patch``; walking
#: only the first is the same shape of blind spot as a checker that reports a
#: problem in patches nobody touched -- coverage that looks total and is not.
PRELINK_DIR = REPO / "vendor" / "UniXNU" / "kits" / "NativePrelink" / "patches"

#: (patch name, hunk line) for every unreachable hunk start in the pinned tree,
#: measured 2026-10-30.  A change to this set is a test failure, in either
#: direction: a new entry is the defect, and a removed one means somebody
#: corrected a patch without recording that it had been wrong.
KNOWN_UNREACHABLE = {
    ("offline-component-load-probes.patch", 92),
        # The hunk in offline-owner-systemos-mount.patch is the SAME one that was
    # already unreachable before 2026-11-01; only its line number moved.  That
    # session added 38 lines to the hunk above it (the system.mount reporter),
    # which shifted this header from line 270 to 308; a later diagnostic insert
    # added 21 more, moving it to 329.  Its old-side start (7086)
    # is unchanged, so this is a renumbering and not a new defect.
    #
    # It is the ONLY entry for this patch, and that is the checker's design
    # rather than a sign the others are clean: check_patch_hunks compares each
    # hunk against the PREVIOUS one, so once this header declares its position
    # the four after it are consistent with it and pass.  They are wrong by the
    # same 5-line systematic offset against hunk 2 -- the offset described in the
    # module docstring -- and that offset is deliberately left alone here.
    ("offline-owner-systemos-mount.patch", 329),
    ("zzz-boot-progress.patch", 223),
    ("zzz-boot-progress.patch", 264),
}


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_patch_hunks", SHARED / "check_patch_hunks.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _patches() -> list[Path]:
    if not PATCH_DIR.is_dir():
        pytest.skip(f"{PATCH_DIR} is not present; the pinned vendor tree is not acquired")
    found = sorted(PATCH_DIR.rglob("*.patch"))
    if PRELINK_DIR.is_dir():
        found += sorted(PRELINK_DIR.glob("*.patch"))
    assert found, f"no .patch file anywhere under {PATCH_DIR}"
    return found


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SHARED / script), *args],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )


def test_the_native_prelink_patches_are_in_the_set_at_all():
    """Coverage that looks total and is not.

    This test's first revision walked ``patches/xnu/*.patch`` and called that
    every patch.  ``build-xnu-4k-baseline.sh:44`` walks that AND
    ``kits/NativePrelink/patches/*.patch``, so two patches were unchecked --
    and they are the two that do not apply to a Windows checkout, because the
    submodule carries no ``.gitattributes`` and ``core.autocrlf=true`` rewrites
    them to CRLF while ``refer/xnu``'s sources stay LF.

    That is the same shape as the defect this file exists for: a check that
    reports a problem in patches nobody touched is bad, but a check that never
    looks at two of them is worse, because it looks like complete coverage.  The
    count is asserted so that adding a third patch directory without adding it
    here fails rather than silently narrowing.
    """
    patches = _patches()
    names = {p.name for p in patches}
    assert "xnu-10063.141.1-sparse-index.patch" in names, (
        "the NativePrelink patches are not being walked; the count below would "
        "still pass with them missing"
    )
    assert "xnu-10063.141.1-security-init.patch" in names
    assert len(patches) >= 55, (
        f"only {len(patches)} patches are walked; the pinned tree carries 53 in "
        "patches/xnu and 2 in kits/NativePrelink/patches, and the build applies "
        "all of them"
    )


def test_no_hunks_counts_disagree_with_its_header():
    """The cheap check, and the one that catches a header left behind by an edit.

    This is the defect that cost run 40971549320: an edit grew a comment by
    thirteen lines and the hunk header kept counting 321.
    """
    patches = _patches()
    checker = _load_checker()
    counts = []
    for patch in patches:
        for problem in checker.check(patch):
            if "declares" in problem:
                counts.append(problem)
    assert not counts, (
        f"{len(counts)} hunk(s) whose declared counts disagree with what they "
        f"carry:\n" + "\n".join(counts)
    )


def test_the_unreachable_hunk_starts_are_exactly_the_known_set():
    """A new unreachable start fails; a corrected one fails too, and should not.

    See the module docstring for why the four are recorded rather than fixed.
    """
    patches = _patches()
    checker = _load_checker()
    found = set()
    for patch in patches:
        for problem in checker.check(patch):
            if "unreachable" not in problem:
                continue
            line = int(problem.split("hunk at line ")[1].split(" ")[0])
            found.add((patch.name, line))
    assert found == KNOWN_UNREACHABLE, (
        "the set of unreachable hunk starts changed. A new entry means a patch "
        "was edited without moving the following hunks' new-side starts -- which "
        "is what failed run 40981242194 at build-xnu-4k-baseline.sh:44. A "
        "removed entry means a patch was corrected; record that it had been "
        "wrong by removing it here deliberately.\n"
        f"  now      : {sorted(found)}\n"
        f"  recorded : {sorted(KNOWN_UNREACHABLE)}"
    )


def test_every_patch_applies_to_its_own_minus_side():
    """``git apply`` itself, against a synthetic target built from the hunks."""
    patches = _patches()
    broken = []
    for patch in patches:
        proc = _run("verify_patch_applies.py", str(patch))
        if proc.returncode != 0 or "exit: 0" not in proc.stdout:
            broken.append(f"{patch.relative_to(REPO)}:\n{proc.stdout}{proc.stderr}")
    assert not broken, (
        f"{len(broken)} of {len(patches)} patches do not apply:\n" + "\n".join(broken)
    )


def test_the_ngpu_directory_fix_is_in_the_patch_that_carries_it():
    """The specific edit, read back out of the patch that has to carry it.

    ``native_gpu_addon_parents()`` used to start its scan at
    ``sizeof("/private/var/ngpu/") - 1``, which is the index AFTER the trailing
    slash, so its first mkdir was ``/private/var/ngpu/addons`` -- and a mkdir
    below a parent that does not exist returns ENOENT.  Measured on 2026-10-30:
    all ten addon files failed with ``native_gpu_addon_error = 2``.

    Asserted structurally rather than by digest, because the point is that the
    loop's *start index* changed and not that some bytes moved.
    """
    patches = _patches()
    patch = next(
        (p for p in patches if p.name == "zzy-native-gpu-addon-mount.patch"), None,
    )
    if patch is None:
        pytest.skip("the GPU addon mount patch is not in this pinned tree")
    text = patch.read_text(encoding="utf-8")
    assert 'sizeof("/private/var/ngpu") - 1' in text, (
        "native_gpu_addon_parents() no longer starts its scan at the slash that "
        "TERMINATES /private/var/ngpu; its first mkdir is back to being "
        "/private/var/ngpu/addons, which fails ENOENT because that directory "
        "is in neither the OS image's /private/var nor the Data seed"
    )
    # Only the CODE lines, never the prose.  In a unified diff every added line
    # starts with '+', so a comment explaining the bug is a '+ line too -- and
    # "the old string is not in the file" is then satisfied by the explanation of
    # the bug and would pass straight over a regression.  That is the fourth time
    # this shape has bitten this project, so the discriminator is explicit: strip
    # the '+', strip whitespace, and drop anything whose first character makes it
    # a comment ('*' or '/').
    def code_lines(text: str) -> list[str]:
        out = []
        for line in text.splitlines():
            if not line.startswith("+"):
                continue
            body = line[1:].lstrip()
            if body[:1] in ("*", "/"):
                continue
            out.append(body)
        return out

    body = code_lines(text)
    assert not any('sizeof("/private/var/ngpu/") - 1' in line for line in body), (
        "the old start index is still in a code line: it creates the directories "
        "BELOW /private/var/ngpu and never /private/var/ngpu itself"
    )
    assert any('sizeof("/private/var/ngpu") - 1' in line for line in body), (
        "the new start index is not in any code line"
    )


# ---------------------------------------------------------------------------
# The offline owner cache mount reports the two relations it establishes
# ---------------------------------------------------------------------------

#: What the macros in the patch expand to, with the values the build supplies.
#: The UUID is the patch's own #ifndef default and, independently, the vol-uuid
#: the guest's APFS printed for disk4s1 in the 2026-11-01 ring capture.
MOUNT_REPORT_UUID = "8846FE78-2910-4297-AF97-D59BB6F8FC16"

#: The events the acceptance gate looks for (milestones.py:1505 and :1508).
MOUNT_REPORT_EVENTS = ("system.mount", "system.volume_identity")


def _mount_patch() -> Path | None:
    for patch in _patches():
        if patch.name == "offline-owner-systemos-mount.patch":
            return patch
    return None


def _code_lines(text: str) -> list[str]:
    """Added lines that are code, not prose.

    The same discriminator the ngpu test above uses, and for the same reason:
    in a unified diff every added line starts with '+', so a comment explaining
    the emitter is a '+ line too, and "the record is emitted" would be satisfied
    by the explanation of the record.
    """
    out = []
    for line in text.splitlines():
        if not line.startswith("+"):
            continue
        body = line[1:].lstrip()
        if body[:1] in ("*", "/"):
            continue
        out.append(body)
    return out


def _macro_value(text: str, macro: str) -> str:
    """The CONTENT of the `#define MACRO "..."` this build selects.

    Read from the patch rather than hardcoded here, and that distinction is the
    whole point: an earlier revision of this test substituted its own UUID, so
    changing the patch's `#define` to a different volume left every assertion
    green.  The mutation run found it (`uuid` survived), and the fix is to let
    the patch say what it says and then assert that against the measurement.

    Only for macros defined ONCE.  XNU_OWNER_CACHE_MOUNTPOINT is defined twice
    -- once per preprocessor arm -- and which arm applies is decided by the
    build's config, not by the file's order; use :func:`_expected_mountpoint`
    for that one.
    """
    import re

    hits = re.findall(
        r"^\+#define\s+" + re.escape(macro) + r'\s+"([^"]*)"', text, re.M)
    assert hits, f"{macro} is not #defined in the patch"
    assert len(hits) == 1, (
        f"{macro} is #defined {len(hits)} times; a macro with more than one arm "
        "must be resolved by the config that selects it, not by taking one"
    )
    return hits[0]


#: The defconfig whose kernel the guest actually boots.
#:
#: Not a guess, and this is the part the first revision of this test got wrong.
#: build-macos.sh:2590-2596 builds `qemu_defconfig` on the first pass and
#: `qemu_root_defconfig` on the second (the one with the owner root chain
#: present), and build-macos.sh:2930-2940 says in those words that the SECOND
#: pass's kernel is the one the ESP carries: "a second pass that does not
#: forward them rebuilds the same dead ESP with a kernel that can finally mount
#: a root volume".  The boot-inputs bundle is collected after the second pass,
#: so this config -- not the first pass's, and not a build script default --
#: decides which arm of every XNU_OWNER_* switch the running guest sees.
#:
#: The prior revision asserted the `#else` arm because
#: build-xnu-4k-baseline.sh:438 defaults XNU_OWNER_UNION_SYSTEMOS to 0.  That
#: default never applies on this board: qemu_root_defconfig:36 sets
#: CONFIG_XNU_OWNER_UNION_SYSTEMOS=y, so the union arm is the live one and the
#: mount point is "/", not "/private/preboot".
BOOTING_KERNEL_DEFCONFIG = (
    REPO / "vendor" / "UniXNU-qemu" / "configs" / "qemu_root_defconfig"
)


def _union_systemos_enabled() -> bool | None:
    """CONFIG_XNU_OWNER_UNION_SYSTEMOS as the booting kernel's defconfig sets it.

    None when the defconfig is ABSENT -- a host that has not acquired the device
    repository cannot know which arm the build selects, and a skip is the honest
    answer there.

    A defconfig that is present but silent about the switch is a different
    thing, and it is not tolerated: that config's own header names the union
    mount as part of what it is, so a config that stops saying so has lost the
    one fact these assertions depend on.  Failing is louder than silently
    falling back to a default nobody chose.
    """
    if not BOOTING_KERNEL_DEFCONFIG.is_file():
        return None
    found = None
    for line in BOOTING_KERNEL_DEFCONFIG.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("CONFIG_XNU_OWNER_UNION_SYSTEMOS="):
            found = stripped.split("=", 1)[1].strip().lower() == "y"
    if found is None:
        raise AssertionError(
            f"{BOOTING_KERNEL_DEFCONFIG.name} does not set "
            "CONFIG_XNU_OWNER_UNION_SYSTEMOS, so which arm of "
            "XNU_OWNER_CACHE_MOUNTPOINT the booting kernel selects is not "
            "recorded anywhere this test can read"
        )
    return found


def _mountpoint_arms(text: str) -> tuple[str, str]:
    """``(union arm, else arm)`` of XNU_OWNER_CACHE_MOUNTPOINT, by structure.

    The patch carries the pair as

        #if defined(XNU_OWNER_UNION_SYSTEMOS) && XNU_OWNER_UNION_SYSTEMOS
        #define XNU_OWNER_CACHE_MOUNTPOINT "/"
        #else
        #define XNU_OWNER_CACHE_MOUNTPOINT "/private/preboot"
        #endif

    so the arm is decided by which side of the `#else` a `#define` sits on, and
    reading them by position is what makes a third `#define` or a deleted
    `#else` a failure here rather than a silent change of mount point.
    """
    import re

    guard = re.search(
        r"^\+#if\s+defined\(XNU_OWNER_UNION_SYSTEMOS\)\s*&&\s*"
        r"XNU_OWNER_UNION_SYSTEMOS\s*$", text, re.M)
    assert guard is not None, (
        "the patch no longer guards XNU_OWNER_CACHE_MOUNTPOINT on "
        "XNU_OWNER_UNION_SYSTEMOS; the arms cannot be told apart"
    )
    else_at = text.find("\n+#else\n", guard.end())
    assert else_at != -1, "the mountpoint's #if has no #else arm"
    endif_at = text.find("\n+#endif\n", else_at)
    assert endif_at != -1, "the mountpoint's #else has no #endif"

    def _arm(window: str) -> str:
        hits = re.findall(
            r"^\+#define\s+XNU_OWNER_CACHE_MOUNTPOINT\s+\"([^\"]*)\"", window, re.M)
        assert len(hits) == 1, (
            f"expected exactly one XNU_OWNER_CACHE_MOUNTPOINT #define in one arm, "
            f"found {len(hits)}"
        )
        return hits[0]

    return _arm(text[guard.end():else_at]), _arm(text[else_at:endif_at])


def _expected_mountpoint(text: str) -> str:
    """The mount point the booting kernel's config selects."""
    union, other = _mountpoint_arms(text)
    enabled = _union_systemos_enabled()
    if enabled is None:
        pytest.skip(
            "the booting kernel's defconfig is not in this checkout, so which "
            "arm of XNU_OWNER_CACHE_MOUNTPOINT the build selects is unknown"
        )
    return union if enabled else other


def _record_payload(text: str, name: str) -> dict:
    """Expand one record's adjacent literals and parse the result as JSON.

    The macros are expanded while they are still bare tokens BETWEEN the
    literals; each expands to a string literal of its own.  Joining the literal
    contents afterwards is exactly what C's concatenation does.

    The mountpoint is expanded with the arm the booting kernel's defconfig
    selects, not with whichever `#define` the file happens to carry last.  A
    record naming the wrong mount point is a false statement about where the
    mount is, and it is invisible on the host: the record parses, the build is
    green, and nothing but this comparison would notice.
    """
    import json
    import re

    hit = re.search(r"static char const " + name + r"\[\] =\s*(.*?);", text, re.S)
    assert hit is not None, f"{name} is not defined in the patch"
    body = hit.group(1)
    body = body.replace(
        "XNU_OWNER_CACHE_MOUNTPOINT", '"' + _expected_mountpoint(text) + '"')
    body = body.replace(
        "XNU_OWNER_CACHE_VOLUME_UUID",
        '"' + _macro_value(text, "XNU_OWNER_CACHE_VOLUME_UUID") + '"')
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
    assert literals, f"{name} holds no string literal"
    joined = "".join(literals)
    # The record must END a line: the host's _guest_records splits the guest log
    # by line and parses each one, so an unterminated record runs into the next.
    # Asserted on the ESCAPED form, because that is what the C source carries at
    # this point -- the backslash-n is still two characters until it is decoded
    # on the next line, and requiring a real newline here is requiring something
    # that is only true one line later.
    assert joined.endswith("\\n"), f"{name} does not end with a newline escape"
    decoded = joined.replace('\\"', '"').replace("\\n", "\n")
    assert decoded.endswith("\n"), f"{name} does not decode to a trailing newline"
    return json.loads(decoded)


def test_the_mount_patch_reports_both_events_the_gate_asks_for():
    """The two records parse as JSON and name the events M7 requires.

    Asserted on the PARSED record, not on the presence of the event name: two
    drafts of this emitter carried the literal "system.mount" and produced a
    record that parses as nothing.
    """
    patch = _mount_patch()
    if patch is None:
        pytest.skip("the offline owner cache mount patch is not in this pinned tree")
    text = patch.read_text(encoding="utf-8")
    # The patch's own #define has to be the volume the guest actually mounted.
    # Read out of the patch, not restated here, so that changing the #define
    # breaks this assertion instead of quietly agreeing with a stale copy.
    assert _macro_value(text, "XNU_OWNER_CACHE_VOLUME_UUID") == MOUNT_REPORT_UUID, (
        "the patch's XNU_OWNER_CACHE_VOLUME_UUID is not the vol-uuid APFS printed "
        f"for disk4s1 in the 2026-11-01 ring capture ({MOUNT_REPORT_UUID}); the "
        "record would then name a volume this board does not have"
    )
    for name, event in (
        ("mount_record", "system.mount"),
        ("identity_record", "system.volume_identity"),
    ):
        payload = _record_payload(text, name)
        assert payload["event"] == event, (
            f"{name} names {payload['event']!r}, not {event!r}; the probe for it "
            "reports the event missing, which reads as the guest never mounting"
        )
        assert payload["volume_uuid"] == MOUNT_REPORT_UUID, (
            f"{name} carries volume_uuid {payload.get('volume_uuid')!r}, not the "
            f"vol-uuid APFS printed for disk4s1 ({MOUNT_REPORT_UUID})"
        )


def test_the_mount_record_asserts_the_mount_it_is_reporting():
    """system.mount states a completed mount, at the mount point really used.

    A record that says "mounted" while the function is about to return an error
    is a false statement in the evidence the gate reads.
    """
    patch = _mount_patch()
    if patch is None:
        pytest.skip("the offline owner cache mount patch is not in this pinned tree")
    payload = _record_payload(patch.read_text(encoding="utf-8"), "mount_record")
    assert payload["mounted"] is True
    assert payload["readonly"] is True, (
        "the mount is MNT_RDONLY and cache_authorized requires readonly, so a "
        "record that does not say so is describing a different mount"
    )
    assert payload["mountpoint"] == _expected_mountpoint(
        patch.read_text(encoding="utf-8")), (
        "the record must name the mount point XNU_OWNER_CACHE_MOUNTPOINT selects "
        "for the kernel that actually boots"
    )


def test_the_mountpoints_two_arms_name_two_different_mount_points():
    """Each arm names the mount point its own branch actually uses.

    The arms are not interchangeable labels.  The union branch overlays the
    original OS volume, and qemu_root_defconfig's own header says so: "the
    original iPadOS OS volume mounted as the root, SystemOS as a union over
    /".  The non-union branch mounts SystemOS on its own at /private/preboot,
    which is where the DYLD_SHARED_CACHE_DIR environment string points when
    that branch is live.

    Without this the two `#define`s are two strings in a known order, and
    swapping them changes nothing any other assertion can see: the record
    reads the macro, the mount reads the macro, and the two agree whichever
    way round they are.  What breaks is the mount itself, on a board nobody
    re-measures until it fails to reach userspace.
    """
    patch = _mount_patch()
    if patch is None:
        pytest.skip("the offline owner cache mount patch is not in this pinned tree")
    union, other = _mountpoint_arms(patch.read_text(encoding="utf-8"))
    assert union == "/", (
        "the union arm must mount over /, as qemu_root_defconfig's header "
        f"describes; it names {union!r}"
    )
    assert other == "/private/preboot", (
        "the non-union arm must mount SystemOS on its own at /private/preboot, "
        f"where the DYLD_SHARED_CACHE_DIR string points; it names {other!r}"
    )


def test_the_mount_report_is_gated_on_success_and_the_symbol():
    """Emitted only when the mount succeeded AND the console is linked.

    The console archive is behind its own build switch, so the reference is
    weak_imported (zzy-native-gpu-addon-mount.patch:76's idiom); without the
    null check a build with no console archive calls address zero.
    """
    patch = _mount_patch()
    if patch is None:
        pytest.skip("the offline owner cache mount patch is not in this pinned tree")
    code = _code_lines(patch.read_text(encoding="utf-8"))
    assert any(
        "native_console_report" in line and "weak_import" in line for line in code
    ), "native_console_report is not weak-imported; a kernel built without the console archive will not link"
    assert any("if (!error && native_console_report)" in line for line in code), (
        "the emitter is not gated on !error AND the symbol"
    )
    assert any(
        line.startswith("native_console_report(mount_record") for line in code
    ), "the mount record is never handed to the port"
    assert any(
        line.startswith("native_console_report(identity_record") for line in code
    ), "the identity record is never handed to the port"


def test_the_mount_report_is_emitted_after_the_mount_is_authorized():
    """The emission is the LAST thing, not an intermediate checkpoint.

    native_owner_cache_checkpoint() records the outcome; emitting before it
    would let a later copyout failure leave "mounted" on the wire next to an
    error return.
    """
    patch = _mount_patch()
    if patch is None:
        pytest.skip("the offline owner cache mount patch is not in this pinned tree")
    text = patch.read_text(encoding="utf-8")
    checkpoint = text.find("native_owner_cache_checkpoint(error, cache_authorized);")
    emit = text.find("native_console_report(mount_record")
    assert checkpoint != -1 and emit != -1
    assert emit > checkpoint, (
        "the record is emitted before the checkpoint that records the outcome; "
        "a later copyout failure would leave 'mounted' on the wire"
    )
    # And only once: a second emission of the same event is silently dropped by
    # the host (_guest_events uses setdefault), so a reader would never see it.
    assert text.count("native_console_report(mount_record") == 1
