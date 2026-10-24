"""A process that takes (or fails to take) the real acceptance lock.

Run as ``python -m tests.acceptance.lock_helper <mode> <run_dir>``.  It exists so
the lock can be tested across *real* process boundaries: the properties that
matter -- released on exit, arbitrated between contenders, not disturbing the
holder -- are properties of the host kernel's object, and none of them can be
observed from inside a single process.

It goes through :func:`unipad.acceptance.runner._run_lock` rather than through
``_acquire_lock`` directly, because that is the code production actually uses
and it is what writes the holder's diagnostic note.  A helper that called the
primitives directly would test a path nobody runs.

Modes
-----
``hold SECONDS``
    Take the lock, report it, hold it for SECONDS, release it, exit 0.
``try``
    Attempt to take the lock without waiting.  Report the outcome and exit
    immediately -- this process never holds anything, so it cannot be
    contending with anybody by the time it returns.
``try-hold SECONDS``
    Attempt to take the lock; if it is free, take it and *keep* it for SECONDS
    before releasing.  This is what makes two contenders genuinely concurrent:
    with plain ``try`` the first would acquire, exit, and free the lock before
    the second even started, and both would report success.
``die``
    Take the lock, report it, then ``os._exit`` without releasing -- the
    closest portable analogue to being killed.  The kernel still closes every
    handle the process owned, which is the whole point of the design.

Every mode prints one line beginning with ``RESULT`` so a caller can parse the
outcome without depending on anything else the process might say.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from unipad.acceptance.runner import _run_lock
from unipad.errors import ErrorCode, UniPadError


def _report(kind: str, **fields: object) -> None:
    parts = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"RESULT {kind} {parts}".rstrip(), flush=True)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        _report("usage-error", expected="<mode> <run_dir>")
        return 2
    mode, run_dir = argv[1], Path(argv[2])
    run_dir.mkdir(parents=True, exist_ok=True)

    holds = mode in ("hold", "try-hold")
    try:
        # The real context manager, so the diagnostic note and the release path
        # are the ones production uses.
        with _run_lock(run_dir):
            _report("acquired", pid=os.getpid())
            if mode == "die":
                # ``os._exit`` terminates immediately: no ``finally``, no
                # ``atexit``, no ``__exit__``.  The kernel closes the handle.
                # Anything that released the lock here would be testing the
                # cleanup path rather than the recovery path.
                os._exit(9)
            if holds:
                time.sleep(float(argv[3]) if len(argv) > 3 else 5.0)
            elif mode not in ("hold", "try", "die"):
                _report("usage-error", mode=mode)
                return 2
    except UniPadError as exc:
        if exc.code != ErrorCode.BLOCKED:
            raise
        _report("blocked", reason=exc.message, pid=os.getpid())
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
