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
    ("offline-owner-systemos-mount.patch", 270),
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
