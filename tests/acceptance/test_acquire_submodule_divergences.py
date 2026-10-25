"""The acquisition step must put every module submodule at the locked commit.

Why this file exists
--------------------
``unixnu fetch`` initialises the module submodules from the DRIVER
repository's own gitlink table (``feeds.py:159-165``).  That is a second place
that knows a sha, and ``config/sources.lock.json`` is supposed to be the
authority (PRD 8.1).  When the two disagree -- which they must, for
``drivers/NativeMobileDisplay``, because the pinned device repository cannot
compile the gitlink's version -- the lock has to win, and it has to win *where
the sources are laid down*.

The first attempt at this put the divergence only in the chain step, which is
after the point where the build refuses.  Measured on run 40199915736:

    pinned sources are missing and this script performs no network access, so
    the upstream driver cannot be allowed to fetch them:
    submodule:drivers/NativeMobileDisplay

and every remaining step skipped, so nothing about the build had been exercised.
The second attempt then shipped a ``NameError`` -- ``name 'pins' is not
defined`` -- because the new function was defined at module level and reached
for a ``main()`` local.  It reached a paid macOS runner before any test saw it.

That second failure is the reason this file runs the step's own code instead of
reading it: ``test_workflow_python_syntax.py`` compiles every embedded block and
says plainly that it does not prove the block *does* anything, and the
data-level assertions in ``test_os_root_chain.py`` only prove certain strings
appear in the YAML.  Neither can see a name that resolves at definition time and
not at call time.

What is proven here
-------------------
That ``apply_submodule_divergences`` moves a checkout whose HEAD differs from
the lock, reports what it moved, leaves a checkout that already agrees alone,
does not invent a submodule the driver never fetched, and refuses rather than
continues when the wanted commit cannot be had.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-macos.yml"
REAL_LOCK = ROOT / "config" / "sources.lock.json"

ACQUISITION = (
    "Acquire and verify the pinned sources (vendor/ + unixnu fetch/tree + pin cross-check)"
)
PATH_NAME = "drivers/NativeMobileDisplay"

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH"),
]


def _workflow() -> Any:
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover - depends on the environment
        pytest.skip("PyYAML is required to parse build-macos.yml")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step_python() -> str:
    """The acquisition step's Python, extracted the way the shell reads it."""
    for step in _workflow()["jobs"]["build"]["steps"]:
        if step.get("name") == ACQUISITION:
            lines = step["run"].splitlines()
            break
    else:  # pragma: no cover - the workflow always ships this step
        raise AssertionError(f"{WORKFLOW.name} has no step named {ACQUISITION!r}")
    start = next(i for i, line in enumerate(lines) if line.strip() == "python3 - <<'PY'")
    end = next(i for i, line in enumerate(lines[start + 1 :], start + 1) if line.strip() == "PY")
    return textwrap.dedent("\n".join(lines[start + 1 : end]))


def _load_step(tmp_path: Path) -> Any:
    """Import the step's own Python as a module, without running ``main()``.

    ``__name__`` is not ``__main__``, so the ``sys.exit(main())`` at the bottom
    never fires -- which is the whole point: the module is loaded for its
    functions, not for its behaviour as a script.

    ``src`` goes on ``sys.path`` because the step's
    ``apply_submodule_divergences`` delegates to ``unipad.host.source_pins``
    rather than carrying a second implementation: the step's own shell sets
    ``PYTHONPATH=src``, and this import is what stands in for it.  Without it
    the load fails with ``ModuleNotFoundError`` and every test here skips for a
    reason that has nothing to do with what they assert.
    """
    source_root = str(ROOT / "src")
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    module_path = tmp_path / "acquisition_step.py"
    module_path.write_text(_step_python(), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("unipad_acquisition_step", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", message],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return _git(root, "rev-parse", "HEAD")


def _workspace(tmp_path: Path) -> tuple[Path, str, str]:
    """A driver checkout whose one submodule sits at the *gitlink* commit.

    Returns ``(driver_root, gitlink, other)``: ``other`` is a second commit in
    the same repository that the lock will name instead.  Both are real commits
    in a real local repository, so the ``git fetch --depth=1 origin <sha>`` the
    step performs resolves without a network.
    """
    driver = tmp_path / "vendor" / "UniXNU"
    (driver / "drivers").mkdir(parents=True)
    _git(driver, "init", "-q")
    (driver / "README.md").write_text("driver checkout\n", encoding="utf-8")
    _commit(driver, "driver")

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    (upstream / "NativeLegacyDisplay.cpp").write_text("// one\n", encoding="utf-8")
    gitlink = _commit(upstream, "the gitlink's version")
    (upstream / "NativeLegacyDisplay.cpp").write_text("// two\n", encoding="utf-8")
    other = _commit(upstream, "the locked version")

    submodule = driver / "drivers" / "NativeMobileDisplay"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(submodule)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(submodule, "checkout", "-q", "--detach", gitlink)
    assert _git(submodule, "rev-parse", "HEAD") == gitlink
    return driver, gitlink, other


def _bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit: str | None = None,
    gitlink: str | None = None,
    record_gitlink: bool = True,
) -> tuple[Any, Path, str, str]:
    """Lay the workspace down, write the lock, load the step.

    Returns ``(module, driver_root, gitlink_commit, other_commit)``.  With no
    arguments the lock names ``other`` and records the real gitlink, which is
    the divergence under test; ``commit`` overrides what the lock pins and
    ``record_gitlink=False`` leaves the field out entirely, which is the shape a
    lock that never diverged would have.  The two commits come back because the
    tests need them and ``_workspace`` is what mints them -- what the LOCK
    records is a separate question, and a lock that records the wrong thing is
    exactly the defect under test.
    """
    driver, gitlink_commit, other = _workspace(tmp_path)
    entry: dict[str, Any] = {"path": PATH_NAME, "commit": commit or other}
    recorded = gitlink if gitlink is not None else (gitlink_commit if record_gitlink else None)
    if recorded is not None:
        entry["gitlink_commit"] = recorded
    payload = (
        json.loads(REAL_LOCK.read_text(encoding="utf-8"))
        if REAL_LOCK.is_file()
        else {"schema_version": 1}
    )
    payload["submodules"] = [entry]
    lock = tmp_path / "sources.lock.json"
    lock.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    monkeypatch.setenv("LOCK_PATH", str(lock))
    monkeypatch.setenv("VENDOR_DIR", str(tmp_path / "vendor"))
    return _load_step(tmp_path), driver, gitlink_commit, other


def _relock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit: str,
    gitlink: str | None = None,
    extra: list[dict[str, Any]] | None = None,
) -> Path:
    """Rewrite the lock the loaded step will read, in place.

    The step reads ``LOCK_PATH`` once, at import -- so the PATH cannot change,
    but the FILE at it can, and the function under test re-reads it on every
    call.  That is what lets one loaded module serve a workspace whose lock says
    something else, which is the shape of every case here except the first.
    """
    payload = (
        json.loads(REAL_LOCK.read_text(encoding="utf-8"))
        if REAL_LOCK.is_file()
        else {"schema_version": 1}
    )
    entries: list[dict[str, Any]] = [{"path": PATH_NAME, "commit": commit}]
    if gitlink is not None:
        entries[0]["gitlink_commit"] = gitlink
    entries.extend(extra or [])
    payload["submodules"] = entries
    lock = tmp_path / "sources.lock.json"
    lock.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    monkeypatch.setenv("LOCK_PATH", str(lock))
    return lock


def test_a_submodule_the_lock_pins_away_from_the_gitlink_is_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect itself: the checkout follows the LOCK, not the gitlink.

    This is the case that made run 40199915736 refuse the whole build, and it is
    the one a string-in-YAML assertion cannot see -- the YAML can contain the
    right call and still raise ``NameError`` when it runs.
    """
    module, driver, gitlink, other = _bind(tmp_path, monkeypatch)
    submodule = driver / "drivers" / "NativeMobileDisplay"
    assert _git(submodule, "rev-parse", "HEAD") == gitlink

    moved = module.apply_submodule_divergences(driver)

    assert [entry["path"] for entry in moved] == [PATH_NAME], moved
    assert moved[0]["gitlink_commit"] == gitlink, moved
    assert moved[0]["commit"] == other, moved
    assert _git(submodule, "rev-parse", "HEAD") == other, (
        "the checkout is still at the gitlink's commit, so prepare-sources "
        "will refuse the build before the plan runs"
    )


def test_a_submodule_already_at_the_locked_commit_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case, and the one that must cost nothing.

    Every submodule the lock agrees with has to be a no-op: a step that
    re-checkouts on every run would invalidate the toolchain cache for no
    reason and would make the log say something moved when nothing did.
    """
    module, driver, gitlink, _other = _bind(tmp_path, monkeypatch)
    _relock(tmp_path, monkeypatch, commit=gitlink, gitlink=gitlink)
    assert module.apply_submodule_divergences(driver) == []


def test_a_submodule_the_driver_never_fetched_is_not_invented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent directory is not a divergence.

    The lock names every module submodule of the driver repository, and a fresh
    acquisition does not have all of them on disk before ``unixnu fetch`` runs.
    Creating one here would put material in ``vendor/`` that no pin asked for,
    which is the class ``_refuse_private`` and the vendor-ignore rule exist to
    prevent.
    """
    module, driver, gitlink, other = _bind(tmp_path, monkeypatch)
    _relock(
        tmp_path,
        monkeypatch,
        commit=other,
        gitlink=gitlink,
        extra=[{"path": "drivers/NotFetched", "commit": other, "gitlink_commit": gitlink}],
    )

    moved = module.apply_submodule_divergences(driver)

    assert [entry["path"] for entry in moved] == [PATH_NAME], moved
    assert not (driver / "drivers" / "NotFetched").exists(), (
        "a submodule the driver never fetched was created out of nothing"
    )


def test_a_commit_that_cannot_be_had_is_a_refusal_not_a_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step refuses, which is what ``_run_git`` turns into a failed job.

    A checkout that silently stays at the gitlink is the failure mode this whole
    function exists to prevent, and it is what a ``check=False`` would produce:
    ``prepare-sources`` would then refuse with a message naming the missing
    submodule, six paid macOS minutes later, with nothing about the cause.
    """
    module, driver, gitlink, _other = _bind(tmp_path, monkeypatch)
    _relock(tmp_path, monkeypatch, commit="0" * 40, gitlink=gitlink)
    with pytest.raises(module.SourcePinError):
        module.apply_submodule_divergences(driver)
