"""Controller experiment journal tests (PRD sections 7.2 and 7.3).

Mapping
-------
* **PRD 7.3** -- ``runs/<experiment-id>/experiment.json`` is written with
  *exactly* the documented field set (no extra keys, so the file stays diffable
  against the PRD) and the documented defaults for ``status``/``build``/
  ``runtime``/``schema_version``.
* **PRD 7.3** -- one *falsifiable* hypothesis: an empty hypothesis is a usage
  error, because "do something and see" is not an experiment.
* **PRD 7.2 "实验失败时保留修改和日志"** -- a finished record is never
  overwritten; status moves and additive appends go through the sanctioned
  paths, which re-read from disk so a stale copy cannot truncate ``changes`` or
  ``evidence``.
* **PRD 9.2** -- recording an evidence path that is not on disk is
  ``artifact_missing``, so missing evidence cannot be laundered into "verified".

Everything writes into ``tmp_path``; the repository working tree is never
touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unipad.controller.experiments import (
    EXPERIMENT_FILENAME,
    ID_PATTERN,
    ExperimentStore,
)
from unipad.errors import ErrorCode, UniPadError
from unipad.models import ExperimentRecord, ExperimentStatus

#: The PRD 7.3 journal format, verbatim.
PRD_7_3_KEYS: frozenset[str] = frozenset(
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

HYPOTHESIS = "a fixed TCG CPU model boots UniXNU past DarwinBoot (M4)"


def _store(tmp_path: Path) -> ExperimentStore:
    """Return a store rooted in ``tmp_path/runs``."""
    return ExperimentStore(tmp_path / "runs")


def _create(store: ExperimentStore, experiment_id: str = "exp-001") -> ExperimentRecord:
    return store.create(
        experiment_id,
        "M4",
        HYPOTHESIS,
        base_commit="a" * 40,
        source_lock_sha256="b" * 64,
    )


def _journal(tmp_path: Path, experiment_id: str = "exp-001") -> dict:
    return json.loads((tmp_path / "runs" / experiment_id / EXPERIMENT_FILENAME).read_text("utf-8"))


# ---------------------------------------------------------------------------
# PRD 7.3: create writes the documented record
# ---------------------------------------------------------------------------


def test_create_writes_experiment_json_with_the_exact_prd_7_3_field_set(tmp_path: Path) -> None:
    store = _store(tmp_path)

    record = _create(store)
    journal_path = tmp_path / "runs" / "exp-001" / EXPERIMENT_FILENAME

    assert journal_path.is_file()
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    # The field set is exactly the PRD 7.3 format: nothing added, nothing lost.
    assert set(payload) == PRD_7_3_KEYS
    assert payload["schema_version"] == 1
    assert payload["experiment_id"] == "exp-001"
    assert payload["milestone"] == "M4"
    assert payload["status"] == ExperimentStatus.PLANNED
    assert payload["hypothesis"] == HYPOTHESIS
    assert payload["base_commit"] == "a" * 40
    assert payload["candidate_commit"] is None
    assert payload["source_lock_sha256"] == "b" * 64
    assert payload["changes"] == []
    assert payload["evidence"] == []
    assert payload["result"] is None
    assert payload["next_action"] is None
    # PRD 7.3 defaults for the two nested blocks.
    assert payload["build"] == {"run_id": None, "artifact_manifest_sha256": None}
    assert payload["runtime"] == {"host_inventory": None, "qemu_version": None, "accel": "tcg"}
    assert record.to_dict() == payload


def test_create_refuses_an_existing_id_so_the_first_journal_survives(tmp_path: Path) -> None:
    # PRD 7.2: a second create would discard the first experiment's evidence.
    store = _store(tmp_path)
    _create(store)

    with pytest.raises(UniPadError) as caught:
        store.create("exp-001", "M4", "a different hypothesis")

    assert caught.value.code == ErrorCode.BLOCKED
    assert caught.value.details["experiment_id"] == "exp-001"
    assert _journal(tmp_path)["hypothesis"] == HYPOTHESIS


@pytest.mark.parametrize(
    ("milestone", "hypothesis"),
    [("", HYPOTHESIS), ("   ", HYPOTHESIS), ("M4", ""), ("M4", "   ")],
)
def test_create_requires_a_milestone_and_a_falsifiable_hypothesis(
    tmp_path: Path, milestone: str, hypothesis: str
) -> None:
    # PRD 7.3: an unstated milestone or an empty hypothesis is a usage error.
    store = _store(tmp_path)

    with pytest.raises(UniPadError) as caught:
        store.create("exp-bad", milestone, hypothesis)

    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert not store.exists("exp-bad")


@pytest.mark.parametrize("bad_id", ["", "..", ".", "../escape", "a/b", "a\\b", "-leading", "x" * 129])
def test_create_rejects_unusable_ids(tmp_path: Path, bad_id: str) -> None:
    # Ids become directory names, so traversal and separators are refused rather
    # than normalised (Windows-correct: both / and \\ are separators).
    store = _store(tmp_path)

    with pytest.raises(UniPadError) as caught:
        store.create(bad_id, "M4", HYPOTHESIS)

    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_the_id_pattern_accepts_the_documented_shape() -> None:
    assert ID_PATTERN.match("exp-001")
    assert ID_PATTERN.match("exp-2026-10-22.M4")
    assert not ID_PATTERN.match(" exp")


# ---------------------------------------------------------------------------
# PRD 7.2: evidence preservation
# ---------------------------------------------------------------------------


def test_save_refuses_to_overwrite_a_non_planned_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _create(store)
    store.set_status("exp-001", ExperimentStatus.FAILED, result="boot stopped at M2")

    with pytest.raises(UniPadError) as caught:
        store.save(record)

    assert caught.value.code == ErrorCode.BLOCKED
    assert caught.value.details["existing_status"] == ExperimentStatus.FAILED
    # The failure journal is still on disk, unmodified.
    assert _journal(tmp_path)["result"] == "boot stopped at M2"


@pytest.mark.parametrize(
    "status",
    [ExperimentStatus.RUNNING, ExperimentStatus.PASSED, ExperimentStatus.FAILED, ExperimentStatus.BLOCKED],
)
def test_save_refuses_every_non_planned_status(tmp_path: Path, status: str) -> None:
    store = _store(tmp_path)
    record = _create(store)
    store.set_status("exp-001", status)

    with pytest.raises(UniPadError) as caught:
        store.save(record)

    assert caught.value.code == ErrorCode.BLOCKED
    assert caught.value.details["existing_status"] == status


def test_append_change_accumulates_and_survives_a_full_record_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.set_status("exp-001", ExperimentStatus.FAILED)

    store.append_change("exp-001", "pin TCG cpu model to cortex-a57")
    store.append_change("exp-001", "disable the second NIC")

    assert _journal(tmp_path)["changes"] == [
        "pin TCG cpu model to cortex-a57",
        "disable the second NIC",
    ]


def test_append_change_requires_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    with pytest.raises(UniPadError) as caught:
        store.append_change("exp-001", "   ")

    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_append_evidence_records_the_path_and_stores_it_relative(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    serial = tmp_path / "runs" / "exp-001" / "serial.log"
    serial.write_text("M4: XNU kernel entered\n", encoding="utf-8")

    store.append_evidence("exp-001", serial)

    assert _journal(tmp_path)["evidence"] == ["serial.log"]


def test_append_evidence_refuses_a_path_that_is_not_on_disk(tmp_path: Path) -> None:
    # PRD 9.2: a journal that references a missing file is not evidence.
    store = _store(tmp_path)
    _create(store)

    with pytest.raises(UniPadError) as caught:
        store.append_evidence("exp-001", "screenshots/springboard.png")

    assert caught.value.code == ErrorCode.ARTIFACT_MISSING
    assert _journal(tmp_path)["evidence"] == []


def test_set_status_moves_the_record_and_can_set_result_and_next_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.append_change("exp-001", "pin the cpu model")

    record = store.set_status(
        "exp-001",
        ExperimentStatus.FAILED,
        result="no XNU banner on serial",
        next_action="probe the GIC configuration",
    )

    assert record.status == ExperimentStatus.FAILED
    assert record.result == "no XNU banner on serial"
    assert record.next_action == "probe the GIC configuration"
    # The sanctioned status path never truncates the accumulated changes.
    assert _journal(tmp_path)["changes"] == ["pin the cpu model"]


def test_set_status_rejects_an_unknown_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    with pytest.raises(UniPadError) as caught:
        store.set_status("exp-001", "succeeded")

    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert _journal(tmp_path)["status"] == ExperimentStatus.PLANNED


def test_a_missing_id_is_a_blocked_journal_not_a_silent_new_one(tmp_path: Path) -> None:
    store = _store(tmp_path)

    for call in (
        lambda: store.load("exp-missing"),
        lambda: store.append_change("exp-missing", "text"),
        lambda: store.set_status("exp-missing", ExperimentStatus.RUNNING),
    ):
        with pytest.raises(UniPadError) as caught:
            call()
        assert caught.value.code == ErrorCode.BLOCKED
        assert caught.value.details["experiment_id"] == "exp-missing"

    assert store.exists("exp-missing") is False


def test_list_and_scan_report_every_readable_journal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "exp-001")
    store.create("exp-002", "M6", "virtio-blk is readable from the guest")
    broken = tmp_path / "runs" / "exp-broken"
    broken.mkdir(parents=True)
    (broken / EXPERIMENT_FILENAME).write_text("{ not json", encoding="utf-8")

    scan = store.scan()

    assert [record.experiment_id for record in scan.records] == ["exp-001", "exp-002"]
    # A broken journal is surfaced, never silently dropped from the list.
    assert [entry["experiment_id"] for entry in scan.errors] == ["exp-broken"]
    assert scan.errors[0]["code"] == ErrorCode.CONFIG_INVALID
