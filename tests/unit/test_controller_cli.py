"""Controller CLI conformance tests (PRD section 9.1, plus 7.4/7.5/8.3/FR08).

Mapping
-------
* **PRD 9.1** -- "命令返回机器可读状态与稳定错误码": every one of the nine
  documented commands is exercised on *this* host and its exit code asserted
  against :class:`unipad.errors.ExitCode` together with the machine-readable
  error code, so a script can branch on fields rather than prose.
* **PRD 9.1** -- "every command prints exactly one JSON object": the envelope is
  decoded with ``raw_decode`` and the remainder must be whitespace, so a child
  process leaking to stdout (or a traceback) fails the test.
* **PRD 9.1** -- the install interface reports package hash, bundle id, current
  stage, trust mode, error type and log location; "文件传输成功不能返回
  ``installed``".  With no guest channel the command is ``blocked`` -- never a
  faked success.
* **PRD 9.1** -- an unknown command is ``unsupported_command`` with exit 2.
* **PRD FR08 / appendix A** -- a missing capability, material or budget is a
  structured ``blocked`` (exit 3) with a concrete reason, not a countdown.
* **PRD 8.3** -- only the ``tcg`` profile may be started on this Intel host.

The suite runs against ``tmp_path`` run roots; no network, no QEMU, no wall
clock.  ``doctor`` does collect the real host inventory (PRD 6 WP00 re-check),
which is the one command here that legitimately inspects this machine.
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
from unipad.errors import ErrorCode, ExitCode
from tests.fixtures.ipa_builder import DEFAULT_BUNDLE_ID, build_ipa

#: The shipped budget configuration (PRD 7.5 initial recommendations).
BUDGET_CONFIG: dict[str, object] = {
    "schema_version": 1,
    "per_hypothesis_max_experiments": 3,
    "same_failure_signature_max": 3,
    "max_actions_minutes_per_week": 0,
    "max_model_tokens_per_session": None,
    "max_local_run_minutes_per_experiment": 45,
}

#: The nine commands of PRD 9.1.
PRD_9_1_COMMANDS: tuple[str, ...] = (
    "doctor",
    "build",
    "fetch",
    "start",
    "install",
    "verify",
    "stop",
    "collect",
    "restore",
)


class CliResult:
    """One CLI invocation: exit code, parsed envelope and raw stdout."""

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

    @property
    def details(self) -> dict[str, Any]:
        error = self.document.get("error") or {}
        return dict(error.get("details") or {})


def _decode_one_object(text: str) -> dict[str, Any]:
    """Parse ``text`` and assert it holds exactly one JSON document.

    PRD 9.1: exactly one JSON object per command.  ``raw_decode`` proves the
    remainder after the first document is whitespace only.
    """
    assert text.strip(), "the CLI must print a JSON object"
    document, end = json.JSONDecoder().raw_decode(text.lstrip())
    assert text.lstrip()[end:].strip() == "", "stdout must contain exactly one JSON object"
    assert isinstance(document, dict)
    return document


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    """A tmp_path run root plus the controller-side config files."""
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps(BUDGET_CONFIG), encoding="utf-8")
    machine = tmp_path / "machine.json"
    machine.write_text(
        json.dumps({"name": "ipad-tcg", "accel": "tcg", "cores": 4, "memory_mb": 3072}),
        encoding="utf-8",
    )
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


@pytest.fixture()
def fast_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the PowerShell CIM probe so envelope tests stay fast.

    ``doctor`` legitimately measures this host, but a single CIM probe costs
    about two seconds; the envelope contract does not depend on it.  Two tests
    below deliberately keep the real probe (PRD 6 WP00 re-check); the rest use
    this fixture and still assert the *shape* of the inventory.
    """
    monkeypatch.setattr(
        "unipad.host.inventory._probe_cim",
        lambda timeout: ({}, "cim probe disabled by the test fixture"),
    )


# ---------------------------------------------------------------------------
# PRD 9.1: the nine commands and their exit codes on this host
# ---------------------------------------------------------------------------


def test_the_command_table_is_exactly_the_prd_9_1_command_list() -> None:
    assert set(cli.COMMANDS) == set(PRD_9_1_COMMANDS)
    assert set(cli._COMMAND_TABLE) == set(PRD_9_1_COMMANDS)  # noqa: SLF001 - contract


@pytest.mark.parametrize(
    ("argv", "expected_exit", "expected_code"),
    [
        # doctor always succeeds: a host that is not ready is a *finding*.
        (["doctor"], int(ExitCode.OK), None),
        # PRD 6/8: builds run on a macOS runner and the sources are not frozen.
        (
            ["build", "--stage", "M4", "--experiment", "exp-build", "--hypothesis", "h"],
            int(ExitCode.BLOCKED),
            ErrorCode.BLOCKED,
        ),
        # No fetch capability on this host.
        (["fetch", "--run-id", "123456"], int(ExitCode.BLOCKED), ErrorCode.BLOCKED),
        # Caller input is validated before the host is probed.
        (
            ["start", "--profile", "tcg", "--artifact", "manifest.json"],
            int(ExitCode.FAILURE),
            ErrorCode.ARTIFACT_MISSING,
        ),
        # No guest channel: the install chain cannot even transfer.
        (["install", "@IPA@"], int(ExitCode.BLOCKED), ErrorCode.BLOCKED),
        # Nothing to verify yet.
        (["verify", "--suite", "ipa-mvp"], int(ExitCode.BLOCKED), ErrorCode.BLOCKED),
        # Nothing records a guest to stop.
        (["stop", "--graceful"], int(ExitCode.BLOCKED), ErrorCode.BLOCKED),
        # The run does not exist.
        (["collect", "--run", "exp-missing"], int(ExitCode.BLOCKED), ErrorCode.BLOCKED),
        # An unverified checkpoint is never restored.
        (["restore", "--checkpoint", "cp-unknown"], int(ExitCode.BLOCKED), ErrorCode.BLOCKED),
    ],
)
def test_every_command_returns_its_documented_exit_code_on_this_host(
    workspace: dict[str, Path],
    fast_inventory: None,
    ipa: Path,
    argv: list[str],
    expected_exit: int,
    expected_code: str | None,
) -> None:
    # ``@IPA@`` is replaced with the fixture IPA so the install case reaches the
    # guest-channel probe instead of failing on a missing file.
    resolved = [str(ipa) if token == "@IPA@" else token for token in argv]
    result = run_cli(resolved, workspace)

    assert result.exit_code == expected_exit
    assert result.document["exit_code"] == expected_exit
    if expected_code is None:
        assert result.status == "ok"
        assert result.document["error"] is None
    else:
        assert result.error_code == expected_code
        # A blocked/failed command never reports ok.
        assert result.status in {"blocked", "failed"}
        assert result.document["ok"] is False


def test_the_envelope_carries_the_prd_9_1_fields(
    workspace: dict[str, Path], fast_inventory: None
) -> None:
    result = run_cli(["doctor"], workspace)
    document = result.document

    assert document["schema_version"] == cli.ENVELOPE_SCHEMA_VERSION
    assert document["unipad_version"] == __version__
    assert document["command"] == "doctor"
    assert document["status"] == "ok"
    assert document["ok"] is True
    assert isinstance(document["data"], dict)
    assert document["error"] is None
    assert document["generated_at"]


def test_the_envelope_status_matches_the_exit_code(
    workspace: dict[str, Path], fast_inventory: None
) -> None:
    ok = run_cli(["doctor"], workspace)
    blocked = run_cli(["stop", "--graceful"], workspace)
    failed = run_cli(["start", "--profile", "tcg", "--artifact", "nope.json"], workspace)

    assert (ok.status, ok.exit_code) == ("ok", int(ExitCode.OK))
    assert (blocked.status, blocked.exit_code) == ("blocked", int(ExitCode.BLOCKED))
    assert (failed.status, failed.exit_code) == ("failed", int(ExitCode.FAILURE))


def test_two_invocations_produce_identical_envelopes(
    workspace: dict[str, Path], fast_inventory: None
) -> None:
    # Determinism: same argv, same envelope (``doctor`` is excluded on purpose:
    # it measures this host, so free memory and the inventory stamp legitimately
    # move between two calls).
    first = run_cli(["stop", "--graceful"], workspace)
    second = run_cli(["stop", "--graceful"], workspace)

    for document in (first.document, second.document):
        document.pop("generated_at")
    assert first.document == second.document


def test_doctor_is_structurally_stable_across_invocations(
    workspace: dict[str, Path], fast_inventory: None
) -> None:
    # The parts of ``doctor`` that are not measurements must not move.
    first = run_cli(["doctor"], workspace)
    second = run_cli(["doctor"], workspace)

    for document in (first.document, second.document):
        document.pop("generated_at")
        document["data"]["budget"].pop("checked_at")
        document["data"]["host_inventory"].pop("collected_at_epoch")
        document["data"]["host_inventory"].pop("memory")
        document["data"]["disks"] = [disk["drive"] for disk in document["data"]["disks"]]
    assert first.document == second.document


# ---------------------------------------------------------------------------
# PRD 9.1: unknown commands and bad usage
# ---------------------------------------------------------------------------


def test_an_unknown_command_is_unsupported_command_with_exit_2(
    workspace: dict[str, Path],
) -> None:
    result = run_cli(["frobnicate"], workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.UNSUPPORTED_COMMAND
    assert result.details["command"] == "frobnicate"
    assert set(result.details["known_commands"]) == set(PRD_9_1_COMMANDS)


def test_no_command_is_a_usage_error(workspace: dict[str, Path]) -> None:
    result = run_cli([], workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID


def test_a_missing_required_option_is_a_usage_error(workspace: dict[str, Path]) -> None:
    result = run_cli(["start"], workspace)  # --artifact is required

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.BLOCKED, int(ExitCode.BLOCKED)),
        (ErrorCode.BUDGET_EXCEEDED, int(ExitCode.BLOCKED)),
        (ErrorCode.CONFIG_INVALID, int(ExitCode.USAGE)),
        (ErrorCode.UNSUPPORTED_COMMAND, int(ExitCode.USAGE)),
        (ErrorCode.EXPERIMENT_STATE_INVALID, int(ExitCode.USAGE)),
        (ErrorCode.ARTIFACT_MISSING, int(ExitCode.FAILURE)),
        (ErrorCode.IMAGE_HASH_MISMATCH, int(ExitCode.FAILURE)),
    ],
)
def test_exit_codes_match_the_error_mapping(code: str, expected: int) -> None:
    from unipad.errors import UniPadError

    assert UniPadError(code, "x").exit_code == expected


# ---------------------------------------------------------------------------
# PRD 9.1: doctor
# ---------------------------------------------------------------------------


def test_doctor_json_carries_the_inventory_and_the_budget(workspace: dict[str, Path]) -> None:
    result = run_cli(["doctor"], workspace)
    data = result.data

    # PRD 9.2: host-inventory.json is part of the minimum evidence set.
    inventory = data["host_inventory"]
    assert isinstance(inventory, dict)
    assert inventory["schema_version"] == 1
    assert inventory["cpu"]["cores_physical"] >= 1
    assert isinstance(inventory["disks"], list)
    assert inventory["disks"], "this host has at least one enumerable drive"
    assert inventory["collector"] == "unipad.host.inventory"

    # PRD 7.5: the budget view travels with the host report.
    budget = data["budget"]
    assert budget["limits"]["per_hypothesis_max_experiments"] == 3
    assert budget["limits"]["same_failure_signature_max"] == 3
    assert budget["limits"]["max_actions_minutes_per_week"] == 0.0
    assert budget["usage"]["records"] == 0

    # The state machine is reported as data (PRD 7.2), for audits.
    assert data["state_machine"]["initial_state"] == "Ready"
    assert len(data["state_machine"]["transitions"]) == 12


def test_doctor_succeeds_and_reports_findings_even_without_qemu(
    workspace: dict[str, Path],
) -> None:
    # PRD 6 M0: no QEMU binary is a finding, not a crash.
    result = run_cli(["doctor"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    assert result.data["qemu"]["available"] in (True, False)
    codes = {finding["code"] for finding in result.data["findings"]}
    if not result.data["qemu"]["available"]:
        assert "qemu_not_found" in codes
    # The command never claims readiness it does not have.
    assert result.data["ready_for_run"] == bool(result.data["qemu"]["available"])


def test_doctor_flags_a_non_tcg_machine_config(
    workspace: dict[str, Path], fast_inventory: None
) -> None:
    workspace["machine"].write_text(json.dumps({"accel": "whpx"}), encoding="utf-8")

    result = run_cli(["doctor"], workspace)

    codes = {finding["code"] for finding in result.data["findings"]}
    assert ErrorCode.ACCELERATOR_NOT_TCG in codes
    assert result.data["ready_for_run"] is False


# ---------------------------------------------------------------------------
# PRD 9.1 / appendix A: install without a guest channel
# ---------------------------------------------------------------------------


@pytest.fixture()
def ipa(workspace: dict[str, Path]) -> Path:
    """A real, acceptable Device IPA built by the fixture builder."""
    return build_ipa(workspace["root"] / "HelloUniPad.ipa")


def test_install_without_a_guest_channel_is_blocked_never_a_success(
    workspace: dict[str, Path], ipa: Path
) -> None:
    result = run_cli(["install", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.status == "blocked"
    assert result.error_code == ErrorCode.BLOCKED
    assert result.details["blocked_reason"] == "guest_channel_missing"
    # The probe list is reported so the missing implementation is visible.
    assert any("unipad.guest" in probe for probe in result.details["probed"])


def test_install_never_reports_an_installed_stage_it_did_not_reach(
    workspace: dict[str, Path], ipa: Path
) -> None:
    # PRD 9.1: "文件传输成功不能返回 installed".
    result = run_cli(["install", str(ipa)], workspace)
    data = result.data

    assert data["stage"] == "checked"
    assert data["stage"] not in ("installed", "registered")
    assert data["trust_mode"] is None
    assert data["error_code"] == ErrorCode.BLOCKED
    assert data["error_message"]


def test_install_reports_the_prd_9_1_contract_fields(
    workspace: dict[str, Path], ipa: Path
) -> None:
    result = run_cli(["install", str(ipa)], workspace)
    data = result.data

    assert len(data["package_sha256"]) == 64
    assert data["bundle_id"] == DEFAULT_BUNDLE_ID
    # The named log file must exist: evidence, not a promise (PRD 9.2).
    log_path = Path(data["log_path"])
    assert log_path.is_file()
    text = log_path.read_text(encoding="utf-8")
    assert "install requested" in text
    assert data["package_sha256"] in text


def test_a_blocked_install_still_leaves_its_journal_behind(
    workspace: dict[str, Path], ipa: Path
) -> None:
    result = run_cli(["install", str(ipa)], workspace)
    run_dir = Path(result.data["log_path"]).parent

    assert (run_dir / "experiment.json").is_file()
    assert (run_dir / "install.log").is_file()
    # No install result is fabricated for a stage that was never reached.
    assert not (run_dir / "install.json").exists()
    journal = json.loads((run_dir / "experiment.json").read_text(encoding="utf-8"))
    assert journal["milestone"] == "M10"
    assert journal["status"] == "planned"


def test_install_of_a_missing_file_is_a_failed_command(workspace: dict[str, Path]) -> None:
    result = run_cli(["install", str(workspace["root"] / "absent.ipa")], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.ARTIFACT_MISSING


def test_install_of_a_simulator_package_reports_the_fault_code(
    workspace: dict[str, Path],
) -> None:
    # PRD 10: a non-Device package is rejected at precheck, not installed.
    package = build_ipa(workspace["root"] / "Sim.ipa", corrupt="simulator")

    result = run_cli(["install", str(package)], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.data["stage"] == "failed"
    assert result.error_code in (
        ErrorCode.UNSUPPORTED_ARCH,
        ErrorCode.UNSUPPORTED_PLATFORM,
        ErrorCode.INVALID_PACKAGE,
    )


# ---------------------------------------------------------------------------
# PRD 7.4 / 9.2: verify and collect refuse to pass incomplete evidence
# ---------------------------------------------------------------------------


@pytest.fixture()
def run_id(workspace: dict[str, Path]) -> str:
    """An experiment run with a journal but no runtime evidence."""
    store = ExperimentStore(workspace["runs"])
    store.create("exp-cli-1", "M4", "a fixed TCG CPU model boots UniXNU past DarwinBoot")
    return "exp-cli-1"


def test_verify_with_incomplete_evidence_refuses_to_pass(
    workspace: dict[str, Path], run_id: str
) -> None:
    result = run_cli(["verify", "--suite", "M4", "--run", run_id], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.EVIDENCE_INCOMPLETE
    assert result.data["gate"]["passed"] is False
    assert "evidence_incomplete" in result.data["gate"]["failure_reasons"]
    assert result.data["missing"]
    # The verifier's own verdict is written into the run directory (PRD 9.2).
    verification = workspace["runs"] / run_id / "verification.json"
    assert verification.is_file()
    assert json.loads(verification.read_text(encoding="utf-8"))["passed"] is False


def test_collect_with_incomplete_evidence_reports_the_missing_items(
    workspace: dict[str, Path], run_id: str
) -> None:
    result = run_cli(["collect", "--run", run_id], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.EVIDENCE_INCOMPLETE
    assert result.data["missing"]
    assert result.data["collected"] == ["experiment.json"]
    # The bundle manifest is written even though the run stays unverified.
    assert (workspace["runs"] / run_id / "evidence-bundle.json").is_file()


def test_collect_requires_a_run_id(workspace: dict[str, Path]) -> None:
    result = run_cli(["collect"], workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# PRD 8.3 / FR02: the accelerator guard
# ---------------------------------------------------------------------------


def test_start_refuses_a_non_tcg_profile(workspace: dict[str, Path], ipa: Path) -> None:
    manifest = workspace["root"] / "manifest.json"
    manifest.write_text(json.dumps({"target": "tcg"}), encoding="utf-8")

    result = run_cli(["start", "--profile", "whpx", "--artifact", str(manifest)], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.ACCELERATOR_NOT_TCG
    assert result.details["profile"] == "whpx"


def test_start_refuses_a_non_tcg_machine_config(workspace: dict[str, Path]) -> None:
    workspace["machine"].write_text(json.dumps({"accel": "hvf"}), encoding="utf-8")
    manifest = workspace["root"] / "manifest.json"
    manifest.write_text(json.dumps({"target": "tcg"}), encoding="utf-8")

    result = run_cli(["start", "--profile", "tcg", "--artifact", str(manifest)], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.ACCELERATOR_NOT_TCG


# ---------------------------------------------------------------------------
# PRD 7.5 / 8.4: the budget stops the paid path
# ---------------------------------------------------------------------------


def test_build_is_blocked_while_the_actions_budget_is_zero(
    workspace: dict[str, Path],
) -> None:
    workspace["lock"].write_text(json.dumps({"frozen": True}), encoding="utf-8")

    result = run_cli(
        [
            "build",
            "--stage",
            "M4",
            "--experiment",
            "exp-build",
            "--hypothesis",
            "a fixed TCG CPU model boots UniXNU past DarwinBoot",
            "--actions-minutes",
            "1",
        ],
        workspace,
    )

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.error_code == ErrorCode.BUDGET_EXCEEDED


def test_build_is_blocked_while_the_sources_are_not_frozen(
    workspace: dict[str, Path],
) -> None:
    result = run_cli(
        ["build", "--stage", "M4", "--experiment", "exp-build", "--hypothesis", "h"],
        workspace,
    )

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.error_code == ErrorCode.BLOCKED
    assert result.details["sources_lock"].endswith("sources.lock.json")
    # The lock file exists but is not frozen: the reason is named, not implied.
    assert result.details["present"] is True
    assert "not frozen" in result.document["error"]["message"]
