"""Compile and run the selector search the guest's scanout client uses.

The bug this covers was invisible to every source-level guard in
``tests/unit/test_qemu_metal_handoff_guard.py``.  The broken loop called
``tunnel``, used its return value, and compared a value to zero -- a guard
reading the source sees all three and passes.  What separates the two versions
is running the loop with a probe that admits different selectors, so that is
what this does: it compiles ``QemuMetalSelector.h`` with the host's own C++
compiler and executes the three cases the fix has to get right.

Why the logic is in a header of plain C++ at all: the search runs inside
backboardd on a macOS guest, in a file that needs IOKit and Mach types and
cannot be built here.  Extracting it into something with no platform
dependencies is what makes "test the behaviour, not the text" possible for it.

Run from the repository root::

    python scripts/windows/test-selector-logic.py

Exit status is 0 when every case passes, 1 when a case fails, and 2 when the
toolchain is missing -- 2 rather than 0, because "could not run" is not
"passed", and a suite that quietly skips the one test that would have caught
the bug is how the bug survived in the first place.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CASES = Path(__file__).resolve().parent / "selector-cases.cpp"
#: The kit directory, which is what ``/I`` wants.
HEADER_DIR = REPO / "vendor" / "UniXNU-qemu" / "package" / "QemuMetal"
HEADER = HEADER_DIR / "QemuMetalSelector.h"
VCVARS = Path(
    r"C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools"
    r"\VC\Auxiliary\Build\vcvars64.bat"
)

#: The MSVC installation this was developed against.  Other versions are found
#: by globbing rather than assumed, because the BuildTools directory is
#: versioned and this host has upgraded it at least once.
VCVARS_GLOB = r"C:\Program Files (x86)\Microsoft Visual Studio\*\*\VC\Auxiliary\Build\vcvars64.bat"


def find_vcvars() -> Path | None:
    """``vcvars64.bat`` for some installed MSVC, or None.

    Searched by glob rather than pinned to one version: the toolchain on this
    host is a moving target, and a script that only works against the build it
    was written on stops working silently at the next upgrade.
    """
    import glob

    if VCVARS.is_file():
        return VCVARS
    for pattern in (VCVARS_GLOB,):
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return Path(matches[0])
    return None


def compile_and_run(vcvars: Path, scratch: Path) -> tuple[int, str]:
    """Build the cases against the header and run them.

    The build goes through a generated ``build.cmd`` rather than a command
    string, and that is not a style choice.  ``subprocess.run`` with a *list*
    on Windows re-quotes every argument through ``list2cmdline``, and a command
    string that already contains quotes gets mangled: the first version of this
    function passed the chain inline and got exit status 1 with empty stdout,
    empty stderr and no executable -- which is indistinguishable from a
    compiler that crashed.  A one-line batch file has no argument to re-quote.

    Two details of what the batch file does are load-bearing:

    * ``call`` on ``vcvars64.bat``.  Running it as a child would leave the
      environment it sets up in that child, and ``cl`` would fail with
      ``C1083: cannot open include file: cstdio``.
    * ``cd /d %~dp0`` rather than relying on the caller's working directory,
      so the scratch directory can be anywhere.
    """
    executable = scratch / "selector-cases.exe"
    script = scratch / "build.cmd"
    script.write_text(
        "@echo off\r\n"
        f'call "{vcvars}" >nul 2>&1\r\n'
        "if errorlevel 1 exit /b 3\r\n"
        "cd /d %~dp0\r\n"
        f"cl /nologo /EHsc /std:c++17 /Fe:{executable.name} {CASES.name} "
        f'/I{HEADER_DIR}\r\n'
        "if errorlevel 1 exit /b 4\r\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        # The absolute path, not the bare name: ``cmd /c`` searches PATH for a
        # command word, and does not look in the working directory first.
        ["cmd", "/c", str(script)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(scratch),
    )
    if built.returncode != 0 or not executable.is_file():
        return 2, (built.stdout + built.stderr).strip()[-2000:]
    ran = subprocess.run(
        [str(executable)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(scratch),
    )
    return ran.returncode, (ran.stdout + ran.stderr).strip()


def main() -> int:
    if not CASES.is_file():
        print(f"REFUSED: {CASES} is missing")
        return 2
    if not HEADER.is_file():
        print(f"REFUSED: {HEADER} is missing")
        return 2
    vcvars = find_vcvars()
    if vcvars is None:
        print(
            "SKIPPED: no MSVC found under "
            r"C:\Program Files (x86)\Microsoft Visual Studio"
        )
        print("  (this is not a pass; the selector cases were not executed)")
        return 2
    with tempfile.TemporaryDirectory(prefix="unipad-selector-") as raw:
        scratch = Path(raw)
        shutil.copyfile(CASES, scratch / CASES.name)
        status, output = compile_and_run(vcvars, scratch)
    print(output)
    if status == 0:
        print(f"\nall selector cases pass (compiled with {vcvars})")
        return 0
    if status == 2:
        # 2 from compile_and_run means the build itself failed, which is a
        # different failure from a case going red and is reported as one.
        print(f"\nBUILD FAILED with {vcvars}")
        return 2
    print("\nthe selector search does not behave as the fix requires")
    return 1


if __name__ == "__main__":
    sys.exit(main())
