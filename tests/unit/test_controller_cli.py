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

#: Repository root, so a test can read the config this repository actually ships.
REPO_ROOT = Path(__file__).resolve().parents[2]

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


def run_dir_of(workspace: dict[str, Path], result: "CliResult") -> Path:
    """The run directory this invocation created, found without parsing the envelope.

    ``_emit`` redacts every string in the envelope (PRD 9.2: an absolute path
    embeds the Windows account name), so ``Path(result.data["log_path"])`` is no
    longer a path -- it is ``<redacted:user_path>/AppData/...``.  That is the
    point of the redaction, and it means a test that wants to look at the evidence
    has to find the directory the way an operator would: it is the one directory
    this invocation created under the runs root.

    Raises if there is not exactly one, rather than picking one: a silent pick
    would make a test that inspects the wrong run's evidence pass.
    """
    created = [path for path in sorted(workspace["runs"].iterdir()) if path.is_dir()]
    assert len(created) == 1, (
        "expected exactly one run directory under the runs root, found "
        f"{[p.name for p in created]}: the test cannot tell which run's evidence "
        "it is inspecting"
    )
    expected = created[0]
    # The envelope must still NAME the file it wrote, redacted or not.
    assert Path(result.data["log_path"]).name == "install.log", result.data["log_path"]
    assert "install.log" in result.data["log_path"], result.data["log_path"]
    return expected


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
        # ``host_inventory`` carries a second, differently shaped copy of the
        # same disk measurement -- ``inventory._disk_block`` calls
        # ``shutil.disk_usage`` per drive letter and rounds free space to 0.1
        # GiB, a few milliseconds after ``data["disks"]`` measured it.  That is
        # the same non-determinism ``collected_at_epoch`` and ``memory`` already
        # are, arriving through the inventory's copy of the list: any other
        # test writing ~100 MiB between these two invocations moves ``free_gb``
        # (measured 2026-10-25, ``D:`` 12.5 -> 12.3 on a 200 MiB allocation),
        # and a drive crossing 10 GiB free adds a ``warning`` key to the record
        # outright -- 9.5 GiB free produced one.  Reduce it to drive identity,
        # exactly as ``data["disks"]`` is reduced above.  The record's values
        # are not being surrendered: ``test_m0_baseline.py`` asserts
        # ``device``/``size_gb``/``free_gb`` and their types directly.
        document["data"]["host_inventory"]["disks"] = [
            disk["device"] for disk in document["data"]["host_inventory"]["disks"]
        ]
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
    # PRD 9.1's nine are still a subset of what the CLI accepts, so a caller
    # that learned the documented list keeps working.  PRD 3.2 P1 (3) adds the
    # lifecycle commands and P1 (2) the matrix command on top, and the error
    # names those too -- a "known commands" list that omitted `history` or
    # `matrix` would send an operator looking for it back to the documentation.
    known = set(result.details["known_commands"])
    assert set(PRD_9_1_COMMANDS) <= known
    assert known == set(cli.SUPPORTED_COMMANDS)


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
# a machine config typo cannot silently change the machine
# ---------------------------------------------------------------------------
def test_a_misspelled_machine_config_key_is_refused(workspace: dict[str, Path]) -> None:
    """``cpu_modle`` must not be dropped in favour of the ``cpu_model`` default.

    ``MachineConfig.from_dict`` drops keys it does not model, which is right for
    a caller holding a superset dict and wrong for a caller reading
    ``config/machine.json``: there, a dropped key is a typo, or a key from a
    newer schema, or a feature the operator believes is configured and is not.
    In every one of those cases the file describes one machine while the launch
    builds another, so the controller has to ask the question and refuse.

    Driven through ``start`` because that is the command that builds a
    :class:`QemuLauncher` from the config -- the one place a dropped key would
    reach a QEMU command line.
    """
    workspace["machine"].write_text(
        json.dumps({"accel": "tcg", "cpu_modle": "cortex-a57"}), encoding="utf-8"
    )

    result = run_cli(["start", "--profile", "tcg", "--artifact", "manifest.json"], workspace)

    assert result.exit_code == int(ExitCode.USAGE)
    assert result.error_code == ErrorCode.CONFIG_INVALID
    # The offending name is listed so the operator knows exactly what to fix...
    assert result.details["unknown_keys"] == ["cpu_modle"]
    assert "cpu_modle" in result.document["error"]["message"]
    # ... and only the name: PRD 9.2 forbids values in any output.
    assert "cortex-a57" not in json.dumps(result.document)


def test_a_documented_machine_config_key_is_tolerated(workspace: dict[str, Path]) -> None:
    """``*_note`` prose and the sections the controller reads directly are fine."""
    workspace["machine"].write_text(
        json.dumps(
            {
                "accel": "tcg",
                "cpu_model_note": "pinned to the board we boot",
                "schema_version": 1,
                "disks": {"system_image": None},
            }
        ),
        encoding="utf-8",
    )

    result = run_cli(["start", "--profile", "tcg", "--artifact", "manifest.json"], workspace)

    # Past the config gate, and stopped by the missing manifest instead: that is
    # what "the config was accepted" looks like from the outside.
    assert result.error_code == ErrorCode.ARTIFACT_MISSING


def test_the_shipped_machine_config_still_loads(workspace: dict[str, Path]) -> None:
    """The gate must not reject the config this repository actually ships.

    ``config/machine.json`` is full of ``*_note`` prose and has a ``disks``
    section the controller reads directly; a gate that tolerated neither would
    fail on the real file and teach everyone to disable it.
    """
    shipped = REPO_ROOT / "config" / "machine.json"
    assert shipped.is_file(), "the repository must ship config/machine.json"
    workspace["machine"].write_text(shipped.read_text(encoding="utf-8"), encoding="utf-8")

    result = run_cli(["start", "--profile", "tcg", "--artifact", "manifest.json"], workspace)

    assert result.error_code == ErrorCode.ARTIFACT_MISSING
    assert "config_invalid" not in json.dumps(result.document["error"])


# ---------------------------------------------------------------------------
# PRD 9.1 / appendix A: install without a guest channel
# ---------------------------------------------------------------------------


@pytest.fixture()
def ipa(workspace: dict[str, Path]) -> Path:
    """A real, acceptable Device IPA built by the fixture builder."""
    return build_ipa(workspace["root"] / "HelloUniPad.ipa")


def test_install_without_a_usable_guest_channel_is_blocked_never_a_success(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """An install with no *usable* guest channel is ``blocked``, never a success.

    The reason changed when ``unipad.guest.GuestClient`` was added to the probe
    list: the factory this host ships is now found, and the refusal names the
    real state -- a channel that exists but cannot be constructed without a
    transport this host has no guest to provide.  ``guest_channel_missing``
    would have sent the operator looking for an implementation that is already
    installed.  What this test pins is the part that must never move: the
    command stays ``blocked`` and never reports a stage it did not reach.
    """
    result = run_cli(["install", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.status == "blocked"
    assert result.error_code == ErrorCode.BLOCKED
    assert result.details["blocked_reason"] == "guest_channel_unavailable"
    # The probe list is reported so the resolved factory stays visible.
    assert any("unipad.guest" in probe for probe in result.details["probed"])
    assert "unipad.guest.GuestClient" in result.details["probed"]


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
    # The named log file must exist: evidence, not a promise (PRD 9.2).  It is
    # found through the runs root, because the envelope carries the path redacted.
    log_path = run_dir_of(workspace, result) / "install.log"
    assert log_path.is_file()
    text = log_path.read_text(encoding="utf-8")
    assert "install requested" in text
    assert data["package_sha256"] in text


def test_a_blocked_install_still_leaves_its_journal_behind(
    workspace: dict[str, Path], ipa: Path
) -> None:
    result = run_cli(["install", str(ipa)], workspace)
    run_dir = run_dir_of(workspace, result)

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


# ---------------------------------------------------------------------------
# the guest-channel probe list must describe the channels this host models
# ---------------------------------------------------------------------------


def test_the_guest_channel_probe_list_resolves_the_factory_unipad_guest_exports() -> None:
    """``unipad install`` must be able to *find* a guest channel on this host.

    ``_CAPABILITIES["guest_channel"]`` is the controller's only route to the
    guest.  ``unipad.guest`` exports :class:`GuestClient` as its client, and
    ``unipad.ipa.installer._default_channel`` searches for exactly that name,
    so a probe list that omits it makes every install report
    ``guest_channel_missing`` -- a false statement about a host that ships the
    channel.  The probe list has to name the channel the controller models.
    """
    import unipad.guest as guest

    assert callable(getattr(guest, "GuestClient", None)), (
        "unipad.guest must export a GuestClient factory for this assertion to mean anything"
    )
    probed = set(cli._probed("guest_channel"))
    assert "unipad.guest.GuestClient" in probed, (
        "the probe list must name the channel unipad.guest actually exports"
    )
    # The load-bearing half: with the module present the capability resolves.
    assert cli._resolve_capability("guest_channel") is not None


def test_install_does_not_claim_the_guest_channel_is_missing_when_one_is_exported(
    workspace: dict[str, Path], ipa: Path
) -> None:
    """A channel that exists but needs a transport is *unavailable*, not missing.

    ``GuestClient`` requires a transport, and this host has no guest to give it
    one, so the install is still ``blocked`` -- but the reason must name the
    real state.  ``guest_channel_missing`` would tell the operator to go and
    implement a channel that is already there.
    """
    result = run_cli(["install", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.status == "blocked"
    assert result.error_code == ErrorCode.BLOCKED
    assert result.details["blocked_reason"] == "guest_channel_unavailable"
    # The probe list is still reported, so the resolved factory is visible.
    assert "unipad.guest.GuestClient" in result.details["probed"]
    # PRD 9.1: a blocked install never reports an install stage it did not reach.
    assert result.data["stage"] == "checked"
    assert result.data["stage"] not in ("installed", "registered")


# ---------------------------------------------------------------------------
# PRD 9.1: an installer result the controller cannot classify
# ---------------------------------------------------------------------------


def _patch_install_chain(monkeypatch: pytest.MonkeyPatch, installer: Any) -> None:
    """Resolve the guest channel and installer capabilities to ``installer``.

    Only the two capabilities that need a real guest are replaced; the precheck
    stays the real one, so a rejected package still fails for the real reason.
    """
    real = cli._resolve_capability

    def fake(name: str) -> Any:
        if name == "guest_channel":
            return lambda: object()
        if name == "ipa_install":
            return installer
        return real(name)

    monkeypatch.setattr(cli, "_resolve_capability", fake)


class _RecordingInstaller:
    """A duck-typed installer that records the stage it was asked to report."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def install(self, path: Any, *, name: Any = None, precheck: Any = None, **kwargs: Any) -> Any:
        return "not-an-install-result"


def test_an_unrecognised_installer_result_is_blocked_not_an_internal_error(
    workspace: dict[str, Path], ipa: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installer returning an unclassifiable value must not crash the CLI.

    ``_coerce_install_result`` promises "the controller never synthesises an
    install stage it did not observe".  Reaching ``main``'s catch-all instead
    turns that promise into ``internal_error`` (exit 1) -- a code outside the
    PRD 10 taxonomy -- and the operator loses the ``blocked`` classification
    that says the host is not the problem.
    """
    _patch_install_chain(monkeypatch, _RecordingInstaller)

    result = run_cli(["install", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    assert result.error_code == ErrorCode.BLOCKED
    assert result.details["returned_type"] == "str"
    assert "NameError" not in json.dumps(result.document)
    # PRD 9.2: the value itself is never reported, only its type.
    assert "not-an-install-result" not in json.dumps(result.document)


def test_a_refused_install_leaves_its_log_behind(
    workspace: dict[str, Path], ipa: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every refusal in the chain must leave the evidence the envelope names.

    ``_persist_install_log`` exists precisely so a blocked install still leaves
    its log ("the envelope names this file, so writing it must not depend on
    reaching the end of the chain").  A refusal that raises before any writer
    records nothing anywhere: the run directory holds only the journal the
    command created before the chain started.
    """
    _patch_install_chain(monkeypatch, _RecordingInstaller)

    result = run_cli(["install", str(ipa)], workspace)

    assert result.exit_code == int(ExitCode.BLOCKED)
    log_path = run_dir_of(workspace, result) / "install.log"
    assert log_path.is_file(), "the envelope names this file, so it must exist"
    text = log_path.read_text(encoding="utf-8")
    assert "install requested" in text
    assert result.data["package_sha256"] in text
    # The refusal itself is recorded, not just the request.
    assert "installer returned no structured result" in text


# ---------------------------------------------------------------------------
# PRD 3.2 P1 (3): the lifecycle surface must not contradict its own entry
# ---------------------------------------------------------------------------


class _InstalledInstaller:
    """An installer that reports a completed install."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    def install(self, path: Any, *, name: Any = None, precheck: Any = None, **kwargs: Any) -> Any:
        from unipad.models import InstallResult

        return InstallResult(
            package_sha256="",
            bundle_id=DEFAULT_BUNDLE_ID,
            stage="installed",
            trust_mode="development-signed",
        )


def _seed_installed(
    workspace: dict[str, Path], ipa: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record one successful install in the default ledger."""
    _patch_install_chain(monkeypatch, _InstalledInstaller)
    result = run_cli(["install", str(ipa)], workspace)
    assert result.exit_code == int(ExitCode.OK), result.raw


def test_uninstall_reports_the_bundle_ids_installed_after_the_removal(
    workspace: dict[str, Path], ipa: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``installed_bundle_ids`` must describe the host *after* the transition.

    The list is what an operator reads to decide what to uninstall next.  A
    value captured before ``record_uninstall`` still names the bundle the same
    envelope has just reported as ``removed`` with ``installed: false`` -- the
    envelope contradicts its own entry, and the next ``uninstall`` of that id
    is refused for a reason the previous answer denied.
    """
    _seed_installed(workspace, ipa, monkeypatch)
    before = run_cli(["uninstall", DEFAULT_BUNDLE_ID], workspace)

    assert before.exit_code == int(ExitCode.OK)
    assert before.data["installed"] is False
    assert before.data["entry"]["stage"] == "removed"
    assert before.data["installed_bundle_ids"] == [], (
        "a bundle this envelope just removed must not be listed as installed"
    )

    after = run_cli(["history", "--all"], workspace)
    assert after.data["installed_bundle_ids"] == []
    assert before.data["installed_bundle_ids"] == after.data["installed_bundle_ids"]


def test_prior_install_check_names_the_check_that_actually_ran(
    workspace: dict[str, Path], ipa: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``prior_install_check`` is evidence, so it may only claim a real check.

    A first install runs no prior-install check -- the whole point of
    ``install`` is that there is nothing to check against.  Reporting
    ``performed_by_controller`` there claims work the controller never did
    (PRD 9.2: evidence missing is unverified, not filled in with model text),
    and the value an operator would use to tell ``install`` from ``update``
    is gone.
    """
    _patch_install_chain(monkeypatch, _InstalledInstaller)
    first = run_cli(["install", str(ipa)], workspace)
    assert first.exit_code == int(ExitCode.OK)
    assert first.data["prior_install_check"] == "not_applicable_first_install"

    second = run_cli(["update", str(ipa)], workspace)
    assert second.exit_code == int(ExitCode.OK)
    assert second.data["prior_install_check"] == "performed_by_controller"


def test_prior_install_check_is_reported_on_a_precheck_rejection_too(
    workspace: dict[str, Path],
) -> None:
    """The precheck-rejection path reports the same field as the others.

    ``_finish_install`` is the one place that builds the PRD 9.1 envelope, so
    dropping the keyword on one call site makes the field silently default and
    a script that branches on it sees a third value for the same question.
    """
    package = build_ipa(workspace["root"] / "Sim.ipa", corrupt="simulator")

    result = run_cli(["install", str(package)], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.data["stage"] == "failed"
    assert result.data["prior_install_check"] == "not_applicable_first_install"


# ---------------------------------------------------------------------------
# PRD 9.1: a malformed launch record is a usage-class error, not a crash
# ---------------------------------------------------------------------------


def test_a_launch_record_with_a_non_integer_pid_is_a_usage_error(
    workspace: dict[str, Path],
) -> None:
    """``launch.json`` is a manifest; a bad ``pid`` is a bad manifest.

    ``int(record["pid"])`` is unguarded, so a launch record whose ``pid`` is a
    string, a list or a mapping raises out of ``cmd_stop`` into ``main``'s
    catch-all and comes back as ``internal_error`` -- a code outside the PRD 10
    taxonomy that says "the controller is broken" when the truth is "this
    record is not one I can read".
    """
    store = ExperimentStore(workspace["runs"])
    store.create("exp-stop-bad-pid", "M4", "a launch record with a broken pid")
    record = workspace["runs"] / "exp-stop-bad-pid" / "launch.json"
    record.write_text(json.dumps({"pid": "not-a-number", "qmp_port": 4444}), encoding="utf-8")

    result = run_cli(["stop", "--run", "exp-stop-bad-pid"], workspace)

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.ARTIFACT_MANIFEST_INVALID
    assert "ValueError" not in json.dumps(result.document)


# ---------------------------------------------------------------------------
# PRD 8.3 / FR02: a refused start must not leave state behind
# ---------------------------------------------------------------------------


def test_a_refused_start_leaves_no_empty_run_directory(
    workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run directory is created only once the launch is going to happen.

    ``run_dir.mkdir`` runs before the launch images are validated, so a start
    that is refused for ``image_not_found`` leaves an empty directory behind:
    no journal, no launch record, nothing.  ``ExperimentStore.scan`` skips a
    directory without ``experiment.json``, so it is invisible to
    ``_latest_run`` and nothing ever removes it -- retrying with a different
    experiment id each time fills the runs root with directories that describe
    nothing.
    """
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "C:/qemu/qemu-system-aarch64"
        if name == "qemu-system-aarch64"
        else None,
    )
    manifest = workspace["root"] / "manifest.json"
    manifest.write_text(json.dumps({"target": "tcg"}), encoding="utf-8")

    result = run_cli(
        ["start", "--profile", "tcg", "--artifact", str(manifest), "--experiment", "exp-never-started"],
        workspace,
    )

    assert result.exit_code == int(ExitCode.FAILURE)
    assert result.error_code == ErrorCode.IMAGE_NOT_FOUND
    assert not (workspace["runs"] / "exp-never-started").exists(), (
        "a refused start must not leave an empty run directory behind"
    )


# ---------------------------------------------------------------------------
# PRD 9.1: one "known commands" answer, and a help text that names the split
# ---------------------------------------------------------------------------


def test_both_usage_errors_name_the_same_known_commands(workspace: dict[str, Path]) -> None:
    """An unknown command and a missing command must answer the same question.

    The two envelopes are the only place the CLI tells an operator what it
    accepts.  Reporting thirteen names in one and nine in the other means a
    caller that learned the list from a typo is told a different set when it
    simply forgets the command -- and neither answer says which of the three
    surfaces the nine belong to.
    """
    unknown = run_cli(["frobnicate"], workspace)
    missing = run_cli([], workspace)

    assert unknown.details["known_commands"] == missing.details["known_commands"]
    assert set(unknown.details["known_commands"]) == set(cli.SUPPORTED_COMMANDS)
    assert set(missing.details["known_commands"]) == set(cli.SUPPORTED_COMMANDS)


def test_help_names_the_prd_9_1_contract_and_the_lifecycle_surface_separately(
    workspace: dict[str, Path],
) -> None:
    """``--help`` must say which commands are the PRD 9.1 contract.

    Thirteen subcommands print under a description that still reads "(PRD 9.1)",
    so an operator cannot tell the documented nine from the four the lifecycle
    and matrix surfaces add.  The help text is the operator-facing half of the
    same disagreement the two error envelopes carry.
    """
    result = run_cli(["--help"], workspace)

    assert result.exit_code == int(ExitCode.OK)
    help_text = result.data["help"]
    assert "PRD 9.1" in help_text
    assert "PRD 3.2 P1 (3)" in help_text
    for name in PRD_9_1_COMMANDS:
        assert name in help_text
    for name in cli.LIFECYCLE_COMMANDS:
        assert name in help_text
    for name in cli.MATRIX_COMMANDS:
        assert name in help_text
    # The nine are named as a list of their own, not only as subparser headings.
    assert ", ".join(PRD_9_1_COMMANDS) in help_text
