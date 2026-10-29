#!/usr/bin/env python3
"""Run an upstream check script, tolerating only the steps whose target file the pinned tree does not contain.

Why this exists
---------------
``vendor/UniXNU/scripts/check-turnip-darwin-host.py:93`` runs
``scripts/check-quartzcore-turnip-cache-host.py`` with ``check=True``.  That
file does not exist.  It is absent from all three trees that were read live on
2026-10-29 -- ``vendor/UniXNU`` (the driver, pin ``ab3199c9``),
``vendor/UniXNU-qemu`` (the device repo, pin ``a88fc1a4``) and upstream UniXNU
main -- while ``vendor/UniXNU/docs/drivers/native-adreno.md:138`` and its
Chinese twin still document it as one of the device repository's
original-input checks.  It is an upstream omission, not a fork defect, and
nothing in this repository can supply it: re-implementing it from the docs
would be inventing a check whose contract we do not have, which is the one
thing the PRD forbids mixing into a build.

So that one step is skipped, loudly, and only that step.  Every other
subprocess the upstream script runs passes through untouched; any nonzero exit
still fails the whole run; and the skip is printed with the exact absolute path
so a reader of the build log can see the chain was not fully exercised.  A
silent pass here would be worse than the failure it replaces, which is why the
tolerated paths are an explicit argument rather than a pattern.

What this is NOT: a general "ignore missing files" shim.  The guard matches an
exact absolute path.  If the file appears upstream later, the guard stops
matching, the real check runs, and this wrapper becomes a no-op passthrough --
which is the intended end state.

Usage
-----
    run-upstream-check.py <script.py> <absent-path> [<absent-path> ...] [-- <script args>]

``<script.py>`` is executed with ``runpy`` under ``__main__`` so its
``Path(__file__).resolve().parents[1]`` still resolves to the pinned tree.  The
absent paths are compared after ``Path.resolve()``, so a relative argument
resolves against the caller's working directory, not against the pinned tree.
"""
from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

USAGE = "usage: run-upstream-check.py <script.py> <absent-path> [...] [-- <script args>]"


def _split_argv(argv: list[str]) -> tuple[Path, set[Path], list[str]]:
    """Parse ``script absent... [-- script args...]`` out of ``argv[1:]``."""
    if "--" in argv:
        cut = argv.index("--")
        head, tail = argv[:cut], argv[cut + 1:]
    else:
        head, tail = argv, []
    if not head:
        raise SystemExit(USAGE)
    script = Path(head[0]).resolve()
    if not script.is_file():
        raise SystemExit(f"no such upstream script: {script}")
    return script, {Path(item).resolve() for item in head[1:]}, tail


def main(argv: list[str]) -> int:
    script, tolerated, forwarded = _split_argv(argv)
    if not tolerated:
        raise SystemExit(f"nothing to tolerate; run {script.name} directly instead: {USAGE}")

    real_run = subprocess.run

    def _tolerated(args) -> Path | None:
        """The first argv element that names a path the caller said to tolerate.

        Position-independent on purpose: the upstream scripts invoke their
        helpers as ``[sys.executable, ROOT / 'scripts/check-....py', ...]``, so
        the absent file is ``args[1]``, not ``args[0]``.  Matching only argv[0]
        would silently tolerate nothing at all.
        """
        for item in args:
            if not isinstance(item, (str, Path)):
                continue
            try:
                resolved = Path(item).resolve()
            except OSError:
                continue
            if resolved in tolerated:
                return resolved
        return None

    def guarded_run(args, *rest, **kwargs):
        """Pass every call through, except a call to a path known to be absent."""
        skipped = _tolerated(args)
        if skipped is not None:
            # Deliberately on stdout: this is the one line a reader must not
            # miss, and it is the reason the chain is partial.
            print(
                f"SKIPPED (the pinned tree does not contain this file; "
                f"not implemented here): {skipped}",
                flush=True,
            )
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, *rest, **kwargs)

    subprocess.run = guarded_run
    sys.argv = [str(script), *forwarded]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exit_exc:
        # The upstream script's own failure path.  Keep its exit status: a skip
        # must never turn a real failure into a success.
        code = exit_exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(f"upstream check failed: {code}", file=sys.stderr, flush=True)
        return 1
    finally:
        subprocess.run = real_run
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
