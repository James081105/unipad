"""Acceptance tests for ``python -m unipad.acceptance`` (DESIGN.md section 10).

What these tests are for
------------------------
``unipad.acceptance.runner`` had no ``__main__``, no ``main()`` and no argparse,
so ``python -m unipad.acceptance.runner`` exited 0 having done nothing -- an
operator would read that exit code as "the acceptance run passed".  These tests
pin three things about the entry point that now exists:

1. **It exists and is argparse-driven.**  ``--help``, ``--version`` and the
   no-argument case each behave, and each returns a distinct exit code.
2. **``--run`` refuses rather than fabricating.**  There is no registered check
   universe yet, and a bundle with zero checks would still roll up to a verdict.
   "No checks ran" must never be able to read as a pass, so the entry point
   says so and exits ``BLOCKED``.
3. **The adapter bridges the four verdict shapes without laundering them.**
   ``as_verdict`` maps a ``P1Check``, an e2e ``SequenceReport``, a milestones
   ``MilestoneResult`` and a runner ``CheckResult`` onto one
   ``(Verdict, FourFields)`` pair, refuses a shape it does not recognise, and
   never upgrades a field on the way through.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from unipad.acceptance import __main__ as gate_main
from unipad.acceptance import record
from unipad.acceptance.record import EvidenceRef, FourFields, Verdict, VerdictKind
from unipad.acceptance.runner import (
    AcceptanceBundle,
    CheckDef,
    CheckResult,
    EnvironmentStamp,
    ExitCode,
    propagate_dependencies,
    release_verdict,
    write_bundle,
)
from unipad.models import ArtifactEntry, ArtifactManifest

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stamp() -> EnvironmentStamp:
    return EnvironmentStamp(
        host_fingerprint="f" * 64,
        host_label="test-host",
        accel="tcg",
        qemu_version="9.0.0",
        os_build="1",
        python_version="3.12.9",
        collected_at="2026-10-23T00:00:00Z",
        host_inventory_sha256="0" * 64,
    )


def _check() -> CheckDef:
    return CheckDef(
        check_id="demo.check",
        prd_citation="docs/PRD.md:1",
        requirement="a demo requirement",
        rejection="a demo rejection",
        tier="blocking",
        gate_id="demo",
        milestone="M0",
    )


def _bundle_dir(tmp_path: Path, *, kind: str = "FAIL") -> Path:
    """Write a real, loadable bundle into ``tmp_path`` and return the directory.

    The evidence file is hashed for real, because ``load_bundle`` re-hashes it:
    a fixture with a made-up digest would be refused for the right reason and
    prove nothing about the entry point.
    """
    run_dir = tmp_path / "demo-run"
    (run_dir / "acceptance").mkdir(parents=True)
    payload = b"serial log payload\n"
    (run_dir / "acceptance" / "serial.log").write_bytes(payload)
    ref = EvidenceRef(
        path="acceptance/serial.log",
        kind="serial_log",
        sha256=hashlib.sha256(payload).hexdigest(),
        scope=record.EvidenceScope.HOST_REAL,
    )
    if kind == "FAIL":
        verdict = Verdict.fail(["demo_reason"], [ref])
    else:
        verdict = Verdict.pass_([ref], ["demo_reason"])
    result = CheckResult(
        check=_check(),
        verdict=verdict,
        environment=_stamp(),
        started_at="2026-10-23T00:00:00Z",
        finished_at="2026-10-23T00:00:01Z",
    )
    checks = propagate_dependencies((result,))
    aggregate_verdict, reasons = release_verdict(checks, coverage_gaps=(), run_complete=True)
    bundle = AcceptanceBundle(
        run_id=run_dir.name,
        run_complete=True,
        environment=_stamp(),
        checks=checks,
        written_at="2026-10-23T00:00:00Z",
        aggregate={"verdict": aggregate_verdict, "reasons": list(reasons)},
    )
    write_bundle(bundle, run_dir)
    return run_dir


# ---------------------------------------------------------------------------
# 1. The entry point exists and is argparse-driven
# ---------------------------------------------------------------------------


def test_the_module_is_runnable_as_a_module() -> None:
    """``python -m unipad.acceptance`` must not exit 0 having done nothing.

    This is the defect the module was written for: ``runner.py`` had no
    ``__main__``, so the old invocation succeeded silently.  Invoking the
    package with no arguments prints usage and exits ``USAGE`` -- a nonzero
    code, so a caller cannot mistake it for a passing run.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "unipad.acceptance"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == int(ExitCode.USAGE), (proc.returncode, proc.stdout, proc.stderr)
    assert "usage:" in proc.stdout


def test_version_prints_the_gate_identity() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "unipad.acceptance", "--version"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "unipad.acceptance.gate" in proc.stdout


def test_help_lists_every_subcommand() -> None:
    """``--help`` must name the real flags, so the entry point is discoverable."""
    parser = gate_main.build_parser()
    text = parser.format_help()
    for flag in ("--run", "--verify", "--json", "--version"):
        assert flag in text, flag
    assert "unipad.acceptance" in text


def test_main_accepts_an_explicit_argv() -> None:
    """``main(argv)`` takes a list, so a test never has to spawn a process."""
    assert gate_main.main(["--version"]) == 0
    assert gate_main.main([]) == int(ExitCode.USAGE)


# ---------------------------------------------------------------------------
# 2. --run evaluates the registered universe and never fabricates a verdict
# ---------------------------------------------------------------------------


def test_run_evaluates_the_registered_universe(capsys) -> None:
    """``--run`` produces a real verdict now that the registry exists.

    This test used to pin the opposite: ``--run`` refused with "no registered
    check universe exists yet", which was correct when no ``CHECKS`` table had
    been built and was also the whole problem -- a gate that cannot run cannot
    report anything.  ``registry.py`` now registers 40 checks, so the
    refusal's precondition is gone.

    What is pinned instead is the property that mattered underneath the
    refusal: a run must produce a *verdict*, and that verdict must never be PASS
    on a host with no guest.  The assertion is on the verdict and the exit code,
    not on a message string, because the message is free to change and the
    verdict is not.
    """
    code = gate_main.main(["--run", "demo-universe"])
    assert code == int(ExitCode.BLOCKED), code
    captured = capsys.readouterr()
    combined = captured.err + captured.out
    assert "refusing" not in combined.lower(), combined
    assert "no registered check universe" not in combined, combined
    # A verdict was reached and printed, rather than a refusal.
    assert "demo-universe" in combined, combined
    assert "BLOCKED" in combined, combined


def test_the_run_verdict_is_machine_readable_with_json(capsys) -> None:
    """The JSON form carries the verdict, the exit code and the reasons.

    Every reason names a specific blocking check and its blocker, because
    ``blocking_by:<id>:<blocker>`` is the only form an operator can act on.  A
    verdict with no reasons would be indistinguishable from a bare refusal.
    """
    code = gate_main.main(["--run", "demo-json", "--json"])
    assert code == int(ExitCode.BLOCKED), code
    payload = json.loads(capsys.readouterr().out)
    # A successful run has no "refused" key at all; asserting on the
    # key's presence is what distinguishes a verdict from a refusal.
    assert payload.get("refused") is not True, payload
    assert payload["run_id"] == "demo-json", payload
    assert payload["verdict"] == "BLOCKED", payload
    assert payload["exit_code"] == int(ExitCode.BLOCKED), payload
    assert payload["reasons"], "a BLOCKED verdict with no reasons says nothing"
    assert all(reason.startswith("blocking_by:") for reason in payload["reasons"]), (
        payload["reasons"]
    )
    # The four-field record is carried, not dropped on the floor.
    assert isinstance(payload["field_unverified"], list), payload


def test_the_run_never_reports_pass_without_a_guest(capsys) -> None:
    """The load-bearing one: a green suite must not substitute for a guest.

    ``release_verdict`` requires at least one ONSITE evidence reference in the
    blocking tier before it will say PASS (``record.py`` §5.5, line 6).  On this
    host there is no iPadOS guest, so even a run in which every blocking probe
    returned PASS would still be BLOCKED.  This test asserts the gate *reaches*
    that rule rather than bypassing it -- the reasons must not be empty, and the
    verdict must not be PASS.
    """
    code = gate_main.main(["--run", "demo-noguest", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code != int(ExitCode.OK), (code, payload)
    assert payload["verdict"] != "PASS", payload
    assert payload["reasons"], payload


# ---------------------------------------------------------------------------
# 3. --verify re-establishes a written bundle
# ---------------------------------------------------------------------------


def test_verify_reports_the_aggregate_of_a_real_bundle(tmp_path, capsys) -> None:
    """A bundle written on disk is re-read, re-hashed and re-reported."""
    run_dir = _bundle_dir(tmp_path)
    code = gate_main.main(["--verify", str(run_dir)])
    assert code == int(ExitCode.BLOCKED), code  # the demo check FAILs
    out = capsys.readouterr().out
    assert "demo" in out
    assert "demo.check" in out
    assert "FAIL" in out


def test_verify_emits_json_with_the_four_fields(tmp_path, capsys) -> None:
    run_dir = _bundle_dir(tmp_path)
    code = gate_main.main(["--verify", str(run_dir), "--json"])
    assert code == int(ExitCode.BLOCKED)
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "demo-run"
    assert payload["run_complete"] is True
    assert payload["verdict"] == VerdictKind.FAIL
    row = payload["four_fields"][0]
    assert row["check_id"] == "demo.check"
    assert row["windows_status"] in record.WINDOWS_STATUSES
    assert row["onsite_status"] in record.ONSITE_STATUSES


def test_verify_refuses_a_directory_with_no_bundle(tmp_path, capsys) -> None:
    """A missing bundle is a refusal to have a verdict, not a crash."""
    code = gate_main.main(["--verify", str(tmp_path / "nothing-here")])
    assert code == int(ExitCode.USAGE), code
    captured = capsys.readouterr()
    assert "cannot verify" in (captured.err + captured.out)


# ---------------------------------------------------------------------------
# 4. The adapter: four shapes, one row, no laundering
# ---------------------------------------------------------------------------


def _p1_check(**overrides):
    """A ``P1Check`` built from the real bullet table, without running a check."""
    from unipad.acceptance.p1 import P1_BULLETS, P1Check

    bullet = P1_BULLETS[4]
    base = dict(
        bullet=bullet.bullet,
        prd_citation=bullet.prd_citation,
        requirement=bullet.requirement,
        probe=bullet.probe,
        verdict=VerdictKind.BLOCKED,
        verdict_reasons=("guest_outbound_requires_a_real_ipados_guest",),
        reasons=("guest outbound has never been observed",),
        observations=(),
        evidence=(),
        implementation="CODE COMPLETE",
        blocker="no real iPadOS guest",
    )
    base.update(overrides)
    return P1Check(**base)


def test_a_p1_check_adapts_to_the_canonical_row() -> None:
    """F8's adapter: a ``P1Check`` becomes a ``(Verdict, FourFields)`` pair."""
    check = _p1_check()
    row = gate_main.as_verdict("p1", check)
    assert row.source == "p1"
    assert row.check_id == check.bullet
    assert isinstance(row.verdict, Verdict)
    assert isinstance(row.fields, FourFields)
    assert row.verdict.kind == VerdictKind.BLOCKED
    # The machine-readable slug survives the crossing; the prose does not.
    assert "guest_outbound_requires_a_real_ipados_guest" in row.verdict.reasons
    assert row.fields.onsite_status == "BLOCKED"
    assert row.fields.blocker == "no real iPadOS guest"


def test_a_p1_pass_without_a_digest_is_refused_not_laundered() -> None:
    """A PASS resting on digestless evidence cannot cross the adapter.

    p1's ``Evidence`` carries no digest by design -- it records what a check
    opened.  Adapting it to a ``PASS`` would manufacture an unverifiable pass,
    so the canonical constructor refuses it and the caller sees the refusal.
    """
    from unipad.acceptance.p1 import Evidence

    check = _p1_check(
        verdict=VerdictKind.PASS,
        verdict_reasons=("exercised_on_this_host",),
        reasons=("exercised",),
        evidence=(Evidence("docs/PRD.md", "doc"),),
        blocker="",
    )
    with pytest.raises(Exception) as raised:
        gate_main.as_verdict("p1", check)
    assert "re-verified" in str(raised.value), str(raised.value)


def test_an_unknown_source_is_refused_not_guessed() -> None:
    """A row read from the wrong shape is a fabricated row."""
    with pytest.raises(Exception) as raised:
        gate_main.as_verdict("telepathy", object())
    assert "unknown verdict source" in str(raised.value)


def test_a_p1_evidence_item_adapts_with_an_explicit_digest_reason() -> None:
    """The digestless case is representable -- it just cannot carry a PASS."""
    from unipad.acceptance.p1 import Evidence

    refs = gate_main._evidence_of([Evidence("docs/PRD.md", "doc")])
    assert len(refs) == 1
    assert refs[0].sha256 is None
    assert refs[0].digest_reason
    assert refs[0].path == "docs/PRD.md"
    # An already-canonical reference passes through untouched.
    canonical = EvidenceRef(path="a/b.log", kind="serial_log", sha256="0" * 64)
    assert gate_main._evidence_of([canonical]) == (canonical,)


def test_a_milestone_result_adapts_through_the_shared_verdict() -> None:
    """Since F8 a milestone's verdict *is* a ``record.Verdict``; the row is free."""
    ref = EvidenceRef(path="acceptance/serial.log", kind="serial_log", sha256="0" * 64)
    verdict = Verdict.blocked("no_system_image", [ref])
    result = SimpleNamespace(
        check_id="M1",
        verdict=verdict,
        implementation="CODE COMPLETE",
        blocker="no_system_image",
    )
    row = gate_main.as_verdict("milestones", result)
    assert row.verdict is verdict
    assert row.fields.check_id == "M1"
    assert row.fields.onsite_status == "BLOCKED"


def test_a_milestone_result_without_a_canonical_verdict_is_refused() -> None:
    """A hand-rolled verdict-shaped object is not a ``record.Verdict``."""
    with pytest.raises(Exception) as raised:
        gate_main.as_verdict(
            "milestones", SimpleNamespace(check_id="M1", verdict="BLOCKED", blocker="x")
        )
    assert "record.Verdict" in str(raised.value)


def test_the_renderer_prints_one_table_for_every_source() -> None:
    """One table, one vocabulary -- the reason the adapter exists."""
    ref = EvidenceRef(path="acceptance/serial.log", kind="serial_log", sha256="0" * 64)
    verdict = Verdict.fail(["demo_reason"], [ref])
    runner_row = gate_main.VerdictFields(
        "runner",
        "demo.check",
        verdict,
        FourFields("demo.check", verdict, (ref,)),
    )
    milestone_row = gate_main.as_verdict(
        "milestones",
        SimpleNamespace(
            check_id="M1",
            verdict=Verdict.blocked("no_system_image"),
            implementation="PARTIAL",
            blocker="no_system_image",
        ),
    )
    text = gate_main.render_rows([runner_row, milestone_row])
    assert "SOURCE" in text and "WINDOWS" in text and "ONSITE" in text
    assert "runner" in text and "milestones" in text
    # Both rows speak the canonical vocabulary, so the columns line up.
    for word in record.WINDOWS_STATUSES:
        if word in text:
            assert word in text


# ---------------------------------------------------------------------------
# 5. The macOS source-acquisition step: pins from the lock, never hardcoded
# ---------------------------------------------------------------------------
#
# build-macos.yml checked the repository out with `submodules: recursive`, but
# this repository declares NO submodules -- there is no .gitmodules and
# `git submodule status` is empty -- so the recursive checkout fetched nothing.
# scripts/macos/build-macos.sh is offline BY POLICY and its prepare-sources step
# expects vendor/UniXNU and vendor/UniXNU-qemu to already be git checkouts.
# Nothing ever put them there, so the build could not reach prepare-sources.
#
# The tests below run the step's own Python -- extracted verbatim from the YAML
# -- so they exercise the bytes the runner executes rather than a copy that can
# drift.  What is proven on this Windows host: the pins are read from
# config/sources.lock.json, a missing or unreadable pin is a hard failure and
# never an empty string, the cross-check refuses a disagreement, and vendor/ is
# ignorable.  What is NOT proven here and needs a macOS runner: a real clone
# from github.com (blocked on this host), and the driver's own fetch.

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-macos.yml"
ACQUISITION_STEP = (
    "Acquire and verify the pinned sources "
    "(vendor/ + unixnu fetch/tree + pin cross-check)"
)
BUILD_STEP = "Run macOS build (scripts/macos/build-macos.sh)"
STAGING_STEP = "Stage allowlisted artifacts and build artifact-manifest.json"
CHECKOUT_STEP = "Checkout"
LOCK_PATH = REPO_ROOT / "config" / "sources.lock.json"


def _workflow(path: Path = WORKFLOW_PATH):
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover - depends on the environment
        pytest.skip("PyYAML is required to parse build-macos.yml; pip install pyyaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(path: Path = WORKFLOW_PATH):
    return _workflow(path)["jobs"]["build"]["steps"]


def _step(name: str, path: Path = WORKFLOW_PATH):
    for step in _steps(path):
        if step.get("name") == name:
            return step
    raise AssertionError(f"{path.name} has no step named {name!r}")


def _step_python(name: str, path: Path = WORKFLOW_PATH) -> str:
    """The Python a step feeds to ``python3 -``, extracted from the YAML."""
    run = _step(name, path)["run"]
    lines = run.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "python3 - <<'PY'")
        end = next(i for i, line in enumerate(lines[start + 1:], start + 1) if line.strip() == "PY")
    except StopIteration:
        raise AssertionError(f"step {name!r} has no `python3 - <<'PY'` heredoc") from None
    return textwrap.dedent("\n".join(lines[start + 1:end]))


def _load_step(name: str, tmp_path: Path, path: Path = WORKFLOW_PATH):
    """Import the step's own Python as a module, without running ``main()``."""
    module_path = tmp_path / "workflow_step.py"
    module_path.write_text(_step_python(name, path), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("unipad_workflow_step", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lock_payload() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def _head_of(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


class _DriverRecorder:
    """Stands in for ``subprocess.run`` inside the step's module.

    Real git still runs; anything that is not git is the upstream driver, whose
    ``status`` output is supplied by the test.

    The real ``subprocess.run`` is captured at construction, before any test
    patches it: the patch replaces the attribute on the shared ``subprocess``
    module, so looking ``subprocess.run`` up again from inside ``__call__``
    would re-enter this recorder and recurse until the interpreter gives up.
    """

    def __init__(self, *, status_text: str = "", status_code: int = 0):
        self._real_run = subprocess.run
        self.calls: list[list[str]] = []
        self.status_text = status_text
        self.status_code = status_code

    def __call__(self, command, *args, **kwargs):
        parts = [str(part) for part in command]
        self.calls.append(parts)
        if parts and parts[0] == "git":
            return self._real_run(command, *args, **kwargs)
        return SimpleNamespace(returncode=self.status_code, stdout=self.status_text, stderr="")


# --- 5.1 the step exists, in the right place, with the right shape ----------


def test_the_source_acquisition_runs_before_the_build() -> None:
    """A source step after the build is decoration."""
    names = [step.get("name") for step in _steps()]
    assert ACQUISITION_STEP in names, names
    assert BUILD_STEP in names, names
    assert names.index(ACQUISITION_STEP) < names.index(BUILD_STEP)


def test_the_checkout_no_longer_asks_for_recursive_submodules() -> None:
    """The named defect: a recursive checkout of a repository with no
    submodules fetches nothing and reads as if the sources were handled."""
    with_block = _step(CHECKOUT_STEP).get("with") or {}
    assert "submodules" not in with_block, with_block
    assert not (REPO_ROOT / ".gitmodules").exists()


def test_the_step_reads_the_lock_and_hardcodes_no_commit(tmp_path: Path) -> None:
    """One source of truth: the workflow must not know a sha of its own."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    source = _step_python(ACQUISITION_STEP)
    lock = _lock_payload()
    for key in ("unixnu", "device_repo"):
        assert lock[key]["commit"] not in source, f"{key}.commit is hardcoded in the workflow"
        assert lock[key]["repo"] not in source, f"{key}.repo is hardcoded in the workflow"
    for feed in lock["feeds"]:
        assert feed["git"] not in source, f"{feed['name']}'s URL is hardcoded in the workflow"
    pins, feeds, frozen, digest = module.load_pins(LOCK_PATH)
    assert pins["unixnu"]["commit"] == lock["unixnu"]["commit"]
    assert pins["device_repo"]["commit"] == lock["device_repo"]["commit"]
    assert pins["unixnu"]["directory"] == "UniXNU"
    assert pins["device_repo"]["directory"] == "UniXNU-qemu"
    assert [entry["name"] for entry in feeds] == [entry["name"] for entry in lock["feeds"]]
    assert frozen is lock["frozen"]
    assert digest == hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest()


# --- 5.2 a pin that is not a pin is a hard failure --------------------------


@pytest.mark.parametrize(
    "case",
    [
        "missing-file",
        "not-json",
        "root-not-an-object",
        "unixnu-section-missing",
        "unixnu-commit-null",
        "unixnu-commit-empty",
        "unixnu-commit-not-a-sha",
        "unixnu-commit-too-short",
        "unixnu-repo-empty",
        "device-commit-empty",
        "feeds-not-an-array",
    ],
)
def test_a_pin_that_is_not_a_pin_is_refused(tmp_path: Path, case: str) -> None:
    """A checkout made from "" is a checkout of whatever HEAD happened to be.

    Every shape a broken lock can take must raise, and the message must say why,
    so an operator reading the log knows which pin to fix.
    """
    module = _load_step(ACQUISITION_STEP, tmp_path)
    path = tmp_path / "sources.lock.json"
    lock = _lock_payload()

    if case == "missing-file":
        path = tmp_path / "does-not-exist.json"
    elif case == "not-json":
        path.write_text("{ not json", encoding="utf-8")
    elif case == "root-not-an-object":
        path.write_text("[]", encoding="utf-8")
    elif case == "unixnu-section-missing":
        del lock["unixnu"]
        path.write_text(json.dumps(lock), encoding="utf-8")
    elif case == "unixnu-commit-null":
        lock["unixnu"]["commit"] = None
        path.write_text(json.dumps(lock), encoding="utf-8")
    elif case == "unixnu-commit-empty":
        lock["unixnu"]["commit"] = ""
        path.write_text(json.dumps(lock), encoding="utf-8")
    elif case == "unixnu-commit-not-a-sha":
        lock["unixnu"]["commit"] = "main"
        path.write_text(json.dumps(lock), encoding="utf-8")
    elif case == "unixnu-commit-too-short":
        lock["unixnu"]["commit"] = "abc123"
        path.write_text(json.dumps(lock), encoding="utf-8")
    elif case == "unixnu-repo-empty":
        lock["unixnu"]["repo"] = "   "
        path.write_text(json.dumps(lock), encoding="utf-8")
    elif case == "device-commit-empty":
        lock["device_repo"]["commit"] = ""
        path.write_text(json.dumps(lock), encoding="utf-8")
    elif case == "feeds-not-an-array":
        lock["feeds"] = {"xnu": {}}
        path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(module.SourcePinError) as raised:
        module.load_pins(path)
    message = str(raised.value)
    assert message.strip(), "a refusal with no message is not a refusal"
    assert any(
        fragment in message
        for fragment in (
            "an empty pin is not a pin",
            "is not a git sha",
            "cannot read",
            "not valid JSON",
            "not an object",
            "not an array",
        )
    ), message


def test_an_abbreviated_but_well_formed_pin_is_accepted(tmp_path: Path) -> None:
    """The refusal is about shape, not length: git itself accepts 7..40 hex."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    path = tmp_path / "sources.lock.json"
    lock = _lock_payload()
    lock["unixnu"]["commit"] = lock["unixnu"]["commit"][:12]
    path.write_text(json.dumps(lock), encoding="utf-8")
    pins, _feeds, _frozen, _digest = module.load_pins(path)
    assert pins["unixnu"]["commit"] == lock["unixnu"]["commit"][:12]


def test_an_empty_pin_never_reaches_git(tmp_path: Path, monkeypatch) -> None:
    """The last gate before git: no URL, no sha, no command."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    recorded: list[list[str]] = []
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: recorded.append([str(x) for x in a[0]])
    )
    destination = tmp_path / "vendor" / "UniXNU"

    with pytest.raises(module.SourcePinError):
        module.acquire_one("", "", destination)
    with pytest.raises(module.SourcePinError):
        module.acquire_one("https://example.invalid/UniXNU.git", "", destination)
    with pytest.raises(module.SourcePinError):
        module.acquire_one("", "0" * 40, destination)
    assert recorded == [], f"an empty pin must be refused before git runs: {recorded}"


# --- 5.3 what the acquisition actually asks git to do ----------------------


def test_the_acquisition_fetches_the_pinned_commit_by_sha(tmp_path: Path, monkeypatch) -> None:
    """Fetch the commit, never a branch: a branch is a moving target."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    commit = "b881ac7dc1695ce676c856873051a487c6a9f963"
    recorded: list[list[str]] = []

    def fake_run(command, *args, **kwargs):
        parts = [str(part) for part in command]
        recorded.append(parts)
        if "init" in parts:
            # What real git does, and what _head_of() keys on: without a .git
            # directory the step reads "no checkout" rather than "a checkout at
            # the pin", and the assertions below are about the latter.
            (tmp_path / "vendor" / "UniXNU" / ".git").mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=0, stdout=f"{commit}\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    outcome = module.acquire_one(
        "https://github.com/UniXNU/UniXNU", commit, tmp_path / "vendor" / "UniXNU"
    )

    assert outcome == {"status": "acquired", "head": commit, "fetched": True}
    assert any("init" in call for call in recorded)
    fetch = next(call for call in recorded if "fetch" in call)
    assert fetch[fetch.index("origin") + 1] == commit
    assert "--depth" in fetch
    assert not any(part in ("HEAD", "main", "master") for part in fetch)
    checkout = next(call for call in recorded if "checkout" in call)
    assert checkout[-2:] == ["--detach", "FETCH_HEAD"]
    assert all(all(part for part in call) for call in recorded), "no argument may be empty"


def test_a_checkout_already_at_the_pin_is_not_refetched(tmp_path: Path, monkeypatch) -> None:
    """Idempotence: a cached or restored vendor/ must not be re-cloned."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    commit = "0" * 40
    destination = tmp_path / "vendor" / "UniXNU"
    (destination / ".git").mkdir(parents=True)
    recorded: list[list[str]] = []

    def fake_run(command, *args, **kwargs):
        parts = [str(part) for part in command]
        recorded.append(parts)
        return SimpleNamespace(returncode=0, stdout=f"{commit}\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    outcome = module.acquire_one("https://example.invalid/x.git", commit, destination)
    assert outcome["status"] == "present" and outcome["fetched"] is False
    assert not any("fetch" in call for call in recorded)


def test_a_checkout_at_the_wrong_commit_is_repinned_and_proven(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_step(ACQUISITION_STEP, tmp_path)
    wanted, wrong = "a" * 40, "b" * 40
    destination = tmp_path / "vendor" / "UniXNU"
    (destination / ".git").mkdir(parents=True)
    recorded: list[list[str]] = []
    state = {"head": wrong}

    def fake_run(command, *args, **kwargs):
        parts = [str(part) for part in command]
        recorded.append(parts)
        if "checkout" in parts:
            state["head"] = wanted
        return SimpleNamespace(returncode=0, stdout=f"{state['head']}\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    outcome = module.acquire_one("https://example.invalid/x.git", wanted, destination)
    assert outcome["head"] == wanted
    assert any("fetch" in call for call in recorded)


def test_a_checkout_that_is_not_at_the_pin_after_fetching_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """Fetching is not proof.  The HEAD must equal the pin or the job fails."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    wanted, got = "a" * 40, "b" * 40

    def fake_run(command, *args, **kwargs):
        parts = [str(part) for part in command]
        if "init" in parts:
            # Real git creates .git; _head_of() reports "no checkout at all"
            # without it, which is a different refusal than the one under test.
            (tmp_path / "vendor" / "UniXNU" / ".git").mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=0, stdout=f"{got}\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(module.SourcePinError) as raised:
        module.acquire_one(
            "https://example.invalid/x.git", wanted, tmp_path / "vendor" / "UniXNU"
        )
    assert wanted in str(raised.value) and got in str(raised.value)


# --- 5.4 vendor/ is fetched material, never committed ----------------------


def test_vendor_is_made_ignorable_without_touching_the_committed_gitignore(
    tmp_path: Path,
) -> None:
    """The ignore rule must be local: a committed .gitignore line is a second
    place the rule lives, and this project has already been bitten by exactly
    that divergence."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    repo = _init_repo(tmp_path / "repo")
    module.ensure_vendor_ignored(repo, "vendor", [])
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", "vendor"], capture_output=True
        ).returncode
        == 0
    )
    assert not (repo / ".gitignore").exists()
    exclude = repo / ".git" / "info" / "exclude"
    # The rule is the path itself, with no trailing slash: that form covers
    # vendor/ whether it ends up a file or a directory, which is the point.
    assert "/vendor" in exclude.read_text(encoding="utf-8").splitlines()
    # Idempotent: running it twice must not duplicate the rule.
    module.ensure_vendor_ignored(repo, "vendor", [])
    assert exclude.read_text(encoding="utf-8").splitlines().count("/vendor") == 1


def test_fetched_material_that_is_already_tracked_is_refused(tmp_path: Path) -> None:
    module = _load_step(ACQUISITION_STEP, tmp_path)
    repo = _init_repo(tmp_path / "repo")
    (repo / "vendor").mkdir()
    (repo / "vendor" / "UniXNU").write_text("fetched\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "vendor/UniXNU"], check=True, capture_output=True)
    with pytest.raises(module.SourcePinError) as raised:
        module.ensure_vendor_ignored(repo, "vendor", [])
    assert "must not be committed" in str(raised.value)


def test_a_gitmodules_that_points_at_a_locked_repo_is_refused(tmp_path: Path) -> None:
    """The pin must exist once.  A gitlink plus a lock entry is the divergence
    the constraint forbids."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    repo = _init_repo(tmp_path / "repo")
    url = _lock_payload()["unixnu"]["repo"]
    (repo / ".gitmodules").write_text(
        '[submodule "UniXNU"]\n\tpath = vendor/UniXNU\n\turl = ' + url + "\n", encoding="utf-8"
    )
    with pytest.raises(module.SourcePinError) as raised:
        module.ensure_vendor_ignored(repo, "vendor", [url])
    assert "twice" in str(raised.value)


def test_nothing_is_acquired_under_local_private(tmp_path: Path) -> None:
    """PRD 12: private material is never fetched, built or published."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    with pytest.raises(module.SourcePinError) as raised:
        module._refuse_private("F:/iosvirtual/local-private/vendor/UniXNU")
    assert "PRD 12" in str(raised.value)
    # A path that merely contains the word is not under it.
    module._refuse_private(tmp_path / "not-local-private-at-all" / "vendor")


def test_the_prd12_guard_in_the_staging_step_is_preserved() -> None:
    """The acquisition work must not have dropped the upload guard."""
    source = _step_python(STAGING_STEP)
    assert "local-private" in source
    assert "PRD 12" in source


def test_the_staging_step_streams_a_copy_and_never_re_reads_it(
    tmp_path: Path, monkeypatch
) -> None:
    """A staged copy is moved in chunks and verified from the bytes written.

    ``dst.write_bytes(src.read_bytes())`` held the whole file in memory to move
    it, and then the staged copy was opened and read again to hash it.  This step
    runs on every build, and ``MAX_FILE_BYTES`` is 2 GiB -- so on the day a real
    kernel or disk image comes through, the step allocates the file, writes it,
    reads it back and throws the buffer away.  Streaming costs one chunk.

    The recorded calls are the proof, and they are the only way to see it from
    outside: the manifest and the staged bytes are identical either way, which is
    exactly why the defect survives a behavioural test.

    What is pinned:
    * nothing under the build-output directory is ever materialised through
      ``read_bytes`` -- the copy reads a chunk, not the file;
    * nothing under the staging directory is ever opened for reading -- the
      digest of the copy comes from the bytes that were written;
    * the manifest still carries the right digest and size, and the staged copy
      still matches them, so the verification did not get cheaper by going away.
    """
    out = tmp_path / "build-output"
    (out / "nested").mkdir(parents=True)
    payload = b"kernel-bytes" * 1024
    (out / "nested" / "kernelcache").write_bytes(payload)
    stage = tmp_path / "staged"
    report = tmp_path / "build"
    log = tmp_path / "macos-build.log"
    log.write_bytes(b"build log evidence\n")

    for name, value in (
        ("BUILD_OUTPUT_DIR", str(out)),
        ("STAGE_DIR", str(stage)),
        ("BUILD_LOG", str(log)),
        ("REPORT_DIR", str(report)),
        ("PAYLOAD_TARGET", "tcg"),
    ):
        monkeypatch.setenv(name, value)

    materialised: list[Path] = []
    real_read_bytes = Path.read_bytes

    def recording_read_bytes(self, *args, **kwargs):
        data = real_read_bytes(self, *args, **kwargs)
        materialised.append(self)
        return data

    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)

    opened: list[tuple[Path, str]] = []
    real_open = Path.open

    def recording_open(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        opened.append((self, mode))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", recording_open)

    with pytest.raises(SystemExit) as raised:
        _load_step(STAGING_STEP, tmp_path)
    assert raised.value.code == 0

    # Both staged sources: every build output, and the build log beside them.
    pulled_whole = [
        path.name for path in materialised if path.is_relative_to(out) or path == log
    ]
    assert pulled_whole == [], (
        "the staging step pulled a whole staged source through read_bytes to copy "
        f"it: {pulled_whole}"
    )
    # The step prints a digest of the manifest it just wrote, so the manifest and
    # the report are read back on purpose; every *staged copy* is not.
    self_describing = {"artifact-manifest.json", "build-report.json"}
    reread = [
        path.name
        for path, mode in opened
        if mode == "rb" and path.is_relative_to(stage) and path.name not in self_describing
    ]
    assert reread == [], (
        "the staging step re-read a staged copy to hash it: "
        f"{reread} -- the digest must come from the bytes written"
    )

    staged = stage / "nested" / "kernelcache"
    assert staged.read_bytes() == payload
    manifest = json.loads((stage / "artifact-manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["artifacts"] if item["path"] == "nested/kernelcache")
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["size_bytes"] == len(payload)
    assert entry["role"] == "kernel"

    written = json.loads((report / "staging-report.json").read_text(encoding="utf-8"))
    assert written["problems"] == [], written["problems"]
    assert written["status"] == "pass"

    # PRD 9.2: the manifest is uploaded, so it must not carry the runner's paths.
    assert str(tmp_path) not in (stage / "artifact-manifest.json").read_text(encoding="utf-8")


# --- 5.5 the driver, not a second fetcher ----------------------------------


def test_the_driver_is_invoked_with_fetch_then_tree_and_the_device_after_the_subcommand(
    tmp_path: Path,
) -> None:
    """cli.py attaches the shared parser to every subparser (parents=[c]), so
    -D and --out come AFTER the subcommand; and --update would move off the
    lock, which is the one thing this step must never do."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    for subcommand in ("fetch", "tree"):
        argv = module.driver_argv(
            subcommand,
            driver="vendor/UniXNU/unixnu",
            device="vendor/UniXNU-qemu",
            out_root="build/unixnu-out",
        )
        assert argv[1] == "vendor/UniXNU/unixnu"
        assert argv[2] == subcommand
        assert argv.index("-D") > argv.index(subcommand), "-D must follow the subcommand"
        assert argv[argv.index("-D") + 1] == "vendor/UniXNU-qemu"
        assert argv.index("--out") > argv.index(subcommand)
        assert "--update" not in argv, "--update ignores the lock and moves to the newest ref"
        assert argv[0] == sys.executable


def test_the_step_invokes_the_driver_for_everything_except_the_two_pins(
    tmp_path: Path,
) -> None:
    """The workflow must not fetch a feed itself: that is the second
    implementation of the fetcher this task forbids."""
    source = _step_python(ACQUISITION_STEP)
    assert 'run_driver("fetch")' in source
    assert 'run_driver("tree")' in source
    for feed in _lock_payload()["feeds"]:
        assert feed["git"] not in source, f"{feed['name']} is fetched by the workflow, not the driver"


# --- 5.6 the cross-check ---------------------------------------------------


def _status_text(pins_by_name: dict) -> str:
    """A faithful `unixnu status` body: cli.py prints
    f'{kind:8} {name:24} {origin}{extra}' per module."""
    lines = ["device   /tmp/device (qemu)", "output   /tmp/out", "config   none (run defconfig)"]
    for kind, name, origin in (
        ("driver", "DarwinBoot", "base"),
        ("kit", "AirNir", "base"),
        ("package", "main", "base"),
    ):
        lines.append(f"{kind:8} {name:24} {origin}")
    for name, commit in pins_by_name.items():
        if commit is None:
            lines.append(f"{'refer':8} {name:24} git NOT LOCKED")
        else:
            lines.append(f"{'refer':8} {name:24} git pinned {commit}")
    return "\n".join(lines) + "\n"


def test_the_status_parser_reads_only_real_module_lines(tmp_path: Path) -> None:
    """The header lines ('device', 'output', 'config') are not modules, and a
    local module's '(clean)' is not a pin."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    pins, unpinned, not_reproducible = module.parse_status(
        "device   /a b/c (qemu)\n"
        "output   /a b/out\n"
        "config   none (run defconfig)\n"
        "driver   DarwinBoot               base (clean)\n"
        "refer    xnu                      git pinned d8b80295118e\n"
        "refer    dyld                     git NOT LOCKED\n"
    )
    assert pins == {"xnu": "d8b80295118e", "dyld": None}
    assert unpinned == ["refer:dyld"]
    assert not_reproducible is False


def test_the_cross_check_passes_when_the_lock_and_the_driver_agree(tmp_path: Path) -> None:
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    pins = {feed["name"]: feed["commit"][:12] for feed in lock["feeds"]}
    driver_lock = {feed["name"]: feed["commit"] for feed in lock["feeds"]}
    problems, _warnings = module.cross_check(lock["feeds"], driver_lock, _status_text(pins), {})
    assert problems == []


def test_the_cross_check_fails_when_a_resolved_pin_disagrees_with_the_lock(
    tmp_path: Path,
) -> None:
    """A build that silently compiles different bytes than the lock promised is
    the failure this prevents."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    pins = {feed["name"]: feed["commit"][:12] for feed in lock["feeds"]}
    driver_lock = {feed["name"]: feed["commit"] for feed in lock["feeds"]}
    pins["xnu"] = "0" * 12
    driver_lock["xnu"] = "1" * 40
    problems, _warnings = module.cross_check(lock["feeds"], driver_lock, _status_text(pins), {})
    assert any("xnu" in problem for problem in problems)
    # Both halves must object: the full commit the driver wrote into its lock,
    # and the abbreviated one `unixnu status` printed.
    assert len(problems) == 2, problems


def test_the_cross_check_fails_when_the_driver_reports_a_source_not_locked(
    tmp_path: Path,
) -> None:
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    pins = {feed["name"]: feed["commit"][:12] for feed in lock["feeds"]}
    pins["dyld"] = None
    problems, _warnings = module.cross_check(lock["feeds"], dict(), _status_text(pins), {})
    assert any("dyld is NOT LOCKED" in problem for problem in problems)


def test_the_cross_check_fails_when_the_lock_is_missing_a_feed(tmp_path: Path) -> None:
    """The dyld gap: a lock derived from UniXNU's feeds.toml alone is missing
    the device repository's own source."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    feeds = [feed for feed in lock["feeds"] if feed["name"] != "dyld"]
    pins = {feed["name"]: feed["commit"][:12] for feed in feeds}
    driver_lock = {feed["name"]: feed["commit"] for feed in lock["feeds"]}
    problems, _warnings = module.cross_check(feeds, driver_lock, _status_text(pins), {})
    assert any("dyld" in problem and "missing a feed" in problem for problem in problems)


def test_the_cross_check_fails_when_the_lock_pins_a_feed_the_driver_never_resolved(
    tmp_path: Path,
) -> None:
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    pins = {feed["name"]: feed["commit"][:12] for feed in lock["feeds"] if feed["name"] != "edk2"}
    driver_lock = {feed["name"]: feed["commit"] for feed in lock["feeds"]}
    problems, _warnings = module.cross_check(lock["feeds"], driver_lock, _status_text(pins), {})
    assert any("edk2" in problem for problem in problems)


def test_the_cross_check_fails_when_a_checkout_is_not_at_its_pin(tmp_path: Path) -> None:
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    pins = {feed["name"]: feed["commit"][:12] for feed in lock["feeds"]}
    driver_lock = {feed["name"]: feed["commit"] for feed in lock["feeds"]}
    heads = {
        "vendor/UniXNU": {"want": lock["unixnu"]["commit"], "got": "f" * 40},
        "vendor/UniXNU-qemu": {
            "want": lock["device_repo"]["commit"],
            "got": lock["device_repo"]["commit"],
        },
    }
    problems, _warnings = module.cross_check(lock["feeds"], driver_lock, _status_text(pins), heads)
    assert any("vendor/UniXNU" in problem and "pins" in problem for problem in problems)


@pytest.mark.parametrize("label,want,got", [
    ("vendor/UniXNU", "", "a" * 40),
    ("vendor/UniXNU-qemu", "b" * 40, ""),
])
def test_the_cross_check_fails_when_a_checkout_or_its_pin_is_absent(
    tmp_path: Path, label: str, want: str, got: str
) -> None:
    """Found by mutation: `elif not got:` replaced with `elif False:` left every
    test green, because no test had ever handed the cross-check an empty HEAD or
    an empty pin.  An empty pair is exactly what a broken acquisition produces,
    and comparing "" with a sha must be a refusal, not a pass."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    pins = {feed["name"]: feed["commit"][:12] for feed in lock["feeds"]}
    driver_lock = {feed["name"]: feed["commit"] for feed in lock["feeds"]}
    problems, _warnings = module.cross_check(
        lock["feeds"], driver_lock, _status_text(pins), {label: {"want": want, "got": got}}
    )
    assert any(label in problem for problem in problems), problems
    assert any("no HEAD" in problem or "no pin" in problem for problem in problems), problems


# --- 5.7 the whole step, end to end, against a fake network ----------------


def _prepare_step_environment(module, tmp_path: Path, *, status_text: str, status_code: int = 0) -> _DriverRecorder:
    """Point the step's knobs at a throwaway workspace.

    Everything lives *inside* the throwaway repository, the way the workflow
    lays it out: ``git check-ignore`` answers 128 for a path outside the
    repository it is asked about, so a vendor/ that is a sibling of the repo
    is not a stricter test, it is a different one.
    """
    repo = _init_repo(tmp_path / "repo")
    lock = _lock_payload()
    (tmp_path / "lock").mkdir()
    lock_path = tmp_path / "lock" / "sources.lock.json"
    lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    device = repo / "vendor" / "UniXNU-qemu"
    device.mkdir(parents=True)
    lines = ['# Resolved git sources, written by "unixnu fetch".']
    for feed in lock["feeds"]:
        lines += ["", f'[source."{feed["name"]}"]', f'commit = "{feed["commit"]}"']
    (device / "unixnu.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")

    module.LOCK_PATH = str(lock_path)
    module.VENDOR_DIR = str(repo / "vendor")
    module.REPORT_DIR = str(repo / "build")
    module.DRIVER = str(repo / "vendor" / "UniXNU" / "unixnu")
    module.DEVICE_REPO = str(device)
    module.OUT_ROOT = str(repo / "out")
    module.REPO_ROOT = str(repo)
    return _DriverRecorder(status_text=status_text, status_code=status_code)


def test_the_step_passes_when_the_driver_agrees_with_the_lock(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    status_text = _status_text({feed["name"]: feed["commit"][:12] for feed in lock["feeds"]})
    recorder = _prepare_step_environment(module, tmp_path, status_text=status_text)
    acquired: list[tuple] = []
    driven: list[str] = []

    module.acquire_one = lambda url, commit, dest, **kw: (
        acquired.append((url, commit, str(dest))),
        {"status": "present", "head": commit, "fetched": False},
    )[1]
    module.run_driver = lambda subcommand, **kw: driven.append(subcommand)
    monkeypatch.setattr(module.subprocess, "run", recorder)

    assert module.main() == 0

    # The pins were handed to git, and ONLY the pins -- three of them since
    # uefi-sheng was added, and nothing else.  The "and only those" half is the
    # load-bearing one: a checkout nobody pinned reaching git is the silent
    # divergence this step exists to prevent.
    assert [item[1] for item in acquired] == [
        lock["unixnu"]["commit"],
        lock["device_repo"]["commit"],
        lock["uefi_sheng"]["commit"],
    ]
    # ...and each one to the directory ACQUISITIONS names, so the build step's
    # --uefi and this step's clone cannot drift apart.
    assert [item[2] for item in acquired] == [
        str(tmp_path / "repo" / "vendor" / "UniXNU"),
        str(tmp_path / "repo" / "vendor" / "UniXNU-qemu"),
        str(tmp_path / "repo" / "vendor" / "uefi-sheng"),
    ]
    assert driven == ["fetch", "tree"], driven
    status_calls = [call for call in recorder.calls if "status" in call]
    assert len(status_calls) == 1
    assert status_calls[0][status_calls[0].index("-D") + 1] == str(
        tmp_path / "repo" / "vendor" / "UniXNU-qemu"
    )

    report = json.loads((tmp_path / "repo" / "build" / "pin-crosscheck.json").read_text(encoding="utf-8"))
    assert report["status"] == "pass", report
    assert report["problems"] == []
    assert report["driver_lock"]["xnu"] == next(
        feed["commit"] for feed in lock["feeds"] if feed["name"] == "xnu"
    )
    acquisition = json.loads(
        (tmp_path / "repo" / "build" / "source-acquisition.json").read_text(encoding="utf-8")
    )
    assert acquisition["status"] == "pass"
    assert acquisition["vendor_ignored"] is True
    assert [item["directory"] for item in acquisition["acquisitions"]] == [
        "UniXNU",
        "UniXNU-qemu",
        "uefi-sheng",
    ]
    # uefi-sheng is a pinned CHECKOUT, not a feed: it must not appear in the
    # driver's own lock, or the cross-check would demand it there on every run.
    assert "uefi_sheng" not in report["driver_lock"], report["driver_lock"]


def test_the_step_fails_when_the_driver_resolves_a_different_commit(
    tmp_path: Path, monkeypatch
) -> None:
    """The headline case: the lock promises one commit, the driver resolved
    another, and the job must refuse rather than build it."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    drifted = {
        feed["name"]: ("0" * 12 if feed["name"] == "xnu" else feed["commit"][:12])
        for feed in lock["feeds"]
    }
    status_text = _status_text(drifted)
    recorder = _prepare_step_environment(module, tmp_path, status_text=status_text)
    module.acquire_one = lambda url, commit, dest, **kw: {
        "status": "present",
        "head": commit,
        "fetched": False,
    }
    module.run_driver = lambda subcommand, **kw: None
    monkeypatch.setattr(module.subprocess, "run", recorder)

    assert module.main() == 1

    report = json.loads((tmp_path / "repo" / "build" / "pin-crosscheck.json").read_text(encoding="utf-8"))
    assert report["status"] == "fail"
    assert any("xnu" in problem for problem in report["problems"]), report["problems"]


def test_the_step_fails_before_any_fetch_when_the_lock_is_unreadable(tmp_path: Path) -> None:
    module = _load_step(ACQUISITION_STEP, tmp_path)
    repo = _init_repo(tmp_path / "repo")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "build").mkdir()
    module.LOCK_PATH = str(tmp_path / "no-such-lock.json")
    module.VENDOR_DIR = str(tmp_path / "vendor")
    module.REPORT_DIR = str(tmp_path / "build")
    module.REPO_ROOT = str(repo)
    fetched: list[str] = []
    module.run_driver = lambda subcommand, **kw: fetched.append(subcommand)
    assert module.main() == 1
    assert fetched == [], "an unreadable lock must fail before the driver runs"
    report = json.loads(
        (tmp_path / "build" / "source-acquisition.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "fail"
    assert report["problems"]


# --- 5.8 what is proven here, and what needs a macOS runner ----------------


def test_the_reports_record_what_cannot_be_proven_on_this_host(
    tmp_path: Path, monkeypatch
) -> None:
    """CODE COMPLETE is never a field PASS: the step must say out loud which
    half of itself only a macOS runner can prove."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    status_text = _status_text({feed["name"]: feed["commit"][:12] for feed in lock["feeds"]})
    _prepare_step_environment(module, tmp_path, status_text=status_text)
    module.acquire_one = lambda url, commit, dest, **kw: {
        "status": "present",
        "head": commit,
        "fetched": False,
    }
    module.run_driver = lambda subcommand, **kw: None
    monkeypatch.setattr(module.subprocess, "run", _DriverRecorder(status_text=status_text))
    module.main()
    for name in ("source-acquisition.json", "pin-crosscheck.json"):
        report = json.loads((tmp_path / "repo" / "build" / name).read_text(encoding="utf-8"))
        joined = " ".join(report["unverified"]).lower()
        assert "github.com" in joined, report
        assert "macos" in joined, report


def test_the_cross_check_accepts_the_real_driver_status_output(tmp_path: Path) -> None:
    """The strongest proof available on Windows: `unixnu status` really runs
    here (it prints 'developed and tested on macOS only' and then works), so
    the parser is checked against the driver's actual output rather than a
    hand-written imitation of it.

    Skipped when the pinned clones under local-private/ are absent, which is
    the case for a fresh checkout of this repository.
    """
    base = REPO_ROOT / "local-private" / "upstream" / "UniXNU"
    device = REPO_ROOT / "local-private" / "upstream" / "UniXNU-qemu"
    # The guard must ask about the driver the step will actually invoke.  The
    # step's default DRIVER is "vendor/UniXNU/unixnu" resolved against the
    # *device repository*, which is where the workflow puts it -- not against
    # this host's clone, whose driver sits at its own root.  Point the step at
    # the real files, then require exactly those files to exist.
    if not (base / "unixnu").is_file() or not (device / "board.toml").is_file():
        pytest.skip("the pinned upstream clones under local-private/ are not present on this host")
    module = _load_step(ACQUISITION_STEP, tmp_path)
    module.DRIVER = str(base / "unixnu")
    module.DEVICE_REPO = str(device)
    pins, feeds, _frozen, _digest = module.load_pins(LOCK_PATH)
    argv = module.driver_argv("status", out_root=str(tmp_path / "driver-out"))
    result = subprocess.run(argv, capture_output=True, text=True, cwd=str(base))
    assert result.returncode == 0, result.stderr
    assert "developed and tested on macOS only" in result.stderr
    parsed, unpinned, not_reproducible = module.parse_status(result.stdout)
    assert unpinned == [] and not_reproducible is False
    assert set(parsed) == {feed["name"] for feed in feeds}
    driver_lock = module.parse_driver_lock(device / "unixnu.lock")
    assert set(driver_lock) == set(parsed)
    heads = {
        "vendor/UniXNU": {"want": pins["unixnu"]["commit"], "got": _head_of(base)},
        "vendor/UniXNU-qemu": {"want": pins["device_repo"]["commit"], "got": _head_of(device)},
    }
    problems, _warnings = module.cross_check(feeds, driver_lock, result.stdout, heads)
    assert problems == [], problems


# --- 5.9 the two refusal paths a mutation proved were untested ---------------
#
# These were found by breaking the workflow's semantics on purpose and watching
# which tests stayed green.  A test that still passes after the code is broken
# is not evidence, so each of the two survivors below now has a test that fails
# when the corresponding guard is removed.


def test_the_cross_check_fails_when_status_reports_not_reproducible(tmp_path: Path) -> None:
    """`unixnu status` prints 'not reproducible: N local source(s) are not
    pinned by unixnu.lock' when a local module has no commit.  Every feed can
    still agree and the build can still be unreproducible, so the banner is its
    own refusal -- and until this test existed, deleting that refusal changed
    nothing."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    pins = {feed["name"]: feed["commit"][:12] for feed in lock["feeds"]}
    driver_lock = {feed["name"]: feed["commit"] for feed in lock["feeds"]}
    status_text = _status_text(pins) + (
        "\nnot reproducible: 3 local source(s) are not pinned by unixnu.lock\n"
    )
    problems, _warnings = module.cross_check(lock["feeds"], driver_lock, status_text, {})
    assert any("not reproducible" in problem for problem in problems), problems
    # The banner is not the same complaint as a NOT LOCKED feed: the feeds agree.
    assert not any("NOT LOCKED" in problem for problem in problems), problems


def test_a_committed_gitignore_that_unignores_vendor_is_refused(tmp_path: Path) -> None:
    """`.git/info/exclude` loses to a tracked `.gitignore`.

    Precedence in gitignore(5) puts a `.gitignore` in the directory ABOVE
    `$GIT_DIR/info/exclude`, so a committed `!vendor` silently re-includes the
    fetched upstreams and the step could commit them.  The re-verification is
    what turns that into a refusal rather than a surprise in the next commit --
    and removing the re-verification left every other test green.
    """
    module = _load_step(ACQUISITION_STEP, tmp_path)
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text("!vendor\n", encoding="utf-8")
    with pytest.raises(module.SourcePinError) as raised:
        module.ensure_vendor_ignored(repo, "vendor", [])
    assert "still not ignored" in str(raised.value)
    # The rule really was written; git simply outranks it, which is the point.
    assert "/vendor" in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()


def test_the_ignore_rule_covers_paths_inside_vendor(tmp_path: Path) -> None:
    """`/vendor` alone is not the contract: vendor/UniXNU must be ignored too,
    or the first `git add -A` picks the fetched tree up file by file."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    repo = _init_repo(tmp_path / "repo")
    module.ensure_vendor_ignored(repo, "vendor", [])
    for candidate in ("vendor", "vendor/UniXNU", "vendor/UniXNU-qemu"):
        assert (
            subprocess.run(
                ["git", "-C", str(repo), "check-ignore", "-q", candidate], capture_output=True
            ).returncode
            == 0
        ), candidate


# --- 5.10 the driver's own failures are refusals, not silent successes -------


def test_the_step_fails_when_the_driver_cannot_assemble_the_tree(tmp_path: Path) -> None:
    """`unixnu fetch`/`tree` failing must fail the job, not fall through to a
    cross-check of nothing."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    status_text = _status_text({feed["name"]: feed["commit"][:12] for feed in lock["feeds"]})
    _prepare_step_environment(module, tmp_path, status_text=status_text)
    module.acquire_one = lambda url, commit, dest, **kw: {
        "status": "present",
        "head": commit,
        "fetched": False,
    }

    def boom(subcommand, **kwargs):
        raise module.SourcePinError(f"unixnu {subcommand} failed (128)")

    module.run_driver = boom
    assert module.main() == 1
    report = json.loads(
        (tmp_path / "repo" / "build" / "pin-crosscheck.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "fail"
    assert any("fetch" in problem for problem in report["problems"]), report["problems"]
    # The acquisition report still says the checkouts were fine: the two
    # failures are separate evidence, not one blurred verdict.
    acquisition = json.loads(
        (tmp_path / "repo" / "build" / "source-acquisition.json").read_text(encoding="utf-8")
    )
    assert acquisition["status"] == "pass"


def test_the_step_fails_when_the_driver_status_itself_fails(tmp_path: Path) -> None:
    """A status that will not run is not a status that agrees."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    _prepare_step_environment(module, tmp_path, status_text="", status_code=1)
    module.acquire_one = lambda url, commit, dest, **kw: {
        "status": "present",
        "head": commit,
        "fetched": False,
    }
    module.run_driver = lambda subcommand, **kw: None
    assert module.main() == 1
    report = json.loads(
        (tmp_path / "repo" / "build" / "pin-crosscheck.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "fail"
    assert report["status_error"], report


def test_the_step_fails_when_the_driver_lock_is_unreadable(tmp_path: Path) -> None:
    """An unparseable unixnu.lock is a refusal, not an empty dict to agree with."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    status_text = _status_text({feed["name"]: feed["commit"][:12] for feed in lock["feeds"]})
    _prepare_step_environment(module, tmp_path, status_text=status_text)
    (tmp_path / "repo" / "vendor" / "UniXNU-qemu" / "unixnu.lock").write_text(
        "this is not toml =\n", encoding="utf-8"
    )
    module.acquire_one = lambda url, commit, dest, **kw: {
        "status": "present",
        "head": commit,
        "fetched": False,
    }
    module.run_driver = lambda subcommand, **kw: None
    assert module.main() == 1
    report = json.loads(
        (tmp_path / "repo" / "build" / "pin-crosscheck.json").read_text(encoding="utf-8")
    )
    assert any("unixnu.lock" in problem for problem in report["problems"]), report["problems"]


# --- 5.11 the manifest attests to the pins, not to the dispatch request ------


def test_the_manifest_quotes_the_pins_the_acquisition_resolved() -> None:
    """The manifest must record what was BUILT.

    The dispatch inputs are a human's request and the lock is what the step
    fetched; while the lock is unfrozen the two are never compared, so a
    manifest that quotes the inputs attests to a commit that may never have
    been compiled.  Every downstream consumer of the commits therefore reads
    the acquisition step's outputs.
    """
    staging = _step(STAGING_STEP)
    env = staging["env"]
    for name in ("UNIXNU_COMMIT", "DEVICE_COMMIT"):
        assert "steps.sources.outputs" in env[name], env[name]
        assert "inputs." in env[name], f"{name} loses the dispatch inputs as its fallback: {env[name]}"
    assert env["LOCK_SHA256"].startswith("${{ steps.sources.outputs.lock_sha256")
    summary = _step("Step summary")["env"]
    assert "steps.sources.outputs" in summary["UNIXNU_COMMIT"]
    # ...and the request is still recorded, so the difference is visible.
    assert summary["REQUESTED_UNIXNU_COMMIT"] == "${{ inputs.unixnu_commit }}"


def test_the_acquisition_publishes_the_pins_as_step_outputs(tmp_path: Path, monkeypatch) -> None:
    """The outputs are the lock's, read at run time -- never a literal."""
    module = _load_step(ACQUISITION_STEP, tmp_path)
    lock = _lock_payload()
    status_text = _status_text({feed["name"]: feed["commit"][:12] for feed in lock["feeds"]})
    _prepare_step_environment(module, tmp_path, status_text=status_text)
    module.acquire_one = lambda url, commit, dest, **kw: {
        "status": "present",
        "head": commit,
        "fetched": False,
    }
    module.run_driver = lambda subcommand, **kw: None
    output = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(module.subprocess, "run", _DriverRecorder(status_text=status_text))
    module.main()
    published = dict(
        line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert published["unixnu_commit"] == lock["unixnu"]["commit"]
    assert published["device_commit"] == lock["device_repo"]["commit"]
    assert published["unixnu_repo"] == lock["unixnu"]["repo"]
    assert published["device_repo"] == lock["device_repo"]["repo"]
    # The digest is of the lock the step actually read -- the scratch copy the
    # harness laid down -- not of the repository's own file.
    scratch = tmp_path / "lock" / "sources.lock.json"
    assert published["lock_sha256"] == hashlib.sha256(scratch.read_bytes()).hexdigest()


def test_the_step_records_the_lock_digest_in_the_manifest_source() -> None:
    """The manifest must carry the lock's sha256, or a reader cannot tell
    whether the lock moved after the run."""
    assert '"sources_lock_sha256"' in _step_python(STAGING_STEP)
    assert "LOCK_SHA256" in _step_python(STAGING_STEP)


# --- 5.12 the acquisition and the build script must agree on the out root ----
#
# This is the call-chain review of the two halves.  The acquisition step runs
# the upstream driver with --out <A>, so the feed clones land in <A>/feeds and
# the assembled tree in <A>/<board>/tree.  scripts/macos/build-macos.sh derives
# its own UNIXNU_OUT as "$OUT_DIR/unixnu-out" and then proves every feed clone
# is already on disk at the locked commit by scanning "$FEEDS_DIR"/* -- and it
# fetches nothing itself, by policy.  Two different roots therefore mean this
# step populates one directory while prepare-sources scans another, finds
# nothing, and refuses with artifact_missing: the build never reaches a build
# stage at all, which is the very defect the acquisition step was added to
# remove.  Measured: with OUT_ROOT=build/unixnu-out and no --out passed to the
# script, the script's default OUT_DIR is scripts/macos/build-out/<run-id>.


BUILD_SCRIPT = REPO_ROOT / "scripts" / "macos" / "build-macos.sh"


def _build_script_text() -> str:
    assert BUILD_SCRIPT.is_file(), BUILD_SCRIPT
    return BUILD_SCRIPT.read_text(encoding="utf-8")


def _build_step_run() -> str:
    return _step(BUILD_STEP)["run"]


def _build_step_env() -> dict:
    return _step(BUILD_STEP).get("env") or {}


def test_the_acquisition_and_the_build_script_name_the_same_out_root() -> None:
    """Derived from the build script's own lines, not restated here."""
    script = _build_script_text()
    # What the script derives its driver out root from.
    assert 'UNIXNU_OUT="$OUT_DIR/unixnu-out"' in script
    assert 'FEEDS_DIR="$UNIXNU_OUT/feeds"' in script
    # The workflow must hand the script the same OUT_DIR the acquisition used.
    run = _build_step_run()
    assert "--out" in run, "the build script defaults OUT_DIR to scripts/macos/build-out/<run-id>"
    out_dir = _build_step_env()["BUILD_OUTPUT_DIR"]
    assert f'--out "$BUILD_OUTPUT_DIR"' in run
    # ...and the acquisition's OUT_ROOT is exactly that plus the suffix the
    # script appends, so one driver run serves both halves.
    acquisition_env = _step(ACQUISITION_STEP)["env"]
    assert acquisition_env["OUT_ROOT"] == f"{out_dir}/unixnu-out"


def test_the_build_step_passes_the_arguments_the_script_requires() -> None:
    """--stage and --experiment are required by the script's own parser; the
    dispatch form feeds them, so the invocation must name them."""
    script = _build_script_text()
    assert "--stage)          STAGE=" in script
    assert "--experiment)     EXPERIMENT=" in script
    assert "die 'config_invalid' '--stage is required.'" in script
    run = _build_step_run()
    assert '--stage "$STAGE"' in run
    assert '--experiment "$EXPERIMENT_ID"' in run
    # The two checkouts prepare-sources looks for are the two named here; the
    # third (Mu-Silicium, --uefi) is asserted in test_payload_font_header.py.
    assert "--unixnu vendor/UniXNU" in run
    assert "--device vendor/UniXNU-qemu" in run
    assert "readonly UNIXNU_DIR_DEFAULT='vendor/UniXNU'" in script
    assert "readonly DEVICE_DIR_DEFAULT='vendor/UniXNU-qemu'" in script


def test_the_build_step_scans_the_same_directory_it_writes_to() -> None:
    """The staging step scans $BUILD_OUTPUT_DIR; the build script writes its
    reports under $OUT_DIR.  If those differ, the manifest is built from an
    empty directory and the job fails at 'no allowlisted payload'."""
    staging_env = _step(STAGING_STEP)["env"]
    build_env = _build_step_env()
    assert staging_env["BUILD_OUTPUT_DIR"] == build_env["BUILD_OUTPUT_DIR"]
    assert f'--out "$BUILD_OUTPUT_DIR"' in _build_step_run()


def test_the_acquisition_places_what_prepare_sources_looks_for() -> None:
    """prepare-sources accepts a checkout only when it has a .git directory,
    and checks the module submodules under $UNIXNU_DIR/<path>."""
    script = _build_script_text()
    assert 'if [[ -d "$checkout/.git" ]]' in script
    assert 'for dir in "$UNIXNU_DIR/$name" "$DEVICE_DIR/$name"; do' in script
    source = _step_python(ACQUISITION_STEP)
    # The acquisition makes each destination a git repository before fetching,
    # which is what the -d .../.git test keys on.
    assert '"init", "-q"' in source
    # ...and it never links or copies the upstreams in: they are real clones,
    # because update_submodules() has to run git inside them.
    assert "symlink" not in source


def test_no_head_after_the_checkout_is_a_refusal(tmp_path: Path, monkeypatch) -> None:
    """A checkout that produces no HEAD is not a checkout at the pin.

    Found by mutation: replacing `if head is None:` with `if False:` left every
    test green, because no test had a git that reports nothing.
    """
    module = _load_step(ACQUISITION_STEP, tmp_path)
    wanted = "a" * 40

    def silent(command, *args, **kwargs):
        parts = [str(part) for part in command]
        if "init" in parts:
            (tmp_path / "vendor" / "UniXNU" / ".git").mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", silent)
    with pytest.raises(module.SourcePinError) as raised:
        module.acquire_one(
            "https://example.invalid/x.git", wanted, tmp_path / "vendor" / "UniXNU"
        )
    assert "no HEAD after checkout" in str(raised.value)


# ---------------------------------------------------------------------------
# 6. scripts/macos/build-macos.sh, run for real against a scratch repository
# ---------------------------------------------------------------------------
#
# The script's own STATUS header used to say, in as many words, that its two
# output files were asserted by no test on any host: "Nothing in the suite reads
# stage-result.json or artifact-manifest.json back off disk, so the *contents* of
# the two files this script writes ... are asserted by no test".  That was true
# when it was written, and this section is what made it false.  The header now
# names this section instead; tests/acceptance/test_macos_pipeline_gates.py keeps
# it honest, which is why the sentence had to go rather than stay.
#
# HOW, given that the Apple toolchain does not exist here
# ------------------------------------------------------
# The script is run, not grepped.  What makes that possible off macOS is a
# scratch repository built in tmp_path whose pieces are all real:
#
#   * the script's own bytes, copied in unchanged and asserted byte-identical
#     by hash, so a drift between the copy and the shipped file fails the test
#     rather than silently testing something else;
#   * vendor/UniXNU and vendor/UniXNU-qemu as real `git init` repositories whose
#     HEADs ARE the lock's pins -- prepare-sources compares a pinned commit to
#     what `git rev-parse HEAD` reports, so a fake would defeat the check the
#     step exists to make;
#   * the feed clone at out/unixnu-out/feeds/xnu, also a real repository at its
#     pin, because that is the directory the script scans and refuses to
#     populate itself;
#   * an upstream driver that records every argv it is handed and writes the
#     artifact each stage then records.  It is the only stand-in, and it stands
#     in for the *driver*, never for the script: the argument construction and
#     the artifact recording under test are the script's own.
#   * a `uname` that answers Darwin plus the rest of kernel.py:73-75's tool
#     list, so apple_step_gate() lets the build stages run.  Everything the
#     gate checks is a fact about the host, and a shim is the only way to state
#     a different one; the test that the gate refuses a non-macOS host runs
#     WITHOUT the shim, on the host's own word.
#
# WHAT IS PROVEN HERE, AND WHAT IS NOT
# -----------------------------------
# Proven on this Windows host: the exact argv each stage hands the driver, the
# canonical shape and the real hashes of artifact-manifest.json, that a step
# which cannot run is recorded not_run with its reason and that the run then
# reports ok=false rather than success, and the refusals (network, wrong
# accelerator, malformed input, absent pinned source).
#
# NOT proven here, and not provable here: that the arguments BUILD anything.
# kernel.build() needs xcodebuild and the Xcode iphoneos SDK (kernel.py:75);
# the driver prints "developed and tested on macOS only" on every other host
# (cli.py:206-207).  A green section 6 says the entry point drives the driver
# correctly and reports honestly -- it says nothing about a kernel.
#
# THE FOUR DEFECTS THIS SECTION WAS WRITTEN AGAINST, AND THE MUTATION EACH ONE
# ---------------------------------------------------------------------------
# Writing the tests broke four things, and each fix was proven by putting the
# defect back and watching the test that names it fail (the project's 对质 rule).
# Recorded here because a test that has never seen its code broken is not
# evidence, and because the next reader deserves to know these were measured
# rather than reasoned about:
#
#   1. offline_missing() did not strip the CR a Windows interpreter leaves on the
#      last field of a piped line, so a feed clone sitting on disk at exactly the
#      locked commit was reported as "the driver would have to fetch" and the run
#      refused with artifact_missing.  Mutation: delete the two `%$'\r'` strips.
#      Witness: every test that reaches prepare-sources.
#   2. All three apple_step_gate call sites added the Homebrew LLVM keg to their
#      requirement list, so a macOS runner with Apple clang and no Homebrew LLVM
#      was told the kernel step could not run -- although util.tool() finds keg
#      tools without the keg being on PATH and toolchain_path() only prepends the
#      kegs that exist.  Mutation: put the keg back in build-kernel's `need`.
#      Witness: test_the_homebrew_keg_is_not_a_requirement_of_the_kernel_step.
#      Measured with it restored: build-kernel not_run, reason "missing:
#      /opt/homebrew/opt/llvm or /usr/local/opt/llvm", while build-bootloader and
#      build-devicetree still ran.
#   3. write_outputs emitted `"unfrozen_build": 1 && printf 'true' || printf
#      'false',` -- the arithmetic expansion was evaluated inside the double
#      quotes and the rest of the line was literal text, so build-report.json was
#      not JSON at all.  Mutation: restore `$(( LOCK_UNFROZEN ))`.
#      Witness: test_the_report_the_script_writes_is_json_and_names_the_frozen_state.
#   4. A declared --include was recorded in the manifest with its BASENAME as the
#      path, so two inputs with the same basename were indistinguishable and the
#      recorded path located nothing.  Mutation: record ${leaf} again.
#      Witness: test_a_full_run_records_what_it_built_in_the_canonical_manifest.
#      Measured with it restored: kernelcache.bin recorded at "kernelcache.bin".


_SCRATCH_BOARD = "qemu"

#: board.toml of the scratch device repository.  It is the pinned board's shape,
#: not its contents: [kernel] defconfig, an [ipsw] whose default is an https URL
#: (so a run without --ipsw has something to refuse), and [payload.adt] whose
#: unique_chip_id is "required".  There is deliberately no [firmware], exactly
#: like the real board (UniXNU-qemu/board.toml:85-86), so assemble-image has the
#: same reason to record not_run here as it does there.
_SCRATCH_BOARD_TOML = """\
[board]
name = "qemu"
kit = "qemu"

[kernel]
defconfig = "qemu_defconfig"
archives = ["xnu"]

[ipsw]
url = "https://example.invalid/iPad.ipsw"
sha256 = "@@IPSW_SHA@@"

[payload]
loader = "driver"

[payload.adt]
tool = "scripts/make-qemu-adt.py"
unique_chip_id = "required"
"""

_SCRATCH_DEFCONFIG = "# placeholder defconfig\nCONFIG_QEMU=y\n"

#: A stand-in for scripts/unixnu/cli.py.  Every invocation is appended to
#: argv.log as JSON, which is what the argument-construction tests read.  The
#: subcommands the entry point issues are answered the way the real driver
#: answers them, including the artifact each stage then records:
#:   defconfig <name>      -> out/<board>/.config            (cli.py:89-101)
#:   build kernel          -> out/<board>/kernel/kernel.macho (kernel.py:105-137)
#:   build package         -> out/<board>/tree/ + the device repo's own builder
#:   ipsw fetch DeviceTree -> out/<board>/ipsw/DeviceTree.im4p (ipsw.py:156-171)
#:   bootimg               -> out/<board>/boot/boot.img       (bootimg.py:71-114)
#: `steps` is a POSITIONAL on the build subparser (cli.py:47-48) and argparse
#: takes it from anywhere in the line, so it is found by scanning rather than by
#: position -- which is also what makes the entry point's habit of putting it
#: after the common options legal.
_SCRATCH_DRIVER = '''\
#!/usr/bin/env python3
"""A stand-in for scripts/unixnu/cli.py that records what it was asked to do."""
import json
import os
import struct
import sys

here = os.path.dirname(os.path.abspath(__file__))
BS = chr(92)          # a backslash, so no level of quoting has to escape one
DARWINBOOT = @@DARWINBOOT@@
argv = sys.argv[1:]
with open(os.path.join(here, "argv.log"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + chr(10))

sub = argv[0] if argv else ""
if sub == "status":
    sys.stderr.write("developed and tested on macOS only" + chr(10))
    print("device   %s (qemu)" % here)
    print("config   none (run defconfig)")
    raise SystemExit(0)

out = ""
if "--out" in argv:
    out = argv[argv.index("--out") + 1]
work = os.path.join(out, "@@BOARD@@")


def write(rel, text="artifact" + chr(10)):
    path = os.path.join(work, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def pe_efi_application():
    """A minimal PE32+ AArch64 image whose subsystem is 10 (EFI application).

    `unixnu build package` builds the real one into package/Build/Out
    (cli.py:38 --target defaults to DEBUG, cli.py:134 forwards it, and
    package/build.sh:18,88 write $BUILD_DIR/Build/Out/$ARCH-$TARGET with
    ARCH=AARCH64), and build-bootloader refuses the whole run when it is
    absent -- so a fixture that omits it cannot reach any stage after the
    bootloader.  It has to be a real PE32+ header rather than text:
    build-bootloader reads the subsystem and refuses anything that is not an
    application, which is what the firmware answers a driver with.
    """
    head = bytearray(0x400)
    head[0:2] = b"MZ"
    offset = 0x80
    struct.pack_into("<I", head, 0x3C, offset)
    head[offset:offset + 4] = b"PE" + bytes(2)
    optional = offset + 24
    struct.pack_into("<H", head, optional, 0x20B)    # PE32+ (64-bit)
    struct.pack_into("<H", head, optional + 68, 10)  # EFI application
    return bytes(head)


if sub == "defconfig":
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, ".config"), "w", encoding="utf-8") as fh:
        fh.write("CONFIG_QEMU=y" + chr(10))
    print("wrote %s" % os.path.join(work, ".config"))
elif sub == "build":
    step = next((a for a in argv[1:] if a in ("kernel", "package", "firmware", "payload")), "")
    if step == "kernel":
        path = write("kernel/kernel.macho", "fake mach-o" + chr(10))
        logs = os.path.join(work, "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "prelink.log"), "w", encoding="utf-8") as fh:
            fh.write("kernelcache sha256 deadbeef" + chr(10))
        print("wrote %s" % path.replace(BS, "/"))
    elif step == "package":
        tree = os.path.join(work, "tree")
        os.makedirs(os.path.join(tree, "scripts"), exist_ok=True)
        os.makedirs(os.path.join(tree, "build", "darwinboot-efi"), exist_ok=True)
        # The device repository's own bootloader builder, which the real driver
        # assembles into the tree from the device repository's scripts/.
        with open(os.path.join(tree, "scripts", "build-darwinboot-efi.py"), "w", encoding="utf-8") as fh:
            fh.write(DARWINBOOT)
        # The UEFI application the firmware starts at EFI/BOOT/BOOTAA64.EFI.
        # build-bootloader refuses the run when it is absent, and reads its PE
        # subsystem: a driver there is what the firmware answers "The image is
        # not an application." to, which is the whole reason UniXNU.efi and not
        # DarwinBoot.efi is the one that goes on the ESP.
        app = os.path.join(work, "package", "Build", "Out", "AARCH64-DEBUG", "UniXNU.efi")
        os.makedirs(os.path.dirname(app), exist_ok=True)
        with open(app, "wb") as fh:
            fh.write(pe_efi_application())
        print("wrote %s" % app.replace(BS, "/"))
        print("wrote %s" % tree.replace(BS, "/"))
elif sub == "ipsw":
    print(write("ipsw/DeviceTree.im4p", "fake device tree" + chr(10)).replace(BS, "/"))
elif sub == "bootimg":
    path = write("boot/boot.img", "fake boot image" + chr(10))
    with open(os.path.join(work, "boot", "boot.json"), "w", encoding="utf-8") as fh:
        json.dump({"image": path}, fh)
    print("wrote %s" % path.replace(BS, "/"))
raise SystemExit(0)
'''

#: A stand-in for the device repository's scripts/build-darwinboot-efi.py.  It
#: derives its output from __file__ the way the real one does
#: (build-darwinboot-efi.py:15-17), so it works wherever the tree was assembled,
#: and prints that path as its last line.
_SCRATCH_DARWINBOOT = '''\
#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
work = ROOT / "build" / "darwinboot-efi"
work.mkdir(parents=True, exist_ok=True)
(work / "DarwinBoot.efi").write_text("fake DarwinBoot.efi" + chr(10), encoding="utf-8")
print(work / "DarwinBoot.efi")
'''

#: A stand-in for the device repository's scripts/make-qemu-adt.py, which shells
#: out to qemu-system-aarch64 (make-qemu-adt.py:96-97).  Refusing to run without
#: it is the behaviour under test, so the stand-in reproduces the refusal rather
#: than papering over it.
_SCRATCH_ADT = '''\
#!/usr/bin/env python3
import shutil
import sys

if shutil.which("qemu-system-aarch64") is None:
    raise SystemExit("qemu-system-aarch64 not found (brew install qemu)")
out = ""
if "--output" in sys.argv:
    out = sys.argv[sys.argv.index("--output") + 1]
import os
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as fh:
    fh.write("fake adt" + chr(10))
print("wrote %s" % out)
'''


def _bash() -> str:
    for candidate in ("bash", "bash.exe"):
        located = shutil.which(candidate)
        if located:
            return located
    pytest.skip("no bash on this host, so the build entry point cannot be run")


def _shim(path: Path, body: str) -> Path:
    """An executable shim on the scratch PATH."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_repo(path: Path) -> str:
    """A real repository with one commit; returns its HEAD, the pin to use."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["add", "-A"], path)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pin"], path)
    return _head_of(path)


class _ScratchRepo:
    """A repository ``build-macos.sh`` can drive, built under a tmp directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.out = root / "out"
        self.unixnu = root / "vendor" / "UniXNU"
        self.device = root / "vendor" / "UniXNU-qemu"
        self.bindir = root / "bin"
        self.darwindir = root / "darwin"
        #: The feed clone is kept as a template and copied into whichever output
        #: directory the next run uses, so every test can have its own out root
        #: without paying for a fourth `git init` or losing its feed.
        self.feed_template = root / "feed-template" / "xnu"
        self.pins: dict[str, str] = {}

    # -- construction ------------------------------------------------------

    def build(self, *, frozen: bool = False) -> "_ScratchRepo":
        script = BUILD_SCRIPT
        assert script.is_file(), script
        payload = script.read_bytes()

        # The real package, not a stub.  build-bootloader reads the PE subsystem
        # of the image it is about to publish through
        # ``unipad.host.unixnu_boot.pe_subsystem`` -- the same reader the Windows
        # host refuses with -- so a scratch repository that stubbed ``unipad``
        # out would make that check fail with a ModuleNotFoundError instead of
        # doing its job, and the one guard that keeps a driver off the ESP would
        # be untested here.  src/unipad is pure Python with no third-party
        # import, so the copy is self-contained.
        shutil.copytree(REPO_ROOT / "src" / "unipad", self.repo / "src" / "unipad")
        (self.repo / "scripts" / "macos").mkdir(parents=True)
        copied = self.repo / "scripts" / "macos" / "build-macos.sh"
        copied.write_bytes(payload)
        # The copy is what gets run.  If it ever stops being the shipped file,
        # every assertion below is about something else.
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()

        driver = _shim(self.unixnu / "unixnu", self._driver_source())
        (self.unixnu / "scripts" / "unixnu").mkdir(parents=True)
        (self.unixnu / "scripts" / "unixnu" / "cli.py").write_text("# stub\n", encoding="utf-8")
        self.pins["unixnu"] = _make_repo(self.unixnu)

        (self.device / "configs").mkdir(parents=True)
        (self.device / "scripts").mkdir(parents=True)
        board = _SCRATCH_BOARD_TOML.replace("@@IPSW_SHA@@", "0" * 64)
        (self.device / "board.toml").write_text(board, encoding="utf-8")
        (self.device / "configs" / "qemu_defconfig").write_text(_SCRATCH_DEFCONFIG, encoding="utf-8")
        (self.device / "scripts" / "build-darwinboot-efi.py").write_text(_SCRATCH_DARWINBOOT, encoding="utf-8")
        (self.device / "scripts" / "make-qemu-adt.py").write_text(_SCRATCH_ADT, encoding="utf-8")
        self.pins["device_repo"] = _make_repo(self.device)

        self.feed_template.mkdir(parents=True)
        (self.feed_template / "README").write_text("xnu\n", encoding="utf-8")
        self.pins["xnu"] = _make_repo(self.feed_template)

        self._write_lock(frozen=frozen)
        self._write_shims()
        assert driver.is_file()
        return self

    def _driver_source(self) -> str:
        source = _SCRATCH_DRIVER.replace("@@BOARD@@", _SCRATCH_BOARD)
        return source.replace("@@DARWINBOOT@@", repr(_SCRATCH_DARWINBOOT))

    def _write_lock(self, *, frozen: bool) -> None:
        lock = {
            "schema_version": 1,
            "unixnu": {"repo": "https://example.invalid/UniXNU", "commit": self.pins["unixnu"]},
            "device_repo": {
                "repo": "https://example.invalid/UniXNU-qemu",
                "commit": self.pins["device_repo"],
            },
            "submodules": [],
            "feeds": (
                [{"name": "xnu", "git": "https://example.invalid/xnu.git", "commit": self.pins["xnu"]}]
            ),
            "ipsw": {"name": "iPad.ipsw", "sha256": "0" * 64},
            "tools": {"qemu": {"accel": "tcg", "build": "native Windows"}},
            "frozen": frozen,
        }
        (self.repo / "config").mkdir(parents=True, exist_ok=True)
        (self.repo / "config" / "sources.lock.json").write_text(
            json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _write_shims(self) -> None:
        """Two shim directories, because the two groups answer different questions.

        ``bin/`` answers "is the tool there": xcodebuild, clang and an
        interpreter, which require_tools() insists on whatever the host is.
        ``darwin/`` answers "is this macOS": a `uname` that says so plus the rest
        of kernel.py:73-75's list, which apple_step_gate() consults.  Keeping them
        apart is what lets the non-macOS test run the real preflight -- real
        xcodebuild and clang shims, the host's own `uname` -- instead of a
        different script path.
        """
        self.bindir.mkdir(parents=True, exist_ok=True)
        for name, line in (
            ("xcodebuild", "Xcode 16.0"),
            ("clang", "clang version 17.0.6"),
        ):
            _shim(self.bindir / name, "#!/bin/sh\nprintf '%s\\n' '%s'\nexit 0\n" % ("%s", line))
        _shim(self.bindir / "python3", '#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)
        # `git` is deliberately NOT shimmed anywhere: the real one is still found
        # further down PATH, and prepare-sources pins checkouts with it.

        self.darwindir.mkdir(parents=True, exist_ok=True)
        _shim(self.darwindir / "uname", "#!/bin/sh\necho Darwin\n")
        for name in ("make", "cmake", "ninja", "unifdef", "llvm-objcopy", "llvm-nm", "fdtput", "fdtget"):
            _shim(self.darwindir / name, "#!/bin/sh\nexit 0\n")

    # -- running -----------------------------------------------------------

    def _ensure_feeds(self) -> None:
        """Put the feed clone where this run's out root expects it.

        The script scans "$OUT_DIR/unixnu-out/feeds" and populates nothing itself,
        so the clone has to be there before the run starts -- under whatever out
        root the run was given, which is why it is copied rather than built once.
        """
        clone = self.out / "unixnu-out" / "feeds" / "xnu"
        if clone.exists():
            shutil.rmtree(clone, ignore_errors=True)
        clone.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.feed_template, clone)

    def run(self, *extra: str, stage: str = "M9", darwin: bool = True,
            experiment: str = "scratch", feeds: bool = True,
            frozen: bool | None = None) -> subprocess.CompletedProcess:
        if frozen is not None:
            self._write_lock(frozen=frozen)
        if feeds:
            self._ensure_feeds()
        # The driver appends to argv.log, so it is cleared per run: what a test
        # reads back is what THIS run asked the driver to do, not the union of
        # every run the shared fixture ever made.
        log = self.unixnu / "argv.log"
        if log.exists():
            log.unlink()
        prefix = self.bindir if not darwin else f"{self.darwindir}{os.pathsep}{self.bindir}"
        env = dict(os.environ)
        env["PATH"] = f"{prefix}{os.pathsep}{env.get('PATH', '')}"
        env["PYTHONIOENCODING"] = "utf-8"
        command = [
            _bash(),
            (self.repo / "scripts" / "macos" / "build-macos.sh").as_posix(),
            "--stage", stage,
            "--experiment", experiment,
            "--out", self.out.as_posix(),
            "--unixnu", self.unixnu.as_posix(),
            "--device", self.device.as_posix(),
            "--min-free-gb", "0",
            *extra,
        ]
        return subprocess.run(
            command, capture_output=True, text=True, env=env,
            cwd=str(self.repo), timeout=900,
        )

    # -- reading the run back ----------------------------------------------

    def argv_log(self) -> list[list[str]]:
        path = self.unixnu / "argv.log"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _json(self, name: str) -> dict:
        path = self.out / name
        assert path.is_file(), f"{name} was not written under {self.out}"
        return json.loads(path.read_text(encoding="utf-8"))

    def report(self) -> dict:
        return self._json("build-report.json")

    def manifest(self) -> dict:
        return self._json("artifact-manifest.json")

    def manifest_bytes(self) -> str:
        return (self.out / "artifact-manifest.json").read_text(encoding="utf-8")

    def stage_result(self) -> dict:
        return self._json("stage-result.json")

    def steps(self) -> dict[str, dict]:
        return {entry["name"]: entry for entry in self.report()["steps"]}

    def stage_steps(self) -> dict[str, dict]:
        return {entry["name"]: entry for entry in self.stage_result()["steps"]}


def _error_of(result: subprocess.CompletedProcess) -> dict:
    """The machine-readable refusal the run exited on, or {} when there is none."""
    for line in (result.stdout + result.stderr).splitlines():
        line = line.strip()
        if line.startswith('{"error"'):
            try:
                return json.loads(line)["error"]
            except json.JSONDecodeError:
                return {}
    return {}


@pytest.fixture(scope="module")
def scratch(tmp_path_factory) -> _ScratchRepo:
    """One scratch repository, shared by every test in this section.

    Building it means three `git init` + commit round trips, and every test that
    runs the script pays the preflight again anyway; sharing the repository keeps
    that cost once instead of once per test.  Each test still gets its own OUTPUT
    directory, so no test reads another test's artifacts.
    """
    root = tmp_path_factory.mktemp("build-macos-scratch")
    return _ScratchRepo(root).build(frozen=False)


@pytest.fixture
def fresh_out(scratch: _ScratchRepo, tmp_path: Path) -> Path:
    """A per-test output directory, so runs never overwrite each other."""
    out = tmp_path / "out"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    scratch.out = out
    return out


# --- 6.1 the exact argv each stage hands the driver -------------------------


def test_a_full_run_constructs_the_arguments_each_stage_hands_the_driver(
    scratch: _ScratchRepo, fresh_out: Path
) -> None:
    """The mapping is the script's own header comment; this proves the bytes match it.

    Read out of the driver's argv.log rather than out of the script's text, so a
    command that is spelled right in a comment and wrong in the code fails here.
    """
    kernelcache = fresh_out.parent / "kernelcache.bin"
    kernelcache.write_text("fake kernelcache\n", encoding="utf-8")
    ipsw = fresh_out.parent / "iPad.ipsw"
    ipsw.write_text("fake ipsw\n", encoding="utf-8")

    result = scratch.run(
        "--kernelcache", kernelcache.as_posix(),
        "--ipsw", ipsw.as_posix(),
        "--include", f"{kernelcache.as_posix()}::kernel::aarch64",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    calls = scratch.argv_log()
    assert calls, "the driver was never invoked, so no argument construction ran"

    # (1) the common arguments every invocation carries, in the order cli.py's
    #     `common()` parser expects: the subcommand, then -D/--out/-j, then the
    #     subcommand's own operands.
    for call in calls:
        assert call[0] in ("defconfig", "build", "ipsw", "bootimg"), call
        assert "-D" in call and call[call.index("-D") + 1] == scratch.device.as_posix(), call
        assert "--out" in call, call
        assert call[call.index("--out") + 1].endswith("unixnu-out"), call
        assert "-j" in call, call
        assert call[call.index("-j") + 1].isdigit(), call

    # (2) build-kernel: `unixnu defconfig <[kernel] defconfig>` then
    #     `unixnu build kernel` (cli.py:89-101, 123-129).  The defconfig name is
    #     board.toml's, not a guess, and the two file inputs are forwarded.
    defconfig = [c for c in calls if c[0] == "defconfig"]
    assert len(defconfig) == 1, calls
    assert defconfig[0][defconfig[0].index("-j") + 2] == "qemu_defconfig"
    assert "--kernelcache" in defconfig[0] and defconfig[0][-1] == ipsw.as_posix()

    build_kernel = [c for c in calls if c[0] == "build" and "kernel" in c]
    assert len(build_kernel) == 1, calls
    # The step's own operands come last, in the order step_build_kernel appends
    # them: the kernelcache then the IPSW.
    assert build_kernel[0][-4:] == [
        "--kernelcache", kernelcache.as_posix(),
        "--ipsw", ipsw.as_posix(),
    ], build_kernel[0]

    # (3) build-bootloader: `unixnu build package` (cli.py:134), no step named,
    #     because the bootloader is the device repository's own builder.
    build_package = [c for c in calls if c[0] == "build" and "package" in c]
    assert len(build_package) == 1, calls

    # (4) build-devicetree: `unixnu ipsw fetch DeviceTree --ipsw FILE`
    #     (cli.py:156-171), the component named explicitly.  The subcommand's own
    #     operands follow the common options, which argparse accepts because they
    #     are positionals on the subparser (cli.py:56-58).
    fetch = [c for c in calls if c[0] == "ipsw"]
    assert len(fetch) == 1, calls
    after_common = fetch[0][fetch[0].index("-j") + 2:]
    assert after_common[:2] == ["fetch", "DeviceTree"], fetch[0]
    assert fetch[0][-2:] == ["--ipsw", ipsw.as_posix()]

    # (5) the driver's output is redirected into this run's directory, never
    #     left to write out/ inside a pinned checkout (config.py:64).
    for call in calls:
        assert fresh_out.as_posix() in call[call.index("--out") + 1], call


# --- 6.2 the manifest: canonical shape, real hashes, portable paths ---------


def test_a_full_run_records_what_it_built_in_the_canonical_manifest(
    scratch: _ScratchRepo, fresh_out: Path
) -> None:
    """The manifest is the Windows verifier's contract; its shape is derived from
    unipad.models.ArtifactManifest rather than restated here, and its hashes are
    recomputed from the files on disk."""
    kernelcache = fresh_out.parent / "kernelcache.bin"
    kernelcache.write_text("fake kernelcache\n", encoding="utf-8")
    ipsw = fresh_out.parent / "iPad.ipsw"
    ipsw.write_text("fake ipsw\n", encoding="utf-8")

    result = scratch.run(
        "--kernelcache", kernelcache.as_posix(),
        "--ipsw", ipsw.as_posix(),
        "--include", f"{kernelcache.as_posix()}::kernel::aarch64",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    raw = scratch.manifest_bytes()
    manifest = json.loads(raw)

    # (1) the shape is ArtifactManifest's, key for key, and the bytes are the
    #     canonical form the Windows verifier consumes without translation:
    #     sort_keys=True, indent=2, trailing newline, and ensure_ascii=False so a
    #     non-ASCII path survives as itself rather than as \uXXXX escapes -- which
    #     is what ArtifactManifest.to_json() emits.
    assert set(manifest) == set(ArtifactManifest.__dataclass_fields__), sorted(manifest)
    assert raw == json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n", (
        "artifact-manifest.json is not in the canonical form "
        "(sort_keys=True, indent=2, ensure_ascii=False, trailing newline)"
    )
    for entry in manifest["artifacts"]:
        assert set(entry) == set(ArtifactEntry.__dataclass_fields__), sorted(entry)

    # (2) the run identity is the lock's, never the request's.
    assert manifest["run_id"].startswith("scratch-M9-")
    assert manifest["target"] == "tcg"
    assert manifest["unixnu_commit"] == scratch.pins["unixnu"]
    assert manifest["device_repo_commit"] == scratch.pins["device_repo"]
    assert manifest["toolchain"]["python3"].startswith("Python ")

    # (3) the artifacts the stages produced, each hashed from the file the
    #     manifest names -- so a record of a file that is not there, or of a hash
    #     that is not the file's, fails here.
    #
    #     UniXNU.efi is in the set because build-bootloader records it: it is the
    #     UEFI *application* the firmware starts at EFI/BOOT/BOOTAA64.EFI, and
    #     until it was collected the runner built it and threw it away.  The
    #     subsystem is checked at the same time (see the same step), so this is
    #     the one entry in the manifest whose absence means the machine cannot
    #     start at all.
    built = {entry["name"]: entry for entry in manifest["artifacts"]}
    assert set(built) == {
        "kernel.macho",
        "DarwinBoot.efi",
        "UniXNU.efi",
        "DeviceTree.im4p",
        "kernelcache.bin",
    }

    def on_disk(entry: dict) -> Path:
        recorded = Path(entry["path"])
        return recorded if recorded.is_absolute() else fresh_out / recorded

    for name, entry in built.items():
        # Assert the file is there BEFORE hashing it: a manifest entry whose path
        # locates nothing is the defect, and it deserves that sentence rather
        # than a FileNotFoundError from the hasher.
        assert on_disk(entry).is_file(), (
            f"the manifest records {name} at {entry['path']!r}, which is not a file"
        )
        assert entry["sha256"] == hashlib.sha256(on_disk(entry).read_bytes()).hexdigest(), name
        assert entry["size_bytes"] == on_disk(entry).stat().st_size, name
    assert built["kernel.macho"]["role"] == "kernel"
    assert built["DarwinBoot.efi"]["role"] == "bootloader"
    assert built["DeviceTree.im4p"]["role"] == "devicetree"

    # (4) the declared deliverable keeps the role and arch the spec named.  '::'
    #     is the delimiter precisely so a Windows drive letter in the path is not
    #     read as "path C, role /Users/...": on this host the spec really does
    #     carry one, and a ':' split would produce a role of the path's tail.
    declared = built["kernelcache.bin"]
    assert declared["role"] == "kernel", declared
    assert declared["arch"] == "aarch64", declared
    assert on_disk(declared).is_file()

    # (5) a path inside the run's output directory is recorded relative to it, so
    #     the manifest is portable; the declared input is outside it and keeps its
    #     absolute path.
    for name in ("kernel.macho", "DarwinBoot.efi", "DeviceTree.im4p"):
        assert not built[name]["path"].startswith("/"), built[name]
        assert not built[name]["path"][:2].endswith(":"), built[name]


# --- 6.3 not_run with a reason, and never success without evidence ----------


def test_a_stage_that_cannot_run_says_so_and_the_run_is_not_a_success(
    scratch: _ScratchRepo, fresh_out: Path
) -> None:
    """PRD 7.4: missing evidence is unverified, never success.

    Four independent reasons a stage cannot run are in play at once -- the
    Apple-format device tree needs qemu-system-aarch64 to dump the machine's own
    device tree, this board declares no [firmware], the owner root chain's first
    sub-step needs a kernel that has not been built yet, and the material
    manifest needs the header that kernel build writes -- and none may be folded
    into "ran".  The run itself succeeds (exit 0) and still reports ok=false.

    The seed is NOT one of the three reasons here, and that is the point of this
    test's history: the scratch repository used to stub ``src/unipad`` out, so
    the seed derivation failed and the sub-step stopped at the seed gate.  With
    the real package in the fixture the derivation succeeds -- as it does on a
    real runner, where the pin is on disk -- and the gate that actually stops
    this run is QEMU.  The seed refusal is pinned separately in
    ``test_the_platform_seed_is_never_invented_when_it_cannot_be_derived``.

    The third reason is the build-os-root ordering constraint, and it is the
    interesting one: the chain has to run *before* the kernel that consumes it,
    but the tree it lives in is what the kernel stage's 'unixnu defconfig'
    assembles, so on a fresh workspace it can only be not_run.  That is recorded
    rather than papered over -- the alternative is a kernel built from a chain
    nobody observed.
    """
    ipsw = fresh_out.parent / "iPad.ipsw"
    ipsw.write_text("fake ipsw\n", encoding="utf-8")
    kernelcache = fresh_out.parent / "kernelcache.bin"
    kernelcache.write_text("fake kernelcache\n", encoding="utf-8")

    result = scratch.run("--kernelcache", kernelcache.as_posix(), "--ipsw", ipsw.as_posix())
    assert result.returncode == 0, result.stdout + result.stderr

    report_steps = scratch.steps()
    # (1) the Apple-format device tree: its own ledger entry, its own reason.  A
    #     real input that the sub-step installs when it can, and a runner with
    #     neither records the tool's own refusal rather than claiming a tree.
    adt = report_steps["build-devicetree/adt"]
    assert adt["status"] == "not_run", adt
    assert "qemu-system-aarch64" in adt["reason"], adt
    assert "brew install qemu" in adt["reason"], adt
    # ...and it produced nothing, so the manifest carries no hash for it.
    assert "adt" not in {entry["name"] for entry in scratch.manifest()["artifacts"]}

    # (2) assemble-image: no [firmware] in this board, named with the measurement
    #     that produced the reason rather than with a guess.
    assemble = report_steps["assemble-image"]
    assert assemble["status"] == "not_run", assemble
    assert "no [firmware]" in assemble["reason"], assemble

    # (2b) build-os-root: the chain has to run *before* the kernel that consumes
    #      it, but on a fresh workspace nothing has been built yet, so its own
    #      script is not even in the tree -- the tree is what 'unixnu defconfig'
    #      assembles, and that is the kernel stage.  This is the ordering
    #      constraint showing up at its earliest gate, and the reason names the
    #      file, the upstream line that draws it and the stage that makes it, so
    #      an operator reading build-report.json knows this is an ordering fact
    #      rather than a broken host.
    os_root = report_steps["build-os-root"]
    assert os_root["status"] == "not_run", os_root
    assert "build-os-root.py" in os_root["reason"], os_root
    assert "tree.py:70-76" in os_root["reason"], os_root
    assert "unixnu defconfig" in os_root["reason"], os_root

    # (2c) material-manifest: the same ordering fact as (2b), one stage later,
    #      and the one this test is really here to pin.  The manifest binds the
    #      Data volume UUID to the kernel that was compiled with it, and the
    #      header carrying that UUID is written by the kernel build itself --
    #      so on a fresh workspace it cannot exist and the stage says so.
    #
    #      The path is asserted in BOTH directions, and that is the point.
    #      ``unixnu`` has two output roots (config.py:80-82: ctx.work and
    #      ctx.work/tree) and the header is in the inner one, because
    #      kernel.py:100 runs the baseline script with cwd=ctx.tree and that
    #      script derives ROOT from BASH_SOURCE.  A version of the stage looked
    #      under ctx.work/build/ instead, and on run 40339678974 it recorded
    #      not_run on both build passes -- the second of which had just built a
    #      real 1 GiB Data seed and a kernel compiled with its UUID -- so the
    #      run produced no delivery at all.  One missing path segment.  The
    #      assertion below is what stops that coming back: the work-dir spelling
    #      must not appear in the reason, because a stage that names the wrong
    #      path tells the operator the kernel stage failed when it did not.
    material = report_steps["material-manifest"]
    assert material["status"] == "not_run", material
    assert "owner-data-volume.h" in material["reason"], material
    assert "unixnu-out/qemu/tree/build/xnu/owner-data-volume.h" in material["reason"], material
    assert "unixnu-out/qemu/build/" not in material["reason"], material

    # (3) the stages that DID run are recorded ran, with what they ran.
    for name in ("build-kernel", "build-bootloader", "build-devicetree"):
        assert report_steps[name]["status"] == "ran", report_steps[name]

    # (4) the run's own verdict: incomplete, not ok, with the reasons collected.
    stage = scratch.stage_result()
    assert stage["ok"] is False, stage
    assert stage["status"] == "incomplete", stage
    assert stage["step_summary"].startswith("ran="), stage
    assert "not_run=4" in stage["step_summary"], stage
    assert len(stage["not_run_reasons"]) == 4, stage
    assert any("qemu-system-aarch64" in reason for reason in stage["not_run_reasons"])
    assert any("no [firmware]" in reason for reason in stage["not_run_reasons"])
    assert any("build-os-root" in reason for reason in stage["not_run_reasons"])
    assert any("material-manifest" in reason for reason in stage["not_run_reasons"])
    # (5) and the same ledger is what build-report.json carries, so the two files
    #     cannot disagree about what ran.
    assert scratch.stage_steps() == report_steps


# --- 6.4 the report is JSON, and it records the frozen state honestly --------


def test_the_report_the_script_writes_is_json_and_names_the_frozen_state(
    scratch: _ScratchRepo, fresh_out: Path
) -> None:
    """The report is machine-readable or it is nothing.

    Measured defect this closes: `write_outputs` emitted
    ``"unfrozen_build": 1 && printf 'true' || printf 'false',`` -- the arithmetic
    expansion was evaluated inside the double quotes and the rest of the line was
    literal text, so the file was not JSON at all and no consumer could read it.
    A scratch lock that is deliberately unfrozen is what makes the value 1, which
    is the branch that produced the garbage.
    """
    result = scratch.run()
    assert result.returncode == 0, result.stdout + result.stderr

    report = scratch.report()          # json.loads, so a non-JSON file fails here
    assert report["lock"]["frozen"] is False
    assert report["lock"]["unfrozen_build"] is True
    assert report["network_access"] == "none"
    assert report["sources_present"] is True
    assert report["sources_incomplete"] is False
    # The log must not claim the opposite of what the report records.
    assert "lock: frozen=false" in result.stdout, result.stdout
    assert "NOT FROZEN" in result.stdout, result.stdout
    # ...and the run id the report carries is the one the log announced.
    assert report["run_id"] in result.stdout


def test_a_frozen_lock_records_a_frozen_build(scratch: _ScratchRepo, tmp_path: Path) -> None:
    """The other branch of the same field, so a fix that hardcodes one value fails."""
    scratch.out = tmp_path / "frozen-out"
    result = scratch.run(stage="M1", frozen=True)
    assert result.returncode == 0, result.stdout + result.stderr

    report = scratch.report()
    assert report["lock"]["frozen"] is True
    assert report["lock"]["unfrozen_build"] is False
    assert "NOT FROZEN" not in result.stdout, result.stdout


# --- 6.5 the Homebrew keg is an input the driver resolves, not a requirement --


def test_the_homebrew_keg_is_not_a_requirement_of_the_kernel_step(
    scratch: _ScratchRepo, fresh_out: Path
) -> None:
    """util.tool() (util.py:33-38) looks on PATH and THEN in the keg, and
    toolchain_path() (kernel.py:40-44) only prepends the kegs that exist -- so an
    absent keg is something the driver reports precisely, not something this
    entry point refuses up front.

    Measured defect this closes: all three apple_step_gate call sites added
    "/opt/homebrew/opt/llvm or /usr/local/opt/llvm" to their requirement list, so
    a macOS runner with Apple clang and no Homebrew LLVM -- which upstream builds
    happily -- was told the step could not run.  No keg exists on this host, so
    the kernel step running at all is the assertion.
    """
    ipsw = fresh_out.parent / "iPad.ipsw"
    ipsw.write_text("fake ipsw\n", encoding="utf-8")
    result = scratch.run("--ipsw", ipsw.as_posix())
    assert result.returncode == 0, result.stdout + result.stderr

    kernel = scratch.steps()["build-kernel"]
    assert kernel["status"] == "ran", kernel
    for entry in scratch.report()["steps"]:
        assert "/opt/homebrew" not in entry["reason"], entry
    # The keg is still reported, so an operator can see which one was found.
    assert "llvm keg:" in result.stdout, result.stdout


# --- 6.6 the build stages refuse on a host that is not macOS -----------------


def test_the_build_stages_refuse_on_a_host_that_is_not_macos(
    scratch: _ScratchRepo, tmp_path: Path
) -> None:
    """The honesty rule, run on the host's own word.

    With no `uname` shim the script asks the machine, and the answer decides what
    is asserted: off macOS every build stage is not_run naming the host and the
    reason, and the manifest is still written; on macOS the same shims make the
    stages run, and that is asserted instead.  Either way the test never claims a
    stage ran that did not.
    """
    scratch.out = tmp_path / "native-out"
    ipsw = tmp_path / "iPad.ipsw"
    ipsw.write_text("fake ipsw\n", encoding="utf-8")
    result = scratch.run("--ipsw", ipsw.as_posix(), stage="M4", darwin=False)
    assert result.returncode == 0, result.stdout + result.stderr

    steps = scratch.steps()
    names = ("build-kernel", "build-bootloader", "build-devicetree")
    if platform.system() == "Darwin":
        for name in names:
            assert steps[name]["status"] == "ran", steps[name]
        return

    # Off macOS the gate names the host it saw -- which is `uname -s`, so on this
    # Windows box that is MINGW64_NT-..., not platform.system()'s "Windows" -- and
    # the reason it will not try.  Asserting the shape rather than one literal
    # keeps the test true on a Linux runner too, and still fails if the host is
    # ever reported as Darwin when it is not.
    for name in names:
        assert steps[name]["status"] == "not_run", steps[name]
        match = re.search(r"host is (\S+) and the Apple toolchain exists on macOS only", steps[name]["reason"])
        assert match, steps[name]
        assert match.group(1) != "Darwin", steps[name]
    # The run still writes the manifest, unchanged in shape: a refusal is a
    # result, not a crash.
    assert set(scratch.manifest()) == set(ArtifactManifest.__dataclass_fields__)
    assert scratch.stage_result()["ok"] is False


# --- 6.7 the refusals ------------------------------------------------------


def test_an_https_ipsw_is_refused_as_network_access(scratch: _ScratchRepo, tmp_path: Path) -> None:
    """The no-network policy has a witness that is not a typo message.

    Deleting the guard leaves the file-existence check to refuse the URL as
    config_invalid, and a caller could not tell "this script will not touch the
    network" from "you mistyped a path".
    """
    scratch.out = tmp_path / "url-out"
    result = scratch.run("--ipsw", "https://example.invalid/iPad.ipsw")
    error = _error_of(result)
    assert error.get("code") == "network_refused", result.stdout + result.stderr
    assert "no network access" in error.get("message", ""), error
    assert result.returncode == 2


def test_a_target_that_contradicts_the_locked_accelerator_is_refused(
    scratch: _ScratchRepo, tmp_path: Path
) -> None:
    """PRD 8.3: an HVF payload must never be produced for this TCG host."""
    scratch.out = tmp_path / "hvf-out"
    result = scratch.run("--target", "hvf")
    error = _error_of(result)
    assert error.get("code") == "accelerator_not_tcg", result.stdout + result.stderr
    assert "tcg" in error.get("message", ""), error


def test_a_malformed_platform_seed_is_refused(scratch: _ScratchRepo, tmp_path: Path) -> None:
    """The driver parses the seed with int(value, 16) (cli.py:35); a 0x-prefixed
    value is refused here rather than by a traceback inside the board tool."""
    scratch.out = tmp_path / "seed-out"
    result = scratch.run("--unique-chip-id", "0x8011")
    error = _error_of(result)
    assert error.get("code") == "config_invalid", result.stdout + result.stderr
    assert "hex digits" in error.get("message", ""), error


def test_a_declared_input_that_is_not_a_file_is_refused(scratch: _ScratchRepo, tmp_path: Path) -> None:
    scratch.out = tmp_path / "notafile-out"
    result = scratch.run("--kernelcache", str(tmp_path))
    error = _error_of(result)
    assert error.get("code") == "config_invalid", result.stdout + result.stderr
    assert "is not a file" in error.get("message", ""), error


# --- 6.8 the pinned sources are verified, never fetched ---------------------


def test_a_missing_pinned_feed_is_a_refusal_not_a_fetch(scratch: _ScratchRepo, tmp_path: Path) -> None:
    """The offline property is the point of the script.

    prepare() fetches whatever the driver's feeds root is missing
    (cli.py:79-86, feeds.py:173-216), so a feed that is not on disk at its pin is
    exactly the case where the driver would reach the network.  The script
    refuses, names the feed, and runs nothing.
    """
    scratch.out = tmp_path / "nofeed-out"
    feed = scratch.out / "unixnu-out" / "feeds" / "xnu"
    result = scratch.run(feeds=False)
    error = _error_of(result)
    assert error.get("code") == "artifact_missing", result.stdout + result.stderr
    assert "feed:xnu" in error.get("message", ""), error
    assert "no network access" in error.get("message", ""), error
    # Nothing was fetched: the driver log is empty because the driver never ran.
    assert not feed.exists()
    assert scratch.argv_log() == []


def test_skip_sources_validates_the_contract_without_the_sources(
    scratch: _ScratchRepo, tmp_path: Path
) -> None:
    """--skip-sources is the explicit opt-out, and it is still not a success.

    The build steps are recorded not_run with the reason, and the manifest is
    written -- so a caller can validate the contract on a machine that has no
    sources without reading an empty manifest as a build.
    """
    scratch.out = tmp_path / "skip-out"
    result = scratch.run("--skip-sources", feeds=False)
    assert result.returncode == 0, result.stdout + result.stderr

    steps = scratch.steps()
    for name in ("build-kernel", "build-bootloader", "build-devicetree", "assemble-image"):
        assert steps[name]["status"] == "not_run", steps[name]
        assert "--skip-sources" in steps[name]["reason"], steps[name]
    assert steps["prepare-sources"]["status"] == "not_run"
    assert scratch.argv_log() == []
    assert scratch.stage_result()["ok"] is False


def test_a_checkout_that_is_not_a_repository_is_refused_with_the_paths_it_looked_for(
    scratch: _ScratchRepo, tmp_path: Path
) -> None:
    scratch.out = tmp_path / "nocase-out"
    elsewhere = tmp_path / "not-a-checkout"
    elsewhere.mkdir()
    result = scratch.run("--unixnu", elsewhere.as_posix())
    error = _error_of(result)
    assert error.get("code") == "artifact_missing", result.stdout + result.stderr
    # The refusal names the path it looked for, so the caller knows which of the
    # two checkouts is missing.  Compared against the parsed details, because
    # json.dumps escapes a non-ASCII path and the comparison would be about
    # escaping rather than about the path.
    details = error.get("details", {})
    assert any(elsewhere.as_posix() in str(value) for value in details.values()), error


# --- 6.9 dry-run writes nothing --------------------------------------------


def test_dry_run_writes_nothing(scratch: _ScratchRepo, tmp_path: Path) -> None:
    """A dry run validates and plans; it must leave no output behind, because the
    next real run's out directory is where a stale file would be mistaken for a
    fresh artifact."""
    scratch.out = tmp_path / "dry-out"
    result = scratch.run("--dry-run", feeds=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry-run" in result.stdout, result.stdout
    for name in ("build.log", "build-report.json", "artifact-manifest.json", "stage-result.json"):
        assert not (scratch.out / name).exists(), name
    # Every planned step is recorded not_run with the reason 'dry-run', and the
    # run id is announced rather than used.
    assert "run id would be" in result.stdout, result.stdout
    assert scratch.argv_log() == []


# --- 6.9b the platform seed: derived, never echoed --------------------------


def test_a_derived_platform_seed_is_kept_by_digest_and_never_echoed(
    scratch: _ScratchRepo, tmp_path: Path
) -> None:
    """board.toml [payload.adt] unique_chip_id = "required" and the value is in
    no repository by design, so a run that is not given one derives it from the
    pinned device-repo commit.  What may be published is its digest.

    Two things are asserted here, and the second is the one that matters: the
    seed file exists in the run's own workspace (chmod 600), and NEITHER the log
    NOR the ledger NOR build-report.json contains the value.  A seed is a device
    identity, not a secret -- nothing derives a key from it -- but a value that
    reached the uploaded log would give every checkout of the same pin the same
    guest, which is the one thing the seed exists to distinguish.

    The refusal path ("no flag, no environment spelling, nothing to derive
    from") is NOT reachable from this fixture: the scratch repository carries a
    real device-repo HEAD and the real lock, which is exactly the situation on a
    real runner, so the derivation always succeeds here.  That refusal is pinned
    where it lives -- ``unipad.host.adt`` refuses a seedless run itself, and
    ``tests/unit/test_host_adt.py`` asserts it -- because the script's gate is a
    conversion of that hard failure into an accurate not_run, not a second
    implementation of it.
    """
    scratch.out = tmp_path / "seed-out"
    ipsw = tmp_path / "iPad.ipsw"
    ipsw.write_text("fake ipsw\n", encoding="utf-8")
    result = scratch.run("--ipsw", ipsw.as_posix(), stage="M4")
    assert result.returncode == 0, result.stdout + result.stderr

    # The value is on disk, in the run's own workspace, and nowhere else.
    seed_file = scratch.out / "platform-seed"
    assert seed_file.is_file(), "no seed was derived for a board that requires one"
    seed = seed_file.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{16}", seed), seed

    record = json.loads((scratch.out / "platform-seed.json").read_text(encoding="utf-8"))
    assert record["seed_sha256"] == hashlib.sha256(seed.encode()).hexdigest()
    assert seed not in json.dumps(record), record
    # The build log is uploaded as evidence, so this is the assertion that keeps
    # the value out of it.  The script writes build.log into its output dir.
    log = scratch.out / "build.log"
    if log.is_file():
        assert seed not in log.read_text(encoding="utf-8", errors="replace")
    report = scratch.report()
    assert seed not in json.dumps(report), "the seed reached build-report.json"


# --- 6.10 the Apple-format device tree refuses to depend on QEMU -------------


def test_the_apple_device_tree_refuses_to_depend_on_qemu(
    scratch: _ScratchRepo, tmp_path: Path
) -> None:
    """make-qemu-adt.py shells out to qemu-system-aarch64 (make-qemu-adt.py:93-101)
    to dump the device tree of the machine it is about to describe, and refuses
    without it (:95, "brew install qemu").  So QEMU is a *real* input of this one
    sub-step -- a Homebrew package the step installs when it can -- and not a
    build input the way Xcode is.  A runner with neither records not_run naming
    the tool's own refusal rather than claiming the tree was produced.

    Only assertable where QEMU is genuinely absent; on a host that has it the
    step may legitimately run, and the test says so rather than pretending.
    """
    if shutil.which("qemu-system-aarch64") is not None:
        pytest.skip("qemu-system-aarch64 is on this host's PATH, so the step may run")
    scratch.out = tmp_path / "adt-out"
    ipsw = tmp_path / "iPad.ipsw"
    ipsw.write_text("fake ipsw\n", encoding="utf-8")
    result = scratch.run("--ipsw", ipsw.as_posix(), "--unique-chip-id", "8011", stage="M4")
    assert result.returncode == 0, result.stdout + result.stderr

    adt = scratch.steps()["build-devicetree/adt"]
    assert adt["status"] == "not_run", adt
    assert "qemu-system-aarch64" in adt["reason"], adt
    # A real input that is installed when it can be, not a foreign artifact: the
    # reason quotes the tool's own refusal and names the package manager.  The
    # negative assertion is the load-bearing half -- the old reason called QEMU
    # "not a macOS build input", which was false and is what let the sub-step
    # stay not_run forever without anyone noticing it could simply be installed.
    assert "brew install qemu" in adt["reason"], adt
    assert "not a macOS build input" not in adt["reason"], adt
    assert "adt" not in {entry["name"] for entry in scratch.manifest()["artifacts"]}



def test_the_decompressed_kernelcache_is_never_an_uploadable_artifact(
    tmp_path: Path,
) -> None:
    """PRD 12, against the staging step's own code rather than its pattern list.

    The chain's acquisition now publishes the decompressed original kernelcache
    into ``out/ipsw/<build>/`` so that ``build-platform-power.py`` can audit the
    AMFI/PMGR contract on any build pass that already has a reference kernel
    (measured on run 40206274286: nine of ten kernel archives built, and the
    tenth died with "The original kernelcache is not in the IPSW cache").

    That file is an Apple binary sitting *inside* BUILD_OUTPUT_DIR, and
    ``ROLE_RULES`` maps "kernelcache" to the allowlisted role ``kernel`` -- so
    the default-deny below would wave it through on name alone unless its
    suffix is in ``PRIVATE_PATTERNS``.  Reading the pattern tuple would only
    prove a string is present; this runs the step and reads what it decided.

    Why this matters beyond tidiness: the step that removes ``out/ipsw/`` runs
    *before* this one today, so the ordering happens to be safe.  PRD 12 does
    not get to rest on a step order.
    """
    code = _step_python(STAGING_STEP)
    out = tmp_path / "build-output"
    # One real deliverable, so the step does not refuse the whole build for
    # having produced nothing (PRD 7.4) -- which is a different failure and
    # would tell us nothing about the file under test.
    kernel = out / "unixnu-out" / "qemu" / "kernel" / "kernel.macho"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"synthetic kernel\n")
    # The file under test, at the exact path the acquisition publishes.
    cached = out / "unixnu-out" / "ipsw" / "21H461" / "kernelcache.decompressed"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"synthetic decompressed Apple kernelcache\n")
    build_log = tmp_path / "macos-build.log"
    build_log.write_text("synthetic build log\n")

    script = tmp_path / "stage.py"
    script.write_text(code, encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "BUILD_OUTPUT_DIR": str(out),
            "STAGE_DIR": str(tmp_path / "staged"),
            "BUILD_LOG": str(build_log),
            "REPORT_DIR": str(tmp_path / "build"),
            "PAYLOAD_TARGET": "tcg",
        }
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, (
        "the staging step refused a build that has a deliverable:\n"
        + (result.stdout + result.stderr)[-2000:]
    )
    report = json.loads(
        (tmp_path / "build" / "staging-report.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "pass", report
    excluded = {item["path"]: item for item in report["excluded"]}
    # The staged set, spelled from the disk rather than from a key this step's
    # report does not carry: the first draft of this assertion read
    # ``manifest["artifacts"]`` after the manifest had been replaced by the
    # report, so removing the pattern made the test fail with a NameError
    # inside its own message -- red for the wrong reason, which is the failure
    # mode this whole file exists to prevent.
    staged = {
        path.relative_to(tmp_path / "staged").as_posix()
        for path in (tmp_path / "staged").rglob("*")
        if path.is_file()
    }
    relative = "unixnu-out/ipsw/21H461/kernelcache.decompressed"
    assert relative in excluded, (
        f"{relative} was not excluded, so the staging step would upload an "
        f"Apple binary.  excluded={sorted(excluded)}; staged={sorted(staged)}"
    )
    assert "pattern" in excluded[relative]["reason"], (
        f"{relative} was excluded for the wrong reason "
        f"({excluded[relative]['reason']!r}); the name is what makes it private"
    )
    assert relative not in staged, (
        f"{relative} was staged as well as excluded"
    )
