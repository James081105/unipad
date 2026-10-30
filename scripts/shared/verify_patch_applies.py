#!/usr/bin/env python3
"""Verify a unified diff actually applies, against a synthetic '-' side.

``git apply --check`` is the authority on whether a patch is well-formed, and it
needs the target files.  This reconstructs the '-' side of every hunk into a
throwaway git repo and runs the real check, so a corrupt hunk header is caught
here rather than on the runner.

On 2026-10-30 an edit to zzy-native-gpu-addon-mount.patch grew a comment by 13
lines and left the hunk header at its old count.  The file read perfectly well
as text; ``git apply`` refused it with "corrupt patch" at
build-xnu-4k-baseline.sh:44, after the whole owner root chain had already run.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def minus_side(patch_path: Path) -> dict[str, list[str]]:
    lines = patch_path.read_text(encoding="utf-8").splitlines()
    files: dict[str, list[str]] = {}
    target = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- a/"):
            target = line[len("--- a/"):]
            files.setdefault(target, [])
        elif line.startswith("--- /dev/null"):
            # A patch that creates a file.  The '-' side is empty, which is
            # what ``-0,0`` in the hunk header already says; without this the
            # helper reports "no file header" on a perfectly good patch.
            target = line[len("--- "):]
            files.setdefault(target, [])
        elif HUNK.match(line):
            index += 1
            while index < len(lines) and not lines[index].startswith("@@"):
                body = lines[index]
                if body.startswith("\\"):
                    index += 1
                    continue
                if body.startswith(("--- ", "+++ ", "diff ")):
                    break
                if body.startswith("+"):
                    pass
                elif body.startswith("-"):
                    files[target].append(body[1:])
                else:
                    files[target].append(body[1:] if body else "")
                index += 1
            continue
        index += 1
    return files


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: verify_patch_applies.py PATCH", file=sys.stderr)
        return 2
    patch_path = Path(argv[0]).resolve()
    files = minus_side(patch_path)
    if not files:
        print(f"{patch_path.name}: no file header found; is this a unified diff?")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        for name, body in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(body) + "\n", encoding="utf-8")
        proc = subprocess.run(
            ["git", "apply", "--check", "-v", str(patch_path)],
            cwd=root, capture_output=True, text=True,
        )
        print(f"git apply --check exit: {proc.returncode}")
        output = (proc.stdout + proc.stderr).strip()
        if output:
            print(output[:800])
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
