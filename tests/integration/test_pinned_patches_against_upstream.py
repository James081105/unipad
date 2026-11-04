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


#: A patch whose ``---``/``+++`` lines are the ONLY path declaration.  Several
#: patches in this set are shaped exactly like this -- measured on
#: ``native-union-getattrlistbulk.patch``, whose first line is the ``---`` -- and
#: a target list built only from ``diff --git`` headers silently misses them.
SYNTHETIC_PATCH = """--- a/bsd/kern/synthetic_target.c
+++ b/bsd/kern/synthetic_target.c
@@ -1,3 +1,4 @@
 line one
+line one and a half
 line two
 line three
"""

TARGET = "bsd/kern/synthetic_target.c"
ORIGINAL = b"line one\nline two\nline three\n"
PATCHED = b"line one\nline one and a half\nline two\nline three\n"


def _synthetic_tree(root: Path) -> Path:
    """A tree that satisfies the script's precondition and holds one target."""
    tree = root / "xnu"
    (tree / "bsd" / "kern").mkdir(parents=True)
    # The script's --xnu-dir guard checks for exactly this file.
    (tree / "bsd" / "kern" / "bsd_init.c").write_bytes(b"// bsd_init\n")
    (tree / TARGET).write_bytes(ORIGINAL)
    return tree


def test_the_xnu_dir_path_never_writes_to_the_tree_it_is_given(tmp_path):
    """``--xnu-dir`` used to apply every patch IN PLACE and leave them there.

    Measured 2026-11-04: one run against ``local-private/upstream/xnu`` dirtied
    67 files in 13 seconds, and nothing said so.  The damage surfaced as an
    unrelated unit test -- ``test_the_patch_applies_to_a_pristine_tree`` --
    failing with a patch-context error, three directories away, with no causal
    link a reader could follow.

    This test closes that by running the real ``main()`` over a synthetic tree
    and comparing the bytes afterwards.  It has teeth: the in-place behaviour it
    forbids is demonstrated two assertions down, on the same file, so a
    regression to it turns this red rather than silent.
    """
    if not SCRIPT.is_file():
        pytest.skip(f"{SCRIPT} is not present")
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")

    tree = _synthetic_tree(tmp_path)
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "0001-synthetic.patch").write_text(SYNTHETIC_PATCH, encoding="utf-8")

    module = _load()
    module.PATCH_DIRS = (patches,)
    result = module.main(["--xnu-dir", str(tree)])

    assert result == 0, "the synthetic patch did not apply to the synthetic tree"
    # The tree is byte-identical, which is the whole point.  Not "git status is
    # quiet" -- on this host git's stat cache reports 67 modified files on a tree
    # that is byte-identical to HEAD, so the comparison is on bytes.
    assert (tree / TARGET).read_bytes() == ORIGINAL, (
        "the --xnu-dir path wrote to the tree it was given.  This is the defect "
        "that dirtied local-private/upstream/xnu and broke an unrelated unit "
        "test.  The scratch tree must hold the applied copy, not the caller's."
    )
    assert not list(tree.rglob("*.orig")), "git apply left backup files behind"

    # TEETH.  The same file, applied in place, really does change -- so the
    # assertion above is testing a behaviour the code can get wrong, not a
    # property that holds for every implementation.
    #
    # The comparison normalises CRLF back to LF, and that is not laziness: this
    # host has core.autocrlf=true, so `git apply` WRITES CRLF into whatever tree
    # it touches.  The first version of this check compared raw bytes, failed,
    # and the failure message was the line endings rather than the mutation --
    # which would have read as "the patch did not apply" when the truth was "the
    # patch applied and git translated the endings on the way out".  Comparing
    # content keeps the check about the thing it is checking.
    import tempfile
    scratch = Path(tempfile.mkdtemp(prefix="teeth-"))
    try:
        shutil.copytree(tree, scratch, dirs_exist_ok=True)
        in_place = scratch / TARGET
        module.apply_in_order(scratch, [patches / "0001-synthetic.patch"], scratch)
        applied = in_place.read_bytes().replace(b"\r\n", b"\n")
        assert applied == PATCHED, (
            "the teeth check is broken: applying the patch did not produce the "
            f"expected content, so the non-mutation assertion above proves "
            f"nothing.  Got {applied!r}, wanted {PATCHED!r}"
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_the_target_list_reads_patches_with_no_diff_git_header(tmp_path):
    """Several patches declare their path only in ``---``/``+++``.

    The first revision of ``targets_of`` read ``diff --git`` alone and found 50
    targets for 59 patches -- under one per patch, which should have been the
    tell.  The run then failed 16 patches with "No such file or directory" for
    files that were in the source tree the whole time.
    """
    module = _load()
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "0001-synthetic.patch").write_text(SYNTHETIC_PATCH, encoding="utf-8")

    assert module.targets_of([patches / "0001-synthetic.patch"]) == {TARGET}
    assert module._strip_dev_null("/dev/null") is None
    assert module._strip_dev_null(TARGET) == TARGET
