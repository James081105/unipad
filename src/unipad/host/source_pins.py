"""The lock's module-submodule commits, applied wherever the driver lays sources down.

Why this file exists
--------------------
``config/sources.lock.json`` is the authority for every commit this project
builds (PRD 8.1).  For ``drivers/NativeMobileDisplay`` it deliberately disagrees
with the commit UniXNU's own gitlink records, because the pinned device
repository cannot compile the gitlink's version -- measured on run 40187668943,
where ``NativeLegacyDisplay.cpp:206:59: error: only virtual member functions can
be marked 'override'`` stopped the owner root chain at its step 9.

The disagreement is only useful if the lock wins *everywhere the sources are
laid down*, and there are three such places, which is what makes this a module
rather than a helper in whichever caller needed it first:

1. the workflow's acquisition step, before ``unixnu fetch`` initialises the
   submodules from the gitlink (``feeds.py:159-165`` reads the DRIVER
   repository's gitlink table -- a second place that knows a sha);
2. the chain step, before ``build-os-root.py`` compiles the display driver;
3. **every** ``unixnu build`` and ``unixnu fetch`` afterwards, because
   ``cli.py:79-86`` -> ``feeds.py:159-165`` runs ``git submodule update --init
   --recursive``, which resets every module submodule to the gitlink's commit.

The third one is the one that was missing, and it is the reason run 40209085545
died: the chain built the Legacy display archive at the locked ``eb9c949``, the
second build pass's ``unixnu build kernel`` reset the checkout to the gitlink's
``c031d882``, and ``build-xnu-4k-baseline.sh:735`` then refused to link with
``Rebuild the owned Legacy archive after changing
drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp``.  A caller that has to
*remember* to re-apply the pin is the same defect one level up, so
``build-macos.sh`` applies it inside ``run_driver`` -- after every driver call,
whatever the subcommand, idempotently.

What is proven here
-------------------
That a checkout whose HEAD differs from the lock is moved to the locked commit
and reported, that a checkout already there is left alone, that a submodule the
driver never fetched is not invented, and that a commit that cannot be had is a
refusal rather than a silence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unipad.errors import ErrorCode, ExitCode, UniPadError

__all__ = [
    "SubmodulePin",
    "SourcePinError",
    "apply_divergences",
    "read_pins",
]

#: The one submodule this project pins away from the driver's gitlink, spelled
#: the way the lock spells it.  Named here only so the module's own docstring and
#: its refusal messages can be specific; nothing branches on it.
DISPLAY_SUBMODULE = "drivers/NativeMobileDisplay"


class SourcePinError(UniPadError):
    """A submodule could not be put where the lock pins it."""


@dataclass(frozen=True)
class SubmodulePin:
    """One ``submodules`` entry of the lock.

    ``commit`` is the one to build; ``gitlink_commit`` is what the driver's own
    gitlink records, and it is carried so a report can say *what was moved
    from* rather than only what it was moved to.  A lock that records the
    divergence is auditable; a ``commit`` that quietly disagrees with the
    gitlink is the class of defect this project keeps hitting.
    """

    path: str
    commit: str
    gitlink_commit: str = ""


def read_pins(lock_path: Path) -> list[SubmodulePin]:
    """Every module submodule the lock pins, in the lock's own order.

    An entry with no ``commit`` is skipped rather than guessed at: the lock is
    the authority, so an entry that does not say where to go has nothing to
    apply, and inventing a commit for it would be exactly the second source of
    truth this module exists to prevent.
    """
    try:
        payload = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as failure:
        raise SourcePinError(
            ErrorCode.ARTIFACT_MISSING,
            f"the sources lock could not be read: {failure}",
            lock=str(lock_path),
        ) from failure
    pins: list[SubmodulePin] = []
    for entry in payload.get("submodules") or []:
        commit = (entry.get("commit") or "").strip()
        if not commit:
            continue
        pins.append(
            SubmodulePin(
                path=entry["path"],
                commit=commit,
                gitlink_commit=(entry.get("gitlink_commit") or "").strip(),
            )
        )
    return pins


def _git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SourcePinError(
            ErrorCode.VERIFICATION_FAILED,
            f"git {' '.join(arguments)} failed in {directory}",
            directory=str(directory),
            stderr=result.stderr.strip()[-500:],
        )
    return result.stdout.strip()


def _head_of(directory: Path) -> str:
    """The checkout's HEAD, or ``""`` when there is nothing to ask.

    A submodule the driver never fetched has no ``.git`` worth asking, and
    ``rev-parse`` failing is the honest answer to "where is this?" -- it is not
    an error, because the caller's question is whether to *move* it.
    """
    if not (directory / ".git").exists():
        return ""
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def apply_divergences(
    driver_root: Path, pins: list[SubmodulePin]
) -> list[dict[str, str]]:
    """Move every diverging module submodule to the commit the lock pins.

    Returns one record per submodule that was moved, each carrying the path, the
    commit it was at, the commit it is at now, and the gitlink the lock records
    -- so a reader of the report can tell a deliberate divergence from an
    accident without re-reading the lock.

    A submodule that is not on disk is skipped, not created: ``unixnu fetch``
    initialises them from the gitlink, and putting material in ``vendor/`` that
    no pin asked for is what the vendor-ignore rule exists to prevent.
    """
    driver_root = Path(driver_root)
    moved: list[dict[str, str]] = []
    for pin in pins:
        directory = driver_root / pin.path
        if not directory.is_dir():
            continue
        head = _head_of(directory)
        if head == pin.commit:
            continue
        # --depth 1 because the runner clones shallow and the wanted commit is
        # reachable from origin; a full fetch of a repository this project does
        # not own is bandwidth nobody asked for.
        _git(directory, "fetch", "-q", "--depth", "1", "origin", pin.commit)
        _git(directory, "checkout", "-q", "--detach", "FETCH_HEAD")
        after = _git(directory, "rev-parse", "HEAD")
        if after != pin.commit:
            raise SourcePinError(
                ErrorCode.VERIFICATION_FAILED,
                f"{pin.path} is at {after}, not the locked {pin.commit}",
                path=pin.path,
                commit=pin.commit,
                head=after,
            )
        record: dict[str, str] = {
            "path": pin.path,
            "commit": pin.commit,
            "from": head,
        }
        if pin.gitlink_commit:
            record["gitlink_commit"] = pin.gitlink_commit
        moved.append(record)
    return moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m unipad.host.source_pins",
        description=(
            "Put every module submodule of the driver repository at the commit "
            "config/sources.lock.json pins, and report what moved (PRD 8.1)."
        ),
    )
    parser.add_argument(
        "--lock",
        default="config/sources.lock.json",
        help="the sources lock carrying the submodule pins (default: %(default)s)",
    )
    parser.add_argument(
        "--driver",
        default="vendor/UniXNU",
        help="the driver repository whose submodules are pinned (default: %(default)s)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing when there is nothing to say",
    )
    args = parser.parse_args(argv)

    pins = read_pins(Path(args.lock))
    moved = apply_divergences(Path(args.driver), pins)
    if moved or not args.quiet:
        for record in moved:
            print(
                f"{record['path']}: {record['from'] or '<absent>'} -> "
                f"{record['commit']}"
                + (
                    f" (the lock records the driver's gitlink as "
                    f"{record['gitlink_commit']})"
                    if record.get("gitlink_commit")
                    else ""
                )
            )
        print(f"source pins: {len(pins)} pinned, {len(moved)} moved")
    return int(ExitCode.OK)


if __name__ == "__main__":  # pragma: no cover - the CLI is exercised as a process
    try:
        sys.exit(main())
    except UniPadError as failure:
        print(f"source_pins: {failure}", file=sys.stderr)
        sys.exit(int(failure.code.exit_code if hasattr(failure.code, "exit_code") else 1))
