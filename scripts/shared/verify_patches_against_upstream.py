#!/usr/bin/env python3
"""Apply every pinned patch to the real upstream XNU, exactly as the build does.

Why this exists
---------------
``build-xnu-4k-baseline.sh:44`` runs ``git -C .cache/xnu-work apply --check`` for
every patch.  That check is the authority on whether a patch is well-formed, and
it is the one that has now failed three times on the same line -- each time after
the acquisition step, the Mesa bootstrap, and the whole owner root chain had run.

``scripts/shared/verify_patch_applies.py`` gets close to it: it rebuilds the
``-`` side of every hunk into a throwaway repository and runs the real
``git apply --check``.  But it applies each patch to a tree built ONLY from that
patch's own hunks, and one at a time.  Two things it therefore cannot see:

* The build applies the patches SEQUENTIALLY, in glob order, each one seeing the
  previous ones' changes.  ``zzz-boot-progress.patch`` edits a ``sheng_pmgr_run``
  block that ``sheng-platform-power.patch`` creates, so it does not apply to
  pristine upstream at all.  Applying them independently is a different question
  from applying them in order.
* ``git apply`` tolerates an unreachable hunk start by searching for the context,
  so a patch whose every hunk header position is wrong still passes.

This script closes the first gap: it downloads the XNU revision the lock pins,
extracts it, and applies every patch in the same order the build does, reporting
per-patch results.  It is the runner's check, locally.

The XNU tarball is cached next to the extraction, so a repeat run costs no
download.  Pass ``--keep`` to leave the extracted tree for inspection.

``--xnu-dir`` NEVER WRITES TO THE TREE IT IS GIVEN
---------------------------------------------------
It used to.  Revision 1 applied all 59 patches in place and left them there: a
13-second run against ``local-private/upstream/xnu`` dirtied 67 files, and the
damage was invisible until an unrelated unit test -- whose entire premise is
that the tree is pristine -- started failing with a patch-context error.  That
test was right and the tool was wrong.

So ``--xnu-dir`` now copies **only the files the patch set names** into a fresh
temporary directory and applies there.  Measured against the pinned tree: 50
distinct target files, 4.2 MB, against a 102 MB tree -- a 24x smaller scratch,
and one that provably cannot touch anything outside the patch set's footprint.
The caller's tree is then fingerprinted before and after and the run fails
loudly if it changed, so a regression to in-place application cannot be silent
again.

Usage:
    python verify_patches_against_upstream.py [--xnu-dir DIR] [--keep]
Exit 0 when every patch applies in order, 1 otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCK = REPO / "config" / "sources.lock.json"
PATCH_DIRS = (
    REPO / "vendor" / "UniXNU" / "patches" / "xnu",
    REPO / "vendor" / "UniXNU" / "kits" / "NativePrelink" / "patches",
)

DIFF_HEADER = re.compile(r"^diff --git a/(.*?) b/(.*)$")
#: Not every patch in this set carries a ``diff --git`` line.  Several begin
#: straight at ``--- a/PATH`` / ``+++ b/PATH`` -- measured on
#: ``native-union-getattrlistbulk.patch``, whose first line IS the ``---``.  The
#: first revision of ``targets_of`` read only the ``diff --git`` header and so
#: found 50 targets for 59 patches, which is under one per patch; the run then
#: failed 16 of them with "No such file or directory" for files that were in the
#: tree all along.  ``git apply -p1`` strips a leading ``a/`` or ``b/`` from
#: whichever header is present, so both forms are read.
UNIFIED_HEADER = re.compile(r"^(?:---|\+\+\+) ([ab])/(.*)$")


def _strip_dev_null(path: str) -> str | None:
    """``/dev/null`` means "no file here", which is not a target to copy."""
    if path in ("/dev/null", "dev/null"):
        return None
    return path


def targets_of(patch_list: list[Path]) -> set[str]:
    """Every worktree-relative path the patch set reads or writes.

    Read from the ``diff --git`` headers where present and the ``---``/``+++``
    headers otherwise, which is what ``git apply -p1`` strips.  Both sides are
    collected: a rename or a delete needs the source present even though only
    the destination is written.
    """
    found: set[str] = set()
    for patch in patch_list:
        for line in patch.read_text(encoding="utf-8", errors="replace").splitlines():
            match = DIFF_HEADER.match(line)
            if match:
                found.add(match.group(1))
                found.add(match.group(2))
                continue
            match = UNIFIED_HEADER.match(line)
            if match:
                path = _strip_dev_null(match.group(2))
                if path:
                    found.add(path)
    return found


def pinned_xnu() -> tuple[str, str]:
    """The (commit, repo) the lock pins for XNU."""
    with LOCK.open(encoding="utf-8") as handle:
        root = json.load(handle)
    for feed in root.get("feeds", []):
        if feed.get("name") == "xnu":
            return feed["commit"], feed["git"]
    raise SystemExit(f"{LOCK} pins no xnu feed")


def download(commit: str, repo: str, cache: Path) -> Path:
    """Fetch the tarball for one commit, reusing the cached copy."""
    cache.mkdir(parents=True, exist_ok=True)
    tarball = cache / f"xnu-{commit}.tar.gz"
    if not tarball.is_file():
        url = f"{repo.rstrip('/').removesuffix('.git')}/archive/{commit}.tar.gz"
        print(f"downloading {url}")
        with urllib.request.urlopen(url) as response, tarball.open("wb") as out:
            shutil.copyfileobj(response, out)
    return tarball


def patches() -> list[Path]:
    """Every patch, in the same order ``patches/xnu/*.patch`` globs."""
    found: list[Path] = []
    for directory in PATCH_DIRS:
        if directory.is_dir():
            found += sorted(directory.glob("*.patch"))
    if not found:
        raise SystemExit("no patches found; the pinned vendor tree is not acquired")
    return found


def scratch_tree(source: Path, patch_list: list[Path], work: Path) -> tuple[Path, int, int]:
    """A throwaway copy of just the files the patch set names.

    Returns the tree, how many target files were copied, and how many the patch
    set names but the source tree does not have (new files, which ``git apply``
    creates and which are not an error).
    """
    wanted = targets_of(patch_list)
    copied = 0
    absent = 0
    for relative in sorted(wanted):
        origin = source / relative
        if not origin.is_file():
            absent += 1
            continue
        destination = work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, destination)
        copied += 1
    return work, copied, absent


def fingerprint(tree: Path, patch_list: list[Path]) -> dict[str, str]:
    """sha256 of every file the patch set touches, keyed by relative path.

    This is the guard.  It is taken before the run and again after, over exactly
    the files the patches could have written, so a tool that mutates its input
    is caught by a digest rather than by a unit test three directories away.
    """
    digests: dict[str, str] = {}
    for relative in sorted(targets_of(patch_list)):
        path = tree / relative
        if not path.is_file():
            continue
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def normalise(patch: Path, staging: Path) -> tuple[Path, bool]:
    """Copy one patch into ``staging`` with LF endings, saying if that changed it.

    ``vendor/UniXNU/.gitattributes`` declares ``*.patch text eol=lf``, so a patch
    is LF in the repository and LF on every checkout -- except inside the
    ``kits/NativePrelink`` SUBMODULE, which carries no ``.gitattributes`` of its
    own.  A host with ``core.autocrlf=true`` therefore checks that submodule's
    patches out as CRLF while ``refer/xnu``'s sources stay LF, and ``git apply``
    matches context byte for byte: every context line then carries a trailing CR
    that the target does not have, and the patch is refused.

    Measured 2026-10-30: both ``kits/NativePrelink/patches`` files are 99 and 58
    lines of pure LF in their committed blobs and CRLF in a Windows worktree.
    They build fine on the macOS runner, which is what makes this a checkout
    artifact rather than a defect -- and a reproduction that is wrong on Windows
    and right on macOS is worse than no reproduction, because it reports a
    failure that does not exist.  So the bytes git actually has are the ones
    applied here, and the normalisation is reported rather than done quietly.
    """
    raw = patch.read_bytes()
    if b"\r\n" not in raw:
        return patch, False
    target = staging / patch.name
    target.write_bytes(raw.replace(b"\r\n", b"\n"))
    return target, True


def apply_in_order(tree: Path, patch_list: list[Path], staging: Path) -> list[str]:
    """Apply each patch with the real git, in order.  Returns the failures."""
    failures = []
    normalised = []
    for original in patch_list:
        patch, changed = normalise(original, staging)
        if changed:
            normalised.append(original.name)
        check = subprocess.run(
            ["git", "apply", "--check", str(patch)],
            cwd=str(tree), capture_output=True, text=True, errors="replace",
        )
        if check.returncode == 0:
            real = subprocess.run(
                ["git", "apply", str(patch)],
                cwd=str(tree), capture_output=True, text=True, errors="replace",
            )
            if real.returncode != 0:
                failures.append(
                    f"{patch.name}: --check passed but apply failed\n{real.stderr.strip()}"
                )
            continue
        failures.append(
            f"{patch.name}: does not apply\n{check.stderr.strip()[:400]}"
        )
    if normalised:
        print(f"  note: {len(normalised)} patch(es) were CRLF in the worktree and were "
              f"applied as the LF bytes git has: {', '.join(sorted(normalised))}")
    return failures


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xnu-dir", type=Path, default=None,
                        help="use this already-extracted XNU tree instead of downloading")
    parser.add_argument("--keep", action="store_true",
                        help="leave the extracted tree on disk for inspection")
    parser.add_argument("--allow-in-place", action="store_true",
                        help="apply into --xnu-dir itself, mutating it.  Requires "
                             "the tree to be a git repository so it can be restored")
    args = parser.parse_args(argv)

    commit, repo = pinned_xnu()
    patch_list = patches()

    if args.xnu_dir is not None:
        source = args.xnu_dir.resolve()
        if not (source / "bsd" / "kern" / "bsd_init.c").is_file():
            print(f"{source} is not an extracted XNU tree", file=sys.stderr)
            return 2

        before = fingerprint(source, patch_list)
        work: Path | None = None
        try:
            if args.allow_in_place:
                # The only path that writes to the caller's tree, and it is
                # opt-in for exactly that reason.  It still refuses unless the
                # tree is a git repository, because a tree that cannot be
                # restored cannot be verified afterwards.
                if not (source / ".git").exists():
                    print(f"REFUSED: --allow-in-place needs a git repository so the "
                          f"tree can be restored afterwards; {source} has no .git",
                          file=sys.stderr)
                    return 2
                print(f"applying IN PLACE to {source} (--allow-in-place)")
                tree = source
            else:
                work = Path(tempfile.mkdtemp(prefix="unipad-xnu-scratch-"))
                tree, copied, absent = scratch_tree(source, patch_list, work)
                print(f"scratch      : {tree}")
                print(f"  copied {copied} of the {len(targets_of(patch_list))} files the "
                      f"patch set names; {absent} are new files it creates")
                print(f"  {source} is READ-ONLY for this run")

            with tempfile.TemporaryDirectory(prefix="unipad-patch-lf-") as staging_name:
                failures = apply_in_order(tree, patch_list, Path(staging_name))
        finally:
            if work is not None and not args.keep:
                shutil.rmtree(work, ignore_errors=True)

        # The guard, and the reason --xnu-dir is safe to point at a tree other
        # code depends on.  Taken over the same file set as `before`.
        after = fingerprint(source, patch_list)
        if after != before:
            changed = sorted(k for k in before if before.get(k) != after.get(k))
            print(f"THE SOURCE TREE CHANGED.  {len(changed)} of the files the patch set "
                  f"names now differ:", file=sys.stderr)
            for name in changed[:20]:
                print(f"  {name}", file=sys.stderr)
            print("This run was not supposed to write to it.  Restore the tree before "
                  "anything else reads it.", file=sys.stderr)
            return 1
        print(f"source tree  : unchanged ({len(before)} files fingerprinted, "
              f"{source.name} is pristine)")
    else:
        cache = Path(tempfile.gettempdir()) / "unipad-xnu-cache"
        tarball = download(commit, repo, cache)
        work = Path(tempfile.mkdtemp(prefix="unipad-xnu-verify-"))
        with tarfile.open(tarball, "r:gz") as archive:
            archive.extractall(work, filter="data")
        extracted = [p for p in work.iterdir() if p.is_dir()]
        if len(extracted) != 1:
            print(f"expected one directory in the tarball, found {len(extracted)}",
                  file=sys.stderr)
            return 2
        tree = extracted[0]

        try:
            with tempfile.TemporaryDirectory(prefix="unipad-patch-lf-") as staging_name:
                failures = apply_in_order(tree, patch_list, Path(staging_name))
        finally:
            if not args.keep:
                shutil.rmtree(work, ignore_errors=True)

    print(f"{len(patch_list)} patches against XNU {commit[:12]}")
    for failure in failures:
        print(f"  FAIL {failure}")
    if failures:
        print(f"{len(failures)} patch(es) do not apply")
        return 1
    print("every patch applies, in the build's order")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
