"""``UNIXNU_GPU_ADDON`` -> ``--gpu-addon``: the spelling the workflow depends on.

Why this exists
---------------
The workflow sets ``UNIXNU_GPU_ADDON`` and ``build-macos.sh`` turns it into the
``--gpu-addon`` flag.  Nothing exercised that spelling: the neighbouring
``test_payload_trust_cache.py`` sets ``GPU_ADDON_PATH`` directly, which is the
*variable*, not the *environment spelling*.  If the resolution between them
broke, the blob would be dropped, the payload would report "no GPU addon", and
that is byte-for-byte what it reports when the chain never ran -- a silent
divergence of exactly the class this script spends its length avoiding.

The region is run under a real ``bash`` for the reason the neighbouring test
gives: the empty-array expansions and the ``case`` patterns are the things most
likely to be wrong, and neither survives being read as text.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "macos" / "build-macos.sh"


def _bash() -> str:
    """Git Bash by absolute path, never the bare name.

    On Windows 11 ``System32\\bash.exe`` is the WSL launcher; if it wins, the
    script runs under WSL and dies with a mount-disk error that has nothing to
    do with the region under test.  Measured: six tests failing with
    ``HCS/ERROR_FILE_NOT_FOUND`` before this resolved the name.
    """
    found = shutil.which("bash")
    assert found is not None, "no bash on PATH; this test cannot run"
    return found

#: Everything the region needs that is not the thing under test.  The variable
#: initialisations are the ones the parse loop assigns to; ``die`` and
#: ``json_escape`` are the two functions it calls.
HARNESS_HEAD = """\
set -uo pipefail
EXIT_OK=0
EXIT_FAILURE=1
EXIT_USAGE=2
die() { printf 'DIE %s %s\\n' "$1" "$2"; exit "${4:-1}"; }
json_escape() { printf '%s' "$1"; }
"""


def _lines() -> list[str]:
    return SCRIPT.read_text(encoding="utf-8").splitlines()


def _region() -> str:
    """From the parse loop's ``while`` through the ``fi`` that closes the
    ``UNIXNU_GPU_ADDON`` resolution.

    Both ends are anchored on content rather than on line numbers, so the
    extraction survives the script growing.  The parse loop is included rather
    than stubbed because "the flag wins over the environment" is half of the
    contract and it lives in the ``case``.
    """
    lines = _lines()
    # From the environment capture, not from the parse loop: the capture is the
    # `${VAR:-}` spelling, and a region that began after it would have to invent
    # the variable and so could not see a `${VAR}` typo at all.
    start = next(
        i for i, line in enumerate(lines)
        if line.strip() == "GPU_ADDON_PATH=''"
    )
    marker = next(
        i for i, line in enumerate(lines)
        if "UNIXNU_GPU_ADDON must be a path" in line
    )
    end = next(i for i in range(marker, len(lines)) if lines[i].strip() == "fi")
    return "\n".join(lines[start:end + 1])


def _run(args: list[str], **environment: str) -> tuple[int, str]:
    """Run the region with ``args`` and the given environment overrides."""
    script = HARNESS_HEAD + textwrap.indent(_region(), "    ") + '\nprintf "RESOLVED %s\\n" "$GPU_ADDON_PATH"\n'
    env = dict(os.environ)
    env.update(environment)
    result = subprocess.run(
        [_bash(), "-s", "--", *args],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        cwd=str(REPO),
    )
    return result.returncode, result.stdout + result.stderr


def test_the_environment_spelling_becomes_the_flag_value(tmp_path):
    """The workflow's spelling reaches the variable the payload step reads."""
    addon = tmp_path / "gpu-addon.ngpu"
    addon.write_bytes(b"NGPUADD2")
    status, output = _run([], UNIXNU_GPU_ADDON=str(addon))
    assert status == 0, output
    assert f"RESOLVED {addon}" in output, output


def test_an_unset_variable_leaves_it_empty_rather_than_a_literal(tmp_path):
    """``${VAR:-}`` and not ``$VAR``: with ``set -u`` an unset reference is fatal."""
    status, output = _run([])
    assert status == 0, output
    assert "RESOLVED \n" in output, output


def test_the_flag_wins_over_the_environment(tmp_path):
    """Both spellings given: the explicit one is what the operator asked for."""
    flagged = tmp_path / "flagged.ngpu"
    from_env = tmp_path / "from-env.ngpu"
    for path in (flagged, from_env):
        path.write_bytes(b"NGPUADD2")
    status, output = _run(["--gpu-addon", str(flagged)], UNIXNU_GPU_ADDON=str(from_env))
    assert status == 0, output
    assert f"RESOLVED {flagged}" in output, output
    assert str(from_env) not in output, output


def test_the_environment_spelling_accepts_the_equals_form(tmp_path):
    """``--gpu-addon=path`` is the other spelling the parse loop documents."""
    addon = tmp_path / "eq.ngpu"
    addon.write_bytes(b"NGPUADD2")
    status, output = _run([f"--gpu-addon={addon}"])
    assert status == 0, output
    assert f"RESOLVED {addon}" in output, output


def test_the_refusal_branch_is_reachable(tmp_path):
    """Teeth: the guard must be able to say no.

    This is the assertion that found the defect.  The ``case`` that resolves the
    environment spelling ends its accept list with ``?*``, which matches every
    non-empty string, so the ``*)`` branch that calls ``die`` could never run --
    a guard that cannot fail.  The test drives a value that must be refused and
    fails while the branch is unreachable; the fix makes it reachable.
    """
    status, output = _run([], UNIXNU_GPU_ADDON="--gpu-addon")
    assert status != 0, (
        "a flag-shaped value was accepted as a path; the guard never fired"
    )
    assert "DIE config_invalid" in output, output


def test_a_plain_filename_is_still_accepted(tmp_path):
    """The guard must not become over-strict: a bare relative name is a path."""
    addon = tmp_path / "addon.ngpu"
    addon.write_bytes(b"NGPUADD2")
    status, output = _run(["--gpu-addon", "addon.ngpu"])
    assert status == 0, output
    assert "RESOLVED addon.ngpu" in output, output
