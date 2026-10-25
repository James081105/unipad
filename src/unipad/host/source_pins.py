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
laid down*, and there are two halves to that, which is what makes this a module
rather than a helper in whichever caller needed it first:

1. **Move the checkout.**  ``unixnu fetch`` initialises the module submodules
   from the DRIVER repository's own gitlink table (``feeds.py:159-165``) -- a
   second place that knows a sha -- and every caller that lays sources down has
   to apply the lock afterwards.  The workflow's acquisition step and
   ``build-macos.sh``'s ``run_driver`` both do, through :func:`pin`.
2. **Stop it being moved back.**  ``cli.py:79-86`` -> ``feeds.py:159-165`` runs
   ``git submodule update --init --recursive`` at the START of every ``unixnu
   build`` and ``unixnu fetch``, which resets every module submodule to the
   gitlink's commit.  Re-applying the pin afterwards is too late: measured on
   run 40217489782, the re-pin correctly moved ``c031d882 -> eb9c949`` at
   17:50:36.310, one second AFTER the link script had already refused at
   17:50:35.288.  :func:`protect` is the half that operates at the right moment,
   because the command doing the reset is the command that reads the setting.

The first attempt at half 2 put the repair inside ``run_driver`` and believed it
was sufficient; the runner measured otherwise, in the log, with both timestamps.

What is proven here
-------------------
That a checkout whose HEAD differs from the lock is moved to the locked commit
and reported, that a checkout already there is left alone, that a submodule the
driver never fetched is not invented, that a commit that cannot be had is a
refusal rather than a silence, and that a protected submodule survives the
driver's own ``submodule update``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unipad.errors import ErrorCode, ExitCode, UniPadError

__all__ = [
    "PinResult",
    "SubmodulePin",
    "SourcePinError",
    "apply_divergences",
    "pin",
    "protect",
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


@dataclass(frozen=True)
class PinResult:
    """What :func:`pin` did, for the caller's record.

    ``moved`` is the list of submodules that were at the wrong commit and were
    put right; ``protected`` is the list the driver's own ``submodule update``
    will now skip.  They are different lists on purpose: a tree that is already
    correct moves nothing and still needs protecting, and a report that folded
    the two together could not say so.
    """

    moved: list[dict[str, str]] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)


def pin(driver_root: Path, pins: list[SubmodulePin]) -> PinResult:
    """Move every diverging submodule to the locked commit, then keep it there.

    One call, because two calls is the defect this module exists to prevent:
    the acquisition step used to apply the pins and the chain step used to apply
    them again, and neither could see a reset that happened afterwards.  The
    ``protect`` half is what operates at the right moment -- the command doing
    the reset reads the setting -- and the ``apply_divergences`` half is what
    puts the checkout there in the first place and repairs it if anything ever
    does move it.

    The CLI must go through this and not call ``protect`` itself: the first
    version of it printed the protected count with its own ``protect(...)``
    call, so removing the call from ``pin`` changed nothing and the test that
    was supposed to catch that passed anyway.
    """
    return PinResult(
        moved=apply_divergences(driver_root, pins),
        protected=protect(driver_root, pins),
    )


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


def protect(driver_root: Path, pins: list[SubmodulePin]) -> list[str]:
    """Stop the driver from moving the pinned submodules back to the gitlink.

    ``feeds.update_submodules`` runs ``git -C <base> submodule update --init
    --recursive`` (``feeds.py:159-165``), which resets every module submodule to
    the commit the DRIVER's gitlink records.  Measured locally against the real
    ``vendor/UniXNU``::

        $ git -C vendor/UniXNU submodule update --init --recursive
        Submodule path 'drivers/NativeMobileDisplay': checked out 'c031d882...'

    -- and that is what run 40209085545 and run 40217489782 both died on, in
    ``build-xnu-4k-baseline.sh:735``:

        Rebuild the owned Legacy archive after changing
        drivers/NativeMobileDisplay/NativeLegacyDisplay.cpp

    Re-applying the pin *after* the driver call is not enough, and that was
    measured too: run 40217489782's log shows the re-pin working correctly
    (``c031d882 -> eb9c949``) at 17:50:36.310, one second *after* the link
    script had already refused at 17:50:35.288.  The reset happens at the START
    of the driver call, so everything the call builds -- the archives and the
    link -- sees the gitlink's commit no matter what runs afterwards.

    ``submodule.<path>.update=none`` is the one lever that operates at the right
    moment, because it is read by the very command doing the reset.  Measured
    against the same checkout::

        $ git -C vendor/UniXNU config submodule.drivers/NativeMobileDisplay.update none
        $ git -C vendor/UniXNU submodule update --init --recursive
        Skipping submodule 'drivers/NativeMobileDisplay'

    It is set in the driver repository's own ``.git/config``, which is local
    state and NOT a commit -- so it survives on a runner that clones the pinned
    repository, and it does not fork a public repository to express a pin.

    ``--init`` is skipped along with the update (measured: a removed submodule
    stays absent), so this must run only once the submodules are on disk.  In
    this workflow that is the order: ``unixnu fetch`` initialises them, then the
    pins are applied and protected.
    """
    driver_root = Path(driver_root)
    protected: list[str] = []
    for pin in pins:
        directory = driver_root / pin.path
        if not directory.is_dir():
            continue
        key = f"submodule.{pin.path}.update"
        result = subprocess.run(
            ["git", "-C", str(driver_root), "config", key, "none"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SourcePinError(
                ErrorCode.VERIFICATION_FAILED,
                f"could not stop the driver from updating {pin.path}",
                path=pin.path,
                stderr=result.stderr.strip()[-500:],
            )
        protected.append(pin.path)
    return protected


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
    result = pin(Path(args.driver), pins)
    if result.moved or not args.quiet:
        for record in result.moved:
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
        print(
            f"source pins: {len(pins)} pinned, {len(result.moved)} moved, "
            f"{len(result.protected)} protected from the driver's own reset"
        )
    return int(ExitCode.OK)


if __name__ == "__main__":  # pragma: no cover - the CLI is exercised as a process
    try:
        sys.exit(main())
    except UniPadError as failure:
        print(f"source_pins: {failure}", file=sys.stderr)
        sys.exit(int(failure.code.exit_code if hasattr(failure.code, "exit_code") else 1))
