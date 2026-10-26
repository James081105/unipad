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


def _git_tracks() -> set[Path] | None:
    """Every ``*.sh`` git tracks, or ``None`` when git cannot answer.

    The repository is what git tracks, and that is the definition this guard
    wants: a script nobody committed is not a script we ship.

    The alternative -- excluding scratch directories by *name* -- is what this
    function replaces, and it failed the way every name-based exclusion
    eventually fails.  Measured 2026-10-25: an agent's scratch worktree root
    holding eight copies of ``build-macos.sh`` reddened this test on a tree
    whose committed content was entirely fine.  ``vendor/`` and
    ``local-private/`` are excluded for exactly the reason this docstring
    gives, and this was the same shape arriving through a fourth door: the
    next agent picks the next directory name, and the guard reddens again for
    a change nobody made.

    ``None`` rather than an empty set when git is unavailable, so the caller
    can fall back instead of concluding the repository ships no scripts at all
    -- which would turn a missing git into a silently vacuous guard.
    """
    git = shutil.which("git")
    if git is None:
        return None
    result = subprocess.run(
        [git, "ls-files", "-z", "*.sh"],
        capture_output=True,
        cwd=ROOT,
    )
    if result.returncode != 0:
        return None
    return {
        (ROOT / name.decode("utf-8")).resolve()
        for name in result.stdout.split(b"\0")
        if name
    }


def test_the_guard_covers_every_shell_script() -> None:
    """A new script that no test names is a script nobody has parsed.

    The glob is ``scripts/**/*.sh``; this asserts the repository has not grown
    a shell script somewhere the glob cannot see, which is the only way this
    file can silently stop covering something.

    "The repository has grown one" means *git tracks it*, which is what
    :func:`_git_tracks` answers.  Two directories hold shell scripts that are
    deliberately not ours and would otherwise be parsed: ``vendor/`` holds the
    *fetched* upstream repositories at the commits ``config/sources.lock.json``
    pins -- their content changes whenever a pin moves, so parsing them would
    redden this guard for a reason no change here caused -- and
    ``local-private/`` is gitignored host material.  Neither is in git's index,
    so neither is in ``on_disk``.

    What this means, stated plainly: the guard covers the scripts *we* ship.  A
    pin bump that breaks an upstream script is the build step's problem, and it
    reports it as a build failure with the script named, not as a coverage gap
    here.  Scratch in the working tree is nobody's script either, and the
    git-tracked definition is what keeps an agent's worktree from reddening a
    guard over content that was never committed -- measured 2026-10-25, eight
    copies of ``build-macos.sh`` under one agent's scratch root.
    """
    tracked = _git_tracks()
    if tracked is not None:
        on_disk = tracked
    else:
        # git could not answer, so fall back to naming what is not ours.  This
        # is the weaker definition and it is why the fallback exists at all:
        # without it a host with no git on PATH would run a guard that covers
        # nothing and reports success.
        on_disk = {
            path
            for path in ROOT.rglob("*.sh")
            if ".git" not in path.parts
            and "local-private" not in path.parts
            and "vendor" not in path.parts
        }
    named = set(SHELL_SCRIPTS)
    assert on_disk == named, (
        "shell scripts exist that this module does not check: "
        f"{sorted(p.relative_to(ROOT).as_posix() for p in on_disk - named)}"
    )


# ---------------------------------------------------------------------------
# portability: constructs bash -n accepts but macOS's bash 3.2 does not
# ---------------------------------------------------------------------------
def test_no_heredoc_inside_a_process_substitution() -> None:
    """``bash -n`` here is bash 5; the runner's is Apple's bash 3.2.

    ``scripts/macos/build-macos.sh`` staged its material delivery with

        done < <(run_unipad - "$manifest" <<'PY'
        ...
        PY
        )

    and ``bash -n`` on this host accepts it, because this host's bash is 5.3.
    macOS ships /bin/bash 3.2, which refuses it -- measured on run 40386064322,
    the first run in this repository's history to reach that line at all, which
    died with

        bad substitution: no closing `)' in <(run_unipad - "$manifest" <<'PY'

    immediately after collecting a complete manifest.  The construct is the only
    one of its kind in a 4,000-line script: the ten other ``done < <(...)`` all
    call a shell *function*, and none of them carries a here-document.  So it had
    never been parsed by the interpreter that would run it -- every earlier run
    refused in the Python half of the stage, one stage earlier, and the shell
    half was never executed even once.

    That is the shape this guard is for.  ``bash -n`` proves the file parses
    under *some* bash; it does not prove it parses under the bash that will run
    it, and a syntax error in a construct nobody has executed is invisible to
    every other test in this repository.

    The rule is narrow on purpose: a here-document anywhere in a process
    substitution, in any script we ship.  It does not forbid process
    substitutions, and it does not forbid here-documents -- ten of the first and
    several of the second are load-bearing elsewhere.
    """
    offenders: list[str] = []
    for script in SHELL_SCRIPTS:
        text = script.read_text(encoding="utf-8")
        # Full-line comments are dropped first.  The stage that carries the fix
        # documents the construct it removed, so a scanner that reads comments
        # reports the documentation as the defect -- which is a guard failing on
        # its own prose, and a guard nobody trusts is a guard nobody keeps.
        # Stripping whole lines only, not trailing comments, is the honest
        # limit: an inline `# ... <(` after code is rare enough that missing it
        # costs a false negative, and a quote-aware comment stripper costs more
        # than the check is worth.
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        for opening in range(len(code)):
            if code[opening : opening + 2] != "<(":
                continue
            depth = 1
            at = opening + 2
            while at < len(code) and depth:
                if code[at] == "(":
                    depth += 1
                elif code[at] == ")":
                    depth -= 1
                at += 1
            body = code[opening + 2 : at - 1]
            if "<<" in body:
                offenders.append(
                    f"{script.relative_to(ROOT).as_posix()}: {body.splitlines()[0][:60]!r}"
                )
    assert not offenders, (
        "a here-document inside a process substitution is refused by macOS's "
        "bash 3.2; write it to a temp file and redirect the loop from that "
        f"instead: {offenders}"
    )
