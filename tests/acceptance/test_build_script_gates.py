"""``scripts/macos/build-macos.sh``'s input gates, run for real.

Why this file exists
--------------------
The frozen-lock gate and the workflow's own preflight disagreed about what an
unfrozen lock *means*, and nothing on the shell side of that disagreement was
tested -- ``test_p0p1_gate_main.py`` runs the workflow's Python steps, and
``test_shell_script_syntax.py`` only parses the script.  The result was a
pipeline that could not build:

* ``build-macos.yml``'s "Validate inputs against sources.lock.json" step treats
  ``frozen: false`` as a **warning** -- "the dispatch inputs are the freeze
  candidates and are recorded verbatim in artifact-manifest.json".
* The acquisition step then fetches the pinned commits and publishes them.
* ``build-macos.sh``'s ``validate_lock`` treats the same ``frozen: false`` as a
  **hard failure** and exits 2 before a single Apple tool is invoked.

So with the repository's own ``config/sources.lock.json`` the workflow spends
macOS minutes on preflight and acquisition and then cannot build.  That is
measured, not theorised: see ``test_the_frozen_gate_does_not_deadlock_the_pipeline``.

What the fix asserts
--------------------
``frozen`` is a *declaration*; the pins are the *guarantee*.  A lock whose every
repository pin is a full 40-hex commit already forbids building from a moving
branch, which is the only thing the gate exists to prevent.  Refusing such a
lock adds no safety and removes the only path to a first build.  A lock with a
null or short pin is still refused, loudly.

What is still unproven
----------------------
That these pins *build*.  That is a build result and belongs in M0's
``clean_build_artifacts`` evidence, not in a boolean that also gates the build.
``frozen`` is recorded verbatim in the manifest so the difference stays visible
to anyone reading a run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"
REAL_LOCK = ROOT / "config" / "sources.lock.json"

pytestmark = pytest.mark.acceptance

#: The two repository pins the gate reads.  Full SHAs, exactly as the real lock
#: has them.
UNIXNU_COMMIT = "b881ac7dc1695ce676c856873051a487c6a9f963"
DEVICE_COMMIT = "97b75b1f63c6b7e895cd7646029db741d5fe5488"

COMMIT_RE = r"^[0-9a-f]{40}$"


def _bash() -> str:
    for candidate in ("bash", "bash.exe"):
        located = shutil.which(candidate)
        if located:
            return located
    pytest.skip("no bash on this host")


def _python_on_path(tmp_path: Path) -> dict[str, str]:
    """An environment whose ``python3`` resolves to a real interpreter.

    ``build-macos.sh`` picks its interpreter with ``command -v python3`` at load
    time and ignores a ``PY`` environment variable, so the only way to hand it
    one is PATH.  A shim directory is used rather than the host PATH because
    this host's ``python3`` is a 3.12 build at a non-ASCII path and the script
    only needs *an* interpreter that can read JSON.
    """
    import sys

    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    target = shim / "python3.cmd"
    target.write_text(f'@echo off\r\n"{sys.executable}" %*\r\n', encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _lock_payload(frozen: bool = False, unixnu: str = UNIXNU_COMMIT, device: str = DEVICE_COMMIT) -> dict:
    """A lock shaped like the real one, with the two pins parameterised."""
    if REAL_LOCK.is_file():
        payload = json.loads(REAL_LOCK.read_text(encoding="utf-8"))
    else:  # pragma: no cover - the repository always ships the lock
        payload = {"schema_version": 1}
    payload["frozen"] = frozen
    payload.setdefault("unixnu", {})["commit"] = unixnu
    payload.setdefault("device_repo", {})["commit"] = device
    return payload


def _write_lock(tmp_path: Path, **kwargs) -> Path:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    path = config / "sources.lock.json"
    path.write_text(json.dumps(_lock_payload(**kwargs), indent=2), encoding="utf-8")
    return path


def _run(tmp_path: Path, lock: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            _bash(),
            SCRIPT.as_posix(),
            "--stage",
            "M0",
            "--experiment",
            "gate-probe",
            "--lock",
            lock.as_posix(),
            "--dry-run",
            "--out",
            (tmp_path / "out").as_posix(),
            *extra,
        ],
        capture_output=True,
        text=True,
        env=_python_on_path(tmp_path),
        cwd=str(tmp_path),
        timeout=180,
    )


def _error_code(result: subprocess.CompletedProcess) -> str:
    for line in (result.stdout + result.stderr).splitlines():
        line = line.strip()
        if line.startswith('{"error"'):
            try:
                return json.loads(line)["error"]["code"]
            except (json.JSONDecodeError, KeyError):
                return ""
    return ""


def test_the_frozen_gate_does_not_deadlock_the_pipeline(tmp_path: Path) -> None:
    """A lock with complete pins must reach the next gate, not stop at ``frozen``.

    This is the deadlock.  The workflow's preflight passes an unfrozen lock
    (warning only) and its acquisition step fetches the pins, so a build step
    that refuses the very same lock makes the whole pipeline unable to build.
    """
    lock = _write_lock(tmp_path, frozen=False)
    result = _run(tmp_path, lock)

    assert _error_code(result) != "config_invalid" or "not frozen" not in (
        result.stdout + result.stderr
    ), (
        "build-macos.sh refuses a lock whose pins are complete full SHAs, so the "
        "workflow -- whose own preflight treats the same lock as a warning and "
        "whose acquisition step fetches the same pins -- can never reach a build. "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # It must get past the lock gate and stop somewhere later (the Apple tools
    # are genuinely absent on this host); anything but the frozen refusal.
    assert result.returncode != 2 or "not frozen" not in (result.stdout + result.stderr)


def _error_message(result: subprocess.CompletedProcess) -> str:
    """The machine-readable refusal's message, or '' when there is none."""
    for line in (result.stdout + result.stderr).splitlines():
        line = line.strip()
        if line.startswith('{"error"'):
            try:
                return json.loads(line)["error"]["message"]
            except (json.JSONDecodeError, KeyError):
                return ""
    return ""


def test_an_incomplete_pin_is_still_refused(tmp_path: Path) -> None:
    """The gate keeps its teeth: a pin that is not a full commit is a refusal.

    Without this the fix above would be "delete the gate", which would let a
    moving branch be built.  The REFUSAL must name the pin -- the unfrozen
    warning is expected to appear too, so what is asserted is the error the run
    exits on.  If the refusal named ``frozen`` instead, the frozen gate would be
    shadowing the check that has teeth and a broken pin would hide behind a
    different message.
    """
    lock = _write_lock(tmp_path, frozen=False, unixnu="main")
    result = _run(tmp_path, lock)
    message = _error_message(result)
    assert _error_code(result) == "config_invalid", result.stdout + result.stderr
    assert "pinned commit" in message, (
        "the refusal does not name the broken pin, so a short or branch-name pin "
        f"is being refused for some other reason:\n{message}"
    )
    assert "not frozen" not in message, (
        "the frozen gate fires before the pin check, so it shadows the check that "
        f"has teeth:\n{message}"
    )


def test_a_null_pin_is_still_refused(tmp_path: Path) -> None:
    lock = _write_lock(tmp_path, frozen=False, device=None)
    result = _run(tmp_path, lock)
    assert _error_code(result) == "config_invalid", result.stdout + result.stderr


def test_the_log_reports_the_frozen_state_the_lock_declares(tmp_path: Path) -> None:
    """The log must not claim ``frozen=true`` for a lock that says false.

    ``main()`` logged ``lock: frozen=true unixnu=...`` unconditionally, so an
    unfrozen run's log asserted the opposite of what its own
    ``artifact-manifest.json`` recorded -- and the log is the one place an
    operator looks to find out which baseline was built.  A dry run stops before
    the manifest is written, so this asserts the log line itself, in both states.
    """
    unfrozen_dir = tmp_path / "unfrozen"
    unfrozen_dir.mkdir()
    result = _run(unfrozen_dir, _write_lock(unfrozen_dir, frozen=False))
    assert "lock: frozen=false" in result.stdout, result.stdout
    assert "NOT FROZEN" in result.stdout, result.stdout

    frozen_dir = tmp_path / "frozen"
    frozen_dir.mkdir()
    frozen_result = _run(frozen_dir, _write_lock(frozen_dir, frozen=True))
    assert "lock: frozen=true" in frozen_result.stdout, frozen_result.stdout
    assert "NOT FROZEN" not in frozen_result.stdout, frozen_result.stdout


def test_the_real_lock_is_not_left_frozen_by_accident() -> None:
    """The shipped lock's own state must match what the audit report claims.

    The audit report records WP00's blocker as ``sources.lock.json still
    declares frozen=false``.  If someone flips the flag without a build behind
    it, that sentence becomes false and the report must be re-measured -- this
    test fails so the report cannot silently drift from the tree.
    """
    payload = json.loads(REAL_LOCK.read_text(encoding="utf-8"))
    assert payload["frozen"] is False, (
        "config/sources.lock.json is now frozen. A frozen lock asserts the pins "
        "are the authority AND that the tool identity is complete; "
        "tools.xcode.version and tools.clang.sha256 are still null, and no clean "
        "build has run against these pins. Re-measure M0/WP00 and rewrite "
        "docs/audit-r8.md section 4 before changing this."
    )
