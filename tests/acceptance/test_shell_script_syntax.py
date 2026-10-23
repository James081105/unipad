"""``bash -n`` over every shell script in the repository.

Why this file exists
--------------------
``scripts/macos/build-macos.sh`` shipped with a missing closing single quote on
one ``printf`` line.  Bash reported it as "unexpected end of file from `{'
command on line 1571" -- 74 lines and one function away from the actual defect,
because an unterminated ``'`` swallows every ``}`` after it, including the one
that closes the function.  Nothing in the suite ran ``bash -n``, so the script
that PRD 8.2 hands to the macOS runner was a file that could not be parsed at
all, and the only thing standing between that and a failed workflow was someone
happening to run ``bash -n`` by hand.

That is the whole point of this file: the cheapest possible check, run by the
cheapest possible runner, on every shell script the repository ships.  A syntax
error is the one class of defect that never needs a macOS host, a QEMU instance
or a network to find.

What it deliberately does not do
--------------------------------
It does not *run* the scripts.  ``build-macos.sh`` needs ``xcodebuild`` and the
upstream driver; ``prepare-image.sh`` needs a mounted image.  Neither is
available on a Windows code-stage host, and pretending otherwise would be the
"CODE COMPLETE is a field PASS" failure CLAUDE.md forbids.  Parse only.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"

pytestmark = pytest.mark.acceptance

#: Every shell script the repository ships, sorted so a failure list is stable.
SHELL_SCRIPTS = tuple(sorted(SCRIPTS_DIR.rglob("*.sh")))

#: A fragment that must parse, and one that must not.  Asking for both is what
#: makes the interpreter check mean something: a ``bash`` that answers 0 to
#: everything (a shim, a stub, a broken MSYS binary) passes the first and fails
#: the second, so it is rejected rather than silently trusted.  They go through
#: ``-c`` rather than ``/dev/stdin``: Git Bash on Windows cannot open
#: ``/dev/stdin`` as a path argument, which would reject every candidate.
_MUST_PARSE = "true() { :; }"
_MUST_NOT_PARSE = "true() { :;"


def _candidate_bashes() -> list[str]:
    """Where a usable bash might live, most trustworthy first."""
    found: list[str] = []
    for name in ("bash", "bash.exe"):
        located = shutil.which(name)
        if located:
            found.append(located)
    for extra in (
        "C:/Program Files/Git/bin/bash.exe",
        "C:/Program Files (x86)/Git/bin/bash.exe",
    ):
        if Path(extra).is_file():
            found.append(extra)
    return found


def _bash() -> str:
    """A bash that can actually tell a parseable script from an unparseable one."""
    if not SHELL_SCRIPTS:
        pytest.skip("the repository ships no shell scripts")
    for candidate in _candidate_bashes():
        good = subprocess.run(
            [candidate, "-n", "-c", _MUST_PARSE], capture_output=True, text=True
        )
        bad = subprocess.run(
            [candidate, "-n", "-c", _MUST_NOT_PARSE], capture_output=True, text=True
        )
        if good.returncode == 0 and bad.returncode != 0:
            return candidate
    pytest.skip(
        "no bash on this host can distinguish a parseable script from an "
        "unparseable one, so the check would prove nothing"
    )


@pytest.mark.parametrize(
    "script",
    SHELL_SCRIPTS,
    ids=lambda path: path.relative_to(ROOT).as_posix(),
)
def test_the_shell_script_parses(script: Path) -> None:
    """``bash -n`` must accept it: an unparseable script cannot run anywhere."""
    bash = _bash()
    result = subprocess.run(
        [bash, "-n", script.as_posix()], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"{script.relative_to(ROOT).as_posix()} does not parse:\n"
        f"{result.stderr.strip()}\n"
        "An unterminated quote reports the line of the construct it swallows, "
        "not its own: count the quotes on the reported line and every line "
        "after it."
    )


def test_the_guard_covers_every_shell_script() -> None:
    """A new script that no test names is a script nobody has parsed.

    The glob is ``scripts/**/*.sh``; this asserts the repository has not grown
    a shell script somewhere the glob cannot see, which is the only way this
    file can silently stop covering something.
    """
    on_disk = {path for path in ROOT.rglob("*.sh") if ".git" not in path.parts}
    on_disk -= {path for path in on_disk if "local-private" in path.parts}
    named = set(SHELL_SCRIPTS)
    assert on_disk == named, (
        "shell scripts exist that this module does not check: "
        f"{sorted(p.relative_to(ROOT).as_posix() for p in on_disk - named)}"
    )
