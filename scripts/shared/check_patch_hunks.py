#!/usr/bin/env python3
"""Check every hunk of a unified diff against its own @@ header.

Why this exists
---------------
A hunk header carries the line counts of both sides.  Adding lines to the ``+``
side without updating the count produces a file that reads perfectly well as
text and that ``git apply`` refuses with "corrupt patch".  That is exactly what
happened on 2026-10-30: an edit to
``vendor/UniXNU/patches/xnu/zzy-native-gpu-addon-mount.patch`` grew one comment
by seven lines, and the failure surfaced only on the runner, at
``build-xnu-4k-baseline.sh:44``'s ``git apply --check``, after the whole owner
root chain had run.  A cost of one runner cycle for a check that takes a
millisecond locally.

Usage:  python check_patch_hunks.py PATCH [PATCH ...]
Exit 0 when every hunk's counts match its header, 1 otherwise.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def check(path: Path) -> list[str]:
    problems: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    hunks = 0
    # Where the previous hunk of THIS file left each side.  A hunk's declared
    # start has to be reachable from there by the SAME gap on both sides --
    # otherwise the two sides disagree about where the hunk is, and git apply
    # refuses the whole patch as corrupt.  Counting only the line totals is not
    # enough: that is exactly the gap that let run 40981242194 fail at
    # build-xnu-4k-baseline.sh:44 on a patch whose every count was correct.
    previous = None
    while index < len(lines):
        line = lines[index]
        if line.startswith(("--- ", "+++ ", "diff ", "index ", "new file", "deleted file")):
            # A new file starts its own line numbering.  Without this reset the
            # second file's first hunk is measured against the end of the first
            # file's last one, which reports a problem in every multi-file patch
            # in the tree -- the same instrument-fails-on-known-good shape as the
            # '--- a/' header being counted as a removal.
            if line.startswith("--- "):
                previous = None
            index += 1
            continue
        match = HUNK.match(line)
        if match is None:
            index += 1
            continue
        hunks += 1
        old_start = int(match.group(1))
        old_count = int(match.group(2) or 1)
        new_start = int(match.group(3))
        new_count = int(match.group(4) or 1)
        header_line = index + 1
        if previous is not None:
            old_at, new_at = previous
            gap_old = old_start - old_at
            gap_new = new_start - new_at
            if gap_old != gap_new:
                problems.append(
                    f"{path}: hunk at line {header_line} is unreachable: the old "
                    f"side reaches it after {gap_old} unchanged lines and the new "
                    f"side after {gap_new}. The two sides disagree about where "
                    f"this hunk is, so git apply refuses the patch as corrupt"
                )
        index += 1
        seen_old = seen_new = 0
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line.startswith("\\"):  # "\ No newline at end of file"
                index += 1
                continue
            # The next file's headers are NOT hunk body.  '--- a/...' and
            # '+++ b/...' start with the same characters as a removal and an
            # addition, and counting them as such is how this checker's first
            # revision reported a problem in every patch in the tree -- including
            # ones nobody had touched.
            if line.startswith(("--- ", "+++ ", "diff ", "index ", "new file", "deleted file")):
                break
            if not line:
                # A genuinely empty context line.  A trailing blank at the end
                # of the file is not part of the hunk, so stop there.
                if index + 1 >= len(lines):
                    break
                seen_old += 1
                seen_new += 1
            elif line.startswith("+"):
                seen_new += 1
            elif line.startswith("-"):
                seen_old += 1
            else:
                seen_old += 1
                seen_new += 1
            index += 1
        if seen_old != old_count or seen_new != new_count:
            problems.append(
                f"{path}: hunk at line {header_line} declares -{old_count},+{new_count} "
                f"but carries -{seen_old},+{seen_new}"
            )
        else:
            previous = (old_start + old_count, new_start + new_count)
    if hunks == 0:
        problems.append(f"{path}: no hunk header found; is this a unified diff?")
    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_patch_hunks.py PATCH [PATCH ...]", file=sys.stderr)
        return 2
    problems: list[str] = []
    for name in argv:
        problems += check(Path(name))
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s)")
        return 1
    print(f"{len(argv)} patch(es), every hunk's counts match its header")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
