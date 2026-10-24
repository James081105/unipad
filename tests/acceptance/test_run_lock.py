"""The acceptance run lock, across real process boundaries (goal §4.1).

The five cases the goal document names, each as its own test:

1. two processes on the same run id: one holds, the other *accurately* reports
   that it is occupied -- with the holder's diagnostic note, not a guess;
2. the second process must not interrupt or kill the first;
3. after the holder is killed, a later evaluation recovers;
4. two processes recovering a stale lock at the same moment must not both get
   into the critical section;
5. one process exiting must not delete a *new* holder's lock.

Why these are subprocess tests
------------------------------
Every property above belongs to the host kernel's lock object -- it is released
when the process exits, it is arbitrated between contenders, and a process that
never held it cannot release it.  None of that is observable from inside one
process, and a mock of the lock would test the mock.  So the lock is exercised
through :mod:`tests.acceptance.lock_helper`, which calls the real
``_acquire_lock`` / ``_release_lock``.

The defect these replace
------------------------
The lock used to record a PID and, before refusing, asked the OS whether that
PID still existed with ``os.kill(pid, 0)``.  Measured on this host on 2026-10-25,
that check cannot answer the question: ``os.kill(pid, 0)`` raises nothing at all
for a PID whose process has exited, and raises ``OSError`` rather than
``ProcessLookupError`` for a PID that never existed.  Every outcome was read as
"held", so the liveness check added in f15c9ef could never detect a stale lock
and the wedge it was written to fix survived.

Case 2 is the regression test for the property that matters either way: a
refusing process must leave the holder running and able to finish normally.
(The CTRL_C_EVENT explanation usually given for ``os.kill(pid, 0)`` on Windows
did *not* reproduce here -- probing a live holder left it untouched.  The
measurement, not the folklore, is what is recorded.)
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from unipad.acceptance.runner import LOCK_FILENAME

HELPER = [sys.executable, "-m", "tests.acceptance.lock_helper"]
REPO_ROOT = Path(__file__).resolve().parents[2]

#: How long a holder holds for, and how long the tests wait for a process to say
#: something.  Generous enough not to flake on a loaded machine, small enough
#: that a wedged lock fails the suite rather than hanging it.
HOLD_SECONDS = 10.0
STARTUP_TIMEOUT = 30.0


class _HelperProcess:
    """A lock-helper subprocess whose output can be read more than once.

    ``Popen.stdout.readline()`` consumes, so a test that waits for the RESULT
    line and then asserts on the whole transcript would be asserting on the
    empty remainder.  A reader thread accumulates everything instead, and
    :meth:`result` waits for the first line without eating it.
    """

    def __init__(self, mode: str, run_dir: Path, *extra: str) -> None:
        self._process = subprocess.Popen(
            [*HELPER, mode, str(run_dir), *extra],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            with self._lock:
                self._lines.append(line)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._process.returncode

    def result(self, timeout: float = STARTUP_TIMEOUT) -> str:
        """The helper's first ``RESULT`` line."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for line in self._lines:
                    if line.startswith("RESULT"):
                        return line.strip()
            if self._process.poll() is not None:
                break
            time.sleep(0.02)
        raise AssertionError(
            "the lock helper produced no RESULT line "
            f"(exit {self._process.returncode}): {self.transcript()!r}"
        )

    def transcript(self) -> str:
        with self._lock:
            return "".join(self._lines)

    def wait(self, timeout: float = STARTUP_TIMEOUT) -> str:
        """Everything the helper said, after it exits."""
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged lock
            self.kill()
            raise AssertionError(
                f"the lock helper did not exit within {timeout}s: {self.transcript()!r}"
            )
        self._reader.join(timeout=5)
        return self.transcript()

    def kill(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=STARTUP_TIMEOUT)
        self._reader.join(timeout=5)


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run-lock"


def test_a_second_process_accurately_reports_that_the_run_is_occupied(
    run_dir: Path,
) -> None:
    """Case 1: one holds, the other is told it is occupied, and by whom."""
    holder = _HelperProcess("hold", run_dir, str(HOLD_SECONDS))
    try:
        assert holder.result().startswith("RESULT acquired")

        contender = _HelperProcess("try", run_dir)
        try:
            outcome = contender.result()
        finally:
            contender.wait()
    finally:
        holder.kill()

    assert "RESULT blocked" in outcome, outcome
    # The refusal names the state, so an operator reading it knows the lock is
    # really held rather than merely unreadable.
    assert "held by another evaluation" in outcome, outcome
    # ...and the lock file carries the holder's diagnostic note, which is the
    # *only* thing the recorded PID is for.
    note = run_dir / LOCK_FILENAME
    assert note.is_file(), "the holder left no diagnostic note"
    text = note.read_text(encoding="utf-8")
    assert f"pid={holder.pid}" in text, text
    assert "since=" in text, text


def test_a_refusing_process_does_not_disturb_the_holder(run_dir: Path) -> None:
    """Case 2: the contender's refusal leaves the holder running and able to finish.

    A refusing process must not disturb the holder in any way -- not by
    signalling it, not by releasing its lock, not by anything else.  The old
    implementation's refusal path probed the holder's PID, which on Windows is
    not the harmless no-op it looks like.  If a refusal path is ever made to
    touch the holder, the holder dies here and the test says so.
    """
    holder = _HelperProcess("hold", run_dir, str(HOLD_SECONDS))
    try:
        assert holder.result().startswith("RESULT acquired")

        contender = _HelperProcess("try", run_dir)
        try:
            assert "RESULT blocked" in contender.result()
        finally:
            contender.wait()
        assert contender.returncode == 0, "the refused process did not exit cleanly"

        # The contender has come and gone.  The holder must still be alive...
        assert holder.returncode is None, "the contender's refusal killed the holder"
        # ...and must still be able to finish on its own terms, which only
        # happens if it still owns the lock.
        finished = holder.wait(timeout=HOLD_SECONDS + STARTUP_TIMEOUT)
    finally:
        holder.kill()

    assert holder.returncode == 0, finished
    assert "RESULT acquired" in finished, finished
    # A normal release takes its own diagnostic note with it.
    assert not (run_dir / LOCK_FILENAME).exists(), "the holder left its lock behind"


def test_a_killed_holders_lock_is_recovered_by_the_next_evaluation(
    run_dir: Path,
) -> None:
    """Case 3: a holder that goes away does not wedge the run directory.

    The lock is released by the kernel when the process exits, including when it
    is killed, so the next evaluation takes over instead of reading BLOCKED
    forever.  (That was the wedge the old ``O_EXCL``-create implementation had:
    two stopped background runs left two run ids permanently BLOCKED, each
    naming a holder that was not there.)
    """
    holder = _HelperProcess("hold", run_dir, str(HOLD_SECONDS))
    try:
        assert holder.result().startswith("RESULT acquired")
    finally:
        holder.kill()
    assert holder.returncode not in (None, 0), "the kill did not take effect"

    successor = _HelperProcess("try", run_dir)
    try:
        outcome = successor.result()
    finally:
        successor.wait()

    assert "RESULT acquired" in outcome, outcome
    # What is asserted here is that the successor *got in*.  It is deliberately
    # not asserted that the kernel reported the lock as abandoned: with a
    # zero-timeout wait the abandoned case is rarely observable at all, because
    # a killed holder usually held the last handle and the object is destroyed
    # with it -- so the successor's ``CreateMutexW`` creates a fresh one and the
    # wait returns WAIT_OBJECT_0.  Claiming "recovered" here would be claiming
    # something the platform does not reliably tell us.  The property that
    # matters -- the run directory is not wedged -- is what is checked.


def test_two_processes_recovering_a_stale_lock_do_not_both_enter(run_dir: Path) -> None:
    """Case 4: simultaneous recovery is arbitrated, not raced.

    Both contenders see the same abandoned lock at the same moment.  Exactly one
    may proceed; the other must be told the run is occupied.  A lock whose
    recovery is "delete the file, then create it again" lets both through, which
    is the failure this pins down.

    Both contenders use ``try-hold`` so they are genuinely concurrent: with
    plain ``try`` the first would acquire, exit, and free the lock before the
    second started, and both would report success -- a test that passes without
    having tested anything.
    """
    stale = _HelperProcess("die", run_dir)
    try:
        assert stale.result().startswith("RESULT acquired")
        stale.wait()
    finally:
        stale.kill()
    assert stale.returncode != 0, "the simulated kill did not take effect"

    first = _HelperProcess("try-hold", run_dir, str(HOLD_SECONDS))
    second = _HelperProcess("try-hold", run_dir, str(HOLD_SECONDS))
    try:
        outcomes = [first.result(), second.result()]
    finally:
        first.kill()
        second.kill()

    acquired = [line for line in outcomes if "RESULT acquired" in line]
    blocked = [line for line in outcomes if "RESULT blocked" in line]
    assert len(acquired) == 1, f"expected exactly one winner, got {outcomes}"
    assert len(blocked) == 1, f"the loser was not told the run is occupied: {outcomes}"


def test_a_losers_exit_does_not_delete_a_new_holders_lock(run_dir: Path) -> None:
    """Case 5: exiting without the lock must not release somebody else's.

    A process that tried, was refused, and exited holds nothing, so its exit
    cannot free the run.  The old implementation removed ``acceptance.lock`` in
    a ``finally`` block whether or not it had created it, which is precisely
    this defect.
    """
    holder = _HelperProcess("hold", run_dir, str(HOLD_SECONDS))
    try:
        assert holder.result().startswith("RESULT acquired")

        refused = _HelperProcess("try", run_dir)
        try:
            assert "RESULT blocked" in refused.result()
        finally:
            refused.wait()
        assert refused.returncode == 0, "the refused process did not exit cleanly"

        # The refused process is gone.  A third evaluation must still find the
        # original holder in place -- if the loser's exit had released the lock,
        # this one would acquire it.
        third = _HelperProcess("try", run_dir)
        try:
            outcome = third.result()
        finally:
            third.wait()
    finally:
        holder.kill()

    assert "RESULT blocked" in outcome, "a process that never held the lock released it on exit: " + outcome
    assert "held by another evaluation" in outcome, outcome


def test_the_lock_is_acquired_and_released_by_the_same_process(run_dir: Path) -> None:
    """The ordinary path: take it, hold it, give it back, leave nothing behind."""
    process = _HelperProcess("hold", run_dir, "0.2")
    try:
        assert process.result().startswith("RESULT acquired")
        output = process.wait(timeout=HOLD_SECONDS + STARTUP_TIMEOUT)
    finally:
        process.kill()

    assert process.returncode == 0, output
    assert "RESULT acquired" in output, output
    assert not (run_dir / LOCK_FILENAME).exists(), "a clean release left the lock file behind"


def test_two_different_runs_do_not_share_a_lock(tmp_path: Path) -> None:
    """The lock is per run, so two runs never block each other."""
    first_dir = tmp_path / "run-a"
    second_dir = tmp_path / "run-b"
    first = _HelperProcess("hold", first_dir, str(HOLD_SECONDS))
    try:
        assert first.result().startswith("RESULT acquired")
        second = _HelperProcess("try", second_dir)
        try:
            outcome = second.result()
        finally:
            second.wait()
    finally:
        first.kill()

    assert "RESULT acquired" in outcome, "the lock leaked between runs: " + outcome


@pytest.mark.skipif(sys.platform != "win32", reason="the os.kill(pid, 0) defect is Windows-specific")
def test_os_kill_pid_0_is_not_a_liveness_check_on_windows() -> None:
    """``os.kill(pid, 0)`` does not answer "does this PID exist" on Windows.

    This is the measurement that explains why the lock is the kernel's and not a
    PID check, and it is a test rather than a comment so that nobody can
    reintroduce ``os.kill(pid, 0)`` as a liveness probe on the strength of it
    *looking* like one.  Measured on this host on 2026-10-25; pinned here on
    2026-10-25.

    The two facts established:

    * a PID belonging to a process that has exited produces **no exception**,
      so the natural reading -- "no exception means alive" -- calls a dead
      process alive;
    * a PID that never existed raises ``OSError`` (``ERROR_INVALID_PARAMETER``),
      not ``ProcessLookupError``, so a caller that only treats
      ``ProcessLookupError`` as "gone" also calls it alive.

    Either way the answer is "held", which is why the check added in f15c9ef
    could not detect a stale lock and the wedge it was written to fix survived.
    """
    exited = [subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"]) for _ in range(3)]
    dead_pids = [child.pid for child in exited]
    for child in exited:
        child.wait()

    reported_stale = 0
    for pid in dead_pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            reported_stale += 1
        except OSError:
            pass  # the old code's ``except OSError: return True`` branch
    assert reported_stale == 0, (
        f"{reported_stale} of {len(dead_pids)} exited PIDs were correctly reported "
        "stale by os.kill(pid, 0), so the behaviour this lock's design was chosen "
        "for no longer holds.  Re-measure before trusting either the old check or "
        "this assertion."
    )

    with pytest.raises(OSError) as never_existed:
        os.kill(999_999, 0)
    assert not isinstance(never_existed.value, ProcessLookupError), (
        "a PID that never existed raised ProcessLookupError, so the old check "
        "would have worked for that case -- re-examine the assertion above"
    )
