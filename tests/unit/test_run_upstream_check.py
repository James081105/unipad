"""Unit tests for scripts/macos/run-upstream-check.py.

Why these exist
---------------
The wrapper exists to skip exactly one upstream step whose script file the
pinned tree does not contain (``check-quartzcore-turnip-cache-host.py``, called
from ``check-turnip-darwin-host.py:93``).  A wrapper whose job is to make a
failing step not fail is one bug away from making *every* step not fail, so the
tests here are mostly about what must still fail: a real subprocess error, the
upstream script's own ``SystemExit``, and an absent path that was not named on
the command line.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WRAPPER = REPO / "scripts" / "macos" / "run-upstream-check.py"
ABSENT = Path("does-not-exist-upstream") / "check-something-host.py"


def _upstream(tmp_path: Path, name: str, body: str) -> Path:
    """Write a stand-in for an upstream check script and return its path."""
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return script


# A stand-in that exercises all three shapes of subprocess call: one that
# succeeds, one to an absent path, and one that exits nonzero.
ALL_SHAPES = """
import subprocess, sys
ok = subprocess.run([sys.executable, '-c', 'print("real-ran")'], check=True)
print('after-real', ok.returncode)
subprocess.run([sys.executable, %r], check=True)
print('after-absent')
"""


def _run(script: Path, *tolerated: str):
    return subprocess.run(
        [sys.executable, str(WRAPPER), str(script), *tolerated],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_a_real_subprocess_still_runs_and_its_output_survives(tmp_path):
    """The passthrough must be a passthrough: output, not just exit status."""
    script = _upstream(tmp_path, "upstream_ok.py", ALL_SHAPES % str(ABSENT))
    result = _run(script, str(ABSENT))
    assert result.returncode == 0, result.stderr
    assert "real-ran" in result.stdout, result.stdout
    assert "after-real 0" in result.stdout, result.stdout
    assert "after-absent" in result.stdout, result.stdout


def test_the_skipped_step_is_announced_with_its_exact_path(tmp_path):
    """A silent skip is the failure mode this wrapper must never have.

    The whole justification for skipping an upstream check is that a reader of
    the build log can see it was skipped.  If the announcement is missing the
    run looks like a full pass.  The announced path is the *resolved* one, so an
    operator reading the log learns which absolute file was missing rather than
    having to re-resolve a relative spelling.
    """
    script = _upstream(tmp_path, "upstream_skip.py", ALL_SHAPES % str(ABSENT))
    result = _run(script, str(ABSENT))
    assert result.returncode == 0, result.stderr
    assert "SKIPPED" in result.stdout, result.stdout
    assert ABSENT.resolve().as_posix() in result.stdout.replace("\\", "/"), result.stdout


def test_a_subprocess_that_fails_still_fails_the_run(tmp_path):
    """Teeth: the wrapper may not launder a real failure into a success.

    This is the assertion that would go red if the guard were widened from
    "an exact path I named" to "anything that looks missing".
    """
    body = (
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'import sys; sys.exit(3)'], check=True)\n"
    )
    script = _upstream(tmp_path, "upstream_bad.py", body)
    result = _run(script, str(ABSENT))
    assert result.returncode != 0, f"a failing subprocess was laundered: {result.stdout!r}"


def test_the_upstream_script_s_own_exit_status_is_preserved(tmp_path):
    """``sys.exit(7)`` inside the upstream script must come out as 7."""
    script = _upstream(tmp_path, "upstream_exit.py", "import sys\nsys.exit(7)\n")
    result = _run(script, str(ABSENT))
    assert result.returncode == 7, (result.returncode, result.stdout, result.stderr)


def test_a_message_exit_is_reported_rather_than_swallowed(tmp_path):
    """``sys.exit('why')`` is a string, not a status; it must still be non-zero."""
    script = _upstream(tmp_path, "upstream_msg.py", "import sys\nsys.exit('the reason')\n")
    result = _run(script, str(ABSENT))
    assert result.returncode == 1, (result.returncode, result.stdout)
    assert "the reason" in result.stderr, result.stderr


def test_only_the_named_path_is_tolerated(tmp_path):
    """Teeth: a *different* absent path must not be skipped.

    If the guard matched by pattern instead of by exact resolved path, this is
    the test that catches it -- and it is the difference between "we skipped the
    one step we know is missing" and "we skip whatever happens to be missing".
    """
    other = Path("some-other-missing") / "check-other-host.py"
    script = _upstream(tmp_path, "upstream_other.py", ALL_SHAPES % str(other))
    result = _run(script, str(ABSENT))
    assert result.returncode != 0, f"an unnamed absent path was tolerated: {result.stdout!r}"


def test_the_wrapper_refuses_to_run_without_a_tolerated_path(tmp_path):
    """With nothing to tolerate there is no reason to interpose at all."""
    script = _upstream(tmp_path, "upstream_none.py", "print('ran')\n")
    result = subprocess.run(
        [sys.executable, str(WRAPPER), str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode != 0
    assert "usage" in (result.stdout + result.stderr).lower()


def test_a_missing_upstream_script_is_reported_not_imported(tmp_path):
    """A typo in the script path must be a clear error, not a traceback."""
    result = subprocess.run(
        [sys.executable, str(WRAPPER), str(tmp_path / "nope.py"), str(ABSENT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode != 0
    assert "no such upstream script" in (result.stdout + result.stderr)


@pytest.mark.parametrize("forwarded", [[], ["--flag", "value"]])
def test_arguments_after_the_separator_reach_the_upstream_script(tmp_path, forwarded):
    """``-- <args>`` must arrive in ``sys.argv``, or a flag-taking check breaks."""
    body = "import sys\nprint('ARGV', sys.argv[1:])\n"
    script = _upstream(tmp_path, "upstream_argv.py", body)
    result = subprocess.run(
        [sys.executable, str(WRAPPER), str(script), str(ABSENT), "--", *forwarded],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert f"ARGV {forwarded}" in result.stdout, result.stdout
