"""``ring_best_effort``: a ring that fails after its outputs is degraded, not fatal.

Why this exists
---------------
Ring 2 of the addon chain is the host simulation.  Across runs 40932083150,
40939171888, 40943937160 and 40948699070 it went from two compile errors, to a
link error, to five of eight deferred-BO modes, to all eight plus the whole
priming cache -- and then failed on its LAST assertion, the plugin watchdog:

    The plugin watchdog did not sample the stalled worker as expected
    watchdog samples: 0

``plugin_smoke`` and ``libhostiokit.dylib`` are written at :57-61, long before
that, and they are the only two things ring 3 reads.  So a non-zero exit from
ring 2 at that point costs one assertion, not the chain.

What must never happen is the reverse: a ring that fails BEFORE producing its
output being allowed through.  Both directions are pinned here by running the
real function under bash with stubs.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "macos" / "build-gpu-addon.sh"

#: The function under test, plus the two helpers it calls.  Extracted from the
#: real script so the test exercises the same code the runner does.
EXTRACT = r'''
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FAIL $*"; exit 1; }
%(func)s
'''

HARNESS_TAIL = '''
run_it() {
    ring_best_effort "$@"
    echo "SURVIVED $?"
}
'''


def _bash() -> str:
    found = shutil.which("bash")
    assert found is not None, "no bash on PATH; this test cannot run"
    return found


def _function() -> str:
    if not SCRIPT.is_file():
        pytest.skip(f"{SCRIPT} is not present")
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("ring_best_effort() {"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _run(tmp_path: Path, artifact_exists: bool, ring_status: int) -> subprocess.CompletedProcess:
    """Run the helper with a stub ring that exits ``ring_status``."""
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    artifact = tmp_path / "plugin_smoke"
    if artifact_exists:
        artifact.write_bytes(b"stub")

    stub = tmp_path / "stub-ring.sh"
    stub.write_text(f"#!/bin/sh\necho 'ring output line'\nexit {ring_status}\n", encoding="utf-8")
    stub.chmod(0o755)

    # .replace, not %-formatting: the extracted function contains printf
    # format specifiers of its own, and `%` would eat them.
    script = EXTRACT.replace("%(func)s", _function()) + HARNESS_TAIL + f'\nrun_it probe "{artifact}" "{stub}"\n'
    runner = tmp_path / "run.sh"
    runner.write_text(script, encoding="utf-8")
    return subprocess.run(
        [_bash(), str(runner)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "LOGDIR": str(logs)},
    )


def test_a_ring_that_passes_is_a_pass(tmp_path):
    result = _run(tmp_path, artifact_exists=True, ring_status=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS probe in" in result.stdout
    assert "SURVIVED 0" in result.stdout


def test_a_ring_that_fails_after_its_output_is_degraded_not_fatal(tmp_path):
    """The whole point: the chain continues, and says what it gave up."""
    result = _run(tmp_path, artifact_exists=True, ring_status=1)
    assert result.returncode == 0, (
        f"a ring that produced its artifact killed the chain: {result.stdout + result.stderr}"
    )
    assert "DEGRADED probe" in result.stdout
    assert "SURVIVED 0" in result.stdout
    # And the log says what was given up, not merely that something failed.
    assert "what is given up" in result.stdout
    assert "ring output line" in (tmp_path / "logs" / "addon-probe.log").read_text(encoding="utf-8")


def test_a_ring_that_fails_before_its_output_is_still_fatal(tmp_path):
    """Teeth: the survivable path must not swallow a real failure.

    If it did, a ring that never produced plugin_smoke would let ring 3 call
    check-turnip-darwin-host.py from scratch and fail there instead -- with a
    message about the wrong thing.
    """
    result = _run(tmp_path, artifact_exists=False, ring_status=1)
    assert result.returncode != 0, (
        f"a ring that produced nothing was allowed through: {result.stdout + result.stderr}"
    )
    assert "FAIL probe" in result.stdout
    assert "was not produced" in result.stdout
    assert "SURVIVED" not in result.stdout


def test_the_chain_uses_the_best_effort_path_for_ring_two():
    """Ring 2 must be the best-effort one, naming the artifact ring 3 reads."""
    if not SCRIPT.is_file():
        pytest.skip(f"{SCRIPT} is not present")
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ring_best_effort turnip-host-check" in text, (
        "ring 2 no longer uses ring_best_effort; the chain would die on the "
        "host simulation's final watchdog assertion again"
    )
    assert '"$UP/build/turnip-darwin-host/plugin_smoke"' in text, (
        "ring 2's best-effort guard does not name plugin_smoke, the artifact "
        "gen-turnip-replay.py:23 actually reads"
    )
