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

Everything runs against ``tmp_path`` run roots and a ``tmp_path`` budget config;
no wall clock, no network, no child process is launched by these tests.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from unipad import __version__
from unipad.controller import cli
from unipad.controller.experiments import ExperimentStore
from unipad.errors import ErrorCode, ExitCode, UniPadError
from tests.fixtures.ipa_builder import DEFAULT_BUNDLE_ID, build_ipa

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
    assert details["blocked_reason"] == "guest_channel_missing"
    assert details["package_sha256"]


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
    # The named log file must actually exist (evidence, not a promise).
    log_path = Path(data["log_path"])
    assert log_path.is_file()
    assert "install requested" in log_path.read_text(encoding="utf-8")


def test_install_writes_the_journal_and_the_install_log(workspace: dict[str, Path]) -> None:
    # PRD 7.3 / 9.2: the blocked install still leaves its evidence behind, and
    # no fabricated install result is written for a stage never reached.
    ipa = build_ipa(workspace["root"] / "HelloUniPad.ipa")

    result = run_cli(["install", str(ipa)], workspace)

    run_dir = Path(result.data["log_path"]).parent
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
