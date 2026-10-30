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

Usage:
    python verify_patches_against_upstream.py [--xnu-dir DIR] [--keep]
Exit 0 when every patch applies in order, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
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
    args = parser.parse_args(argv)

    commit, repo = pinned_xnu()
    patch_list = patches()

    if args.xnu_dir is not None:
        tree = args.xnu_dir.resolve()
        if not (tree / "bsd" / "kern" / "bsd_init.c").is_file():
            print(f"{tree} is not an extracted XNU tree", file=sys.stderr)
            return 2
        work = None
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
        if work is not None and not args.keep:
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
