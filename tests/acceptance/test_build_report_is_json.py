"""``write_outputs`` must emit JSON that actually parses.

Why this file exists
--------------------
``build-macos.sh`` writes three JSON documents.  Only one of them was ever
checked.  ``artifact-manifest.json`` is written by Python (``write_manifest``),
which cannot emit a malformed document, and it was checked.  The other two --
``build-report.json`` and ``stage-result.json`` -- are hand-printed from two
dozen ``printf`` lines, each emitting one fragment, and nothing verified the
result.

Measured, on the real script, before the fix: the line

    printf '    "unfrozen_build": %s,\n' \\
        "$(( LOCK_UNFROZEN )) && printf 'true' || printf 'false'"

evaluates the arithmetic expansion inside the double quotes and leaves the rest
of the argument as literal text, so the file carried::

    "unfrozen_build": 0 && printf 'true' || printf 'false',

which is not JSON at all.  ``json.load`` on the downloaded artifact's report
raised ``Expecting value: line 1, column 1 (char 0)``.  Nothing in the pipeline
caught it -- the one JSON check in the script was pointed at the one file
Python wrote -- so a macOS runner would have spent its minutes, published a
corrupt report, and exited 0.

The two things that were wrong
------------------------------
1. The quoting on that one line (fixed: the whole conditional is now inside a
   single command substitution, matching the working ``frozen`` line above it).
2. The missing check.  Fixing only the line leaves the class of bug alive: any
   future ``printf`` fragment with a quoting mistake ships silently.  So the
   script now runs ``assert_json_file`` over all three of its JSON outputs, and
   this file proves that guard fires.

How the real function is reached
--------------------------------
``build-macos.sh`` parses its arguments and resolves its paths in top-level
code, and ends in an unguarded ``main "$@"`` -- so it can neither be sourced
as-is nor have ``main`` stubbed from outside.  The harness therefore takes the
file **verbatim**, drops the shebang and that final line, and wraps it: real
argv in front (the parser is top-level code, so an empty argv is a usage
refusal), and after the body the calls ``main()`` itself would have made before
``write_outputs`` -- ``prepare_output_dir`` and ``validate_lock``, the real
ones, so the output paths and the two lock variables come from where they
actually come from.  Everything executed is the shipped code, byte for byte.
This is not a copy of the logic maintained beside the script.

What is proven here
-------------------
That a real run of ``write_outputs`` produces two parseable reports carrying
``unfrozen_build`` as a JSON boolean, in both lock states; and that the
``assert_json_file`` guard refuses a corrupt report instead of publishing it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"
REAL_LOCK = ROOT / "config" / "sources.lock.json"

pytestmark = pytest.mark.acceptance

#: The two repository pins the lock carries.  Full SHAs, exactly as the real
#: lock has them, so ``validate_lock`` accepts the copy the harness writes.
UNIXNU_COMMIT = "b881ac7dc1695ce676c856873051a487c6a9f963"
DEVICE_COMMIT = "97b75b1f63c6b7e895cd7646029db741d5fe5488"


def _bash() -> str:
    for candidate in ("bash", "bash.exe"):
        located = shutil.which(candidate)
        if located:
            return located
    pytest.skip("no bash on this host")


def _write_lock(tmp_path: Path, frozen: bool) -> Path:
    payload = json.loads(REAL_LOCK.read_text(encoding="utf-8"))
    payload["frozen"] = frozen
    payload.setdefault("unixnu", {})["commit"] = UNIXNU_COMMIT
    payload.setdefault("device_repo", {})["commit"] = DEVICE_COMMIT
    path = tmp_path / "sources.lock.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


#: The defect as it shipped: the arithmetic expansion inside the double quotes,
#: with the rest of the conditional left as literal text.
BROKEN_UNFROZEN = (
    '        printf \'    "unfrozen_build": %s,\\n\' \\\n'
    '            "$(( LOCK_UNFROZEN )) && printf \'true\' || printf \'false\'"'
)


def _restore_the_defect(body: str) -> tuple[str, int]:
    """Put the historical quoting back, and report how many sites were changed."""
    pattern = re.compile(
        r"        printf '    \"unfrozen_build\": %s,\\n' \\\n"
        r'            "\$\(\[\[ "\$LOCK_UNFROZEN" == \'1\' \]\] && printf \'true\' \|\| printf \'false\'\)"'
    )
    return pattern.subn(BROKEN_UNFROZEN, body, count=1)


def _build_harness(tmp_path: Path, frozen: bool, break_unfrozen_line: bool = False) -> Path:
    """The real script with a prelude, and ``write_outputs`` instead of ``main``.

    The script resolves its repository root from ``../..`` above its own
    location and refuses to run without ``src/unipad`` there, so the harness is
    laid out as ``<tmp>/repo/scripts/macos/harness.sh`` with a ``src/unipad``
    directory beside it.  Everything below that is the shipped script: its
    argument parser, its path resolution, ``validate_lock`` and
    ``write_outputs`` itself, byte for byte.

    ``break_unfrozen_line`` restores the historical defect -- the one line,
    spelled the way it was spelled when it shipped -- so the guard can be shown
    to catch the thing it was added for.  Everything else stays verbatim.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].startswith("#!"), lines[0]
    assert lines[-1].strip() == 'main "$@"', (
        "build-macos.sh no longer ends in an unguarded main call, so this "
        f"harness's verbatim-body assumption is stale (last line: {lines[-1]!r})"
    )
    body = "\n".join(lines[1:-1])

    if break_unfrozen_line:
        body, count = _restore_the_defect(body)
        assert count == 1, (
            "expected exactly one unfrozen_build printf to substitute, found "
            f"{count}; the harness would not be testing the real defect"
        )

    repo = tmp_path / "repo"
    here = repo / "scripts" / "macos"
    here.mkdir(parents=True, exist_ok=True)
    (repo / "src" / "unipad").mkdir(parents=True, exist_ok=True)

    out = (tmp_path / "out").resolve()
    lock = _write_lock(tmp_path, frozen)
    harness = here / "harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        # Real argv, because the parser and the path resolution are top-level
        # code and an empty argv is a usage refusal.  This must come before the
        # body; everything below must come after it, because the functions it
        # calls are defined in the body.
        "set -- --stage M0 --experiment gate-probe"
        f" --out {json.dumps(out.as_posix(), ensure_ascii=False)}"
        f" --lock {json.dumps(lock.as_posix(), ensure_ascii=False)}"
        " --run-id gate-probe\n"
        f"{body}\n"
        # --- what main() would have done before calling write_outputs --------
        # main() declares these three as locals before it calls
        # write_outputs; they are the function's contract, so the harness
        # supplies the same empty arrays rather than a different shape.
        "inc_paths=(); inc_roles=(); inc_archs=()\n"
        # require_tools() refuses outright on a non-macOS host (it exists to
        # fail before a single Apple tool is invoked), so its tool-identity
        # strings are stood in for here.  They reach build-report.json verbatim,
        # which is what the assertions below read back.
        "TOOL_XCODEBUILD='harness-stand-in'\n"
        "TOOL_CLANG='harness-stand-in'\n"
        "TOOL_PYTHON=\"$PY --version 2>&1 | head -1\"\n"
        "TOOL_GIT='harness-stand-in'\n"
        # prepare_output_dir creates the directory and derives the three output
        # paths; calling the real one keeps the harness honest about where they
        # come from (DRY_RUN is 0 by default, so it does create it).
        "prepare_output_dir\n"
        # validate_lock is what reads the lock and derives LOCK_FROZEN and
        # LOCK_UNFROZEN; calling the real one keeps the harness honest about
        # where those two values come from.
        "validate_lock\n"
        "write_outputs 1 0\n",
        encoding="utf-8",
    )
    return harness


def _run(tmp_path: Path, frozen: bool = False) -> tuple[subprocess.CompletedProcess, Path]:
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    harness = _build_harness(tmp_path, frozen)
    result = subprocess.run(
        [_bash(), harness.as_posix()],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=180,
    )
    return result, out


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_build_report_parses_and_carries_unfrozen_build(tmp_path: Path) -> None:
    """The regression itself: the report must be JSON, with a real boolean.

    Before the fix this raised ``json.JSONDecodeError`` on the fragment
    ``"unfrozen_build": 0 && printf 'true' || printf 'false',``.  A test that
    only asserted "the file exists" would have passed; a test that asserted
    ``"unfrozen_build" in text`` would also have passed, because the corrupt
    fragment contains the key.  Only parsing it catches the defect.
    """
    result, out = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    report_path = out / "build-report.json"
    assert report_path.is_file(), result.stdout + result.stderr
    report = _load(report_path)  # raises if the document is not JSON

    lock = report["lock"]
    assert lock["frozen"] is False, lock
    assert lock["unfrozen_build"] is True, (
        "unfrozen_build is not a JSON boolean, so a downstream consumer reading "
        f"it gets a string or a fragment: {lock['unfrozen_build']!r}"
    )
    assert report["run_id"] == "gate-probe", report
    assert report["stage"] == "M0", report


def test_a_frozen_lock_reports_a_frozen_build(tmp_path: Path) -> None:
    """The other half of the boolean: a frozen lock must not claim otherwise.

    Without this the fix could be "always print true", which would make every
    run look unfrozen -- the exact failure the field exists to prevent, in the
    opposite direction.
    """
    result, out = _run(tmp_path, frozen=True)
    assert result.returncode == 0, result.stdout + result.stderr
    report = _load(out / "build-report.json")
    assert report["lock"]["frozen"] is True, report["lock"]
    assert report["lock"]["unfrozen_build"] is False, report["lock"]


def test_the_stage_result_also_parses(tmp_path: Path) -> None:
    """``stage-result.json`` is the second hand-printed document."""
    result, out = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    stage = _load(out / "stage-result.json")
    assert stage["stage"] == "M0", stage
    assert isinstance(stage["ok"], bool), stage
    assert isinstance(stage["steps"], list), stage


def test_a_corrupt_report_is_refused_not_published(tmp_path: Path) -> None:
    """The guard must fire on the defect that actually shipped.

    The historical quoting is restored in the harness body -- that one line
    only, the rest of the script verbatim -- so what runs is the real
    ``write_outputs`` producing the real corrupt fragment.  The run must then
    fail, name the file, and exit non-zero.  Before the guard existed this run
    exited 0 with a report no consumer could parse.
    """
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    harness = _build_harness(tmp_path, frozen=False, break_unfrozen_line=True)
    result = subprocess.run(
        [_bash(), harness.as_posix()],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert result.returncode != 0, (
        "a build report that is not valid JSON was published and the run still "
        f"succeeded:\n{result.stdout}\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "build_report_invalid" in combined, combined
    assert "build-report.json" in combined, combined
