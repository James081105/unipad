"""Unit tests for the controller CLI (PRD 9.1, 7.4, 7.5, 8.3, FR08).

Mapping
-------
* PRD 9.1 -- every command prints **exactly one** machine-readable JSON object
  and returns a fixed exit code; the object always carries
  ``command``/``status``/``exit_code``/``data``/``error`` so scripts branch on
  fields, never on prose.
* PRD 9.1 -- the install interface reports package hash, bundle id, furthest
  stage, trust mode, error type and log location; a successful file transfer
  never reports ``installed``.
* PRD FR08 / appendix A -- when the capability cannot run on this host (no QEMU
  binary, no guest channel, unfrozen sources) the command returns a structured
  ``blocked`` result with a concrete reason.  A fake success is never produced.
* PRD 7.4 / 9.2 -- ``collect`` with an incomplete evidence set reports
  ``evidence_incomplete`` and leaves the run unverified.
* PRD 8.3 -- the accelerator guard: only the ``tcg`` profile is accepted.
* PRD 2.2-1 / 9.2 -- the launch record transcribes the command line it started,
  so the recorded accelerator is evidence rather than a constant.
* PRD 9.2 -- ``doctor`` names the interpreter by its final component, because an
  absolute path embeds the Windows username and this envelope is evidence.
* PRD 3.2 P1 (2) -- the compatibility matrix's unreadable lines reach an
  operator, split by cause, instead of vanishing.
* callchain finding 10 -- a crash in the readiness wait stops the child it
  started, so QEMU is never orphaned and unstoppable.

Almost everything runs against ``tmp_path`` run roots and a ``tmp_path`` budget
config, with no wall clock and no network.  The two ``start`` tests are the
exception and do launch a child: they drive the **real**
:class:`~unipad.host.qemu.QemuLauncher` against the fake-QEMU script
(``docs/testing-strategy.md`` D3), because the contracts under test -- what the
launch record says, and whether the child survives a crash -- are properties of
a real launch and cannot be observed through a stub.  Both arrange for the child
to exit or be stopped before the test ends.
"""

from __future__ import annotations

import io
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

import pytest

from unipad import __version__
from unipad.controller import cli
from unipad.controller.experiments import ExperimentStore
from unipad.errors import ErrorCode, ExitCode, UniPadError
from unipad.host.qemu import QemuLauncher, QmpReadiness
from unipad.ipa.matrix import Authorisation, MatrixEntry, MatrixKey, TrustCategory, Verdict
from tests.fakes import fake_qemu as fq
from tests.fixtures.ipa_builder import DEFAULT_BUNDLE_ID, build_ipa

#: The fake-QEMU script, started in place of the real binary (D3).  Read out of
#: the fake's own public launcher helper rather than spelled, so the script the
#: real :class:`~unipad.host.qemu.QemuLauncher` starts is the one that ships.
FAKE_SCRIPT = Path(fq.fake_qemu_launcher()[1][0])

#: The budget configuration used by every CLI invocation in this module.
BUDGET_CONFIG: dict[str, object] = {
    "schema_version": 1,
    "per_hypothesis_max_experiments": 3,
    "same_failure_signature_max": 3,
    "max_actions_minutes_per_week": 0,
    "max_model_tokens_per_session": None,
    "max_local_run_minutes_per_experiment": 45,
}

_HYPOTHESIS = "a fixed TCG CPU model boots UniXNU past DarwinBoot"


class CliResult:
    """One CLI invocation: exit code plus the parsed envelope."""

    def __init__(self, exit_code: int, document: dict[str, Any], raw: str) -> None:
        self.exit_code = exit_code
        self.document = document
        self.raw = raw

    @property
    def command(self) -> str:
        return str(self.document["command"])

    @property
    def status(self) -> str:
        return str(self.document["status"])

    @property
    def error_code(self) -> str | None:
        error = self.document.get("error")
        return None if not error else str(error["code"])

    @property
    def data(self) -> dict[str, Any]:
        return dict(self.document.get("data") or {})


def _decode_one_object(text: str) -> dict[str, Any]:
    """Parse ``text`` and assert it holds exactly one JSON document.

    PRD 9.1: exactly one JSON object per command.  ``raw_decode`` proves the
    remainder after the first document is whitespace only, so a stray second
    print (a child process leaking to stdout, a traceback) fails here.
    """
    assert text.strip(), "the CLI must print a JSON object"
    document, end = json.JSONDecoder().raw_decode(text.lstrip())
    assert text.lstrip()[end:].strip() == "", "stdout must contain exactly one JSON object"
    assert isinstance(document, dict)
    return document


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    """A tmp_path run root, budget config and machine config."""
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps(BUDGET_CONFIG), encoding="utf-8")
    machine = tmp_path / "machine.json"
    machine.write_text(json.dumps({"name": "ipad-tcg", "accel": "tcg", "cores": 4}), encoding="utf-8")
    lock = tmp_path / "sources.lock.json"
    lock.write_text(json.dumps({"frozen": False}), encoding="utf-8")
    return {
        "root": tmp_path,
        "runs": runs,
        "budget": budget,
        "machine": machine,
        "lock": lock,
    }


def run_cli(argv: list[str], workspace: dict[str, Path]) -> CliResult:
    """Invoke ``cli.main`` with the workspace paths and capture stdout."""
    full = [
        "--runs-root",
        str(workspace["runs"]),
        "--budget-config",
        str(workspace["budget"]),
        "--machine-config",
        str(workspace["machine"]),
        "--sources-lock",
        str(workspace["lock"]),
        "--artifacts-root",
        str(workspace["root"] / "artifacts"),
        *argv,
    ]
    buffer = io.StringIO()
    exit_code = cli.main(full, buffer)
    return CliResult(exit_code, _decode_one_object(buffer.getvalue()), buffer.getvalue())


def run_dir_of(workspace: dict[str, Path], result: CliResult, *, named: str) -> Path:
    """The run directory this invocation created, found without the envelope.

    ``_emit`` redacts every string in the envelope (PRD 9.2: an absolute path
    under the runs root embeds the Windows account name), so
    ``Path(result.data["log_path"])`` is no longer a path -- it is
    ``<redacted:user_path>/...``.  That is the point of the redaction, so a test
    that wants to read the evidence has to find the directory the way an
    operator would: by looking for the file the envelope names, under the runs
    root.

    ``named`` is the file the envelope claims to have written.  It is asserted to
    still be named there, redacted or not, *and* used as the search key, because
    "the envelope names this file, so it must exist" is the contract under test:
    without that assertion the test would pass even if the envelope stopped
    naming the file at all.

    The search is by ``named`` rather than "the only directory here" because the
    runs root is shared -- ``InstallLedger`` keeps its own ``lifecycle/``
    directory beside the run directories -- so "exactly one directory" is not
    true and picking the newest would be a guess.  Raises when the file is not
    found in exactly one directory, so a command that writes its evidence
    somewhere else fails here instead of quietly reading another run's evidence.
    """
    # The envelope must still NAME the file it wrote, redacted or not.
    assert Path(result.data["log_path"]).name == named, result.data["log_path"]
    assert named in result.data["log_path"], result.data["log_path"]
    everything = sorted(path for path in workspace["runs"].iterdir() if path.is_dir())
    created = [path for path in everything if (path / named).is_file()]
    assert len(created) == 1, (
        f"expected exactly one run directory under {workspace['runs']} holding "
        f"{named}, found {[p.name for p in created]} among "
        f"{[p.name for p in everything]}: the test cannot tell which run's "
        "evidence it is inspecting"
    )
    return created[0]


def _create_run(workspace: dict[str, Path], experiment_id: str = "exp-cli-1") -> str:
    store = ExperimentStore(workspace["runs"])
    store.create(experiment_id, "M4", _HYPOTHESIS)
    return experiment_id


def _exit_code_for(code: str) -> int:
    """Return the exit code the contract maps ``code`` onto (PRD 9.1)."""
    return int(UniPadError(code, "x").exit_code)


# ---------------------------------------------------------------------------
# envelope contract
# ---------------------------------------------------------------------------
def test_every_command_prints_exactly_one_json_object(workspace: dict[str, Path]) -> None:
    # Arrange: a run directory so the run-scoped commands have something to read.
    run_id = _create_run(workspace)
    ipa = build_ipa(workspace["root"] / "HelloUniPad.ipa")
    manifest = workspace["root"] / "artifact-manifest.json"
    manifest.write_text(json.dumps({"target": "tcg", "artifacts": []}), encoding="utf-8")

    invocations = {
        "doctor": ["doctor"],
        "build": ["build", "--stage", "M4", "--experiment", run_id, "--hypothesis", _HYPOTHESIS],
        "fetch": ["fetch", "--run-id", "123456"],
        "start": ["start", "--profile", "tcg", "--artifact", str(manifest)],
        "install": ["install", str(ipa)],
        "verify": ["verify", "--suite", "ipa-mvp", "--run", run_id],
        "stop": ["stop", "--run", run_id],
        "collect": ["collect", "--run", run_id],
        "restore": ["restore", "--checkpoint", "cp-1"],
    }

    # Act / assert: PRD 9.1 -- one JSON object per command, always.
    assert set(invocations) == set(cli.COMMANDS)
    for command, argv in invocations.items():
        result = run_cli(argv, workspace)
        assert result.command == command
        assert set(result.document) >= {
            "command",
            "status",
            "exit_code",
            "data",
            "error",
            "schema_version",
            "unipad_version",
        }
        assert result.document["schema_version"] == cli.ENVELOPE_SCHEMA_VERSION
        assert result.document["unipad_version"] == __version__
        assert result.document["exit_code"] == result.exit_code


def test_envelope_status_matches_the_exit_code(workspace: dict[str, Path]) -> None:
    # Arrange.
    result = run_cli(["doctor"], workspace)

    # Assert: PRD 9.1 -- status vocabulary is derived from the exit code.
    assert result.exit_code == int(ExitCode.OK)
    assert result.status == "ok"
    assert result.document["ok"] is True
    assert result.document["error"] is None


def test_a_blocked_command_reports_status_blocked_and_exit_3(workspace: dict[str, Path]) -> None:
    # Arrange: no launch record for this run.
    run_id = _create_run(workspace)

    # Act.
    result = run_cli(["stop", "--run", run_id], workspace)

    # Assert: PRD appendix A -- blocked with a concrete reason, exit 3.
    assert result.status == "blocked"
    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.error_code == ErrorCode.BLOCKED
    assert result.document["ok"] is False


@pytest.mark.parametrize(
    ("argv", "expected_code"),
    [
        (["frobnicate"], ErrorCode.UNSUPPORTED_COMMAND),
        (["collect"], ErrorCode.CONFIG_INVALID),
        (["restore"], ErrorCode.CONFIG_INVALID),
        (["fetch"], ErrorCode.CONFIG_INVALID),
        (["verify", "--suite", "not-a-gate"], ErrorCode.UNSUPPORTED_COMMAND),
        (["start", "--profile", "hvf", "--artifact", "x.json"], ErrorCode.ACCELERATOR_NOT_TCG),
        (["install", "does-not-exist.ipa"], ErrorCode.ARTIFACT_MISSING),
    ],
)
def test_exit_codes_match_the_error_mapping(
    workspace: dict[str, Path], argv: list[str], expected_code: str
) -> None:
    # PRD 9.1: scripts branch on the exit code, which follows errors.ExitCode.
    result = run_cli(argv, workspace)

    assert result.error_code == expected_code
    assert result.exit_code == _exit_code_for(expected_code)


def test_no_command_given_is_a_usage_error(workspace: dict[str, Path]) -> None:
    result = run_cli([], workspace)

    assert result.error_code == ErrorCode.CONFIG_INVALID
    assert result.exit_code == int(ExitCode.USAGE)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def test_doctor_returns_ok_with_inventory_and_budget_sections(workspace: dict[str, Path]) -> None:
    # Arrange / act.
    result = run_cli(["doctor"], workspace)

    # Assert: PRD 6 WP00 / 9.2 -- the inventory is real, and the budget section
    # reports the loaded limits instead of hiding them.
    assert result.exit_code == int(ExitCode.OK)
    assert result.status == "ok"
    data = result.data
    assert "inventory" in data or "host_inventory" in data
    assert "budget" in data
    assert data["budget"]["limits"]["per_hypothesis_max_experiments"] == 3
    assert data["budget"]["limits"]["max_actions_minutes_per_week"] == 0
    assert "qemu" in data
    assert "state_machine" in data
    assert data["state_machine"]["initial_state"] == "Ready"


def test_doctor_reports_a_non_tcg_machine_config_as_a_finding(workspace: dict[str, Path]) -> None:
    # PRD 2.2-1 / 8.3: the accelerator guard is surfaced, not silently applied.
    workspace["machine"].write_text(
        json.dumps({"name": "ipad-whpx", "accel": "whpx"}), encoding="utf-8"
    )

    result = run_cli(["doctor"], workspace)

    codes = [finding["code"] for finding in result.data["findings"]]
    assert ErrorCode.ACCELERATOR_NOT_TCG in codes
    assert result.data["machine"]["accel"] == "whpx"
    assert result.data["ready_for_run"] is False


def test_doctor_succeeds_even_when_no_qemu_is_on_path(workspace: dict[str, Path]) -> None:
    # A missing binary is a finding, never a crash (PRD 6 M0).
    result = run_cli(["doctor"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["qemu"]["available"] is False
    codes = [finding["code"] for finding in result.data["findings"]]
    assert "qemu_not_found" in codes


# ---------------------------------------------------------------------------
# unknown command / bad option
# ---------------------------------------------------------------------------
def test_unknown_command_gives_unsupported_command_and_exit_2(workspace: dict[str, Path]) -> None:
    result = run_cli(["teleport"], workspace)

    assert result.error_code == ErrorCode.UNSUPPORTED_COMMAND
    assert result.exit_code == int(ExitCode.USAGE)
    assert result.status == "failed"
    # The envelope still names the PRD 9.1 command set so the caller can fix it.
    assert result.document["error"]["details"]["command"] == "teleport"
    assert "doctor" in result.document["error"]["details"]["known_commands"]


def test_bad_option_gives_a_usage_error_with_one_json_object(workspace: dict[str, Path]) -> None:
    # Act.
    result = run_cli(["doctor", "--not-an-option"], workspace)

    # Assert: a usage error, exit 2, and stdout is still exactly one object.
    assert result.error_code == ErrorCode.CONFIG_INVALID
    assert result.exit_code == int(ExitCode.USAGE)
    assert "usage" in result.document["error"]["details"]
    assert result.command == "doctor"


def test_help_is_carried_inside_the_envelope(workspace: dict[str, Path]) -> None:
    # argparse would write help to stdout directly; the CLI keeps it in JSON.
    result = run_cli(["--help"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.status == "ok"
    assert "help" in result.data


# ---------------------------------------------------------------------------
# install (PRD 9.1 / FR06)
# ---------------------------------------------------------------------------
def test_install_with_a_fixture_ipa_and_no_guest_channel_is_blocked(
    workspace: dict[str, Path],
) -> None:
    # Arrange: a real, acceptable Device IPA (tests.fixtures.ipa_builder).
    ipa = build_ipa(workspace["root"] / "HelloUniPad.ipa")

    # Act.
    result = run_cli(["install", str(ipa)], workspace)

    # Assert: PRD 9.1 / appendix A -- no guest channel means BLOCKED with a
    # concrete reason, never a faked success.
    assert result.error_code == ErrorCode.BLOCKED
    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.status == "blocked"
    details = result.document["error"]["details"]
    # ``unavailable``, not ``missing``.  This host *ships* the channel --
    # ``unipad.guest.GuestClient`` is modelled and exported -- so the truthful
    # reason is that no guest is reachable, not that the controller lacks the
    # concept.  The distinction is load-bearing: ``missing`` used to be
    # structurally always true, which made it a reason that carried no
    # information about the host whatsoever.  The other reason is pinned
    # reachable by the test below.
    assert details["blocked_reason"] == "guest_channel_unavailable"
    assert details["package_sha256"]


def test_a_guest_channel_the_controller_does_not_model_is_missing(
    workspace: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``guest_channel_missing`` stays reachable, and only for that one case.

    The two reasons must not collapse into a single one.  ``missing`` means the
    controller models no channel at all; ``unavailable`` means it models one and
    none is reachable right now.  A host that ships the channel can only ever be
    the second, so the first has to be forced by taking the channel out of the
    probe list -- which is the only situation that should ever produce it.
    """
    ipa = build_ipa(workspace["root"] / "HelloUniPad.ipa")
    monkeypatch.setattr(
        cli,
        "_CAPABILITIES",
        {
            **cli._CAPABILITIES,
            "guest_channel": (("unipad.guest", "NotAnExportedChannel"),),
        },
    )

    result = run_cli(["install", str(ipa)], workspace)

    assert result.error_code == ErrorCode.BLOCKED
    assert result.document["error"]["details"]["blocked_reason"] == "guest_channel_missing"
    # The reason must not be the only thing that changed: a refused install
    # still records nothing.
    history = run_cli(["history", "--all"], workspace)
    assert history.data["entry_count"] == 0


def test_install_never_reports_a_successful_stage_without_a_guest(
    workspace: dict[str, Path],
) -> None:
    # PRD 9.1: "文件传输成功不能返回 installed".
    ipa = build_ipa(workspace["root"] / "HelloUniPad.ipa")

    result = run_cli(["install", str(ipa)], workspace)

    data = result.data
    assert data["stage"] == "checked"
    assert data["stage"] not in ("installed", "registered")
    assert data["trust_mode"] is None
    assert data["error_code"] == ErrorCode.BLOCKED


def test_install_reports_the_prd_9_1_contract_fields(workspace: dict[str, Path]) -> None:
    # PRD 9.1: package hash, bundle id, stage, trust mode, error, log location.
    ipa = build_ipa(workspace["root"] / "HelloUniPad.ipa")

    result = run_cli(["install", str(ipa)], workspace)

    data = result.data
    assert len(data["package_sha256"]) == 64
    assert data["bundle_id"] == DEFAULT_BUNDLE_ID
    assert data["stage"]
    assert data["error_code"] == ErrorCode.BLOCKED
    assert data["error_message"]
    # The named log file must actually exist (evidence, not a promise).  The path
    # is resolved through the runs root because the envelope's ``log_path`` is
    # redacted (PRD 9.2); ``run_dir_of`` still asserts the envelope names
    # install.log, which is the half redaction must not break.
    log_path = run_dir_of(workspace, result, named="install.log") / "install.log"
    assert log_path.is_file()
    assert "install requested" in log_path.read_text(encoding="utf-8")


def test_install_writes_the_journal_and_the_install_log(workspace: dict[str, Path]) -> None:
    # PRD 7.3 / 9.2: the blocked install still leaves its evidence behind, and
    # no fabricated install result is written for a stage never reached.
    ipa = build_ipa(workspace["root"] / "HelloUniPad.ipa")

    result = run_cli(["install", str(ipa)], workspace)

    # Resolved through the runs root, not the redacted ``log_path`` string (PRD
    # 9.2): an absolute run path embeds the Windows account name, so the envelope
    # carries ``<redacted:user_path>/...`` and cannot be parsed back into a path.
    # ``run_dir_of`` still proves the envelope names install.log.
    run_dir = run_dir_of(workspace, result, named="install.log")
    assert (run_dir / "experiment.json").is_file()
    assert (run_dir / "install.log").is_file()
    assert not (run_dir / "install.json").exists()


def test_install_of_a_rejected_package_reports_the_fault_code(workspace: dict[str, Path]) -> None:
    # PRD 10: a simulator-only package is rejected at precheck, not installed.
    ipa = build_ipa(workspace["root"] / "Sim.ipa", corrupt="simulator")

    result = run_cli(["install", str(ipa)], workspace)

    assert result.status == "failed"
    assert result.data["stage"] == "failed"
    assert result.data["error_code"] in (
        ErrorCode.UNSUPPORTED_ARCH,
        ErrorCode.UNSUPPORTED_PLATFORM,
        ErrorCode.INVALID_PACKAGE,
    )


# ---------------------------------------------------------------------------
# collect (PRD 9.2 / FR09)
# ---------------------------------------------------------------------------
def test_collect_with_incomplete_evidence_gives_evidence_incomplete(
    workspace: dict[str, Path],
) -> None:
    # Arrange: a journal with no runtime evidence at all.
    run_id = _create_run(workspace)

    # Act.
    result = run_cli(["collect", "--run", run_id], workspace)

    # Assert: PRD 9.2 -- the run stays unverified and the missing items are named.
    assert result.error_code == ErrorCode.EVIDENCE_INCOMPLETE
    assert result.exit_code == _exit_code_for(result.error_code)
    assert result.status == "failed"
    missing = result.data["missing"]
    assert missing
    assert "host_inventory" in missing
    assert "artifact_manifest" in missing
    assert "qemu_args" in missing
    assert "serial_log" in missing
    assert "verification_json" in missing
    assert result.document["error"]["details"]["missing"] == missing


def test_collect_of_a_complete_run_reports_ok(workspace: dict[str, Path]) -> None:
    # Arrange: every required evidence item materialised on disk.
    run_id = _create_run(workspace)
    run_dir = workspace["runs"] / run_id
    (run_dir / "host-inventory.json").write_text("{}", encoding="utf-8")
    (run_dir / "artifact-manifest.json").write_text("{}", encoding="utf-8")
    (run_dir / "qemu-args.txt").write_text("-accel tcg\n", encoding="utf-8")
    (run_dir / "serial.log").write_text("boot\n", encoding="utf-8")
    (run_dir / "guest.log").write_text("launchd\n", encoding="utf-8")
    (run_dir / "verification.json").write_text(
        json.dumps({"schema_version": 1, "passed": True}), encoding="utf-8"
    )

    # Act.
    result = run_cli(["collect", "--run", run_id], workspace)

    # Assert: the bundle is complete, so the command succeeds.
    assert result.exit_code == int(ExitCode.OK)
    assert result.data["missing"] == []
    assert "experiment.json" in result.data["collected"]
    assert "verification.json" in result.data["collected"]
    assert (run_dir / "evidence-bundle.json").is_file()


def test_collect_requires_a_run_id(workspace: dict[str, Path]) -> None:
    result = run_cli(["collect"], workspace)

    assert result.error_code == ErrorCode.CONFIG_INVALID
    assert result.exit_code == int(ExitCode.USAGE)


def test_collect_of_a_missing_run_is_blocked(workspace: dict[str, Path]) -> None:
    # A journal that does not exist is a blocked controller state.
    result = run_cli(["collect", "--run", "exp-absent"], workspace)

    assert result.error_code == ErrorCode.BLOCKED
    assert result.exit_code == int(ExitCode.BLOCKED)


# ---------------------------------------------------------------------------
# verify (PRD 7.4)
# ---------------------------------------------------------------------------
def test_verify_with_incomplete_evidence_refuses_to_pass(workspace: dict[str, Path]) -> None:
    # Arrange.
    run_id = _create_run(workspace)

    # Act.
    result = run_cli(["verify", "--suite", "M4", "--run", run_id], workspace)

    # Assert: the gate fails closed and verification.json records why.
    assert result.error_code == ErrorCode.EVIDENCE_INCOMPLETE
    assert result.data["gate"]["passed"] is False
    verification = workspace["runs"] / run_id / "verification.json"
    assert verification.is_file()
    payload = json.loads(verification.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["verifier_id"] == "unipad.controller.verifier"


def test_verify_without_any_run_is_blocked(workspace: dict[str, Path]) -> None:
    result = run_cli(["verify", "--suite", "M4"], workspace)

    assert result.error_code == ErrorCode.BLOCKED
    assert result.exit_code == int(ExitCode.BLOCKED)


# ---------------------------------------------------------------------------
# budget (PRD 7.5)
# ---------------------------------------------------------------------------
def test_build_refuses_when_the_actions_budget_is_zero(workspace: dict[str, Path]) -> None:
    # PRD 8.4: the paid path is not authorised yet, so a paid build is refused.
    result = run_cli(
        ["build", "--stage", "M4", "--actions-minutes", "10"], workspace
    )

    assert result.error_code == ErrorCode.BUDGET_EXCEEDED
    assert result.exit_code == int(ExitCode.BLOCKED)


def test_build_is_blocked_while_the_sources_are_not_frozen(workspace: dict[str, Path]) -> None:
    # PRD 6/8: never build from a moving branch.
    result = run_cli(["build", "--stage", "M4"], workspace)

    assert result.error_code == ErrorCode.BLOCKED
    assert result.exit_code == int(ExitCode.BLOCKED)
    assert "sources.lock.json" in result.document["error"]["message"]


def test_start_is_blocked_when_no_qemu_is_on_path(workspace: dict[str, Path]) -> None:
    # PRD FR08: a missing binary is blocked, never a faked launch.
    manifest = workspace["root"] / "artifact-manifest.json"
    manifest.write_text(json.dumps({"target": "tcg"}), encoding="utf-8")

    result = run_cli(["start", "--profile", "tcg", "--artifact", str(manifest)], workspace)

    assert result.error_code == ErrorCode.BLOCKED
    assert result.exit_code == int(ExitCode.BLOCKED)


def test_start_refuses_a_non_tcg_profile(workspace: dict[str, Path]) -> None:
    # PRD 8.3: an HVF payload must never run on this Intel host.
    manifest = workspace["root"] / "artifact-manifest.json"
    manifest.write_text(json.dumps({"target": "tcg"}), encoding="utf-8")

    result = run_cli(["start", "--profile", "hvf", "--artifact", str(manifest)], workspace)

    assert result.error_code == ErrorCode.ACCELERATOR_NOT_TCG
    assert result.exit_code == int(ExitCode.FAILURE)


def test_start_refuses_a_missing_manifest(workspace: dict[str, Path]) -> None:
    result = run_cli(
        ["start", "--profile", "tcg", "--artifact", str(workspace["root"] / "absent.json")],
        workspace,
    )

    assert result.error_code == ErrorCode.ARTIFACT_MISSING
    assert result.exit_code == int(ExitCode.FAILURE)


def test_fetch_is_blocked_without_a_fetch_capability(workspace: dict[str, Path]) -> None:
    result = run_cli(["fetch", "--run-id", "123456"], workspace)

    assert result.error_code == ErrorCode.BLOCKED
    assert result.exit_code == int(ExitCode.BLOCKED)


def test_restore_of_an_absent_checkpoint_is_blocked(workspace: dict[str, Path]) -> None:
    result = run_cli(["restore", "--checkpoint", "cp-absent"], workspace)

    assert result.error_code == ErrorCode.BLOCKED
    assert result.exit_code == int(ExitCode.BLOCKED)


# ---------------------------------------------------------------------------
# determinism / no leakage
# ---------------------------------------------------------------------------
def test_two_invocations_produce_identical_envelopes(workspace: dict[str, Path]) -> None:
    # The volatile parts are host state (free disk bytes, caller-stamped
    # timestamps); the contract surface must be byte-stable.
    first = run_cli(["doctor"], workspace)
    second = run_cli(["doctor"], workspace)

    def contract(document: dict[str, Any]) -> dict[str, Any]:
        data = document["data"]
        return {
            "command": document["command"],
            "status": document["status"],
            "exit_code": document["exit_code"],
            "ok": document["ok"],
            "unipad_version": document["unipad_version"],
            "state_machine": data["state_machine"],
            "limits": data["budget"]["limits"],
            "findings": [finding["code"] for finding in data["findings"]],
            "ready_for_run": data["ready_for_run"],
            "qemu_available": data["qemu"]["available"],
        }

    assert contract(first.document) == contract(second.document)


def test_main_accepts_argv_none_and_writes_to_stdout(
    monkeypatch: pytest.MonkeyPatch, workspace: dict[str, Path]
) -> None:
    # PRD 9.1 signature: main(argv=None, stream=None) -> int.
    monkeypatch.setattr(
        "sys.argv",
        [
            "unipad",
            "--runs-root",
            str(workspace["runs"]),
            "--budget-config",
            str(workspace["budget"]),
            "stop",
        ],
    )
    buffer = io.StringIO()
    monkeypatch.setattr("sys.stdout", buffer)

    exit_code = cli.main(None, buffer)

    assert exit_code == int(ExitCode.BLOCKED)
    assert _decode_one_object(buffer.getvalue())["command"] == "stop"


def test_command_table_covers_every_prd_9_1_command() -> None:
    # PRD 9.1 command list, verbatim.
    assert set(cli.COMMANDS) == {
        "doctor",
        "build",
        "fetch",
        "start",
        "install",
        "verify",
        "stop",
        "collect",
        "restore",
    }
    assert set(cli._COMMAND_TABLE) == set(cli.COMMANDS)


# ---------------------------------------------------------------------------
# the launch record describes the command line, not a constant (PRD 2.2-1 / 9.2)
# ---------------------------------------------------------------------------
def test_start_records_the_accelerator_the_command_line_states(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``accel`` is transcribed out of the argv, never spelled in the controller.

    ``cmd_start`` wrote ``"accel": "tcg"`` into both ``launch.json`` and the
    envelope.  The value was right, which is what made it a defect: it was a
    second, independent description of the run, free to disagree with the
    command line the launcher actually started -- the exact disagreement
    ``QemuLauncher._launch_evidence`` was rewritten to remove, and the reason
    that module now transcribes every machine fact out of the argv.

    The accelerator guard accepts ``-accel TCG`` (the comparison is
    case-insensitive, and ``_argv_accel`` returns the argv's own spelling), so
    this drives an argv that states it that way.  A controller that transcribes
    reports ``TCG``; a controller that spells a literal reports ``tcg``.  The
    difference is observable, so the mutation is provable rather than assumed.

    Everything below the argv is the real launcher: the real ``launch``, the
    real ``_argv_accel`` guard, the real ``_launch_evidence`` and the real
    ``wait_for_qmp``.  Only ``build_args`` is replaced, because the point is
    what the controller does with an argv it did not build itself.
    """
    firmware = workspace["root"] / "firmware.bin"
    firmware.write_bytes(b"firmware")
    system = workspace["root"] / "system.img"
    system.write_bytes(b"system")
    manifest = workspace["root"] / "artifact-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "target": "tcg",
                "firmware": firmware.as_posix(),
                "system_image": system.as_posix(),
            }
        ),
        encoding="utf-8",
    )
    # The child exits on its own, so the test cannot leak a process either way.
    monkeypatch.setenv("UNIPAD_FAKE_FAIL_MODE", "exit-early")
    monkeypatch.setattr(cli.shutil, "which", lambda name, *a, **k: "qemu-system-aarch64")

    class _FakeLaunchQemuLauncher(QemuLauncher):
        """The real launcher, pointed at the fake QEMU script.

        Only the *command* is redirected: ``build_args``, ``launch``,
        ``_argv_accel``, ``_launch_evidence`` and ``wait_for_qmp`` are all the
        real implementations, so what is under test is still the controller's
        handling of a real launch result.
        """

        def launch(self, qemu_binary, args=None, **kwargs):
            return super().launch(
                [sys.executable, FAKE_SCRIPT.as_posix(), *list(args or [])], **kwargs
            )

    monkeypatch.setattr(
        cli,
        "_launcher",
        lambda args: _FakeLaunchQemuLauncher(cli._machine_config(args), qmp_host="127.0.0.1"),
    )
    # The real ``build_args`` always states ``-accel tcg``; this states the same
    # accelerator in the spelling the guard accepts, so a controller that
    # transcribes the argv says ``TCG`` and one that spells a constant says
    # ``tcg``.
    monkeypatch.setattr(
        QemuLauncher,
        "build_args",
        lambda self, *a, **k: ["-accel", "TCG", "-machine", "virt"],
    )

    result = run_cli(["start", "--profile", "tcg", "--artifact", str(manifest)], workspace)

    # The launch was attempted (the guest then failed readiness, which is fine:
    # the evidence under test is written before the readiness verdict).
    assert result.data["qemu_args"] == ["-accel", "TCG", "-machine", "virt"]
    assert result.data["accel"] == "TCG", (
        "the envelope must report the accelerator the command line states"
    )
    record = json.loads((workspace["runs"] / "launch.json").read_text(encoding="utf-8"))
    assert record["accel"] == "TCG"
    assert record["evidence"]["accel"] == "TCG"
    # ...and the record still describes the same command line that ran (the
    # launcher's own argv, which carries the interpreter prefix the test added).
    assert record["args"] == [FAKE_SCRIPT.as_posix(), *result.data["qemu_args"]]


# ---------------------------------------------------------------------------
# PRD 9.2 -- the runtime evidence set never names the host account
# ---------------------------------------------------------------------------
def test_doctors_python_block_names_the_interpreter_without_the_profile(
    workspace: dict[str, Path],
) -> None:
    """PRD 9.2: ``data.python.executable`` is a final component, not a path.

    ``sys.executable`` is absolute, and on a Windows host the profile directory
    is ``C:\\Users\\<account>\\...`` -- so the literal put the Windows username
    into ``doctor``'s envelope, and ``doctor`` is part of the PRD 9.2 minimum
    runtime evidence set, which means the account name reached every evidence
    bundle the project produced.  The interpreter is still *named*: two
    interpreters that differ differ in their final component, which is what
    :func:`unipad.host.paths.safe_location` keeps and what the module's own
    toolchain block already did.
    """
    result = run_cli(["doctor"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    python = result.data["python"]
    executable = python["executable"]
    # A final component: no drive, no directory, no separator of either flavour.
    assert executable
    assert executable == Path(executable).name
    assert "\\" not in executable and "/" not in executable
    assert ":" not in executable
    # ...and it is the interpreter that is actually running, not a placeholder.
    assert executable == Path(sys.executable).name
    # What was withheld is recorded, so the redaction is not a silent loss.
    note = python["executable_note"]
    assert isinstance(note, str) and note.strip()
    assert "9.2" in note
    # The rest of the block is unaffected: the version facts are the point.
    assert python["version"] == platform.python_version()
    assert python["implementation"] == platform.python_implementation()


# ---------------------------------------------------------------------------
# reverse-m8, CLI half -- the matrix's skipped lines reach an operator
# ---------------------------------------------------------------------------
def _write_matrix_line(path: Path, line: str) -> None:
    """Append one raw line to the matrix file, exactly as a hand edit would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


def _valid_matrix_line(sha256: str = "a" * 64) -> str:
    """One line the loader accepts, produced by the module that owns the gate."""
    entry = MatrixEntry(
        key=MatrixKey("1.0.0", "21H461", "tcg-ramfb"),
        authorisation=Authorisation.OWN_SOURCE,
        trust_category=TrustCategory.DEVELOPMENT_SIGNED,
        bundle_id="com.unipad.probe",
        package_sha256=sha256,
        verdict=Verdict.UNTESTED,
    )
    return json.dumps(entry.to_dict())


def test_doctor_reports_an_absent_matrix_without_inventing_one(
    workspace: dict[str, Path],
) -> None:
    """No matrix yet is a true answer, and the block says so rather than guessing.

    ``doctor`` must not grow a finding for a store nothing has written: the
    honest report is ``present: False`` with zero counters, which is
    distinguishable from a matrix that was read and lost nothing.
    """
    result = run_cli(["doctor"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    block = result.data["matrix"]
    assert block["present"] is False
    assert block["entry_count"] == 0
    assert block["unreadable_lines"] == 0
    assert block["corrupt_lines"] == 0
    assert block["refused_lines"] == 0
    # The location is relative to the runs root: an absolute path would embed
    # the Windows username, and this block is in the PRD 9.2 evidence set.
    assert block["location"] == "lifecycle/compatibility-matrix.jsonl"
    assert block["overridden"] is False
    assert "matrix_unreadable_lines" not in [f["code"] for f in result.data["findings"]]


def test_doctor_reports_a_matrix_that_lost_no_lines(workspace: dict[str, Path]) -> None:
    """A readable matrix is counted, and reports that nothing was skipped."""
    matrix_path = workspace["runs"] / "lifecycle" / "compatibility-matrix.jsonl"
    _write_matrix_line(matrix_path, _valid_matrix_line())
    _write_matrix_line(matrix_path, _valid_matrix_line(sha256="b" * 64))

    result = run_cli(["doctor"], workspace)

    block = result.data["matrix"]
    assert block["present"] is True
    assert block["entry_count"] == 1, "one key, so one current entry"
    assert block["observation_count"] == 2, "both samples are observations"
    assert block["unreadable_lines"] == 0
    assert "matrix_unreadable_lines" not in [f["code"] for f in result.data["findings"]]


def test_doctor_surfaces_a_matrix_line_the_loader_refused(
    workspace: dict[str, Path],
) -> None:
    """reverse-m8's CLI half: a skipped line must reach the operator.

    A hand-edited or schema-drifted line used to vanish silently -- no command
    reported ``unreadable_lines`` for the matrix at all, unlike the install
    ledger whose ``history`` surfaces it.  So a line the loader turned away was
    a compatibility claim the operator believed was recorded and was not.
    """
    matrix_path = workspace["runs"] / "lifecycle" / "compatibility-matrix.jsonl"
    _write_matrix_line(matrix_path, _valid_matrix_line())
    # A line that parses as JSON but is not an entry's shape: damage.
    _write_matrix_line(matrix_path, json.dumps({"package_version": "1.0.0"}))
    # A line the authorisation gate refuses: a security signal, not damage.
    _write_matrix_line(
        matrix_path,
        json.dumps(
            {
                "schema_version": 1,
                "key": {"package_version": "1.0.0", "system_build": "21H461", "run_config": "tcg-ramfb"},
                "authorisation": "not-authorised",
                "trust_category": "development-signed",
                "bundle_id": "com.unipad.probe",
                "package_sha256": "c" * 64,
                "verdict": "untested",
            }
        ),
    )

    result = run_cli(["doctor"], workspace)

    block = result.data["matrix"]
    assert block["unreadable_lines"] == 2
    # Split by cause: the two call for different actions, and summing them
    # would hide a refused line inside a disk error.
    assert block["corrupt_lines"] == 1
    assert block["refused_lines"] == 1
    # ...and it is a finding, so it is visible without reading the block.
    finding = next(f for f in result.data["findings"] if f["code"] == "matrix_unreadable_lines")
    assert "2" in finding["detail"]
    assert finding["corrupt_lines"] == 1
    assert finding["refused_lines"] == 1
    # The command still answers: a damaged matrix is a report, not a crash.
    assert result.exit_code == int(ExitCode.OK)
    assert result.data["matrix"]["entry_count"] == 1


def test_doctor_reads_the_matrix_the_operator_named(workspace: dict[str, Path]) -> None:
    """``--matrix`` overrides the default, the way ``--ledger`` does."""
    matrix_path = workspace["root"] / "elsewhere" / "matrix.jsonl"
    _write_matrix_line(matrix_path, _valid_matrix_line())
    _write_matrix_line(matrix_path, '{"not": "an entry"}')

    result = run_cli(["doctor", "--matrix", str(matrix_path)], workspace)

    block = result.data["matrix"]
    # Outside the runs root, so only the final component is named -- the same
    # redaction :func:`unipad.host.paths.safe_location` applies everywhere else.
    assert block["location"] == matrix_path.name
    assert block["overridden"] is True
    assert block["entry_count"] == 1
    assert block["unreadable_lines"] == 1
    # The default location is untouched, so the override really was one.
    assert not (workspace["runs"] / "lifecycle" / "compatibility-matrix.jsonl").exists()


def test_doctor_names_why_the_matrix_could_not_be_opened(workspace: dict[str, Path]) -> None:
    """``blocked`` is a category; the block must carry the reason beside it.

    A path that is not a file and a module that will not import are both
    ``blocked``, and the ``matrix`` subcommand already reports the first as
    ``blocked_reason="matrix_not_a_file"``.  Answering only ``"blocked"`` here
    would leave an operator re-running the command under another name to find
    out which of the two they have.
    """
    bogus = workspace["runs"] / "lifecycle" / "compatibility-matrix.jsonl"
    bogus.mkdir(parents=True, exist_ok=True)

    result = run_cli(["doctor"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    block = result.data["matrix"]
    assert block["present"] is False
    assert block["error_code"] == ErrorCode.BLOCKED
    assert block["blocked_reason"] == "matrix_not_a_file"
    # The counters stay unknown, not zero -- the note says so.
    assert block["entry_count"] == 0
    assert "unknown rather than zero" in block["note"]
    # ...and the whole command still answers, because that is doctor's job.
    assert result.status == "ok"


def _alive(pid: int) -> bool:
    """Whether ``pid`` names a live process (no signal is sent)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def test_a_crash_in_the_readiness_wait_does_not_orphan_qemu(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """callchain finding 10: the child may only outlive a command that said so.

    ``QemuLauncher`` has no ``atexit`` hook, no ``__del__`` and no signal
    handler, and the child is started in its own process group with no Job
    Object -- so nothing the OS does reaps it if this controller dies first.
    ``main`` also swallows ``KeyboardInterrupt`` and answers with an envelope,
    which is how a Ctrl-C during the readiness wait used to exit *cleanly* with
    QEMU still running and no ``launch.json`` naming it: ``unipad stop`` then
    reported ``no_launch_record`` about a guest that was demonstrably started,
    and the process was unreachable by any later command.

    So the launch record is written while the child is alive, and anything that
    goes wrong before the outcome stops the child first.  The fake QEMU is
    started for real and left running, so the assertion that it is gone is about
    the process, not about a recorded flag.
    """
    firmware = workspace["root"] / "firmware.bin"
    firmware.write_bytes(b"firmware")
    system = workspace["root"] / "system.img"
    system.write_bytes(b"system")
    manifest = workspace["root"] / "artifact-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "target": "tcg",
                "firmware": firmware.as_posix(),
                "system_image": system.as_posix(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name, *a, **k: "qemu-system-aarch64")

    seen: dict[str, Any] = {}

    class _InterruptedQemuLauncher(QemuLauncher):
        """The real launcher, whose readiness wait is interrupted mid-flight."""

        def launch(self, qemu_binary, args=None, **kwargs):
            result = super().launch(
                [sys.executable, FAKE_SCRIPT.as_posix(), *list(args or [])], **kwargs
            )
            seen["pid"] = result.pid
            return result

        def wait_for_qmp(self, timeout=None):
            # The child is running when this happens: that is the whole point.
            assert _alive(int(seen["pid"])), "the child must be alive before the crash"
            raise KeyboardInterrupt

        def stop(self, graceful=True, timeout=None):
            result = super().stop(graceful=graceful, timeout=timeout)
            seen["stop"] = result
            return result

        def close(self):
            seen["closed"] = True
            return super().close()

    def _build(args):
        launcher = _InterruptedQemuLauncher(cli._machine_config(args), qmp_host="127.0.0.1")
        seen["launcher"] = launcher
        return launcher

    monkeypatch.setattr(cli, "_launcher", _build)
    monkeypatch.setattr(
        QemuLauncher,
        "build_args",
        lambda self, *a, **k: ["-accel", "tcg", "-machine", "virt"],
    )

    result = run_cli(["start", "--profile", "tcg", "--artifact", str(manifest)], workspace)

    # ``main`` still answers as the interface: one envelope, a fixed code.
    assert result.status == "failed"
    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == cli.INTERNAL_ERROR_CODE

    # The child is stopped, not orphaned.  ``is_running()`` is the launcher's
    # own oracle and it reaps through ``poll()``; ``os.kill(pid, 0)`` is not
    # usable here, because on Windows a terminated child whose handle has not
    # been waited on still answers it.
    launcher = seen["launcher"]
    assert seen.get("closed") is True
    assert launcher.is_running() is False, "a crash in the readiness wait must not orphan qemu"
    stop = seen["stop"]
    assert stop.ok is True
    # A real termination, not the no-op the launcher returns for a process it
    # never started -- which is what an unguarded crash path would leave.
    assert stop.method in ("qmp-quit", "terminate", "kill"), stop.method

    # ...and the launch record names it, so a later ``stop`` answers about a
    # guest that really was started instead of denying one exists.
    record = json.loads((workspace["runs"] / "launch.json").read_text(encoding="utf-8"))
    assert record["pid"] == int(seen["pid"])
    assert record["args"][-4:] == ["-accel", "tcg", "-machine", "virt"]
    # Readiness was never established -- which is not the same as "not ready".
    assert record["readiness"] is None


def test_a_failed_readiness_stops_the_child_it_started(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """callchain finding 1: an ordinary failure return leaks the child.

    The crash path above stops the launcher because it is inside
    ``except BaseException``.  The *ordinary* failure return --
    ``if not launched.ok or not readiness.ready`` -- sits below that block and
    returns straight out of ``cmd_start``, so a start whose guest never became
    ready left a live QEMU behind with nothing in the command able to reach it:
    ``QemuLauncher`` has no ``atexit`` hook, no ``__del__`` and no Job Object,
    and the child is started in its own process group.

    The guard the function states for itself is "the child is allowed to
    outlive this command *only* if the command says it did" -- and a
    ``failed`` envelope says it did not.  So the same cleanup the crash path
    performs must run before that return too.

    The fake QEMU is started for real and left running, so the assertion that
    it is gone is about the process, not about a recorded flag.
    """
    firmware = workspace["root"] / "firmware.bin"
    firmware.write_bytes(b"firmware")
    system = workspace["root"] / "system.img"
    system.write_bytes(b"system")
    manifest = workspace["root"] / "artifact-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "target": "tcg",
                "firmware": firmware.as_posix(),
                "system_image": system.as_posix(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name, *a, **k: "qemu-system-aarch64")

    seen: dict[str, Any] = {}

    class _NotReadyQemuLauncher(QemuLauncher):
        """The real launcher, whose guest never answers QMP."""

        def launch(self, qemu_binary, args=None, **kwargs):
            result = super().launch(
                [sys.executable, FAKE_SCRIPT.as_posix(), *list(args or [])], **kwargs
            )
            seen["pid"] = result.pid
            return result

        def wait_for_qmp(self, timeout=None):
            # The child is running when this happens: that is the whole point.
            assert _alive(int(seen["pid"])), "the child must be alive before the verdict"
            return QmpReadiness(
                ready=False,
                error_code=ErrorCode.QMP_UNAVAILABLE,
                error_message="the guest never answered QMP",
            )

        def stop(self, graceful=True, timeout=None):
            result = super().stop(graceful=graceful, timeout=timeout)
            seen["stop"] = result
            return result

        def close(self):
            seen["closed"] = True
            return super().close()

    def _build(args):
        launcher = _NotReadyQemuLauncher(cli._machine_config(args), qmp_host="127.0.0.1")
        seen["launcher"] = launcher
        return launcher

    monkeypatch.setattr(cli, "_launcher", _build)
    monkeypatch.setattr(
        QemuLauncher,
        "build_args",
        lambda self, *a, **k: ["-accel", "tcg", "-machine", "virt"],
    )

    result = run_cli(["start", "--profile", "tcg", "--artifact", str(manifest)], workspace)

    # The command reports the failure it observed...
    assert result.status == "failed"
    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.QMP_UNAVAILABLE

    # ...and it does not leave the guest it started running.
    launcher = seen["launcher"]
    assert seen.get("closed") is True
    assert launcher.is_running() is False, "a failed readiness must not orphan qemu"
    stop = seen["stop"]
    assert stop.ok is True
    assert stop.method in ("qmp-quit", "terminate", "kill"), stop.method

    # The record carries the readiness that was established -- "established as
    # not ready" is a different fact from "not established yet", and this path
    # observed the first one.
    record = json.loads((workspace["runs"] / "launch.json").read_text(encoding="utf-8"))
    assert record["pid"] == int(seen["pid"])
    assert record["readiness"]["ready"] is False
