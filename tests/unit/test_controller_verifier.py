"""Controller verifier tests (PRD sections 7.4, 9.2 and 6).

Mapping
-------
* **PRD 7.4** -- "模型提交门禁证据，验证器依据固定条件判定": a gate outcome comes
  from the registered conditions, never from the agent that produced the
  evidence.  Every result is stamped with ``verifier_id`` so a change in
  verification logic is traceable independently of feature changes.
* **PRD 9.2** -- missing evidence is *unverified*: an incomplete set fails with
  reason ``evidence_incomplete``, the code ``evidence_incomplete`` and the list
  of missing items -- never a pass.
* **PRD 7.4** -- "串口文本用于定位，不单独证明阶段成功": the M9 family of gates
  requires a guest-side log and a screenshot in addition to the core set.
* **PRD 7.4** -- extra checks are additive: they can only *add* failure reasons,
  and a check that raises fails closed instead of passing.
* **PRD 6 M10 / 9.1** -- "文件传输成功不能返回 installed": the install
  conditions demand a terminal stage, a recorded bundle id and no error code.
* **PRD 7.5 / H04** -- the persistence condition compares the marker written
  before shutdown with the one read back afterwards.

``tmp_path`` is used for every file the verifier reads or writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unipad.controller.verifier import (
    VERIFIER_ID,
    VERIFIER_VERSION,
    Verifier,
    describe_gate,
    iter_condition_ids,
)
from unipad.errors import ErrorCode, UniPadError
from unipad.models import EvidenceSet, GateResult

#: The PRD 9.2 minimum runtime evidence set (minus ``verification.json``, which
#: the verifier writes itself when a run directory is supplied).
CORE_REQUIRED: tuple[str, ...] = (
    "experiment_json",
    "host_inventory",
    "artifact_manifest",
    "qemu_args",
    "serial_log",
    "verification_json",
)


@pytest.fixture()
def verifier() -> Verifier:
    return Verifier()


def _complete_evidence(run_dir: Path, **overrides: str | None) -> EvidenceSet:
    """A PRD 9.2 evidence set whose referenced files really exist.

    ``overrides`` drives the fault injection: ``None`` leaves the item out of the
    set (missing), ``""`` records it as a blank string (claimed but empty), and
    any other string is written to disk and referenced.
    """
    names = {
        "experiment_json": "experiment.json",
        "host_inventory": "host-inventory.json",
        "artifact_manifest": "artifact-manifest.json",
        "qemu_args": "qemu-args.txt",
        "serial_log": "serial.log",
        "guest_log": "guest.log",
        "verification_json": "verification.json",
    }
    evidence = EvidenceSet(run_id=run_dir.name)
    for attribute, name in names.items():
        if attribute in overrides:
            value = overrides[attribute]
            if value is not None:
                setattr(evidence, attribute, value)
            continue
        target = run_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{name}\n", encoding="utf-8")
        setattr(evidence, attribute, name)
    return evidence


# ---------------------------------------------------------------------------
# PRD 9.2: incomplete evidence never passes
# ---------------------------------------------------------------------------


def test_an_empty_evidence_set_is_not_passed(verifier: Verifier, tmp_path: Path) -> None:
    evidence = EvidenceSet(run_id="exp-001")

    result = verifier.evaluate("M4", "M4", evidence)

    assert isinstance(result, GateResult)
    assert result.passed is False
    assert result.milestone == "M4"
    # The refusal is structured: the reason, the code and the missing names.
    assert result.failure_reasons[0] == "evidence_incomplete"
    assert set(result.failure_reasons[1:]) == {f"missing:{name}" for name in CORE_REQUIRED}


def test_a_partially_complete_evidence_set_is_not_passed(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path, host_inventory=None, serial_log=None)

    outcome = verifier.evaluate_detailed("M4", "M4", evidence)

    assert outcome.result.passed is False
    assert outcome.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert outcome.missing == ["host_inventory", "serial_log"]
    assert outcome.result.failure_reasons[:3] == [
        "evidence_incomplete",
        "missing:host_inventory",
        "missing:serial_log",
    ]


@pytest.mark.parametrize("absent", CORE_REQUIRED)
def test_any_single_missing_required_item_refuses_the_gate(
    verifier: Verifier, tmp_path: Path, absent: str
) -> None:
    # PRD 9.2: the minimum set is a set -- one hole is as fatal as six.
    evidence = _complete_evidence(tmp_path, **{absent: None})

    outcome = verifier.evaluate_detailed("M4", "M4", evidence)

    assert outcome.result.passed is False
    assert absent in outcome.missing
    assert outcome.code == ErrorCode.EVIDENCE_INCOMPLETE


def test_a_blank_evidence_string_counts_as_missing(verifier: Verifier, tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path, serial_log="")

    outcome = verifier.evaluate_detailed("M4", "M4", evidence)

    assert outcome.result.passed is False
    assert "serial_log" in outcome.missing


def test_the_m9_gates_need_a_guest_log_and_a_screenshot(
    verifier: Verifier, tmp_path: Path
) -> None:
    # PRD 7.4: serial text alone is not proof of a stage.
    evidence = _complete_evidence(tmp_path, guest_log=None)

    outcome = verifier.evaluate_detailed("M9", "M9", evidence)

    assert outcome.result.passed is False
    assert "missing:guest_log" in outcome.result.failure_reasons
    assert "missing:screenshots" in outcome.result.failure_reasons


def test_a_referenced_evidence_file_that_is_absent_fails(
    verifier: Verifier, tmp_path: Path
) -> None:
    # PRD 9.2: a journal that names a log which is not on disk is not evidence.
    evidence = _complete_evidence(tmp_path)
    (tmp_path / "serial.log").unlink()

    outcome = verifier.evaluate_detailed("M4", "M4", evidence, run_dir=tmp_path)

    assert outcome.result.passed is False
    assert "missing_evidence_file:serial_log:serial.log" in outcome.result.failure_reasons


# ---------------------------------------------------------------------------
# PRD 7.4: a complete evidence set passes the core gate
# ---------------------------------------------------------------------------


def test_a_complete_evidence_set_passes_a_core_gate(verifier: Verifier, tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)

    result = verifier.evaluate("M4", "M4", evidence, run_dir=tmp_path)

    assert result.passed is True
    assert result.failure_reasons == []
    # The evidence listing is what the gate actually saw.
    assert sorted(result.evidence) == sorted(
        [
            "experiment.json",
            "host-inventory.json",
            "artifact-manifest.json",
            "qemu-args.txt",
            "serial.log",
            "guest.log",
            "verification.json",
        ]
    )


def test_the_m9_gate_passes_with_a_screenshot_and_a_guest_log(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path)
    shot = tmp_path / "screenshots" / "springboard.ppm"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"P6\n2 2\n255\nrgb")
    evidence.screenshots = ["screenshots/springboard.ppm"]

    result = verifier.evaluate("M9", "M9", evidence, run_dir=tmp_path)

    assert result.passed is True
    assert result.failure_reasons == []


# ---------------------------------------------------------------------------
# PRD 6 M10 / 9.1: install facts
# ---------------------------------------------------------------------------


def test_a_transfer_only_install_never_passes_the_install_gate(
    verifier: Verifier, tmp_path: Path
) -> None:
    # PRD 9.1: "文件传输成功不能返回 installed".
    evidence = _complete_evidence(tmp_path)
    shot = tmp_path / "screenshots" / "icon.ppm"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"P6\n2 2\n255\nrgb")
    evidence.screenshots = ["screenshots/icon.ppm"]
    facts = {
        "install": {
            "package_sha256": "c" * 64,
            "bundle_id": "com.unipad.hello",
            "stage": "transferred",
            "trust_mode": None,
            "error_code": None,
        }
    }

    outcome = verifier.evaluate_detailed("M10", "M10", evidence, run_dir=tmp_path, facts=facts)

    assert outcome.result.passed is False
    assert "install_stage_not_terminal:transferred" in outcome.result.failure_reasons


def test_a_registered_install_with_a_bundle_id_passes_the_install_gate(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path)
    shot = tmp_path / "screenshots" / "app.ppm"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"P6\n2 2\n255\nrgb")
    evidence.screenshots = ["screenshots/app.ppm"]
    facts = {
        "install": {
            "package_sha256": "c" * 64,
            "bundle_id": "com.unipad.hello",
            "stage": "registered",
            "trust_mode": "self-signed-dev",
            "error_code": None,
        }
    }

    result = verifier.evaluate("M10", "M10", evidence, run_dir=tmp_path, facts=facts)

    assert result.passed is True


def test_an_install_error_code_fails_the_gate_even_at_a_terminal_stage(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path)
    shot = tmp_path / "screenshots" / "failed.ppm"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"P6\n2 2\n255\nrgb")
    evidence.screenshots = ["screenshots/failed.ppm"]
    facts = {
        "install": {
            "package_sha256": "c" * 64,
            "bundle_id": "com.unipad.hello",
            "stage": "failed",
            "trust_mode": None,
            "error_code": ErrorCode.REGISTRATION_FAILED,
        }
    }

    outcome = verifier.evaluate_detailed("M10", "M10", evidence, run_dir=tmp_path, facts=facts)

    assert outcome.result.passed is False
    assert f"install_error:{ErrorCode.REGISTRATION_FAILED}" in outcome.result.failure_reasons


def test_missing_install_facts_fail_closed(verifier: Verifier, tmp_path: Path) -> None:
    # PRD 7.4: "no data" is a failure, not a pass.
    evidence = _complete_evidence(tmp_path)

    outcome = verifier.evaluate_detailed("M10", "M10", evidence, run_dir=tmp_path, facts={})

    assert outcome.result.passed is False
    assert outcome.result.failure_reasons.count("facts_missing:install") == 3


# ---------------------------------------------------------------------------
# PRD H04 / 5.2 case 8: persistence
# ---------------------------------------------------------------------------


def test_a_marker_that_does_not_read_back_fails_the_persistence_gate(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path)
    facts = {"persistence": {"marker": "unipad-abc123", "read_back": "unipad-xyz789"}}

    outcome = verifier.evaluate_detailed("M9b", "M9b", evidence, run_dir=tmp_path, facts=facts)

    assert outcome.result.passed is False
    assert "persistence_marker_mismatch" in outcome.result.failure_reasons


def test_a_marker_that_reads_back_passes_the_persistence_gate(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path)
    shot = tmp_path / "screenshots" / "notes.ppm"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.write_bytes(b"P6\n2 2\n255\nrgb")
    evidence.screenshots = ["screenshots/notes.ppm"]
    facts = {"persistence": {"marker": "unipad-abc123", "read_back": "unipad-abc123"}}

    result = verifier.evaluate("M9b", "M9b", evidence, run_dir=tmp_path, facts=facts)

    assert result.passed is True


# ---------------------------------------------------------------------------
# PRD 7.4: extra checks are additive and fail closed
# ---------------------------------------------------------------------------


def test_an_extra_check_can_add_a_failure_reason(verifier: Verifier, tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)

    outcome = verifier.evaluate_detailed(
        "M4",
        "M4",
        evidence,
        extra_checks=[lambda: ["challenge_response_mismatch"]],
        run_dir=tmp_path,
    )

    assert outcome.result.passed is False
    assert "challenge_response_mismatch" in outcome.result.failure_reasons
    assert outcome.code == ErrorCode.VERIFICATION_FAILED


def test_an_extra_check_cannot_remove_a_builtin_reason(verifier: Verifier, tmp_path: Path) -> None:
    evidence = EvidenceSet(run_id="exp-001")  # nothing collected at all

    outcome = verifier.evaluate_detailed(
        "M4", "M4", evidence, extra_checks=[lambda: []], run_dir=tmp_path
    )

    assert outcome.result.passed is False
    assert outcome.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert outcome.missing


def test_a_raising_extra_check_fails_closed(verifier: Verifier, tmp_path: Path) -> None:
    # A crashed check must never be read as a pass (PRD 7.4).
    def explode() -> list[str]:
        raise RuntimeError("collector died")

    evidence = _complete_evidence(tmp_path)

    outcome = verifier.evaluate_detailed("M4", "M4", evidence, extra_checks=[explode], run_dir=tmp_path)

    assert outcome.result.passed is False
    assert "check_error:0:RuntimeError" in outcome.result.failure_reasons


def test_an_extra_check_returning_none_is_not_a_failure(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path)

    result = verifier.evaluate("M4", "M4", evidence, extra_checks=[lambda: None], run_dir=tmp_path)

    assert result.passed is True


def test_a_bound_extra_check_is_labelled_in_the_report(verifier: Verifier, tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)

    outcome = verifier.evaluate_detailed(
        "M4",
        "M4",
        evidence,
        extra_checks=[("guest.panic_absent", lambda: ["guest_panic_in_serial"])],
        run_dir=tmp_path,
    )

    extra_row = next(row for row in outcome.conditions if row["source"] == "extra")
    assert extra_row["condition_id"] == "guest.panic_absent"
    assert extra_row["passed"] is False
    assert extra_row["failure_reasons"] == ["guest_panic_in_serial"]


def test_a_malformed_extra_check_is_a_usage_error(verifier: Verifier, tmp_path: Path) -> None:
    evidence = _complete_evidence(tmp_path)

    with pytest.raises(UniPadError) as caught:
        verifier.evaluate_detailed("M4", "M4", evidence, extra_checks=[("only-a-name",)], run_dir=tmp_path)

    assert caught.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# PRD 7.4: the verifier's identity and its independence
# ---------------------------------------------------------------------------


def test_every_result_is_stamped_with_the_verifier_identity(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path)

    result = verifier.evaluate("M4", "M4", evidence, checked_at="2026-10-22T00:00:00+00:00")

    assert result.verifier_id == f"{VERIFIER_ID}@{VERIFIER_VERSION}"
    assert result.checked_at == "2026-10-22T00:00:00+00:00"


def test_the_outcome_carries_the_same_stamp_and_the_condition_rows(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path)

    outcome = verifier.evaluate_detailed(
        "M4", "M4", evidence, run_dir=tmp_path, checked_at="2026-10-22T00:00:00+00:00"
    )

    assert outcome.gate_id == "M4"
    assert outcome.result.verifier_id == verifier.verifier_id
    assert outcome.result.checked_at == "2026-10-22T00:00:00+00:00"
    assert {row["condition_id"] for row in outcome.conditions} >= {
        "evidence.complete",
        "evidence.files_exist",
    }
    assert all(row["passed"] for row in outcome.conditions)


def test_verification_json_is_written_for_a_run_directory(
    verifier: Verifier, tmp_path: Path
) -> None:
    evidence = _complete_evidence(tmp_path)

    outcome = verifier.evaluate_detailed("M4", "M4", evidence, run_dir=tmp_path)

    document = json.loads((tmp_path / "verification.json").read_text(encoding="utf-8"))
    assert outcome.verification_path == tmp_path / "verification.json"
    assert document["gate_id"] == "M4"
    assert document["verifier_version"] == VERIFIER_VERSION
    assert document["passed"] is True
    assert document["error_code"] is None
    # The evidence set is kept self-consistent for the next run.
    assert evidence.verification_json == "verification.json"


def test_an_unknown_gate_is_a_usage_error(verifier: Verifier, tmp_path: Path) -> None:
    # A typo must never be evaluated against a weaker default set.
    evidence = _complete_evidence(tmp_path)

    with pytest.raises(UniPadError) as caught:
        verifier.evaluate_detailed("M99", "M99", evidence, run_dir=tmp_path)

    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert caught.value.details["gate_id"] == "M99"


def test_the_condition_registry_covers_every_milestone(verifier: Verifier) -> None:
    gates = verifier.known_gates()

    assert "ipa-mvp" in gates
    for milestone in ("M0", "M4", "M9", "M9a", "M9b", "M10", "M10a", "M11", "M12", "M13", "M14", "M15"):
        assert milestone in gates
    # The default group is not itself a gate.
    assert "default" not in gates


def test_describe_gate_reports_the_conditions_as_data(verifier: Verifier) -> None:
    payload = describe_gate("M10")

    assert payload["gate_id"] == "M10"
    assert payload["verifier_id"] == VERIFIER_ID
    ids = [condition["condition_id"] for condition in payload["conditions"]]
    # A condition is a named rule: it appears once, even though the default group
    # also registers it.
    assert len(ids) == len(set(ids))
    assert "evidence.complete" in ids
    assert "install.stage_terminal" in ids
    assert tuple(iter_condition_ids("M10")) == tuple(ids)
