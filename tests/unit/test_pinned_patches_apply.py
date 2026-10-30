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
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHARED = REPO / "scripts" / "shared"
PATCH_DIR = REPO / "vendor" / "UniXNU" / "patches"


def _patches() -> list[Path]:
    if not PATCH_DIR.is_dir():
        pytest.skip(f"{PATCH_DIR} is not present; the pinned vendor tree is not acquired")
    found = sorted(PATCH_DIR.rglob("*.patch"))
    assert found, f"no .patch file anywhere under {PATCH_DIR}"
    return found


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SHARED / script), *args],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )


def test_every_hunks_declared_counts_match_what_it_carries():
    """The cheap check, and the one that catches a header left behind by an edit."""
    patches = _patches()
    proc = _run("check_patch_hunks.py", *[str(p) for p in patches])
    assert proc.returncode == 0, (
        f"{len(patches)} patches checked and these are malformed:\n"
        f"{proc.stdout}{proc.stderr}"
    )
    assert "every hunk's counts match its header" in proc.stdout, proc.stdout


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
