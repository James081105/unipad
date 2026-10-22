"""Unit tests for the fixed-condition verifier (PRD 7.4 and 9.2).

Mapping
-------
* PRD 7.4 "模型提交门禁证据，验证器依据固定条件判定" -- the verifier disposes of
  evidence by fixed conditions, and every outcome is stamped with the verifier
  identity and version so a change in verification logic is traceable
  independently of feature changes.
* PRD 9.2 / testing-strategy D2 -- missing evidence is *unverified*, never
  "probably fine": an incomplete set never passes, and the failure reasons name
  exactly what is missing.
* PRD 7.4 "验证逻辑的修改独立记录，不能随功能修改偷偷放宽" -- extra checks are
  additive (they can only add failure reasons), and a check that raises fails
  closed instead of being read as a pass.
* PRD 7.4 -- serial text alone is not proof: the M9+ gates additionally require
  screenshots and a guest-side log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.models import EvidenceSet
from unipad.controller.verifier import (
    CONDITIONS,
    VERIFICATION_FILENAME,
    VERIFIER_ID,
    VERIFIER_VERSION,
    Verifier,
    describe_gate,
    iter_condition_ids,
    load_facts,
)


def _complete_evidence(run_id: str = "run-1") -> EvidenceSet:
    """A complete PRD 9.2 evidence set (no optional items)."""
    return EvidenceSet(
        run_id=run_id,
        experiment_json="experiment.json",
        host_inventory="host-inventory.json",
        artifact_manifest="artifact-manifest.json",
        qemu_args="qemu-args.txt",
        serial_log="serial.log",
        verification_json="verification.json",
    )


def _write_run_dir(run_dir: Path) -> Path:
    """Materialise every referenced evidence file inside ``run_dir``."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "experiment.json",
        "host-inventory.json",
        "artifact-manifest.json",
        "qemu-args.txt",
        "serial.log",
    ):
        (run_dir / name).write_text("{}\n" if name.endswith(".json") else "-accel tcg\n", encoding="utf-8")
    return run_dir


@pytest.fixture()
def verifier() -> Verifier:
    return Verifier()


# ---------------------------------------------------------------------------
# incomplete evidence never passes
# ---------------------------------------------------------------------------
def test_an_empty_evidence_set_never_passes(verifier: Verifier) -> None:
    # Arrange: nothing collected at all.
    evidence = EvidenceSet(run_id="run-1")

    # Act.
    outcome = verifier.evaluate_detailed("M0", "M0", evidence)

    # Assert: PRD 9.2 -- missing evidence is unverified, and the reasons name
    # every absent item.
    assert outcome.result.passed is False
    assert outcome.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert outcome.missing == [
        "experiment_json",
        "host_inventory",
        "artifact_manifest",
        "qemu_args",
        "serial_log",
        "verification_json",
    ]
    assert outcome.result.failure_reasons[0] == "evidence_incomplete"
    assert "missing:serial_log" in outcome.result.failure_reasons
    assert "missing:verification_json" in outcome.result.failure_reasons


def test_a_partially_complete_evidence_set_never_passes(verifier: Verifier) -> None:
    # Arrange: only the journal is recorded.
    evidence = EvidenceSet(run_id="run-1", experiment_json="experiment.json")

    # Act.
    outcome = verifier.evaluate_detailed("M0", "M0", evidence)

    # Assert: exactly the remaining required items are reported.
    assert outcome.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert outcome.missing == [
        "host_inventory",
        "artifact_manifest",
        "qemu_args",
        "serial_log",
        "verification_json",
    ]
    for name in outcome.missing:
        assert f"missing:{name}" in outcome.result.failure_reasons


def test_evaluate_returns_the_gate_result_and_it_refuses_to_pass(verifier: Verifier) -> None:
    # The PRD 6 contract returns only the GateResult.
    result = verifier.evaluate("M4", "M4", EvidenceSet(run_id="run-1"))

    assert result.passed is False
    assert result.failure_reasons[0] == "evidence_incomplete"
    assert result.milestone == "M4"


def test_blank_evidence_strings_count_as_missing(verifier: Verifier) -> None:
    # PRD 9.2: a recorded path that is empty is not evidence.
    evidence = _complete_evidence()
    evidence.serial_log = ""

    outcome = verifier.evaluate_detailed("M0", "M0", evidence)

    assert outcome.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert outcome.missing == ["serial_log"]


# ---------------------------------------------------------------------------
# a complete evidence set passes
# ---------------------------------------------------------------------------
def test_a_complete_evidence_set_passes_a_core_gate(verifier: Verifier) -> None:
    # Arrange: every required item recorded (run_dir omitted: no file checks).
    evidence = _complete_evidence()

    # Act.
    outcome = verifier.evaluate_detailed("M0", "M0", evidence)

    # Assert.
    assert outcome.code is None
    assert outcome.missing == []
    assert outcome.result.passed is True
    assert outcome.result.failure_reasons == []


def test_a_complete_evidence_set_with_files_on_disk_passes(verifier: Verifier, tmp_path: Path) -> None:
    # Arrange: the evidence files really exist inside the run directory.
    run_dir = _write_run_dir(tmp_path / "run-1")
    evidence = _complete_evidence()

    # Act.
    outcome = verifier.evaluate_detailed("M0", "M0", evidence, run_dir=run_dir)

    # Assert.
    assert outcome.result.passed is True
    assert outcome.code is None


def test_a_referenced_evidence_file_that_is_absent_fails(verifier: Verifier, tmp_path: Path) -> None:
    # PRD 9.2: a journal referencing a log that is not on disk is not evidence.
    run_dir = _write_run_dir(tmp_path / "run-1")
    (run_dir / "serial.log").unlink()
    evidence = _complete_evidence()

    outcome = verifier.evaluate_detailed("M0", "M0", evidence, run_dir=run_dir)

    assert outcome.result.passed is False
    assert outcome.code == ErrorCode.VERIFICATION_FAILED
    assert "missing_evidence_file:serial_log:serial.log" in outcome.result.failure_reasons


def test_m9_gates_require_screenshots_and_a_guest_log(verifier: Verifier, tmp_path: Path) -> None:
    # PRD 7.4: serial text alone is not proof of a stage.
    run_dir = _write_run_dir(tmp_path / "run-1")
    evidence = _complete_evidence()

    outcome = verifier.evaluate_detailed("M9", "M9", evidence, run_dir=run_dir)

    assert outcome.result.passed is False
    assert "missing:screenshots" in outcome.result.failure_reasons
    assert "missing:guest_log" in outcome.result.failure_reasons


def test_m9_gate_passes_with_screenshots_and_a_guest_log(verifier: Verifier, tmp_path: Path) -> None:
    # Arrange.
    run_dir = _write_run_dir(tmp_path / "run-1")
    (run_dir / "guest.log").write_text("launchd: boot complete\n", encoding="utf-8")
    shots = run_dir / "screenshots"
    shots.mkdir()
    (shots / "springboard.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    evidence = _complete_evidence()
    evidence.guest_log = "guest.log"
    evidence.screenshots = ["screenshots/springboard.png"]

    # Act.
    outcome = verifier.evaluate_detailed("M9", "M9", evidence, run_dir=run_dir)

    # Assert.
    assert outcome.result.passed is True
    assert outcome.code is None


# ---------------------------------------------------------------------------
# install / persistence conditions
# ---------------------------------------------------------------------------
def test_a_transfer_only_install_never_passes_the_install_gate(verifier: Verifier, tmp_path: Path) -> None:
    # PRD 9.1 / 10: a file copy alone is not an install.
    run_dir = _write_run_dir(tmp_path / "run-1")
    evidence = _complete_evidence()
    facts = {"install": {"stage": "transferred", "bundle_id": "com.unipad.hello", "error_code": None}}

    outcome = verifier.evaluate_detailed("M10", "M10", evidence, run_dir=run_dir, facts=facts)

    assert outcome.result.passed is False
    assert "install_stage_not_terminal:transferred" in outcome.result.failure_reasons


def test_a_registered_install_with_a_bundle_id_passes_the_install_gate(
    verifier: Verifier, tmp_path: Path
) -> None:
    run_dir = _write_run_dir(tmp_path / "run-1")
    (run_dir / "guest.log").write_text("launchd: boot complete\n", encoding="utf-8")
    shots = run_dir / "screenshots"
    shots.mkdir()
    (shots / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    evidence = _complete_evidence()
    evidence.guest_log = "guest.log"
    evidence.screenshots = ["screenshots/icon.png"]
    facts = {
        "install": {
            "stage": "registered",
            "bundle_id": "com.unipad.hello",
            "error_code": None,
        }
    }

    outcome = verifier.evaluate_detailed("M10", "M10", evidence, run_dir=run_dir, facts=facts)

    assert outcome.result.passed is True


def test_missing_install_facts_fail_closed(verifier: Verifier, tmp_path: Path) -> None:
    # PRD 7.4: absent data is a failure, not a pass.
    run_dir = _write_run_dir(tmp_path / "run-1")
    (run_dir / "guest.log").write_text("boot\n", encoding="utf-8")
    shots = run_dir / "screenshots"
    shots.mkdir()
    (shots / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    evidence = _complete_evidence()
    evidence.guest_log = "guest.log"
    evidence.screenshots = ["screenshots/icon.png"]

    outcome = verifier.evaluate_detailed("M10", "M10", evidence, run_dir=run_dir, facts={})

    assert outcome.result.passed is False
    assert "facts_missing:install" in outcome.result.failure_reasons


def test_a_mismatched_persistence_marker_fails(verifier: Verifier, tmp_path: Path) -> None:
    # PRD 2.3 H04: the random marker must read back identically.
    run_dir = _write_run_dir(tmp_path / "run-1")
    (run_dir / "guest.log").write_text("boot\n", encoding="utf-8")
    shots = run_dir / "screenshots"
    shots.mkdir()
    (shots / "reboot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    evidence = _complete_evidence()
    evidence.guest_log = "guest.log"
    evidence.screenshots = ["screenshots/reboot.png"]
    facts = {
        "install": {"stage": "installed", "bundle_id": "com.unipad.notes", "error_code": None},
        "persistence": {"marker": "abc123", "read_back": "different"},
    }

    outcome = verifier.evaluate_detailed("M9b", "M9b", evidence, run_dir=run_dir, facts=facts)

    assert outcome.result.passed is False
    assert "persistence_marker_mismatch" in outcome.result.failure_reasons


# ---------------------------------------------------------------------------
# extra checks are additive and fail closed
# ---------------------------------------------------------------------------
def test_an_extra_check_can_add_a_failure_reason(verifier: Verifier) -> None:
    # Arrange: a complete evidence set that would otherwise pass.
    evidence = _complete_evidence()

    # Act.
    outcome = verifier.evaluate_detailed(
        "M0", "M0", evidence, extra_checks=[lambda: ["challenge_response_missing"]]
    )

    # Assert: additive -- the caller check can only add reasons.
    assert outcome.result.passed is False
    assert "challenge_response_missing" in outcome.result.failure_reasons
    assert outcome.code == ErrorCode.VERIFICATION_FAILED


def test_an_extra_check_cannot_remove_a_builtin_reason(verifier: Verifier) -> None:
    # Arrange: a gate whose builtin conditions already fail.
    run_dir_evidence = _complete_evidence()
    run_dir_evidence.serial_log = "serial.log"

    outcome = verifier.evaluate_detailed(
        "M9",
        "M9",
        run_dir_evidence,
        extra_checks=[lambda: []],  # a passing caller check
    )

    # Assert: the builtin reasons survive.
    assert "missing:screenshots" in outcome.result.failure_reasons
    assert "missing:guest_log" in outcome.result.failure_reasons


def test_a_raising_extra_check_fails_closed(verifier: Verifier) -> None:
    # PRD 7.4: a crashed check must never be read as a pass.
    evidence = _complete_evidence()

    def exploding_check() -> list[str]:
        raise RuntimeError("probe crashed")

    outcome = verifier.evaluate_detailed("M0", "M0", evidence, extra_checks=[exploding_check])

    assert outcome.result.passed is False
    assert outcome.code == ErrorCode.VERIFICATION_FAILED
    assert any(reason.startswith("check_error:0:RuntimeError") for reason in outcome.result.failure_reasons)


def test_an_extra_check_returning_none_is_not_a_failure(verifier: Verifier) -> None:
    # A check that reports nothing adds nothing.
    evidence = _complete_evidence()

    outcome = verifier.evaluate_detailed("M0", "M0", evidence, extra_checks=[lambda: None])

    assert outcome.result.passed is True


def test_a_bound_extra_check_is_labelled_in_the_report(verifier: Verifier) -> None:
    evidence = _complete_evidence()

    outcome = verifier.evaluate_detailed(
        "M0",
        "M0",
        evidence,
        extra_checks=[("ipa-mvp.challenge", lambda: ["challenge_not_observed"])],
    )

    rows = {row["condition_id"]: row for row in outcome.conditions}
    assert "ipa-mvp.challenge" in rows
    assert rows["ipa-mvp.challenge"]["source"] == "extra"
    assert rows["ipa-mvp.challenge"]["passed"] is False


def test_a_malformed_extra_check_is_config_invalid(verifier: Verifier) -> None:
    evidence = _complete_evidence()

    with pytest.raises(UniPadError) as caught:
        verifier.evaluate_detailed("M0", "M0", evidence, extra_checks=[("id",)])  # type: ignore[list-item]

    assert caught.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# unknown gates
# ---------------------------------------------------------------------------
def test_an_unknown_gate_is_config_invalid(verifier: Verifier) -> None:
    # A typo must never be evaluated against a weaker default set.
    evidence = _complete_evidence()

    with pytest.raises(UniPadError) as caught:
        verifier.evaluate_detailed("M99", "M99", evidence)

    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert caught.value.details["gate_id"] == "M99"


def test_known_gates_excludes_the_default_group(verifier: Verifier) -> None:
    gates = verifier.known_gates()

    assert "default" not in gates
    assert "ipa-mvp" in gates
    assert "M0" in gates
    assert "M12" in gates


def test_every_milestone_has_a_registered_gate() -> None:
    # Import-time self-check, asserted here so a regression is visible.
    for milestone in ("M0", "M4", "M9a", "M9b", "M10", "M10a", "M11", "M12"):
        assert milestone in CONDITIONS, milestone


def test_describe_gate_lists_the_conditions(verifier: Verifier) -> None:
    payload = describe_gate("M10")

    assert payload["gate_id"] == "M10"
    assert payload["verifier_id"] == VERIFIER_ID
    assert payload["verifier_version"] == VERIFIER_VERSION
    assert "install.stage_terminal" in [c["condition_id"] for c in payload["conditions"]]


def test_iter_condition_ids_matches_the_registry(verifier: Verifier) -> None:
    assert tuple(iter_condition_ids("M0")) == ("evidence.complete", "evidence.files_exist")


# ---------------------------------------------------------------------------
# identity stamping
# ---------------------------------------------------------------------------
def test_the_result_is_stamped_with_the_verifier_identity_and_version(verifier: Verifier) -> None:
    evidence = _complete_evidence()

    result = verifier.evaluate("M0", "M0", evidence)

    assert result.verifier_id == f"{VERIFIER_ID}@{VERIFIER_VERSION}"
    assert VERIFIER_ID == "unipad.controller.verifier"
    assert result.verifier_id.startswith("unipad.controller.verifier@")


def test_the_outcome_carries_the_same_stamp(verifier: Verifier) -> None:
    outcome = verifier.evaluate_detailed("M0", "M0", EvidenceSet(run_id="run-1"))

    assert outcome.result.verifier_id == f"{VERIFIER_ID}@{VERIFIER_VERSION}"
    assert outcome.gate_id == "M0"


def test_checked_at_can_be_injected_for_determinism(verifier: Verifier) -> None:
    evidence = _complete_evidence()

    result = verifier.evaluate("M0", "M0", evidence, checked_at="2026-10-22T00:00:00+00:00")

    assert result.checked_at == "2026-10-22T00:00:00+00:00"


# ---------------------------------------------------------------------------
# verification.json
# ---------------------------------------------------------------------------
def test_verification_json_is_written_when_a_run_dir_is_given(verifier: Verifier, tmp_path: Path) -> None:
    # PRD 9.2: verification.json is part of the minimum evidence set.
    run_dir = _write_run_dir(tmp_path / "run-1")
    evidence = _complete_evidence()

    outcome = verifier.evaluate_detailed("M0", "M0", evidence, run_dir=run_dir)

    target = run_dir / VERIFICATION_FILENAME
    assert target.is_file()
    assert outcome.verification_path == target
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["gate_id"] == "M0"
    assert payload["milestone"] == "M0"
    assert payload["verifier_id"] == VERIFIER_ID
    assert payload["verifier_version"] == VERIFIER_VERSION
    assert payload["passed"] is True
    assert payload["error_code"] is None
    assert payload["failure_reasons"] == []
    assert payload["evidence"]["run_id"] == "run-1"


def test_verification_json_is_written_even_for_a_failing_gate(verifier: Verifier, tmp_path: Path) -> None:
    # The failure must be recorded, not just reported on stdout.
    run_dir = tmp_path / "run-1"

    outcome = verifier.evaluate_detailed("M0", "M0", EvidenceSet(run_id="run-1"), run_dir=run_dir)

    payload = json.loads((run_dir / VERIFICATION_FILENAME).read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["error_code"] == ErrorCode.EVIDENCE_INCOMPLETE
    assert "evidence_incomplete" in payload["failure_reasons"]
    assert outcome.result.passed is False


def test_no_run_dir_means_no_verification_file(verifier: Verifier, tmp_path: Path) -> None:
    evidence = _complete_evidence()

    outcome = verifier.evaluate_detailed("M0", "M0", evidence)

    assert outcome.verification_path is None
    assert not (tmp_path / VERIFICATION_FILENAME).exists()


def test_a_self_provided_verification_json_is_satisfied_by_the_run_dir(
    verifier: Verifier, tmp_path: Path
) -> None:
    # PRD 9.2 lists verification.json, but the verifier writes it: with a run
    # directory the one documented exception is recorded, not silently dropped.
    run_dir = _write_run_dir(tmp_path / "run-1")
    evidence = _complete_evidence()
    evidence.verification_json = None

    outcome = verifier.evaluate_detailed("M0", "M0", evidence, run_dir=run_dir)

    assert outcome.result.passed is True
    assert outcome.self_provided == ["verification_json"]
    assert evidence.verification_json == VERIFICATION_FILENAME
    rows = {row["condition_id"]: row for row in outcome.conditions}
    assert rows["evidence.complete"]["self_provided_by_verifier"] == ["verification_json"]


def test_without_a_run_dir_a_missing_verification_json_still_fails(verifier: Verifier) -> None:
    # No run directory -> the verifier cannot supply it -> fail closed.
    evidence = _complete_evidence()
    evidence.verification_json = None

    outcome = verifier.evaluate_detailed("M0", "M0", evidence)

    assert outcome.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert outcome.missing == ["verification_json"]
    assert outcome.self_provided == []


# ---------------------------------------------------------------------------
# load_facts
# ---------------------------------------------------------------------------
def test_load_facts_skips_absent_files(tmp_path: Path) -> None:
    facts = load_facts(tmp_path / "install.json", tmp_path / "persistence.json")

    assert facts == {}


def test_load_facts_keys_on_the_file_stem(tmp_path: Path) -> None:
    (tmp_path / "install.json").write_text(json.dumps({"stage": "registered"}), encoding="utf-8")

    facts = load_facts(tmp_path / "install.json")

    assert facts == {"install": {"stage": "registered"}}


def test_load_facts_rejects_malformed_json(tmp_path: Path) -> None:
    # A broken fact file cannot be mistaken for "no data".
    (tmp_path / "install.json").write_text("{oops", encoding="utf-8")

    with pytest.raises(UniPadError) as caught:
        load_facts(tmp_path / "install.json")
    assert caught.value.code == ErrorCode.CONFIG_INVALID
