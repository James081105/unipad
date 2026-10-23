"""Unit tests for the shared data models (PRD 7.3, 9.2, 9, 8.1, 9.1, 6).

Mapping
-------
* PRD 7.3 -- ``ExperimentRecord`` is the experiment journal format *verbatim*:
  exactly the documented key set, round-trippable through JSON.
* PRD 9.2 -- ``EvidenceSet.check_complete`` names exactly the missing items of
  the minimum runtime evidence set; a non-empty list is never a pass.
* PRD 9 -- ``MachineConfig`` defaults to explicit TCG on the Intel host.
* PRD 9.1 -- ``InstallResult.ok`` is true only for a terminal successful stage
  with no error code; a file transfer alone is never ``installed``.
* PRD 6 / 7.4 -- ``GateResult`` is the machine-readable gate outcome.
* PRD 8.1 -- ``ArtifactManifest`` / ``ArtifactEntry`` build output contract.

Only observable behaviour is asserted: dict key sets, return values, raised
codes and the bytes written to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

#: The shipped machine definition, read from disk rather than hand-written, so
#: a key added to the file without being added to the model fails a test here
#: instead of silently changing the machine an operator thinks they configured.
MACHINE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "machine.json"

from unipad.models import (
    IPA_MVP_GATES,
    M13,
    M15,
    ArtifactEntry,
    ArtifactManifest,
    EvidenceSet,
    ExperimentRecord,
    ExperimentStatus,
    GateResult,
    InstallResult,
    MachineConfig,
    read_json,
    write_json,
)

#: PRD 7.3 key set, verbatim from the journal format block.
PRD_7_3_KEYS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "milestone",
        "status",
        "base_commit",
        "candidate_commit",
        "hypothesis",
        "changes",
        "source_lock_sha256",
        "build",
        "runtime",
        "evidence",
        "result",
        "next_action",
    }
)


# ---------------------------------------------------------------------------
# ExperimentRecord (PRD 7.3)
# ---------------------------------------------------------------------------
def test_experiment_record_defaults_match_prd_7_3() -> None:
    # Arrange / act.
    record = ExperimentRecord(experiment_id="exp-001", milestone="M4")

    # Assert: the journal starts planned, with the documented nested defaults.
    assert record.status == ExperimentStatus.PLANNED
    assert record.schema_version == 1
    assert record.changes == []
    assert record.evidence == []
    assert record.build == {"run_id": None, "artifact_manifest_sha256": None}
    assert record.runtime == {"host_inventory": None, "qemu_version": None, "accel": "tcg"}


def test_experiment_record_to_dict_uses_exact_prd_7_3_key_set() -> None:
    # Arrange.
    record = ExperimentRecord(experiment_id="exp-001", milestone="M4", hypothesis="falsifiable")

    # Act.
    payload = record.to_dict()

    # Assert: no extra keys, none missing -- the file stays diffable against the
    # PRD, so an added key is a contract change and must fail here.
    assert set(payload) == PRD_7_3_KEYS
    assert len(payload) == len(PRD_7_3_KEYS)


def test_experiment_record_json_round_trip() -> None:
    # Arrange: a fully populated record.
    original = ExperimentRecord(
        experiment_id="exp-002",
        milestone="M9b",
        status=ExperimentStatus.FAILED,
        hypothesis="a RAM-disk Data volume survives a normal reboot",
        base_commit="a" * 40,
        candidate_commit="b" * 40,
        changes=["patch: virtio-blk data volume"],
        source_lock_sha256="c" * 64,
        build={"run_id": "12345", "artifact_manifest_sha256": "d" * 64},
        runtime={"host_inventory": "host-inventory.json", "qemu_version": "9.2.0", "accel": "tcg"},
        evidence=["serial.log", "screenshots/boot.png"],
        result="H04 not met: marker lost",
        next_action="implement a persistent Data volume",
    )

    # Act.
    restored = ExperimentRecord.from_dict(json.loads(original.to_json()))

    # Assert: every documented key survives the round trip unchanged.
    assert restored.to_dict() == original.to_dict()
    assert restored.experiment_id == "exp-002"
    assert restored.milestone == "M9b"
    assert restored.status == ExperimentStatus.FAILED
    assert restored.changes == ["patch: virtio-blk data volume"]
    assert restored.evidence == ["serial.log", "screenshots/boot.png"]
    assert restored.runtime["accel"] == "tcg"
    assert restored.result == "H04 not met: marker lost"


def test_experiment_record_from_dict_ignores_unknown_keys() -> None:
    # Arrange: a journal written by a newer controller.
    raw = {
        "experiment_id": "exp-003",
        "milestone": "M10",
        "status": "planned",
        "hypothesis": "x",
        "future_key": {"ignored": True},
    }

    # Act.
    record = ExperimentRecord.from_dict(raw)

    # Assert: forward compatibility without silently widening the contract.
    assert record.experiment_id == "exp-003"
    assert not hasattr(record, "future_key")


def test_experiment_record_from_dict_requires_mandatory_fields() -> None:
    # Arrange: a journal missing the caller-assigned id.
    raw = {"milestone": "M4", "hypothesis": "x"}

    # Act / assert: PRD 7.3 -- experiment_id and milestone are mandatory.
    with pytest.raises(TypeError):
        ExperimentRecord.from_dict(raw)


# ---------------------------------------------------------------------------
# EvidenceSet (PRD 9.2)
# ---------------------------------------------------------------------------
def test_evidence_set_required_items_are_the_prd_9_2_minimum() -> None:
    # PRD 9.2 minimum runtime evidence set.
    assert set(EvidenceSet.REQUIRED) == {
        "experiment_json",
        "host_inventory",
        "artifact_manifest",
        "qemu_args",
        "serial_log",
        "verification_json",
    }


def test_check_complete_lists_exactly_the_missing_required_items() -> None:
    # Arrange: only two required items recorded.
    evidence = EvidenceSet(run_id="run-1", experiment_json="experiment.json", serial_log="serial.log")

    # Act.
    missing = evidence.check_complete()

    # Assert: exactly the absent required names, in the documented order.
    assert missing == ["host_inventory", "artifact_manifest", "qemu_args", "verification_json"]
    assert evidence.missing == missing


def test_check_complete_is_empty_for_a_complete_set() -> None:
    # Arrange.
    evidence = EvidenceSet(
        run_id="run-1",
        experiment_json="experiment.json",
        host_inventory="host-inventory.json",
        artifact_manifest="artifact-manifest.json",
        qemu_args="qemu-args.txt",
        serial_log="serial.log",
        verification_json="verification.json",
    )

    # Act / assert.
    assert evidence.check_complete() == []
    assert evidence.missing == []


def test_check_complete_treats_blank_strings_as_missing() -> None:
    # Arrange: a path recorded as an empty string is not evidence (PRD 9.2).
    evidence = EvidenceSet(
        run_id="run-1",
        experiment_json="",
        host_inventory="host-inventory.json",
        artifact_manifest="artifact-manifest.json",
        qemu_args="qemu-args.txt",
        serial_log="serial.log",
        verification_json="verification.json",
    )

    # Act.
    missing = evidence.check_complete()

    # Assert.
    assert missing == ["experiment_json"]


def test_check_complete_ignores_optional_items() -> None:
    # Arrange: guest_log / qmp_log / gpu_bridge_log are valuable but optional.
    evidence = EvidenceSet(
        run_id="run-1",
        experiment_json="experiment.json",
        host_inventory="host-inventory.json",
        artifact_manifest="artifact-manifest.json",
        qemu_args="qemu-args.txt",
        serial_log="serial.log",
        verification_json="verification.json",
    )

    # Act / assert: their absence must not widen the missing list.
    assert evidence.check_complete() == []


# ---------------------------------------------------------------------------
# InstallResult (PRD 9.1)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stage", ["installed", "registered"])
def test_install_result_ok_for_successful_terminal_stages(stage: str) -> None:
    # PRD 9.1: only a completed install/registration is a success.
    result = InstallResult(package_sha256="a" * 64, bundle_id="com.unipad.hello", stage=stage)

    assert result.ok is True


@pytest.mark.parametrize("stage", ["received", "checked", "transferred", "failed"])
def test_install_result_not_ok_for_non_successful_stages(stage: str) -> None:
    # PRD 9.1: "文件传输成功不能返回 installed" -- transfer alone is not install.
    result = InstallResult(package_sha256="a" * 64, stage=stage)

    assert result.ok is False


def test_install_result_ok_is_false_when_an_error_code_is_set() -> None:
    # Arrange: a stage that looks terminal but carries a fault.
    result = InstallResult(
        package_sha256="a" * 64,
        stage="registered",
        error_code="registration_failed",
        error_message="icon never appeared",
    )

    # Act / assert.
    assert result.ok is False


def test_install_result_default_stage_is_received() -> None:
    # PRD 9.1: the furthest stage actually completed, defaulting to the weakest.
    assert InstallResult(package_sha256="a" * 64).stage == "received"
    assert InstallResult(package_sha256="a" * 64).ok is False


def test_install_result_to_dict_carries_the_prd_9_1_fields() -> None:
    # PRD 9.1: package hash, bundle id, stage, trust mode, error, log location.
    result = InstallResult(
        package_sha256="a" * 64,
        bundle_id="com.unipad.hello",
        stage="transferred",
        trust_mode=None,
        error_code="transfer_failed",
        error_message="hash mismatch",
        log_path="runs/exp-1/install.log",
    )

    payload = result.to_dict()

    assert payload["package_sha256"] == "a" * 64
    assert payload["bundle_id"] == "com.unipad.hello"
    assert payload["stage"] == "transferred"
    assert payload["trust_mode"] is None
    assert payload["error_code"] == "transfer_failed"
    assert payload["log_path"] == "runs/exp-1/install.log"


# ---------------------------------------------------------------------------
# GateResult (PRD 6 / 7.4)
# ---------------------------------------------------------------------------
def test_gate_result_json_shape() -> None:
    # Arrange.
    result = GateResult(
        milestone="M10",
        passed=False,
        checked_at="2026-10-22T00:00:00+00:00",
        evidence=["serial.log"],
        failure_reasons=["evidence_incomplete", "missing:verification_json"],
        verifier_id="unipad.controller.verifier@1.0.0",
    )

    # Act.
    payload = json.loads(result.to_json())

    # Assert: the PRD 6 gate outcome as one machine-readable document.
    assert payload["milestone"] == "M10"
    assert payload["passed"] is False
    assert payload["checked_at"] == "2026-10-22T00:00:00+00:00"
    assert payload["evidence"] == ["serial.log"]
    assert payload["failure_reasons"] == [
        "evidence_incomplete",
        "missing:verification_json",
    ]
    assert payload["verifier_id"] == "unipad.controller.verifier@1.0.0"
    assert set(payload) == {
        "milestone",
        "passed",
        "checked_at",
        "evidence",
        "failure_reasons",
        "verifier_id",
    }


def test_gate_result_defaults_to_failing_with_no_evidence() -> None:
    # Arrange / act: a gate result built without arguments.
    result = GateResult(milestone="M4", passed=True, checked_at="2026-10-22T00:00:00+00:00")

    # Assert: no silent evidence list, no silent pass attribution.
    assert result.evidence == []
    assert result.failure_reasons == []
    assert result.verifier_id == ""


# ---------------------------------------------------------------------------
# MachineConfig (PRD 9)
# ---------------------------------------------------------------------------
def test_machine_config_defaults_to_tcg() -> None:
    # PRD 2.2-1 / 9: Intel host means explicit TCG, never WHPX/HVF.
    config = MachineConfig()

    assert config.accel == "tcg"
    assert config.to_dict()["accel"] == "tcg"


def test_machine_config_round_trip(tmp_path: Path) -> None:
    # Arrange.
    original = MachineConfig(name="ipad-tcg", cores=2, memory_mb=2048)

    # Act.
    restored = MachineConfig.from_dict(json.loads(json.dumps(original.to_dict())))

    # Assert.
    assert restored.to_dict() == original.to_dict()


def test_machine_config_from_dict_ignores_unknown_keys() -> None:
    # Arrange: config/machine.json carries documentation-only keys.
    raw = {"name": "ipad-tcg", "accel": "tcg", "accel_note": "TCG is mandatory"}

    # Act.
    config = MachineConfig.from_dict(raw)

    # Assert.
    assert config.accel == "tcg"
    assert not hasattr(config, "accel_note")


def test_machine_config_unknown_keys_reports_exactly_what_it_dropped() -> None:
    """``from_dict`` dropping a key silently is only safe if someone can ask.

    The lenient behaviour is right for a caller holding a superset dict.  It is
    wrong for the caller reading ``config/machine.json``: there, a dropped key is
    a typo or a feature the operator believes is configured and is not, and the
    file would then disagree with the machine it describes.  ``unknown_keys`` is
    the question that caller must ask before trusting the file.
    """
    # Arrange: a misspelling of a real key and an invented one, deliberately out
    # of alphabetical order so the sort is observable rather than incidental.
    raw = {"zeta_typo": 1, "name": "ipad-tcg", "cpu_modle": "cortex-a710"}

    # Act.
    dropped = MachineConfig.unknown_keys(raw)

    # Assert: sorted, complete, and nothing else.
    assert dropped == ("cpu_modle", "zeta_typo")


def test_machine_config_unknown_keys_tolerates_documentation_and_declared_sections() -> None:
    """The tolerance is a decision, not an accident.

    ``*_note`` keys are prose explaining a modelled field and ``NON_MODELLED_KEYS``
    are real sections the controller reads directly.  Both must pass, because
    otherwise the shipped ``config/machine.json`` could never be validated and
    ``cpu_modle`` would be indistinguishable from ``cpu_model_note``.
    """
    # Arrange: every tolerated form, plus one modelled key.
    raw = {
        "name": "ipad-tcg",
        "cpu_model_note": "prose",
        "memory_note": "prose",
        "schema_version": 1,
        "disks": {"artifact_root": "F:/iosvirtual/artifacts"},
    }

    # Act / Assert.
    assert MachineConfig.unknown_keys(raw) == ()


def test_machine_config_unknown_keys_is_empty_for_the_shipped_config() -> None:
    """The shipped file must pass its own validation.

    Read from disk rather than hand-written, so a key added to the file without
    being added to the model -- or added to the model without being declared in
    ``NON_MODELLED_KEYS`` -- fails here instead of silently changing the machine
    an operator believes they configured.
    """
    # Arrange.
    raw = json.loads(MACHINE_CONFIG_PATH.read_text(encoding="utf-8"))

    # Act / Assert.
    assert MachineConfig.unknown_keys(raw) == (), (
        "config/machine.json has keys MachineConfig does not model and does not "
        "declare; either the model, NON_MODELLED_KEYS, or the file is wrong"
    )


# ---------------------------------------------------------------------------
# ArtifactManifest (PRD 8.1)
# ---------------------------------------------------------------------------
def test_artifact_manifest_lookup_and_dict_shape() -> None:
    # Arrange.
    manifest = ArtifactManifest(
        run_id="42",
        target="tcg",
        artifacts=[
            ArtifactEntry(name="kernelcache", sha256="e" * 64, size_bytes=1024, arch="aarch64"),
            ArtifactEntry(name="devicetree", sha256="f" * 64, size_bytes=64, role="devicetree"),
        ],
    )

    # Act.
    payload = manifest.to_dict()

    # Assert: PRD 8.1 -- each deliverable is hash- and size-identified.
    assert payload["run_id"] == "42"
    assert payload["target"] == "tcg"
    assert payload["artifacts"][0] == {
        "name": "kernelcache",
        "sha256": "e" * 64,
        "size_bytes": 1024,
        "arch": "aarch64",
        "role": None,
        "path": None,
    }
    found = manifest.by_name("kernelcache")
    assert found is not None
    assert found.arch == "aarch64"
    assert found.size_bytes == 1024
    assert manifest.by_name("absent") is None


def test_artifact_manifest_defaults_to_tcg_target() -> None:
    # PRD 8.3: an HVF payload must never be the default for this host.
    assert ArtifactManifest(run_id=None).target == "tcg"


# ---------------------------------------------------------------------------
# read_json / write_json
# ---------------------------------------------------------------------------
def test_write_then_read_json_round_trip(tmp_path: Path) -> None:
    # Arrange.
    target = tmp_path / "nested" / "host-inventory.json"
    payload = {"schema_version": 1, "cpu": {"model": "Intel i5", "cores": 4}, "notes": []}

    # Act.
    write_json(target, payload)

    # Assert: the file is valid JSON on disk and reads back identically.
    assert target.is_file()
    assert read_json(target) == payload


def test_write_json_creates_parent_directories(tmp_path: Path) -> None:
    # Arrange.
    target = tmp_path / "a" / "b" / "c.json"

    # Act.
    write_json(target, {"ok": True})

    # Assert.
    assert read_json(target) == {"ok": True}


def test_write_json_output_is_sorted_and_newline_terminated(tmp_path: Path) -> None:
    # Deterministic output so two runs produce a diffable file (testing-strategy D3).
    target = tmp_path / "out.json"

    write_json(target, {"b": 1, "a": 2})

    text = target.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.index('"a"') < text.index('"b"')


def test_write_json_fsyncs_the_file_before_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bytes reach the platter before the target is swapped in.

    callchain-5: a crash while writing a run record must not leave a truncated
    file behind.  The guarantee comes from fsyncing the temporary file *before*
    the rename, so this asserts the fsync actually happens.  A plain
    ``write_text`` never fsyncs, so reverting to it makes this fail.
    """
    import os

    synced: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    target = tmp_path / "out.json"

    write_json(target, {"a": 1})

    assert target.is_file()
    assert synced, "write_json must fsync so the file's contents are durable before the rename"


def test_write_json_a_failed_rename_leaves_the_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicity: a write that cannot be swapped in changes nothing on disk.

    The rename is the commit point.  If it fails, the previous file must be
    exactly as it was and no partial temporary file may survive.  A direct
    ``write_text`` has no rename to fail, so forcing :func:`os.replace` to raise
    leaves it happily overwriting the target -- reverting to it makes this fail
    at ``pytest.raises`` and again on the unchanged-content assertion.
    """
    import os

    target = tmp_path / "out.json"
    write_json(target, {"version": 1})
    original = target.read_text(encoding="utf-8")

    def exploding_replace(*args: object, **kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", exploding_replace)

    with pytest.raises(OSError):
        write_json(target, {"version": 2})

    assert target.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.tmp")) == [], "a failed write must not leave a temp file behind"


def test_write_json_leaves_no_temp_file_behind_on_success(tmp_path: Path) -> None:
    # The temporary file is renamed onto the target, so nothing lingers.
    target = tmp_path / "nested" / "out.json"

    write_json(target, {"a": 1})

    assert target.is_file()
    assert list((tmp_path / "nested").glob("*.tmp")) == []
    assert list((tmp_path / "nested").glob("out.json.*")) == []


# ---------------------------------------------------------------------------
# milestone vocabulary
# ---------------------------------------------------------------------------
def test_ipa_mvp_gates_cover_the_prd_6_release_gate() -> None:
    # PRD 6: "IPA MVP 的发布门禁包含 M0–M12 及附加关卡".
    assert set(IPA_MVP_GATES) == {
        "M0",
        "M1",
        "M2",
        "M3",
        "M4",
        "M5",
        "M6",
        "M7",
        "M8",
        "M9",
        "M9a",
        "M9b",
        "M10",
        "M10a",
        "M11",
        "M12",
    }
    assert M13 not in IPA_MVP_GATES
    assert M15 not in IPA_MVP_GATES


def test_experiment_status_vocabulary() -> None:
    # The controller state machine statuses used by the journal.
    assert {
        ExperimentStatus.PLANNED,
        ExperimentStatus.RUNNING,
        ExperimentStatus.PASSED,
        ExperimentStatus.FAILED,
        ExperimentStatus.BLOCKED,
    } == {"planned", "running", "passed", "failed", "blocked"}
