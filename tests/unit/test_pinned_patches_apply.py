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
    #
    # 329 -> 346 on 2026-11-02: the console-port declaration moved above the
    # XNU_OWNER_CACHE_MOUNT guard so the load_init_program diagnostic could see
    # it, and that move added 14 comment lines above this header.  A renumbering,
    # not a new defect -- the old-side start (7086) is unchanged.
    ("offline-owner-systemos-mount.patch", 346),
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
# The dyld shared cache read reports the mapping it just established
# ---------------------------------------------------------------------------

#: The event the acceptance gate looks for (milestones.py:1510).
CACHE_READ_EVENT = "dyld.cache_read"

#: The defconfig the FIRST kernel pass builds.  board.toml [kernel] names it and
#: build-macos.sh:2590-2596 uses it whenever the owner root chain is not yet on
#: disk, which is every run that has to build the archives from it.  It sets
#: CONFIG_XNU_NATIVE_CS_HASH_CACHE and does NOT set
#: CONFIG_XNU_NATIVE_VIRTIO_CONSOLE -- the fact the emitter's gate turns on.
FIRST_PASS_DEFCONFIG = (
    REPO / "vendor" / "UniXNU-qemu" / "configs" / "qemu_defconfig"
)


def _cache_read_patch() -> Path | None:
    for patch in _patches():
        if patch.name == "native-dyld-cache-read.patch":
            return patch
    return None


def _cache_read_hunks(text: str) -> list[tuple[str, list[str]]]:
    """``(header, body)`` per hunk, with the diff markers still on the body.

    The site assertions need the CONTEXT lines, not just the added ones: what
    makes the emitter's position meaningful is which function's tail it sits in,
    and that is carried by the context on both sides of it.
    """
    import re

    hunks: list[tuple[str, list[str]]] = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        match = re.match(r"^@@ .* @@$", lines[index])
        if match is None:
            index += 1
            continue
        body: list[str] = []
        index += 1
        while index < len(lines) and not lines[index].startswith("@@"):
            if lines[index].startswith(("--- ", "+++ ", "diff ")):
                break
            body.append(lines[index])
            index += 1
        hunks.append((match.group(0), body))
    return hunks


def _cache_read_record(text: str) -> dict:
    """Expand the record's adjacent literals and parse the result as JSON.

    Not :func:`_record_payload`, which expands the mount patch's
    XNU_OWNER_CACHE_* macros and refuses a patch that does not define them.  The
    shape is the same on purpose: the literals are joined the way C's
    concatenation does, the backslash-n is still two characters until it is
    decoded on the next line, and the record must END a line because the host's
    _guest_records splits the guest log by line and parses each one.
    """
    import json
    import re

    hit = re.search(r"static char const cache_read_record\[\] =\s*(.*?);", text, re.S)
    assert hit is not None, "cache_read_record is not defined in the patch"
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', hit.group(1))
    assert literals, "cache_read_record holds no string literal"
    joined = "".join(literals)
    assert joined.endswith("\\n"), (
        "cache_read_record does not end with a newline escape; an unterminated "
        "record runs into the next line the host parses"
    )
    decoded = joined.replace('\\"', '"').replace("\\n", "\n")
    assert decoded.endswith("\n"), "cache_read_record does not decode to a trailing newline"
    return json.loads(decoded)


def test_the_cache_read_patch_is_in_the_set_at_all():
    """Coverage that looks total and is not, in the other direction.

    Every assertion below is about one patch.  If that patch stops being walked
    -- renamed, moved out of ``patches/xnu``, or excluded by a glob -- every one
    of them still passes, because each looks its own file up by name and skips
    when it is absent.  The count and the target file are asserted here so that
    a patch nobody walks is a red test rather than seven green ones.
    """
    patches = _patches()
    names = {p.name for p in patches}
    assert "native-dyld-cache-read.patch" in names, (
        "native-dyld-cache-read.patch is not being walked; the assertions about "
        "it below would each skip and read as not-verified-here"
    )
    patch = next(p for p in patches if p.name == "native-dyld-cache-read.patch")
    text = patch.read_text(encoding="utf-8")
    assert "--- a/bsd/vm/vm_unix.c" in text and "+++ b/bsd/vm/vm_unix.c" in text, (
        "the emitter no longer targets bsd/vm/vm_unix.c, so the vm_unix.o "
        "CFLAGS_ADD pairing this patch depends on is about the wrong file"
    )


def test_the_cache_read_record_is_valid_json_naming_the_gates_event():
    """The record parses as JSON and names the event M7's third requirement wants.

    Asserted on the PARSED record, not on the presence of the event name: the
    mount emitter's two drafts carried the literal "system.mount" and produced a
    record that parsed as nothing, and both defects compiled clean.
    """
    patch = _cache_read_patch()
    if patch is None:
        pytest.skip("the dyld cache read patch is not in this pinned tree")
    payload = _cache_read_record(patch.read_text(encoding="utf-8"))
    assert payload["event"] == CACHE_READ_EVENT, (
        f"the record names {payload['event']!r}, not {CACHE_READ_EVENT!r}; the "
        "probe for it reports the event missing, which reads as the guest never "
        "reading the dyld shared cache"
    )
    assert payload["source"] == "XNU._shared_region_map_and_slide", (
        f"the record's source is {payload.get('source')!r}, not the function that "
        "emits it; a record whose source names a different site cannot be "
        "cross-checked against the one that produced it"
    )


def test_the_cache_read_report_is_gated_on_success():
    """Emitted only when the shared-region mapping succeeded, and only once.

    The gate is ``!error`` and NOTHING ELSE, and that is a change with a
    measurement behind it.  This test used to require
    ``if (!error && native_console_report)`` on the mount emitter's reasoning
    that "a strong reference that somehow resolved to null would otherwise be a
    call through zero, and the linker's guarantee costs one dead branch to keep
    visible".

    That reasoning is wrong, and build 41330622788 is why.  A
    ``__attribute__((weak_import))`` reference resolved to null on a real guest
    and dropped both records behind a green build with the records byte-level in
    the booting kernel -- and the null check made that absence look like a
    deliberate design decision.  **A guard that tests whether an optional
    dependency is present is indistinguishable, from the source, from a guard
    hiding that the dependency was never linked.**  The two have opposite
    remedies, and the guard picks the wrong one.

    Liveness is answered by the return status instead.  ``native_console_report()``
    returns ``kIOReturnNotOpen`` when there is no live console, so a caller that
    cares can tell; the emitter here does not need to, because a strong reference
    means a missing archive fails the LINK naming the symbol.

    ``!error`` stays because it is about the EVENT's truth, not the
    dependency's: the cleanup above has already released the file mappings and
    the shared-region reference, so an earlier emission would put "read" on the
    wire for a mapping a later failure then abandoned.
    """
    patch = _cache_read_patch()
    if patch is None:
        pytest.skip("the dyld cache read patch is not in this pinned tree")
    text = patch.read_text(encoding="utf-8")
    code = _code_lines(text)
    assert any("if (!error && !native_dyld_cache_read_reported)" in line
               for line in code), (
        "the emitter is not gated on !error AND the one-record bound.  The "
        "second half is what keeps a per-process site from flooding the "
        "channel: dyld calls _shared_region_map_and_slide() in every process "
        "that uses the shared cache."
    )
    assert any(
        line.startswith("native_console_report(cache_read_record") for line in code
    ), "the cache read record is never handed to the port"
    # And only once: a second emission of the same event is silently dropped by
    # the host (_guest_events uses setdefault), so a reader would never see it.
    assert text.count("native_console_report(cache_read_record") == 1
    # The bound is a flag that is never cleared, and it is set BEFORE the call
    # rather than after: a record that is in flight is a record that has been
    # reported, and a re-entrant path must not put a second one on the wire.
    assert "native_dyld_cache_read_reported = 1;" in text, (
        "the one-record bound is never set"
    )
    assert "static int native_dyld_cache_read_reported = 0;" in text, (
        "the one-record bound is not declared at file scope, so it does not "
        "survive between calls"
    )


def test_the_cache_read_reference_is_strong_not_weak():
    """``native_console_report`` must be a STRONG reference.

    The whole reason build 41330622788 existed.  This used to be
    ``__attribute__((weak_import))`` in the mount emitter on the reasoning that
    the console archive is behind its own build switch, and MEASURED on a real
    guest it resolved to null: both records were dropped with a green build and
    the records byte-level in the booting kernel.  A weak reference does not
    oblige the linker to keep the definition, so the symbol the call site tests
    for is simply absent, and the null check makes that absence look deliberate.
    """
    patch = _cache_read_patch()
    if patch is None:
        pytest.skip("the dyld cache read patch is not in this pinned tree")
    code = _code_lines(patch.read_text(encoding="utf-8"))
    declarations = [
        line for line in code
        if line.startswith("extern") and "native_console_report" in line
    ]
    assert len(declarations) == 1, (
        f"expected exactly one declaration of native_console_report, found "
        f"{len(declarations)}: {declarations}"
    )
    assert "weak_import" not in declarations[0], (
        "native_console_report is weak-imported. Measured on build 41330622788: "
        "the symbol then resolves to null and the records are dropped silently, "
        "with the build green and the records byte-level present in the kernel. "
        "Use a strong extern."
    )
    assert declarations[0].startswith("extern int native_console_report("), (
        f"the declaration is not the expected strong extern: {declarations[0]!r}"
    )


def test_the_cache_read_record_is_an_array_whose_sizeof_is_of_the_array():
    """``sizeof(record) - 1`` on an ARRAY, never on a decayed pointer.

    This is the defect that cost the previous driver pin three records on a real
    guest: the record was selected by a ternary whose two arms were arrays of
    different lengths, the conditional operator decayed both to ``char *``, and
    ``sizeof`` became 8.  The literals were in the kernel the whole time and the
    build was green, so neither a build nor a byte-level grep of the image could
    see it.  Both halves are asserted: the declaration must be the array form,
    and the call must pass that array directly rather than an expression that
    could select between two of them.
    """
    patch = _cache_read_patch()
    if patch is None:
        pytest.skip("the dyld cache read patch is not in this pinned tree")
    code = _code_lines(patch.read_text(encoding="utf-8"))

    declarations = [
        line for line in code if line.startswith("static char const cache_read_record")
    ]
    assert len(declarations) == 1, (
        f"expected exactly one declaration of cache_read_record, found "
        f"{len(declarations)}: {declarations}"
    )
    assert declarations[0].endswith("[] ="), (
        f"cache_read_record is not declared as an array: {declarations[0]!r}. "
        "sizeof on a pointer is 8 on arm64, which is the seven-byte record"
    )
    assert "*" not in declarations[0], (
        f"cache_read_record is declared through a pointer: {declarations[0]!r}"
    )
    assert any(
        line == "native_console_report(cache_read_record, sizeof(cache_read_record) - 1);"
        for line in code
    ), (
        "the record is not handed to the port as sizeof(cache_read_record) - 1; "
        "any other length expression is the eight-byte record"
    )
    # No conditional operator anywhere in the emitter: the ternary that decayed
    # two arrays to one pointer is the shape being excluded.
    start = next(i for i, line in enumerate(code)
                 if "if (!error && !native_dyld_cache_read_reported)" in line)
    block = code[start:start + 10]
    assert not any("?" in line for line in block), (
        f"the emitter block carries a conditional operator: {block}"
    )


def test_the_cache_read_site_is_the_shared_cache_mapping_function():
    """The emitter is the last statement of the function dyld calls for the cache.

    Two hunks, and the context is what identifies them.  The first sits between
    the end of ``shared_region_map_and_slide_setup()`` and the comment block that
    introduces ``_shared_region_map_and_slide()``, whose own words are "dyld uses
    this to map a shared cache file into a shared region" -- so the extern is
    declared at file scope immediately before the right function.  The second
    sits between that function's ``<- map`` trace and its ``return error;``, so
    the emitter is the last thing it does before returning, after the cleanup.

    This is the assertion that would have caught the site the first revision of
    this patch was written for.  ``cs_native4k_prepare()`` in
    bsd/kern/ubc_subr.c reads and authenticates the signed region of an ARBITRARY
    code-signed vnode -- every 16K-hashed Mach-O on the board, not the dyld
    shared cache -- so an event named dyld.cache_read emitted there is false for
    the overwhelming majority of its calls.  The mapped line numbers were exact
    (function at 5767, the vn_rdwr at 5818, error = 0 at 5858) and the site was
    still wrong.
    """
    patch = _cache_read_patch()
    if patch is None:
        pytest.skip("the dyld cache read patch is not in this pinned tree")
    hunks = _cache_read_hunks(patch.read_text(encoding="utf-8"))
    assert len(hunks) == 2, f"expected two hunks, found {len(hunks)}"

    first_header, first_body = hunks[0]
    assert any(
        "dyld uses this to map a shared cache file into a shared region" in line
        for line in first_body
    ), (
        f"the first hunk ({first_header}) is not immediately before the function "
        "documented as mapping a shared cache file into a shared region, so the "
        "extern it declares is not in scope of the right function"
    )

    second_header, second_body = hunks[1]
    trace_at = next(
        (i for i, line in enumerate(second_body) if "<- map" in line), None
    )
    return_at = next(
        (i for i, line in enumerate(second_body)
         if line.lstrip("+ ").strip() == "return error;"), None
    )
    gate_at = next(
        (i for i, line in enumerate(second_body)
         if "if (!error && !native_dyld_cache_read_reported)" in line), None
    )
    assert trace_at is not None and return_at is not None and gate_at is not None, (
        f"the second hunk ({second_header}) does not carry the trace, the return "
        "and the gate, so the emitter's position inside the function cannot be "
        "read from the patch"
    )
    assert trace_at < gate_at < return_at, (
        f"the emitter is not between the function's trace and its return: trace "
        f"at {trace_at}, gate at {gate_at}, return at {return_at}"
    )


def test_the_cache_read_emitter_is_gated_on_the_switch_that_links_the_console():
    """Guarded on XNU_NATIVE_VIRTIO_CONSOLE, not on the hash-cache switch.

    A strong reference makes the definition load-bearing: if the archive is ever
    missing while this code is compiled, the link fails naming the symbol.  That
    is the loud failure and the correct one -- but it means the gate has to be
    the switch that actually puts the archive on the link.

    XNU_NATIVE_VIRTIO_CONSOLE is that switch: build-xnu-4k-baseline.sh:197-198
    adds -DXNU_NATIVE_VIRTIO_CONSOLE=1 and :666-670 adds
    -Wl,-force_load,<archive>.  XNU_NATIVE_CS_HASH_CACHE is NOT: both defconfigs
    set it, and the first-pass one sets it without the console, so an emitter
    gated on it would fail the first-pass link with an undefined symbol -- or, if
    the reference were weak to avoid that, drop the record silently.  Both halves
    are asserted here, because the first-pass defconfig is a file that can change
    independently of this patch.
    """
    patch = _cache_read_patch()
    if patch is None:
        pytest.skip("the dyld cache read patch is not in this pinned tree")
    text = patch.read_text(encoding="utf-8")
    guard = "#if defined(XNU_NATIVE_VIRTIO_CONSOLE) && XNU_NATIVE_VIRTIO_CONSOLE"
    assert text.count(guard) >= 1, (
        f"the console switch guard appears {text.count(guard)} times, not at "
        f"least once"
    )
    # The EMITTER must be inside the guard, and the DECLARATION must not be.
    #
    # This test used to require BOTH inside it, on the reasoning that "the strong
    # extern is not inside the console switch guard, so it is declared whether or
    # not the archive is on the link".  Build 41441479230 disproved that
    # reasoning the expensive way:
    #
    #     vm_unix.c:2941:3: error: call to undeclared function
    #                      'native_console_report'
    #
    # A declaration with no definition is FREE -- the linker only resolves a
    # symbol something REFERENCES.  So an unconditional extern costs nothing when
    # the emitter is compiled out, and a CONDITIONAL one is absent exactly when a
    # caller outside it needs it.  The asymmetry is the whole point: the
    # declaration must be at least as visible as the shallowest caller, and the
    # caller is the half that must be conditional.
    #
    # The visibility half is asserted by
    # test_every_console_call_site_has_a_visible_declaration, which walks the
    # depth; what is asserted here is that the CALL is behind the switch, so a
    # configuration without the archive produces no undefined symbol.
    call_at = text.index("native_console_report(cache_read_record")
    guard_at = text.index(guard)
    assert guard_at < call_at, (
        "the emitter's call is not behind the console switch guard, so a "
        "configuration that does not force-load libNativeVirtioConsole.a would "
        "fail the link naming the symbol"
    )
    # And nothing between the guard and the call closes it.  The slice starts
    # AFTER the guard's own line, because that line contains "#if" and counting
    # it would make every patch fail by one.
    between = text[guard_at + len(guard):call_at]
    assert between.count("#endif") == between.count("#if"), (
        "the guard is closed and not re-opened between the switch and the call, "
        "so the call is not actually behind it"
    )

    first_pass = FIRST_PASS_DEFCONFIG
    if not first_pass.is_file():
        pytest.skip(
            f"{first_pass.name} is not in this checkout, so which switches the "
            "first kernel pass builds with is unknown"
        )
    config = first_pass.read_text(encoding="utf-8")
    assert "CONFIG_XNU_NATIVE_CS_HASH_CACHE=y" in config, (
        f"{first_pass.name} no longer sets CONFIG_XNU_NATIVE_CS_HASH_CACHE; the "
        "claim that this switch is not a console proxy is no longer measurable "
        "and the gate has to be re-derived"
    )
    assert "CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y" not in config, (
        f"{first_pass.name} now sets CONFIG_XNU_NATIVE_VIRTIO_CONSOLE, so the "
        "two switches are no longer distinguishable and this assertion's premise "
        "has to be re-derived"
    )


def test_the_cache_read_patch_applies_to_its_own_minus_side():
    """``git apply`` itself, plus the header check, for this patch alone.

    The whole-file tests above already do both for every patch.  This one exists
    because those tests walk a directory: a patch that is added, applies, and is
    never named by any other assertion here would pass them and be measured by
    nothing.  It is also the only place the new patch's OWN hunk headers are
    checked for reachability, which is the defect that cost run 40981242194 a
    patch whose every count was correct.
    """
    patch = _cache_read_patch()
    if patch is None:
        pytest.skip("the dyld cache read patch is not in this pinned tree")
    checker = _load_checker()
    problems = checker.check(patch)
    assert not problems, (
        f"{len(problems)} problem(s) in this patch's hunks:\n" + "\n".join(problems)
    )
    proc = _run("verify_patch_applies.py", str(patch))
    assert proc.returncode == 0 and "exit: 0" in proc.stdout, (
        f"the patch does not apply to its own reconstructed minus side:\n"
        f"{proc.stdout}{proc.stderr}"
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

    The null check stays even though the reference is now strong, because a
    strong reference that somehow resolved to null would otherwise be a call
    through zero.  The linker guarantees it cannot, and the guard costs one
    dead branch.
    """
    patch = _mount_patch()
    if patch is None:
        pytest.skip("the offline owner cache mount patch is not in this pinned tree")
    code = _code_lines(patch.read_text(encoding="utf-8"))
    assert any("if (!error && native_console_report)" in line for line in code), (
        "the emitter is not gated on !error AND the symbol"
    )
    assert any(
        line.startswith("native_console_report(mount_record") for line in code
    ), "the mount record is never handed to the port"
    assert any(
        line.startswith("native_console_report(identity_record") for line in code
    ), "the identity record is never handed to the port"


def test_the_console_reference_is_strong_not_weak():
    """``native_console_report`` must be a STRONG reference.

    This is the whole reason build 41330622788 existed, and the defect it
    caught is the most expensive kind: silent, and invisible to every
    source-level check.

    The reference used to be ``__attribute__((weak_import))``, on the
    reasoning that the console archive is behind its own build switch and the
    patch had to link with or without it.  It does not work.  MEASURED: an
    ungated record at the emitter function's first statement never arrived on a
    boot where the guest ran to t=1530 s -- twenty-four times past that function
    -- while five records from the same port on the same boot arrived complete.
    The records were byte-level in the booting kernel, the mount succeeded, and
    the call site panics on any non-zero return with no panic in the ring, so
    ``error`` was 0.  A weak_import does not oblige the linker to keep the
    definition, so the symbol the call site tests for was simply absent, and
    the ``if (native_console_report)`` guard made that absence look like a
    deliberate design decision.

    A strong reference makes the definition load-bearing: if the archive is
    ever missing while this code is compiled, the link fails naming the symbol
    instead of dropping the records.

    The switch pairing is what makes the strong reference safe, and it is
    asserted here too, because "the two switches are always set together" is a
    claim about a config file that can change independently of this patch:

    * ``XNU_OWNER_CACHE_MOUNT`` is set only by ``qemu_root_defconfig``, which is
      also the config that sets ``CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y`` -- the
      switch that puts ``libNativeVirtioConsole.a`` on the kernel link with
      ``-Wl,-force_load``.
    """
    patch = _mount_patch()
    if patch is None:
        pytest.skip("the offline owner cache mount patch is not in this pinned tree")
    text = patch.read_text(encoding="utf-8")
    code = _code_lines(text)

    declarations = [
        line for line in code
        if line.startswith("extern") and "native_console_report" in line
    ]
    assert len(declarations) == 1, (
        f"expected exactly one declaration of native_console_report, found "
        f"{len(declarations)}: {declarations}"
    )
    assert "weak_import" not in declarations[0], (
        "native_console_report is weak-imported. Measured on build "
        "41330622788: the symbol then resolves to null and BOTH records are "
        "dropped silently, with the build green and the records byte-level "
        "present in the kernel. Use a strong extern."
    )
    assert declarations[0].startswith("extern int native_console_report("), (
        f"the declaration is not the expected strong extern: {declarations[0]!r}"
    )


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


def test_this_patch_keeps_the_lines_the_next_patch_uses_as_context() -> None:
    """The patch set has inter-patch dependencies no per-patch check can see.

    ``zzz-boot-progress.patch`` carries a hunk at ``@@ -7389,11 +7393,22 @@``
    whose PRETEXT -- the whole run of context lines it needs contiguous -- is
    TEN lines, and nine of them are lines THIS patch adds::

        \t(void) mach_vm_allocate_kernel(map, &scratch_addr, ...)   <- pristine
        \tuser_addr_t init_envp = USER_ADDR_NULL;                    <- added here
        #if defined(XNU_OWNER_CACHE_MOUNT) && XNU_OWNER_CACHE_MOUNT   <- added here
        \tint cache_error = native_owner_cache_prepare(...)          <- added here
        \tif (cache_error) panic(...)                                <- added here
        #endif                                                       <- added here
        #if defined(XNU_OWNER_DATA_MOUNT) && XNU_OWNER_DATA_MOUNT     <- added here
        \tint data_error = native_owner_data_prepare(...)            <- added here
        \tif (data_error) panic(...)                                 <- added here
        #endif                                                       <- added here
        #if CONFIG_MEMORYSTATUS                                      <- pristine

    ``git apply`` requires that block contiguous.  So the set is
    order-dependent in a way that
    :func:`test_every_patch_applies_to_its_own_minus_side` structurally cannot
    catch: that test reconstructs each patch's minus side, which is the PRISTINE
    file, and applies it there.  A change that keeps a patch self-consistent can
    still break the patch that follows it.

    Measured twice.  Build 41346094752 failed at
    ``build-xnu-4k-baseline.sh:44`` with ``patch failed:
    bsd/kern/kern_exec.c:7389`` -- a line in ``zzz-boot-progress.patch``, from
    an edit to THIS patch.  The first fix kept ``mach_vm_allocate_kernel``
    adjacent to ``user_addr_t init_envp``, which was TRUE and which changed
    nothing: build 41350610831 failed with the identical error, because the
    constraint was the ten-line block and not that one adjacency.  The guard
    written with the first fix asserted the wrong thing and passed while the
    build failed, which is worse than no guard -- and it is recorded here rather
    than quietly replaced.

    The constraint is asserted as a literal list, because that is the only
    formulation with no ambiguity: a general cross-patch checker was attempted
    and reported 18 splits on a tree where real ``git apply`` takes all 56
    patches, because anchoring on lines like ``#endif`` and ``}`` matches in the
    wrong place.  An instrument that is red on a green tree is worse than none.

    The instrument that IS validated is
    ``local-private/tmp/repro-patch-order.py``, which replays the runner's own
    order with real ``git apply`` against the cloned XNU source.  It needs that
    source, so it cannot run in CI; this test is the CI-side half, and it is
    precise rather than general.
    """
    patch = _mount_patch()
    if patch is None:
        pytest.skip("the offline owner cache mount patch is not in this pinned tree")
    text = patch.read_text(encoding="utf-8")
    lines = text.split("\n")

    block = [
        " \t(void) mach_vm_allocate_kernel(map, &scratch_addr, map_page_size, VM_FLAGS_ANYWHERE, VM_KERN_MEMORY_NONE);",
        "+\tuser_addr_t init_envp = USER_ADDR_NULL;",
        "+#if defined(XNU_OWNER_CACHE_MOUNT) && XNU_OWNER_CACHE_MOUNT",
        "+\tint cache_error = native_owner_cache_prepare(p, (user_addr_t)scratch_addr, &init_envp);",
        '+\tif (cache_error) panic("offline owner SystemOS mount failed: %d", cache_error);',
        "+#endif",
        "+#if defined(XNU_OWNER_DATA_MOUNT) && XNU_OWNER_DATA_MOUNT",
        "+\tint data_error = native_owner_data_prepare(p, (user_addr_t)scratch_addr);",
        '+\tif (data_error) panic("offline scratch Data mount failed: %d", data_error);',
        "+#endif",
        " #if CONFIG_MEMORYSTATUS",
    ]

    start = next((i for i, ln in enumerate(lines) if ln == block[0]), None)
    if start is None:
        pytest.fail(
            "the mach_vm_allocate_kernel line is not in the patch, so the "
            "constraint is unchecked"
        )
    got = lines[start:start + len(block)]
    if got != block:
        for offset, (want, have) in enumerate(zip(block, got)):
            if want != have:
                raise AssertionError(
                    f"patch line {start + offset + 1} is {have.strip()[:70]!r} "
                    f"where zzz-boot-progress.patch needs {want.strip()[:70]!r}. "
                    "That patch's context is the whole ten-line block above, "
                    "so anything inserted inside it breaks a patch that was not "
                    "edited (measured: builds 41346094752 and 41350610831 both "
                    "failed at kern_exec.c:7389)"
                )
        raise AssertionError(
            f"the block is {len(got)} lines where it must be {len(block)}; "
            "zzz-boot-progress.patch needs all of them contiguous"
        )

    # The diagnostic that motivated this constraint must still sit ABOVE the
    # block, or the measurement it exists for is no longer the one being taken:
    # it has to fire before the prepare calls.
    diag = next((i for i, ln in enumerate(lines)
                 if "DIAGNOSTIC, TEMPORARY" in ln), None)
    if diag is not None:
        assert diag < start, (
            f"the diagnostic is at patch line {diag + 1}, below the allocate "
            f"line at {start + 1}. It must fire before the prepare calls, and "
            "the only position that is both before them and outside "
            "zzz-boot-progress.patch's context block is above the allocate."
        )


# ---------------------------------------------------------------------------
# M8: the original launchd, and the daemons that followed it
# ---------------------------------------------------------------------------

#: The patch that carries the two M8 emitters.  A NEW file rather than more
#: hunks in offline-owner-systemos-mount.patch, because that patch's
#: mach_vm_allocate_kernel / init_envp context is shared with
#: zzz-boot-progress.patch and a new file cannot shift it.
M8_PATCH_NAME = "zzz-m8-launchd-daemon-events.patch"

#: The two events the gate asks for (milestones.py:1512-1513).  They are ONE
#: change, not two: the veto m8.veto.mockfs_pid1_as_launchd fires when
#: launchd.running arrives WITHOUT daemon.spawned, so landing half the pair
#: turns M8 from UNVERIFIED into FAIL and makes the release verdict worse.
M8_EVENTS = ("launchd.running", "daemon.spawned")

#: The build step that decides which CFLAGS kern_exec.o is compiled with.
XNU_BUILD_SCRIPT = REPO / "vendor" / "UniXNU" / "scripts" / "build-xnu-4k-baseline.sh"

#: The switch this emitter is guarded on, and the reason it is that one:
#: build-xnu-4k-baseline.sh:466 passes it as kern_exec.o_CFLAGS_ADD, and it is
#: set only by qemu_root_defconfig:33 -- the same config that sets
#: CONFIG_XNU_NATIVE_VIRTIO_CONSOLE=y at :47, which is what puts
#: libNativeVirtioConsole.a on the kernel link with -Wl,-force_load (:674-678).
M8_GUARD = "XNU_OWNER_CACHE_MOUNT"


def _m8_patch() -> Path | None:
    for patch in _patches():
        if patch.name == M8_PATCH_NAME:
            return patch
    return None


def _m8_text() -> str:
    """The M8 patch's text, or a skip when the pinned tree is not acquired."""
    patch = _m8_patch()
    if patch is None:
        pytest.skip(f"{M8_PATCH_NAME} is not in this pinned tree")
    return patch.read_text(encoding="utf-8")


def _added(text: str) -> list[tuple[int, str]]:
    """``(patch line number, added code line)`` for every added line.

    Code, not prose: the same discriminator :func:`_code_lines` uses, and for
    the same reason -- in a unified diff every added line starts with '+', so
    the comment explaining the emitter is a '+ line too, and "the record is
    emitted" would be satisfied by the explanation of the record.
    """
    out: list[tuple[int, str]] = []
    for index, line in enumerate(text.split("\n")):
        if not line.startswith("+"):
            continue
        body = line[1:].lstrip()
        if body[:1] in ("*", "/"):
            continue
        out.append((index, body))
    return out


def _event_names(text: str) -> set[str]:
    """Every event name the patch emits, read out of its own record literals."""
    import re

    return set(re.findall(r'\\"event\\":\\"([A-Za-z0-9_.]+)\\"', text))


def _c_strings(body: str) -> list[str]:
    """The decoded contents of the C string literals in ``body``.

    Adjacent literals are what C concatenates, so the caller joins these and
    gets the record exactly as the compiler builds it.  The decode is the same
    one :func:`_record_payload` uses, and for the same reason: at this point the
    backslash-n is still two characters, and the record only ends a line one
    step later.
    """
    import re

    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
    assert literals, "no string literal found"
    return [
        literal.replace('\\"', '"').replace("\\n", "\n")
        for literal in literals
    ]


def _static_record(text: str, name: str, occurrence: int = 0) -> str:
    """The joined literal of one ``static char const NAME[] = ...;``.

    ``occurrence`` selects which one: the launchd record is declared twice, once
    per arm of the index guard, and taking only the first would let the other
    arm drift unnoticed.
    """
    import re

    hits = list(re.finditer(
        r"static char const " + re.escape(name) + r"\[\] =\s*(.*?);",
        text, re.S))
    assert hits, f"{name} is not declared in the patch"
    assert occurrence < len(hits), (
        f"{name} is declared {len(hits)} times; occurrence {occurrence} is out "
        "of range"
    )
    return "".join(_c_strings(hits[occurrence].group(1)))


def _m8_require_build_script() -> Path:
    """The kernel build script, failing when the pinned tree is half-acquired."""
    if not XNU_BUILD_SCRIPT.is_file():
        pytest.skip(f"{XNU_BUILD_SCRIPT} is not present; the pinned vendor tree "
                    "is not acquired")
    return XNU_BUILD_SCRIPT


def test_the_m8_patch_is_in_the_pinned_tree_at_all():
    """The patch is present, and its absence is a failure and not a skip.

    ``_patches()`` skips the whole file when ``vendor/UniXNU/patches`` is
    absent, which is the honest outcome on a host that never acquired the pin.
    A host that HAS the pin and does not have this file is a different
    situation: the two emitters are simply not going to be built, and M8 stays
    UNVERIFIED while every other check in here reads green.  That is the shape
    :func:`tests.vendor_paths.require` was written for, and this is the same
    discriminator applied to a file that did not exist when it was written.

    **AND IT IS CURRENTLY HELD BACK ON PURPOSE, WHICH THIS TEST MUST SAY OUT
    LOUD RATHER THAN LET IT LOOK LIKE AN OVERSIGHT.**

    M8 breaks the build's SECOND kernel pass, and the failure names a file
    nobody was editing:

        error: patch failed: bsd/kern/kern_exec.c:183
        error: bsd/kern/kern_exec.c: patch does not apply
        build-xnu-4k-baseline.sh: line 44: git -C .cache/xnu-work apply --check

    ``build-xnu-4k-baseline.sh`` applies ``patches/xnu/*.patch`` TWICE -- once
    in the bootstrap pass (``XNU_BOOTSTRAP=1``) and again after the archive
    builders -- and its loop decides whether to re-apply with
    ``git apply --reverse --check``.  That test is not sound for M8: M8's third
    hunk inserts inside ``load_init_program()``'s ``if (!error) {`` block, which
    is also the trailing context of ``offline-owner-systemos-mount.patch``'s
    ``@@ -7186,7 +7520,7 @@`` hunk.  With M8 applied, the mount patch can no longer be
    reversed, so the loop tries to apply it again and fails at the include line
    its first hunk already changed.

    Isolated by experiment, not by reasoning: with M7 and M8 both present the
    second pass fails; with M7 alone it is clean; with M8 alone it fails.  The
    reproduction is ``local-private/tmp/repro-patch-twopass.py``, which replays
    both passes and is the instrument that found this -- the single-pass
    ``repro-patch-order.py`` reports "all patches applied" and is blind to it.

    So M8 is removed from the pinned tree until one of two things happens: its
    emitter moves to a site that does not split the mount patch's context, or
    the build script's already-applied test stops depending on reversibility.
    M6, M7 and the install diagnostics are unaffected and are measured in the
    same build.
    """
    if not (REPO / "vendor" / "UniXNU" / "patches").is_dir():
        pytest.skip("the pinned vendor tree is not acquired")
    patch = _m8_patch()
    if patch is None:
        held = REPO / "local-private" / "tmp" / "parked-patches" / M8_PATCH_NAME
        assert held.is_file(), (
            f"{M8_PATCH_NAME} is neither in the pinned tree nor parked, so the "
            "hold-back recorded in this test's docstring has no evidence behind "
            "it and M8 has simply been lost"
        )
        pytest.xfail(
            f"{M8_PATCH_NAME} is held back: it breaks the build's second kernel "
            "pass by splitting offline-owner-systemos-mount.patch's trailing "
            "context.  Reproduce with local-private/tmp/repro-patch-twopass.py. "
            "Restoring it requires moving the emitter or fixing the build "
            "script's already-applied test."
        )
    assert patch is not None


def test_the_m8_records_are_valid_json():
    """Both records parse as JSON, and the daemon one parses with real values.

    Two defects in this repository's emitters compiled clean and read on the
    host as "the guest never did it": a dropped ``"volume_uuid":`` key, and a
    macro whose value came out unquoted.  Both are invisible to a grep for the
    event name, so the assertion is on the PARSED record.

    The daemon record is built at run time, so it is validated by assembling
    the three literals the way the C does -- prefix, a pid, middle, a comm,
    suffix -- and parsing the result.  A separator in the wrong place is a
    record that parses as nothing, and nothing else here would notice.
    """
    import json

    text = _m8_text()
    for occurrence in (0, 1):
        payload = json.loads(_static_record(text, "launchd_record", occurrence))
        assert payload["event"] == "launchd.running"
        assert payload["pid"] == 1
    prefix = "".join(_c_strings(
        _added_value(text, "m8_daemon_prefix")))
    middle = "".join(_c_strings(_added_value(text, "m8_daemon_middle")))
    suffix = "".join(_c_strings(_added_value(text, "m8_daemon_suffix")))
    assembled = prefix + "128" + middle + "backboardd" + suffix
    assert assembled.endswith("\n"), (
        "the assembled daemon record does not end with a newline, so it runs "
        "into the next line of the guest log and the host's per-line parse "
        "drops it"
    )
    payload = json.loads(assembled)
    assert payload == {
        "event": "daemon.spawned",
        "pid": 128,
        "comm": "backboardd",
        "source": "XNU.__mac_execve",
    }, f"the assembled daemon record is {payload!r}"


def _added_value(text: str, name: str) -> str:
    """The right-hand side of one ``static char const NAME[] = ...;``."""
    import re

    hit = re.search(r"static char const " + re.escape(name) + r"\[\] =\s*(.*?);",
                    text, re.S)
    assert hit is not None, f"{name} is not declared in the patch"
    return hit.group(1)


def test_the_m8_event_names_are_exactly_the_two_the_gate_asks_for():
    """The event names, read out of the patch rather than restated here.

    An earlier revision of the mount test substituted its own UUID and every
    assertion stayed green when the patch changed.  The same trap applies to a
    name: assert what the patch SAYS, then assert that against the gate.
    """
    text = _m8_text()
    found = _event_names(text)
    assert found == set(M8_EVENTS), (
        f"the patch emits {sorted(found)}, not {sorted(M8_EVENTS)}.  A third "
        "name is a record nobody reads; a missing one is half the pair, and "
        "half the pair trips the m8.veto.mockfs_pid1_as_launchd veto"
    )


def test_both_m8_events_are_in_the_same_patch_set():
    """The anti-veto test: one event without the other must be detectable.

    ``_guest_events`` merges with ``setdefault``, so the gate reads the FIRST
    record for each event name and the veto compares the two probes directly:
    launchd.running present and daemon.spawned absent is a FAIL, not a
    UNVERIFIED.  This test asserts the pair is in one patch set, and then
    asserts that a set carrying only one of them is told apart -- so a
    discriminator that always answered "both" cannot pass.
    """
    text = _m8_text()
    assert _event_names(text) == set(M8_EVENTS)
    # ... and both are actually emitted.  A patch that declares the record
    # literals and never calls the port carries the event name in its source
    # and nothing on the channel, which is the same outcome as not shipping it
    # -- so the presence of the name is not the thing being asserted here.
    assert "native_console_report(launchd_record" in text, (
        "launchd.running is declared in the patch but never handed to the "
        "console port"
    )
    assert "native_m8_report_daemon(p);" in text, (
        "daemon.spawned is declared in the patch but never emitted"
    )

    # Strip the daemon hunk out and ask again.  If the discriminator were
    # vacuous -- reading the event names out of this test's own constants, say
    # -- the second assertion would still pass and the first would be the only
    # thing standing between a half-landed pair and a green build.
    launchd_only = _without_hunk(text, "native_m8_report_daemon")
    assert "native_m8_report_daemon" not in launchd_only, (
        "the daemon hunk was not stripped, so the next assertion proves nothing"
    )
    assert _event_names(launchd_only) == {"launchd.running"}, (
        "stripping the daemon hunk did not remove daemon.spawned, so the "
        "discriminator cannot tell a whole pair from half of one"
    )
    assert "daemon.spawned" in _event_names(text)


def _without_hunk(text: str, marker: str) -> str:
    """``text`` with the hunk carrying ``marker`` removed entirely.

    Used to build the half-landed pair the veto is about, so the anti-veto
    assertion above can be run against something rather than against a
    constant.
    """
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("@@"):
            body = []
            follow = index + 1
            while follow < len(lines) and not lines[follow].startswith("@@"):
                body.append(lines[follow])
                follow += 1
            if any(marker in item for item in body):
                index = follow
                continue
        out.append(line)
        index += 1
    return "\n".join(out)


def test_the_launchd_emitter_is_inside_the_success_branch():
    """The record is emitted AFTER the load succeeded, not before it.

    This is the one that would catch a record claiming a launchd that then
    failed to load.  load_init_program() tries every entry in
    ``init_programs[]`` and panics when all of them fail, so an emission placed
    before ``load_init_program_at_path()`` puts "launchd.running" on the wire
    for a binary that never ran -- and the panic() four lines below is the only
    thing that would contradict it, in the ring, on a channel the gate does not
    read.
    """
    text = _m8_text()
    call = _find(text, "load_init_program_at_path(p, (user_addr_t)scratch_addr, "
                       "init_programs[i], init_envp);")
    branch = _find_after(text, "if (!error) {", call)
    emit = _find(text, "native_console_report(launchd_record")
    returned = _find_after(text, "return;", emit)
    assert call < branch < emit < returned, (
        "the launchd record is not inside the success branch.  Order in the "
        f"patch: load call {call}, `if (!error)` {branch}, emit {emit}, "
        f"`return;` {returned}.  An emission before the load call claims a "
        "launchd that then failed, and the panic() below it is the only "
        "contradiction -- in the kernel ring, not on the guest channel"
    )


def _find(text: str, needle: str, start: int = 0) -> int:
    """The patch line index of the first added or context line containing needle."""
    lines = text.split("\n")
    for index in range(start, len(lines)):
        if needle in lines[index].lstrip("+ "):
            return index
    pytest.fail(f"{needle!r} is not in the patch, so its position is unchecked")


def _find_after(text: str, needle: str, start: int) -> int:
    return _find(text, needle, start + 1)


def _find_close(text: str, start: int) -> int:
    """The patch line index of the next line that is exactly ``}``.

    A brace, not the character: the hunk's trailing context is full of lines
    that merely contain one, and matching on the character would put the
    emitter "inside the block" on the strength of a comment.
    """
    lines = text.split("\n")
    for index in range(start + 1, len(lines)):
        if lines[index].lstrip("+ ").strip() == "}":
            return index
    pytest.fail("no closing brace after the emitter, so its nesting is unchecked")


def test_the_daemon_emitter_is_inside_the_error_free_block():
    """One call, inside ``if (!error)``, and nowhere else.

    The site is the kernel's own definition of a successful exec:
    ``exec_done = TRUE`` is set in that block, and the NOTE_EXEC notification a
    few lines below fires on the same flag.  Emitting anywhere earlier in
    ``__mac_execve`` would report daemons that failed to load; emitting after
    ``process_signature()`` would report daemons the kernel then refused.
    """
    text = _m8_text()
    branch = _find(text, "if (!error) {")
    exec_done = _find_after(text, "exec_done = TRUE;", branch)
    emit = _find(text, "native_m8_report_daemon(p);")
    close = _find_close(text, emit)
    assert branch < exec_done < emit < close, (
        "the daemon emitter is not inside the `if (!error)` block that sets "
        f"exec_done.  Order in the patch: branch {branch}, exec_done "
        f"{exec_done}, emit {emit}, close {close}"
    )
    # The hunk's own context has to carry both ends of that block.  Without the
    # closing brace in the trailing context the ordering above would hold for
    # an emitter placed anywhere in the function, which is the dead-guard
    # failure mode one level out.
    assert "}" in text.split("\n")[close], (
        "the daemon hunk's trailing context does not reach the block's closing "
        "brace, so 'inside if (!error)' cannot be told from 'after it'"
    )
    # Exactly once.  A second emission of the same event is silently dropped by
    # the host (_guest_events uses setdefault), so a reader of the log would
    # never see it -- and two emitters is the shape that hides one of them.
    assert text.count("native_m8_report_daemon(p);") == 1, (
        "the daemon emitter is called more than once; the host keeps only the "
        "first record per event name, so the rest are invisible"
    )


def test_the_m8_console_reference_is_strong_not_weak():
    """``native_console_report`` is a STRONG reference in this patch too.

    The measured defect is in offline-owner-systemos-mount.patch's own
    docstring: on build 41330622788 a ``__attribute__((weak_import))``
    reference resolved to null, both records were byte-level in the booting
    kernel, the mount succeeded, the build was green -- and the records never
    arrived.  A strong reference makes the definition load-bearing, so the
    failure is a link error naming the symbol.
    """
    text = _m8_text()
    declarations = [
        line for _, line in _added(text)
        if line.startswith("extern") and "native_console_report" in line
    ]
    assert len(declarations) == 1, (
        f"expected exactly one declaration of native_console_report, found "
        f"{len(declarations)}: {declarations}"
    )
    assert "weak_import" not in declarations[0], (
        "native_console_report is weak-imported. Measured on build "
        "41330622788: the symbol then resolves to null and the records are "
        "dropped silently, with the build green. Use a strong extern."
    )
    assert declarations[0].startswith("extern int native_console_report("), (
        f"the declaration is not the expected strong extern: {declarations[0]!r}"
    )
    # And the null check is the dead-branch shape: the call sits inside
    # `if (native_console_report) { ... }`, never as a one-line
    # `if (native_console_report) native_console_report(...)` whose whole
    # purpose is to decide whether the record is emitted.  Whether a record is
    # emitted is decided by the success branches the two ordering tests cover.
    calls = [line for _, line in _added(text)
             if line.startswith("native_console_report(")]
    assert len(calls) == 3, (
        f"expected three calls to native_console_report (two launchd arms and "
        f"the runtime daemon buffer), found {len(calls)}: {calls}"
    )
    assert sum(1 for line in calls if "launchd_record" in line) == 2
    assert sum(1 for line in calls if "record, (size_t)at" in line) == 1
    assert "if (native_console_report) native_console_report" not in text, (
        "the null check is the gate that decides whether the record is "
        "emitted; keep it as a dead branch around a call the success branch "
        "already authorised"
    )


def test_the_record_lengths_come_from_arrays_not_pointers():
    """``sizeof(record) - 1`` on an array, and the fill count on a buffer.

    The defect this guards is measured and expensive: a ternary selecting
    between two arrays of different lengths decays both to ``char *``, so
    ``sizeof(arr)`` becomes 8 and ``sizeof(arr) - 1`` is 7.  Three records were
    sent seven bytes long on a real guest, the build was green, and a
    byte-level grep of the image could not see it.
    """
    text = _m8_text()
    code = [line for _, line in _added(text)]
    assert text.count("sizeof(launchd_record) - 1") == 2, (
        "both arms of the launchd record must hand the port "
        "sizeof(launchd_record) - 1; the array form is the only thing that "
        "makes that the record's own length"
    )
    # The runtime buffer is an ARRAY, and the length handed over is its fill
    # count.  sizeof() there would be the whole 192.
    assert any(line == "char record[M8_RECORD_MAX];" for line in code), (
        "the daemon record's buffer is not declared as an array of "
        "M8_RECORD_MAX; a pointer would make any sizeof of it the word size"
    )
    assert any(line.startswith("native_console_report(record, (size_t)at)")
               for line in code), (
        "the runtime record must be handed over with its fill count, not "
        "sizeof(record) -- which would be the whole buffer"
    )
    # Only the CODE lines, never the prose: this patch's own comment explains
    # why sizeof(record) would be wrong, so a whole-text search finds the
    # explanation and reports a defect that is not there.  That is the fourth
    # time this shape has bitten this project.
    assert not any("sizeof(record)" in line for line in code), (
        "sizeof(record) on the runtime buffer is the whole 192 bytes, not the "
        "record; use the fill count"
    )


def test_the_m8_guard_is_the_switch_the_build_passes_to_kern_exec():
    """Guarded, and on the switch that actually reaches this object file.

    Two halves of one fact.  Without a guard the strong extern fails the link
    on every config that does not build the console archive -- including the
    first pass's qemu_defconfig -- so the emitter has to be conditional.  And
    the condition has to be one the kernel build passes to kern_exec.o, or the
    guard is a no-op that compiles the emitter out everywhere and drops both
    records behind a green build.
    """
    text = _m8_text()
    assert f"#if defined({M8_GUARD}) && {M8_GUARD}" in text, (
        f"the emitter is not guarded on {M8_GUARD}, so a config without the "
        "console archive fails to link -- or, worse, one where the macro is "
        "never defined compiles it out and drops both records"
    )
    # Three guarded regions -- the shared block and the two call sites -- and
    # the counts have to match.  A call site that loses its `#if` compiles the
    # emitter out of the file on every config, which is the silent half of the
    # failure: the records are in the source, byte-level absent from the
    # kernel, and nothing on the host can tell the difference.
    assert text.count(f"#if defined({M8_GUARD}) && {M8_GUARD}") == 3, (
        f"expected three {M8_GUARD} guards (the shared block and the two call "
        f"sites), found {text.count(f'#if defined({M8_GUARD}) && {M8_GUARD}')}"
    )
    assert text.count("+#endif") == 3, (
        f"expected three #endif to close them, found {text.count('+#endif')}; "
        "an unclosed guard swallows the rest of the file"
    )
    script = _m8_require_build_script()
    body = script.read_text(encoding="utf-8")
    assert "kern_exec.o_CFLAGS_ADD" in body and M8_GUARD in body, (
        f"the build script no longer passes {M8_GUARD} to kern_exec.o, so the "
        "guard compiles the emitter out of the file that carries it"
    )


def test_the_daemon_record_names_the_process_it_measured():
    """pid and comm from the proc, and NOT from the freed namei buffer.

    ``imgp->ip_ndp`` is released by ``nameidone()`` inside
    ``exec_activate_image()``, which has already returned by the time the
    emitter runs.  Reading it would be a use-after-free that happens to look
    like a working record on most boots.  ``p_comm`` is the proc's own name, it
    is what the kernel ring prints as ``name[pid]``, and
    ``set_proc_name()`` NUL-terminates it.
    """
    text = _m8_text()
    code = "\n".join(line for _, line in _added(text))
    assert "p->p_pid" in code, "the record does not name the pid it measured"
    assert "p->p_comm" in code, "the record does not name the comm it measured"
    assert "ip_ndp" not in code, (
        "the emitter reads imgp->ip_ndp, which exec_activate_image() has "
        "already released with nameidone(); use p_comm, which the kernel ring "
        "prints as name[pid]"
    )


def test_the_m8_patch_applies_to_its_own_minus_side():
    """The real ``git apply --check``, against a synthetic '-' side.

    The whole-set version of this (:func:`test_every_patch_applies_to_its_own_minus_side`)
    covers it by glob.  This one names the file, so a failure says which patch
    rather than counting.
    """
    patch = _m8_patch()
    if patch is None:
        pytest.skip(f"{M8_PATCH_NAME} is not in this pinned tree")
    proc = _run("verify_patch_applies.py", str(patch))
    assert proc.returncode == 0 and "exit: 0" in proc.stdout, (
        f"{patch.relative_to(REPO)} does not apply:\n{proc.stdout}{proc.stderr}"
    )



# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The declaration/caller pairing
# ---------------------------------------------------------------------------
