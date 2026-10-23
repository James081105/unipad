"""Acceptance tests for the UniPad Windows launcher (PRD 3.2 P1 bullet 6).

These tests exercise the **real** ``scripts/windows/UniPad-Launcher.ps1`` by
launching ``powershell -NoProfile -ExecutionPolicy Bypass -File ...`` as a child
process and reading its exit code and stdout -- never a reimplementation of the
launcher's logic (a test that mirrors the code proves nothing).

What is *not* here, on purpose
------------------------------
No test starts a real QEMU.  Every path that would reach QEMU is either
(a) intercepted before launch by the launcher's ``-WhatIf`` dry run,
(b) a non-ASCII path that the launcher's pre-flight guard refuses, or
(c) a controller call that is blocked or fails before any guest exists
(``stop`` against an empty runs root, ``install`` of an absent IPA).  Two tests
do invoke the controller for real, on purpose: a ``-WhatIf`` plan only shows the
argv the launcher *intends*, and the two vectors are not the same one, so only a
real invocation pins the argv the CLI *actually* received.  ``Start`` delegates
to ``Start-UniPad.ps1``; the tests never invoke that delegation for real, only
its ``-WhatIf`` plan.  Field status stays ``UNVERIFIED``.

Load-bearing contract pinned here
---------------------------------
* usage errors (bad ``-Action``, missing required argument, wrong action/param
  combination, non-ASCII path) exit **2**, matching the CLI/USAGE convention;
* ``-WhatIf`` prints exactly one JSON plan object and exits 0;
* the repository root is resolved from the script's own location, so the plan
  is identical no matter which working directory the process was launched from;
* the Python probe reports which interpreter it chose (``python`` then ``py``);
* ``Start`` delegates to ``Start-UniPad.ps1`` rather than reimplementing it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Repository root: tests/acceptance/test_launcher_script.py -> repo.
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "windows" / "UniPad-Launcher.ps1"

pytestmark = pytest.mark.acceptance


def _powershell() -> str:
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    if not exe:
        pytest.skip("powershell.exe is not on PATH")
    return exe


def run_launcher(args: list[str], *, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """Invoke the real launcher script as a child process."""
    cmd = [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def plan_of(result: subprocess.CompletedProcess) -> dict:
    """Parse the single JSON plan object a ``-WhatIf`` run writes to stdout."""
    assert result.returncode == 0, f"expected exit 0, got {result.returncode}: {result.stderr}"
    text = result.stdout.strip()
    assert text, f"no stdout from -WhatIf run; stderr={result.stderr!r}"
    return json.loads(text)


def _same_path(a: str, b: Path) -> bool:
    return os.path.normcase(str(Path(a))) == os.path.normcase(str(b))


# ---------------------------------------------------------------------------
# usage errors -> exit 2 (matching the CLI's ExitCode.USAGE convention)
# ---------------------------------------------------------------------------


def test_bad_action_exits_2():
    result = run_launcher(["-Action", "definitely-not-an-action"])
    assert result.returncode == 2, result.stderr
    assert "start" in result.stderr and "restore" in result.stderr  # lists the valid actions


@pytest.mark.parametrize(
    "args",
    [
        ["-Action", "start"],          # missing -Artifact
        ["-Action", "install"],        # missing -Ipa
        ["-Action", "restore"],        # missing -Checkpoint
        ["-Action", "stop", "-Graceful", "-Force"],  # mutually exclusive
        ["-Action", "start", "-Artifact", "C:\\x.json", "-Ipa", "C:\\y.ipa"],  # wrong param
        ["-Action", "logs", "-Follow", "-Checkpoint", "cp1"],  # wrong param
    ],
)
def test_usage_errors_exit_2(args):
    result = run_launcher(args)
    assert result.returncode == 2, f"{args} -> {result.returncode}: {result.stderr}"


# ---------------------------------------------------------------------------
# the non-ASCII path guard -- the single most important robustness feature
# ---------------------------------------------------------------------------


@pytest.fixture()
def ascii_scratch():
    """An ASCII base directory (the repo) holding a throwaway sub-tree.

    The system ``%TEMP%`` on this host lives under a non-ASCII user directory,
    so it cannot isolate a non-ASCII *leaf*: the base must be ASCII and only the
    final component non-ASCII.  The repository root (``F:\\iosvirtual``) is
    ASCII, so a scratch subtree there does exactly that.
    """
    base = ROOT / ".pytest-launcher-scratch"
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_non_ascii_artifact_is_refused(ascii_scratch):
    """A non-ASCII artifact path must be refused *before* QEMU is ever started."""
    bad = ascii_scratch / "manifest-caf\u00e9.json"   # base ASCII, leaf has U+00E9
    bad.write_text("{}", encoding="utf-8")
    result = run_launcher(["-Action", "start", "-Artifact", str(bad)])
    assert result.returncode == 2, result.stderr
    combined = (result.stdout + result.stderr).lower()
    assert "ascii" in combined, f"guard did not explain itself: {result.stderr!r}"


def test_non_ascii_ipa_is_refused(ascii_scratch):
    bad = ascii_scratch / "\u6d4b\u8bd5.ipa"   # CJK leaf, ASCII base
    bad.write_bytes(b"x")
    result = run_launcher(["-Action", "install", "-Ipa", str(bad)])
    assert result.returncode == 2, result.stderr
    assert "ascii" in (result.stdout + result.stderr).lower()


def test_non_ascii_run_id_is_refused(ascii_scratch):
    """-Run is an *id* the controller joins to --runs-root, so it is a path too.

    Without this guard an operator could hand the controller a run id that
    puts QEMU on a non-ASCII directory, which is exactly the failure the guard
    exists to prevent -- just reached through a different parameter.
    """
    result = run_launcher(
        ["-Action", "stop", "-Run", "run-caf\u00e9", "-RunsRoot", str(ascii_scratch)]
    )
    assert result.returncode == 2, result.stderr
    assert "ascii" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize(
    "action,required",
    [
        # install (cli.py:1161/1168) and start (cli.py:986/989) both derive the
        # run directory -- and therefore the serial.log handed to QEMU -- from
        # the experiment id, so it is a path just as much as -Run is.
        ("install", ["-Ipa"]),
        ("start", ["-Artifact"]),
    ],
)
def test_non_ascii_experiment_id_is_refused(ascii_scratch, action, required):
    """-ExperimentId reaches QEMU through a path, so the guard must cover it.

    The guard already refuses a non-ASCII ``-Run``; ``-ExperimentId`` is the same
    kind of value (an id the controller joins to the runs root) and is accepted
    by exactly the two actions that go on to launch a guest, so a non-ASCII one
    used to sail past the guard and fail ~30s later inside QEMU with an opaque
    "open <path> failed".
    """
    leaf = ascii_scratch / "payload.bin"   # ASCII and absent: the guard is a
    # string check, so an absent file keeps the unguarded run cheap (the
    # controller refuses it with artifact_missing long before any guest).
    args = ["-Action", action, *required, str(leaf), "-ExperimentId", "exp-café"]
    result = run_launcher(args)
    assert result.returncode == 2, (
        f"{action} accepted a non-ASCII experiment id: {result.stdout}{result.stderr}"
    )
    assert "ascii" in (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# repo root is resolved from the script, not from the caller's working directory
# ---------------------------------------------------------------------------


def test_repo_root_resolves_from_foreign_cwd(tmp_path):
    foreign = tmp_path / "somewhere-else"
    foreign.mkdir()
    result = run_launcher(["-Action", "stop", "-WhatIf"], cwd=foreign)
    plan = plan_of(result)
    assert _same_path(plan["repo_root"], ROOT)
    # The controller runs with its working directory pinned to the repo root, so
    # relative defaults (runs/, artifacts/, config/*) resolve identically no
    # matter where the operator double-clicked from.
    assert _same_path(plan["working_directory"], ROOT)


# ---------------------------------------------------------------------------
# -WhatIf prints the resolved argv as one JSON object, exits 0, launches nothing
# ---------------------------------------------------------------------------


def test_whatif_start_delegates_to_start_unipad():
    artifact = "C:\\ascii-only\\artifact-manifest.json"
    plan = plan_of(run_launcher(["-Action", "start", "-Artifact", artifact, "-WhatIf"]))
    assert plan["action"] == "start"
    # Start must DELEGATE, not reimplement the TCG guard / verdict logic.
    assert plan["delegates_to"] == "Start-UniPad.ps1"
    cmd = plan["command"]
    assert "Start-UniPad.ps1" in " ".join(cmd)
    assert artifact in cmd


def test_whatif_controller_action_plans():
    ipa = "C:\\ascii-only\\App.ipa"
    install = plan_of(run_launcher(["-Action", "install", "-Ipa", ipa, "-WhatIf"]))
    assert "unipad.controller" in install["command"] and "install" in install["command"]
    assert ipa in install["command"]

    stop = plan_of(run_launcher(["-Action", "stop", "-Graceful", "-WhatIf"]))
    assert "unipad.controller" in stop["command"] and "stop" in stop["command"]
    assert "--graceful" in stop["command"]

    restore = plan_of(run_launcher(["-Action", "restore", "-Checkpoint", "cp-2026-01", "-WhatIf"]))
    assert "restore" in restore["command"]
    assert "--checkpoint" in restore["command"] and "cp-2026-01" in restore["command"]


def test_whatif_reports_python_interpreter():
    plan = plan_of(run_launcher(["-Action", "stop", "-WhatIf"]))
    assert plan["interpreter"], "the probe must report which interpreter it chose"


def test_whatif_logs_plan_points_at_serial_log():
    plan = plan_of(run_launcher(["-Action", "logs", "-Run", "exp-42", "-WhatIf"]))
    assert plan["action"] == "logs"
    assert plan["follow"] is False
    assert _same_path(plan["serial_log"], ROOT / "runs" / "exp-42" / "serial.log")

    followed = plan_of(run_launcher(["-Action", "logs", "-Run", "exp-42", "-Follow", "-WhatIf"]))
    assert followed["follow"] is True


# ---------------------------------------------------------------------------
# logs reads the serial log from disk without ever starting a guest
# ---------------------------------------------------------------------------


def test_logs_prints_serial_log(ascii_scratch):
    run_dir = ascii_scratch / "runs" / "exp-logs"
    run_dir.mkdir(parents=True)
    (run_dir / "serial.log").write_text("boot line 1\nboot line 2\n", encoding="utf-8")
    result = run_launcher(
        ["-Action", "logs", "-Run", "exp-logs", "-RunsRoot", str(ascii_scratch / "runs")]
    )
    assert result.returncode == 0, result.stderr
    assert "boot line 1" in result.stdout and "boot line 2" in result.stdout


def test_logs_without_follow_is_bounded_to_the_same_tail(ascii_scratch):
    """Non-follow `logs` must apply the same tail as `-Follow`, not dump the file.

    `-Follow` used `-Tail 100` while the plain read used no tail at all, so the
    two invocations of one verb disagreed about how much log the operator sees
    and an unbounded number of guest serial bytes could reach stdout.  A QEMU
    serial log grows for as long as the guest runs; the launcher is the
    operator's *view* of it, not a firehose.

    The bound must be a **tail**, not a head: the lines that explain a failure
    are the last ones.  And the truncation must be declared on stderr with the
    totals and the full path, so nothing is silently withheld.
    """
    run_dir = ascii_scratch / "runs" / "exp-big"
    run_dir.mkdir(parents=True)
    body = "".join(f"serial-line-{i:04d}\n" for i in range(400))
    (run_dir / "serial.log").write_text(body, encoding="utf-8")

    result = run_launcher(
        ["-Action", "logs", "-Run", "exp-big", "-RunsRoot", str(ascii_scratch / "runs")]
    )
    assert result.returncode == 0, result.stderr

    shown = [line for line in result.stdout.splitlines() if line.strip()]
    assert 0 < len(shown) < 400, (
        f"non-follow logs printed {len(shown)} of 400 lines; it must be bounded"
    )
    # a tail, not a head: the newest line is in, the oldest is out
    assert "serial-line-0399" in shown
    assert "serial-line-0000" not in shown
    # the truncation is declared, not silent
    assert "400" in result.stderr, f"stderr did not declare the total: {result.stderr!r}"
    assert str(run_dir / "serial.log") in result.stderr, (
        f"stderr did not point at the full log: {result.stderr!r}"
    )


def test_logs_follow_and_plain_read_agree_on_the_tail_size(ascii_scratch):
    """One bound, two call sites -- so they cannot drift apart again.

    This is the regression pin for the *cause*: the tail size was spelled
    literally at each `Get-Content`, which is exactly how the two drifted.
    """
    run_dir = ascii_scratch / "runs" / "exp-agree"
    run_dir.mkdir(parents=True)
    body = "".join(f"agree-line-{i:04d}\n" for i in range(400))
    (run_dir / "serial.log").write_text(body, encoding="utf-8")

    plain = run_launcher(
        ["-Action", "logs", "-Run", "exp-agree", "-RunsRoot", str(ascii_scratch / "runs")]
    )
    assert plain.returncode == 0, plain.stderr
    plain_lines = [line for line in plain.stdout.splitlines() if line.strip()]

    # -Follow on a log that already exists: Get-Content -Wait -Tail N prints the
    # tail immediately and then blocks, so it has to be run with a timeout and
    # the exit code is not meaningful.
    followed = _run_launcher_with_timeout(
        ["-Action", "logs", "-Run", "exp-agree", "-Follow",
         "-RunsRoot", str(ascii_scratch / "runs")],
        timeout=12,
    )
    followed_lines = [line for line in followed.splitlines() if line.strip()]
    assert plain_lines == followed_lines, (
        f"plain read showed {len(plain_lines)} lines, -Follow showed "
        f"{len(followed_lines)}; the two must agree"
    )


def _unreadable_log_run(ascii_scratch, name: str) -> tuple[Path, Path]:
    """Build a run whose serial-log path exists but cannot be read as a file.

    A directory sitting where the log belongs is the deterministic case: it
    passes the ``Test-Path`` existence check and then fails on the first read.
    """
    runs_root = ascii_scratch / "runs"
    run_dir = runs_root / name
    run_dir.mkdir(parents=True)
    (run_dir / "serial.log").mkdir()
    return runs_root, run_dir


def test_logs_follow_reports_why_it_could_not_read_the_log(ascii_scratch):
    """`-Follow` must not turn an unreadable log into a silent success.

    The follow wraps its wait loop and ``Get-Content -Wait`` in a bare
    ``catch {}`` so a Ctrl-C stop is clean.  That catch also swallowed every
    *failure*: with a serial-log path that exists but cannot be read,
    ``logs -Follow`` exited **0** and printed **nothing at all** -- the operator
    was told the follow had worked when nothing had ever been shown.
    """
    runs_root, run_dir = _unreadable_log_run(ascii_scratch, "exp-locked")
    result = run_launcher(
        ["-Action", "logs", "-Run", "exp-locked", "-Follow", "-RunsRoot", str(runs_root)]
    )
    assert result.returncode != 0, (
        "logs -Follow exited 0 on an unreadable serial log; a swallowed error is "
        "reported to the operator as success"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "serial" in combined, f"no reason was given at all: {result.stderr!r}"


def test_logs_unreadable_serial_log_is_an_artifact_problem(ascii_scratch):
    """The error taxonomy must say what happened, and the exit code must agree.

    An unreadable log used to escape the ``logs`` branch and land in the outer
    catch, which reports ``blocked`` -- a code the launcher's own
    ``Get-ExitCodeForErrorCode`` maps to exit **3** -- while exiting **1**.  The
    plain path already calls a *missing* log ``artifact_missing``; an
    *unreadable* one is the same class of problem and must get the same code and
    the same exit, so a caller branching on the code is never surprised.
    """
    runs_root, run_dir = _unreadable_log_run(ascii_scratch, "exp-locked2")
    result = run_launcher(
        ["-Action", "logs", "-Run", "exp-locked2", "-RunsRoot", str(runs_root)]
    )
    assert result.returncode == 1, result.stderr
    assert '"artifact_missing"' in result.stderr, (
        f"unreadable log was not reported as an artifact problem: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# the outer catch: an exception no inner handler claimed
# ---------------------------------------------------------------------------


def test_unexpected_error_code_and_exit_code_agree(ascii_scratch):
    """The outer catch must not report a code its own exit mapping contradicts.

    ``Get-ExitCodeForErrorCode`` is the launcher's mirror of
    ``unipad.errors._CODE_TO_EXIT``: ``blocked`` means *the operator is missing
    material, a credential or budget* (PRD 7.5) and maps to exit **3**.  The
    outer catch reported exactly that code while exiting **1**, so a caller
    branching on ``error.code`` concluded "blocked, exit 3" and a caller
    branching on the process exit code concluded "failed, exit 1" about the
    very same run.  An exception that escaped every inner handler is neither:
    it is an internal fault, which is what the controller calls it too
    (``unipad.controller.cli``'s own catch-all), and it exits 1.

    The exit code is derived from the code rather than spelled beside it, so
    the two can never drift apart again -- the same shape of defect the two
    ``Get-Content`` tail literals had.

    An illegal character in a Windows path (``|``) makes
    ``[System.IO.Path]::GetFullPath`` throw before the non-ASCII guard runs,
    which is the deterministic way to reach this catch without touching QEMU.
    """
    bad = ascii_scratch / "bad|path.ipa"
    result = run_launcher(["-Action", "install", "-Ipa", str(bad)])

    assert result.returncode == 1, (
        f"an unexpected launcher error must exit 1 (generic failure), "
        f"not {result.returncode}: {result.stderr}"
    )
    envelope = json.loads(result.stderr.strip())
    assert envelope["error"]["code"] == "internal_error", (
        f"the outer catch reported {envelope['error']['code']!r}, whose mapped "
        f"exit code is not 1 -- code and exit code contradict each other"
    )
    # The raw .NET message is not relayed.  On this host it is a localized
    # Chinese string written in the ANSI code page, so relaying it handed the
    # operator mojibake; and a .NET message quotes the offending value, which
    # on a host whose working directory is non-ASCII would put the Windows
    # username on stderr (PRD 9.2).  Only the exception *type* is reported,
    # exactly as httpsvc's handle_error does.  The mojibake assertion is the
    # load-bearing half: the path-quoting variant of the same leak is real but
    # not reproducible through a deterministic trigger, so it is deliberately
    # not pinned here rather than pinned by an assertion that cannot fail.
    assert "�" not in result.stderr, (
        f"the error message is not readable text: {result.stderr!r}"
    )
    assert set(envelope["error"]["details"]) == {"exception"}, (
        f"the outer catch reported more than the exception type: {result.stderr!r}"
    )


def _run_launcher_with_timeout(args: list[str], *, timeout: float) -> str:
    """Run the launcher, kill it after ``timeout`` and return what it printed.

    ``-Follow`` never returns on its own once the log exists (that is the point
    of ``tail -f``), so its output has to be collected under a deadline.
    """
    import threading

    proc = subprocess.Popen(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(SCRIPT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    box: dict[str, str] = {}

    def _drain(pipe, key):
        box[key] = pipe.read()

    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, "err"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
    for thread in threads:
        thread.join(timeout=5)
    return box.get("out", "")


def test_logs_without_run_selects_the_latest_experiment(ascii_scratch):
    """`-Action logs` with no `-Run` must agree with the controller.

    The controller's own ``_latest_run`` (``cli.py``) only considers run
    directories that actually carry an ``experiment.json`` journal and ranks
    those by the *journal's* mtime.  The launcher used to rank every directory
    under the runs root by the directory's mtime, so the install ledger's parent
    (``runs/lifecycle``, or any other book-keeping directory) could win and
    ``logs`` then failed with ``artifact_missing`` even though a perfectly good
    serial log sat one directory over.

    ``aaa-ledger`` sorts *first* by name but is the *newest* by mtime, so a
    name-ordered pick would pass this test by luck: only a journal-mtime rule
    that skips journal-less directories gets it right.
    """
    runs_root = ascii_scratch / "runs"
    real_run = runs_root / "zzz-real-run"
    real_run.mkdir(parents=True)
    (real_run / "experiment.json").write_text(
        json.dumps({"experiment_id": "zzz-real-run", "status": "running"}),
        encoding="utf-8",
    )
    (real_run / "serial.log").write_text(
        "the-real-serial-line\n", encoding="utf-8"
    )

    ledger = runs_root / "aaa-ledger"
    ledger.mkdir(parents=True)
    (ledger / "install-ledger.jsonl").write_text(
        '{"entry_id": "le-1"}\n', encoding="utf-8"
    )

    old = time.time() - 3600
    new = time.time()
    os.utime(real_run / "experiment.json", (old, old))
    os.utime(real_run / "serial.log", (old, old))
    os.utime(real_run, (old, old))
    os.utime(ledger / "install-ledger.jsonl", (new, new))
    os.utime(ledger, (new, new))

    result = run_launcher(["-Action", "logs", "-RunsRoot", str(runs_root)])
    assert result.returncode == 0, (
        f"logs picked a directory without an experiment journal: {result.stderr}"
    )
    assert "the-real-serial-line" in result.stdout


def test_logs_missing_run_exits_nonzero(ascii_scratch):
    result = run_launcher(
        ["-Action", "logs", "-Run", "no-such-run", "-RunsRoot", str(ascii_scratch / "runs")]
    )
    assert result.returncode != 0, result.stderr
    assert "serial" in (result.stdout + result.stderr).lower()


def test_logs_follow_reports_the_exception_type_without_the_raw_message(ascii_scratch):
    """`-Follow` must report *why* it ended without relaying the .NET message.

    A byte-range lock on the serial log is the deterministic way to make
    ``Get-Content -Wait`` raise: ``msvcrt.locking`` denies the read the cmdlet
    needs, so it throws ``System.IO.IOException``.  (Ctrl-C itself cannot be
    delivered to a child whose stdio is a pipe -- see docs/launcher.md -- so this
    is the only observable way into that catch.)

    The catch used to append ``$_.Exception.Message`` to its note.  On this host
    that message is a localized Chinese string written in the ANSI code page, so
    the operator received mojibake -- the exact reason the launcher's *outer*
    catch reports the exception type only and nothing else (PRD 9.2).  The
    ``-Follow`` catch was the one place that still relayed it, so the two catches
    disagreed about what may reach stderr.

    The exit code stays 0 on purpose: a Ctrl-C stop is not a failure, and this
    host cannot tell a Ctrl-C from an IOException by type, so branching on the
    type would be a guess.  The stderr note is therefore the signal, and it has
    to be readable text.
    """
    import msvcrt

    run_dir = ascii_scratch / "runs" / "exp-locked3"
    run_dir.mkdir(parents=True)
    log = run_dir / "serial.log"
    log.write_text("locked-line-1\nlocked-line-2\n", encoding="utf-8")

    handle = open(log, "rb")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, log.stat().st_size)
        result = run_launcher(
            ["-Action", "logs", "-Run", "exp-locked3", "-Follow",
             "-RunsRoot", str(ascii_scratch / "runs")]
        )
    finally:
        handle.close()

    assert result.returncode == 0, result.stderr
    notes = [line for line in result.stderr.splitlines() if "follow ended" in line]
    assert notes, f"the follow ended without saying why: {result.stderr!r}"
    # the exception TYPE is reported ...
    assert "System.IO.IOException" in notes[0], (
        f"the follow did not name the exception type: {notes[0]!r}"
    )
    # ... and nothing that is not readable ASCII: the raw .NET message on this
    # host is a localized Chinese string in the ANSI code page, and relaying it
    # is what put mojibake on the operator's stderr.
    assert "�" not in result.stderr, (
        f"the follow relayed a message this console cannot decode: {result.stderr!r}"
    )
    notes[0].encode("ascii")  # raises UnicodeEncodeError if any of it is not ASCII


# ---------------------------------------------------------------------------
# the REAL invocation, not just the -WhatIf display vector
# ---------------------------------------------------------------------------
# The -WhatIf plan shows the argv the launcher *intends*.  These tests run the
# controller for real and read the envelope it prints, which is the only thing
# that pins the argv it *actually* received.  Both cases are blocked or fail
# before any guest exists, so no QEMU is ever started.


def _envelope(result: subprocess.CompletedProcess) -> dict:
    assert result.stdout.strip(), f"no envelope on stdout; stderr={result.stderr!r}"
    return json.loads(result.stdout)


def test_real_stop_reports_the_stop_command(ascii_scratch):
    """`command` must be `stop`, not `unipad.controller`.

    This is the regression test for a real bug: the launcher once handed the
    controller a vector that already contained `-m unipad.controller`, and
    `Invoke-UniPadController` prepended it again, so the CLI saw
    `python -m unipad.controller -m unipad.controller stop`, skipped the stray
    `-m`, and reported `command: unipad.controller` with exit 2.  The -WhatIf
    plan looked correct throughout, because it displays a different vector.
    """
    empty_runs = ascii_scratch / "empty-runs"
    empty_runs.mkdir(parents=True, exist_ok=True)
    result = run_launcher(["-Action", "stop", "-RunsRoot", str(empty_runs)])
    assert result.returncode == 3, result.stderr  # blocked: no run to stop
    envelope = _envelope(result)
    assert envelope["command"] == "stop"
    assert envelope["error"]["code"] == "blocked"


def test_real_install_reports_the_install_command(ascii_scratch):
    ipa = ascii_scratch / "missing.ipa"  # ASCII, and deliberately absent
    result = run_launcher(["-Action", "install", "-Ipa", str(ipa)])
    assert result.returncode == 1, result.stderr
    envelope = _envelope(result)
    assert envelope["command"] == "install"
    assert envelope["error"]["code"] == "artifact_missing"


# ---------------------------------------------------------------------------
# the interpreter probe must check the import, not just PATH
# ---------------------------------------------------------------------------


def test_interpreter_that_cannot_import_unipad_is_skipped(ascii_scratch):
    """A PATH-resolvable interpreter is not good enough.

    On this host `py` is 3.14 and has no unipad shim, while `python` is 3.12.9
    and has one.  A probe that only asks "does this resolve on PATH?" would
    happily select `py` and fail thirty seconds later with an opaque
    ``No module named unipad``.  Shadowing `python` with a copy of `py` forces
    exactly that situation: neither candidate can import unipad, so the launcher
    must report `blocked` (exit 3) rather than guess.
    """
    stub_dir = ascii_scratch / "stub"
    stub_dir.mkdir(parents=True, exist_ok=True)
    py_exe = shutil.which("py") or shutil.which("py.exe")
    if not py_exe:
        pytest.skip("py.exe is not on PATH")
    shutil.copy(py_exe, stub_dir / "python.exe")

    empty_runs = ascii_scratch / "empty-runs"
    empty_runs.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    # Windows-style separators: a native child cannot search a POSIX-style PATH.
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    env.pop("UNIPAD_PYTHON", None)
    cmd = [
        _powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
        "-Action", "stop", "-RunsRoot", str(empty_runs),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120, env=env)
    assert result.returncode == 3, f"expected blocked, got {result.returncode}: {result.stderr}"
    assert "interpreter" in (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# docs/launcher.md must describe the launcher that actually ships
# ---------------------------------------------------------------------------
# CLAUDE.md treats the four-field record as evidence, so a number or a
# behavioural claim in the document is a claim about the operator's view of the
# machine.  These tests derive both from the tree rather than trusting the prose,
# which is standards-review R-7's conforming fix ("derive the numbers, or
# re-check them on every edit").


def _launcher_doc() -> str:
    return (ROOT / "docs" / "launcher.md").read_text(encoding="utf-8")


def _collected_test_count() -> int:
    """How many cases pytest collects for this file, asked of pytest itself."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(Path(__file__).resolve())],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        cwd=str(ROOT),
    )
    name = Path(__file__).name
    for line in reversed(proc.stdout.splitlines()):
        match = re.match(r"^(.*?):\s*(\d+)\s*$", line.strip())
        if match and Path(match.group(1).replace("\\", "/")).name == name:
            return int(match.group(2))
    raise AssertionError(f"could not read the collected test count from: {proc.stdout[-400:]!r}")


def test_docs_describe_the_bounded_tail_the_launcher_implements():
    """`logs` is bounded, and the document must not still promise a whole-file dump.

    The plain read used to be a bare ``Get-Content -LiteralPath $serialLog`` --
    the entire guest serial log on stdout -- while ``-Follow`` tailed 100 lines,
    and ``docs/launcher.md`` described exactly that ("prints the *whole* serial
    log, like ``cat``: the earliest sign of a failed boot is at the top and must
    not be cut").  Both call sites now read one constant
    (``$script:LogTailLines``) and the truncation is declared on stderr, so the
    stale sentence is a false promise about how much log the operator sees --
    and a guest serial log grows for as long as the guest runs.
    """
    doc = _launcher_doc()
    # The stale sentence's two distinctive phrases: it called the unbounded read
    # "equivalent to `cat`" and argued the head "must not be cut".
    for stale in ("等同 `cat`", "不该被截掉"):
        assert stale not in doc, (
            f"docs/launcher.md still describes the plain `logs` read with {stale!r}; "
            "the launcher now applies the same bounded tail as -Follow"
        )
    assert "LogTailLines" in doc, (
        "docs/launcher.md does not name the one constant both `logs` forms read"
    )
    assert "只显示最后 100 行" in doc, (
        "docs/launcher.md does not state that the non-follow read is bounded to the "
        "same 100-line tail"
    )
    assert "stderr" in doc.lower(), (
        "docs/launcher.md does not say where the truncation is declared"
    )


def test_docs_scope_the_json_contract_away_from_logs():
    """Only the four controller-backed actions promise one JSON object on stdout.

    PRD 9.1's "exactly one JSON object" discipline is real for
    start/stop/install/restore; ``logs`` writes a serial-log tail instead.  The
    document used to claim stdout was pipe-parseable without scoping that to the
    controller-backed actions, which is the self-contradiction
    standards-review R-3 named.
    """
    doc = _launcher_doc()
    assert "start/stop/install/restore" in doc, (
        "docs/launcher.md does not scope the one-JSON-object contract to the four "
        "controller-backed actions"
    )
    assert "不是 JSON" in doc or "并非 JSON" in doc, (
        "docs/launcher.md does not say that `logs` stdout is not a JSON object"
    )


def test_docs_numbers_are_derived_from_the_tree():
    """The four-field record's counts are measured, not remembered.

    standards-review R-7 caught two stale numbers here (the launcher's line count
    and the test count).  The launcher's line count and this file's test-function
    count are read straight off disk, and the collected count is asked of pytest
    itself, so an edit to any of the three has to update the document.
    """
    doc = _launcher_doc()
    launcher_lines = len(
        (ROOT / "scripts" / "windows" / "UniPad-Launcher.ps1")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert f"{launcher_lines} 行" in doc, (
        f"docs/launcher.md does not state the launcher's real line count ({launcher_lines})"
    )
    functions = len(
        re.findall(r"^def test_", Path(__file__).resolve().read_text(encoding="utf-8"), re.MULTILINE)
    )
    assert f"{functions} 个测试函数" in doc, (
        f"docs/launcher.md does not state the real number of test functions ({functions})"
    )
    collected = _collected_test_count()
    assert f"{collected} 项" in doc, (
        f"docs/launcher.md does not state the real collected test count ({collected})"
    )
