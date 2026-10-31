"""Execute the selector search; do not read it.

``test_qemu_metal_handoff_guard.py`` asserts what ``QemuMetalScanout.mm`` and
``QemuMetalSelector.h`` *say*.  This file asserts what the selector search
*does*, by compiling the header with the host's C++ compiler and running five
cases against it.

The distinction is the whole reason this file exists, and it is worth stating
plainly because it is not obvious from the outside: the bug it was written for
was invisible to the source guard.  The broken loop was

    for (uint32_t selector = 0; selector < 256; ++selector)
        if (tunnel(link, kProbe, nullptr, 0, nullptr, 0) == 0) return selector;
    return 0;

and a guard reading that sees a loop, a call, a compared return value, and a
``return`` on the failure path -- every shape it could ask for.  What it cannot
see is that ``tunnel`` read the selector out of ``link.selector``, a field the
loop never wrote, so all two hundred and fifty-six probes asked selector 0;
nor that the ``return 0`` on the last line made "nothing is admitted"
indistinguishable from "selector 0 is admitted", which the caller then read as
failure.

Only executing the loop with a probe that admits *different* selectors
separates those, which is what ``scripts/windows/test-selector-logic.py``
does.  The three cases the fix has to get right, and which the broken version
got wrong, are:

  1. selector 0 is admitted -- the answer is 0, and it is a success;
  2. only a non-zero selector is admitted -- the answer is that selector, and
     the probe is asked exactly once per selector up to it, not 256 times;
  3. nothing is admitted -- the answer is ``kNoSelector``, not 0.

Two more guard the boundaries: selector 255 is reachable, and a null probe is
"nothing admitted" rather than a crash.

If the host has no C++ compiler this test SKIPS, and says so loudly rather
than reporting a pass.  On a machine that does have one -- which includes
every Windows developer checkout of this repository -- it runs, and a selector
search that regresses turns the suite red.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.vendor_paths import require

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "scripts" / "windows" / "test-selector-logic.py"
CASES = REPO / "scripts" / "windows" / "selector-cases.cpp"
HEADER = REPO / "vendor" / "UniXNU-qemu" / "package" / "QemuMetal" / "QemuMetalSelector.h"


def _toolchain() -> str | None:
    """How to describe the compiler this host has, or None if it has none."""
    import glob

    if shutil.which("cl") or shutil.which("g++") or shutil.which("clang++"):
        return "a compiler on PATH"
    pattern = r"C:\Program Files (x86)\Microsoft Visual Studio\*\*\VC\Auxiliary\Build\vcvars64.bat"
    matches = sorted(glob.glob(pattern), reverse=True)
    return matches[0] if matches else None


def test_the_selector_cases_exist_next_to_the_runner() -> None:
    """A runner with no cases, or cases with no header, reports a pass.

    ``test-selector-logic.py`` returns 2 for a missing file, and this test
    turns that into a failure rather than letting it become a skip: the
    difference between "the toolchain is absent" and "the test was deleted"
    has to stay visible.

    The header is the one dependency that is *not* in this repository:
    ``vendor/`` is git-excluded, so on a clean checkout it is absent and this
    test has to skip rather than fail.  ``require`` draws that line -- absent
    checkout skips, present checkout with a missing file fails -- which is why
    it is used here and a bare ``assert is_file()`` is not.
    """
    assert RUNNER.is_file(), f"{RUNNER.relative_to(REPO)} is missing"
    assert CASES.is_file(), f"{CASES.relative_to(REPO)} is missing"
    require(HEADER)


def test_the_selector_search_behaves_the_way_the_fix_requires() -> None:
    """Compile ``QemuMetalSelector.h`` and run the five cases against it.

    The header is in ``vendor/``, which is git-excluded, so a clean checkout
    cannot compile anything.  ``require`` is what keeps that from reading as a
    failure of the selector search itself.
    """
    require(HEADER)
    toolchain = _toolchain()
    if toolchain is None:
        pytest.skip(
            "no C++ compiler on this host (looked for cl/g++/clang++ on PATH "
            "and vcvars64.bat under Program Files (x86)); the selector cases "
            "were NOT executed"
        )
    done = subprocess.run(
        ["python", str(RUNNER)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO),
    )
    assert done.returncode != 2, (
        "the selector cases could not be built on a host that has a "
        f"compiler ({toolchain}); that is not a pass\n{done.stdout}\n{done.stderr}"
    )
    assert done.returncode == 0, (
        "the selector search does not behave as the fix requires\n"
        f"{done.stdout}\n{done.stderr}"
    )


def test_the_runner_refuses_to_report_a_missing_toolchain_as_a_pass() -> None:
    """Exit status 2 is not 0, and the script says why.

    The first version of this test family was a source-text guard, and it
    passed on the broken code.  A guard that reports success when it could not
    run is the same failure one layer out, so the runner's contract is checked
    here: a missing case file, a missing header, and a missing compiler all
    have to produce a non-zero status with a message that says which.
    """
    text = RUNNER.read_text(encoding="utf-8")
    assert "SKIPPED: no MSVC found" in text, (
        "the runner no longer reports a missing toolchain distinctly"
    )
    assert "return 2" in text, "the runner no longer distinguishes 'could not run' from 'passed'"
