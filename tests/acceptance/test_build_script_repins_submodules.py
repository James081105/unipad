"""``run_driver`` must put the module submodules back after every driver call.

Why this file exists
--------------------
``config/sources.lock.json`` pins ``drivers/NativeMobileDisplay`` one commit
*behind* UniXNU's own gitlink, because the pinned device repository cannot
compile the gitlink's version (measured on run 40187668943: ``only virtual
member functions can be marked 'override'``).  The lock is the authority, so
the checkout has to be moved to the locked commit -- and it has to be moved
**every time the driver touches the submodules**, because every ``unixnu``
subcommand that reaches ``prepare()`` runs ``git submodule update --init
--recursive`` (``cli.py:79-86`` -> ``feeds.py:159-165``), which resets every
module submodule to the gitlink's commit.

Measured on run 40209085545, which is the run that got the whole owner root
chain to ``os_root_status=pass`` and then died in the *second* build pass::

    Rebuild the owned Legacy archive after changing
    drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp

The chain had built the Legacy display archive at the locked ``eb9c949``; the
second pass's ``unixnu build kernel`` reset the checkout to the gitlink's
``c031d882``; and ``build-xnu-4k-baseline.sh:735`` refused to link.  The
divergence was applied in the acquisition step and once more before the chain,
and neither of those could see a reset that happened afterwards.

What is proven here
-------------------
That the re-pin is inside ``run_driver`` -- so it happens for every driver
subcommand, whatever it is, instead of being remembered by each caller -- and
that it delegates to ``unipad.host.source_pins`` rather than carrying a second
implementation of "which commit".  The module's own behaviour (moves a diverging
checkout, leaves an agreeing one, refuses an unfetchable commit) is proven
against real git repositories in ``test_acquire_submodule_divergences.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"
REAL_LOCK = ROOT / "config" / "sources.lock.json"
MODULE = "unipad.host.source_pins"

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH"),
]


def _function(name: str) -> str:
    """One function of ``build-macos.sh``, exactly as the shell reads it."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def test_the_repin_is_inside_run_driver_not_in_its_callers() -> None:
    """The wiring, and the reason it is there rather than at each call site.

    A caller that has to *remember* to re-apply the pin is the same defect one
    level up: the chain step remembered, and the second build pass undid it.
    The call is what carries this; a comment promising it does not.
    """
    run_driver = _function("run_driver")
    assert "pin_module_submodules" in run_driver, (
        "run_driver does not put the module submodules back, so every "
        "`unixnu build`/`fetch` -- which runs `git submodule update --init "
        "--recursive` and resets them to the driver's gitlink -- leaves the "
        "tree at commits the lock does not name. Measured on run 40209085545."
    )
    # ...and it has to come AFTER the driver runs, or there is nothing to
    # repair.  Matching the bare name would find the function definition
    # further up the file, which is the mistake the first draft of this
    # assertion made.
    call = run_driver.index('pin_module_submodules "$step"')
    assert run_driver.index('"${cmd[@]}"') < call, (
        "the module submodules are pinned before the driver runs, so a reset "
        "the driver performs is never repaired"
    )
    assert call < run_driver.rindex("return"), (
        "the module submodules are pinned after run_driver returns, so a "
        "failure of the driver would skip it"
    )


def test_the_repin_delegates_rather_than_reimplementing() -> None:
    """One implementation of "which commit", because two is the defect class.

    The workflow's acquisition step applies the same pins.  If ``build-macos.sh``
    grew its own git-fetch-and-checkout, the two would drift and the second
    place that knows a sha would be this repository's own.
    """
    helper = _function("pin_module_submodules")
    assert MODULE in helper, (
        f"pin_module_submodules does not call {MODULE}, so the pin logic lives "
        "in more than one place"
    )
    # The git operations, not the word: the function is *named*
    # pin_module_submodules, so "submodule" is in it by construction, and a
    # forbidden list that included it failed on the function's own name.
    for forbidden in ("git ", "rev-parse", "FETCH_HEAD", "checkout", "submodule update"):
        assert forbidden not in helper, (
            f"pin_module_submodules contains {forbidden!r}, so it is a second "
            f"implementation of what {MODULE} already does"
        )
    # A pin that cannot be applied is a refusal, not a warning: a build that is
    # not the pinned one must not be allowed to look like a result.
    assert "die " in helper, (
        "a submodule that cannot be moved to the locked commit is not a "
        "refusal, so the build would continue at a commit the lock does not name"
    )


def _git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(directory: Path, message: str) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    _git(directory, "add", "-A")
    subprocess.run(
        ["git", "-C", str(directory), "commit", "-q", "--allow-empty", "-m", message],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return _git(directory, "rev-parse", "HEAD")


def _workspace(tmp_path: Path) -> tuple[Path, str, str]:
    """A driver checkout whose submodule sits at the *gitlink* commit.

    ``(driver_root, gitlink, locked)``.  Both commits are real commits in a real
    local repository, so the ``git fetch --depth=1 origin <sha>`` the module
    performs resolves without a network.
    """
    driver = tmp_path / "vendor" / "UniXNU"
    (driver / "drivers").mkdir(parents=True)
    _git(driver, "init", "-q")
    (driver / "README.md").write_text("driver checkout\n", encoding="utf-8")
    _commit(driver, "driver")

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    (upstream / "NativeLegacyDisplay.cpp").write_text("// the gitlink's version\n", encoding="utf-8")
    gitlink = _commit(upstream, "the gitlink's version")
    (upstream / "NativeLegacyDisplay.cpp").write_text("// the locked version\n", encoding="utf-8")
    locked = _commit(upstream, "the locked version")

    # A clone is not a submodule: without .gitmodules and a gitlink entry,
    # `git submodule update --init --recursive` is a NO-OP, and the first draft
    # of this workspace passed its "survives the reset" assertion with the
    # protection removed because there was no reset to survive.  So the
    # submodule is registered the way the driver repository registers it, with a
    # .gitmodules file and a gitlink recording `gitlink`.
    submodule = driver / "drivers" / "NativeMobileDisplay"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(submodule)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(submodule, "checkout", "-q", "--detach", gitlink)
    modules = (
        '[submodule "drivers/NativeMobileDisplay"]\n'
        "\tpath = drivers/NativeMobileDisplay\n"
        f"\turl = {upstream.as_posix()}\n"
    )
    (driver / ".gitmodules").write_text(modules, encoding="utf-8")
    _git(driver, "add", ".gitmodules")
    _commit(driver, "register the module submodule")
    # The upstream is a local directory, and git refuses `file` transport for
    # submodules by default (the CVE-2022-39253 mitigation).  Without this the
    # first `submodule update` that actually has to FETCH dies with
    # "fatal: transport 'file' not allowed" -- which exits 128 and leaves the
    # checkout where it was, so the "survives the reset" assertion would pass
    # with the protection removed.  The real driver's submodules are https; this
    # is what makes a local fixture behave like one.  It has to be set in BOTH
    # repositories, because the fetch runs inside the submodule.
    _git(driver, "config", "protocol.file.allow", "always")
    _git(submodule, "config", "protocol.file.allow", "always")
    _git(
        driver,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{gitlink},drivers/NativeMobileDisplay",
    )
    _commit(driver, "the gitlink records the gitlink's version")
    # Sanity: the reset this test depends on must really be a reset.  Without
    # this the assertion below would pass for the wrong reason.
    _git(driver, "submodule", "update", "--init", "--recursive")
    assert _git(submodule, "rev-parse", "HEAD") == gitlink, (
        "the fixture's submodule is not registered, so `submodule update` "
        "cannot move it and the protection under test is never exercised"
    )
    _git(submodule, "checkout", "-q", "--detach", gitlink)
    return driver, gitlink, locked


def _lock(tmp_path: Path, commit: str, gitlink: str) -> Path:
    payload: dict[str, Any] = (
        json.loads(REAL_LOCK.read_text(encoding="utf-8"))
        if REAL_LOCK.is_file()
        else {"schema_version": 1}
    )
    payload["submodules"] = [
        {
            "path": "drivers/NativeMobileDisplay",
            "commit": commit,
            "gitlink_commit": gitlink,
        }
    ]
    lock = tmp_path / "sources.lock.json"
    lock.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return lock


def test_the_command_the_shell_builds_repairs_a_reset_submodule(tmp_path: Path) -> None:
    """The command itself, run against a checkout the driver has just reset.

    This is the shape of the failure: the driver runs, the submodule is back at
    the gitlink, and the next thing that reads the sources is not the thing that
    reset them.  The command ``pin_module_submodules`` builds is executed here
    as a real process against a real git workspace, with ``PYTHONPATH`` set the
    way ``run_unipad`` sets it -- so what is proven is that the shell's
    invocation works, not that a similar one would.
    """
    driver, gitlink, locked = _workspace(tmp_path)
    submodule = driver / "drivers" / "NativeMobileDisplay"
    assert _git(submodule, "rev-parse", "HEAD") == gitlink

    # What the driver does: reset every module submodule to the gitlink.
    _git(driver, "submodule", "update", "--init", "--recursive")
    assert _git(submodule, "rev-parse", "HEAD") == gitlink

    lock = _lock(tmp_path, locked, gitlink)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE,
            "--lock",
            str(lock),
            "--driver",
            str(driver),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert result.returncode == 0, (
        "the command pin_module_submodules builds did not succeed:\n"
        + (result.stdout + result.stderr)[-2000:]
    )
    assert _git(submodule, "rev-parse", "HEAD") == locked, (
        "the reset submodule was not put back at the locked commit, so the "
        "next build stage reads sources the lock does not name"
    )
    # --quiet means a second run prints nothing and changes nothing.
    again = subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE,
            "--lock",
            str(lock),
            "--driver",
            str(driver),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert again.returncode == 0, again.stdout + again.stderr
    assert again.stdout == "", (
        f"an already-pinned tree printed something, so the common path is not "
        f"quiet: {again.stdout!r}"
    )
    assert _git(submodule, "rev-parse", "HEAD") == locked


def test_a_protected_submodule_survives_the_drivers_own_reset(tmp_path: Path) -> None:
    """The half that actually fixes it, against the command doing the reset.

    Re-applying the pin *after* a driver call is not enough, and that was
    measured rather than argued: run 40217489782's log shows the re-pin working
    correctly (``c031d882 -> eb9c949``) at 17:50:36.310, one second AFTER
    ``build-xnu-4k-baseline.sh:735`` had already refused at 17:50:35.288.  The
    reset is the driver's FIRST act (``cli.py:79-86`` -> ``prepare()`` ->
    ``feeds.update_submodules``), so everything the call builds afterwards --
    the archives and the link -- sees the gitlink's commit no matter what runs
    once it returns.

    ``submodule.<path>.update=none`` is the one lever that operates at the right
    moment, because it is read by the very command doing the reset.  This test
    runs that command -- ``git submodule update --init --recursive``, exactly as
    ``feeds.py:159-165`` spells it -- and asserts the checkout does not move.
    """
    driver, gitlink, locked = _workspace(tmp_path)
    submodule = driver / "drivers" / "NativeMobileDisplay"
    lock = _lock(tmp_path, locked, gitlink)

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", MODULE, "--lock", str(lock), "--driver", str(driver)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert result.returncode == 0, (
        "the command pin_module_submodules builds did not succeed:\n"
        + (result.stdout + result.stderr)[-2000:]
    )
    assert _git(submodule, "rev-parse", "HEAD") == locked

    # The driver's own reset, spelled the way feeds.py:164 spells it.
    _git(driver, "submodule", "update", "--init", "--recursive")
    assert _git(submodule, "rev-parse", "HEAD") == locked, (
        "the driver's `submodule update` moved the protected submodule back to "
        f"{gitlink}, so every archive and the link see the gitlink's commit. "
        "Measured on runs 40209085545 and 40217489782 as "
        "build-xnu-4k-baseline.sh:735's refusal to link."
    )

    # ...and it is still protected after a second application, which is what
    # run_driver does on every driver call.
    again = subprocess.run(
        [sys.executable, "-m", MODULE, "--lock", str(lock), "--driver", str(driver)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
        timeout=180,
    )
    assert again.returncode == 0, again.stdout + again.stderr
    _git(driver, "submodule", "update", "--init", "--recursive")
    assert _git(submodule, "rev-parse", "HEAD") == locked
