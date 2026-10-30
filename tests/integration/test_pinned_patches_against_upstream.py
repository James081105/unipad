"""Every pinned patch applies to the real upstream XNU, in the build's order.

Why this exists
---------------
``build-xnu-4k-baseline.sh:44`` runs ``git -C .cache/xnu-work apply --check`` for
every patch, under ``set -e``.  That line has now failed three times, each time
after the acquisition step, the Mesa bootstrap, and the whole owner root chain
had run -- one runner cycle per failure, for a check that takes about a minute
here.

``tests/unit/test_pinned_patches_apply.py`` gets close.  It compares every hunk's
declared counts against what it carries, checks that consecutive hunks are
reachable by the same gap on both sides, and reconstructs each patch's ``-`` side
into a throwaway repository to run the real ``git apply --check``.  Two things it
cannot see, and this test closes both:

* It applies each patch to a tree built ONLY from that patch's own hunks, and one
  at a time.  The build applies them SEQUENTIALLY in glob order, each seeing the
  previous ones' changes -- so ``zzz-boot-progress.patch`` edits a ``sheng_pmgr_run``
  block that ``sheng-platform-power.patch`` creates, and does not apply to
  pristine upstream at all.  Applying them independently answers a different
  question from applying them in order.
* ``git apply`` tolerates an unreachable hunk start by searching for the context,
  so a patch whose every hunk position is wrong still passes both local checks.

What this test deliberately does NOT claim
------------------------------------------
It needs the XNU tarball, which ``scripts/shared/verify_patches_against_upstream.py``
downloads and caches under the system temp directory.  Without a network the
download fails, so the test SKIPS rather than failing: an environment that cannot
reach github.com is not evidence that a patch is broken.  When it does run, it
reports the runner's own verdict -- including the two ``kits/NativePrelink``
patches whose CRLF worktree copies are normalised back to the LF bytes git has.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHARED = REPO / "scripts" / "shared"
SCRIPT = SHARED / "verify_patches_against_upstream.py"
CACHE = Path(__import__("tempfile").gettempdir()) / "unipad-xnu-cache"


def _load():
    spec = importlib.util.spec_from_file_location("verify_upstream", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cached_tarball() -> Path | None:
    if not CACHE.is_dir():
        return None
    found = sorted(CACHE.glob("xnu-*.tar.gz"))
    return found[0] if found else None


def test_every_patch_applies_to_the_pinned_upstream_in_order():
    """The runner's ``git apply --check``, for all 55 patches at once."""
    if not SCRIPT.is_file():
        pytest.skip(f"{SCRIPT} is not present")
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    if not (REPO / "vendor" / "UniXNU" / "patches" / "xnu").is_dir():
        pytest.skip("the pinned vendor tree is not acquired")

    tarball = _cached_tarball()
    if tarball is None:
        # One attempt to populate the cache, then skip rather than fail: a host
        # with no network cannot answer this question, and a failure here would
        # read as "a patch is broken" when the truth is "nobody looked".
        try:
            subprocess.run(
                [sys.executable, str(SCRIPT)],
                cwd=str(REPO), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1800,
            )
        except subprocess.TimeoutExpired:
            pytest.skip("downloading the pinned XNU took over 30 minutes")
        tarball = _cached_tarball()
        if tarball is None:
            pytest.skip("the pinned XNU could not be downloaded; no network?")

    module = _load()
    commit, repo = module.pinned_xnu()
    if not tarball.name.endswith(f"{commit}.tar.gz"):
        pytest.skip(
            f"the cache holds {tarball.name} but the lock pins {commit}; "
            "delete the cache directory to refresh it"
        )

    work = Path(__import__("tempfile").mkdtemp(prefix="unipad-xnu-test-"))
    try:
        import tarfile
        with tarfile.open(tarball, "r:gz") as archive:
            archive.extractall(work, filter="data")
        extracted = [p for p in work.iterdir() if p.is_dir()]
        assert len(extracted) == 1, f"expected one directory, found {len(extracted)}"
        failures = module.apply_in_order(extracted[0], module.patches(), work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    assert not failures, (
        f"{len(failures)} of {len(module.patches())} patches do not apply to XNU "
        f"{commit[:12]} in the build's order. This is the check "
        f"build-xnu-4k-baseline.sh:44 runs, and it is what has failed three "
        f"times on the runner after the whole owner root chain had run:\n"
        + "\n".join(failures)
    )
