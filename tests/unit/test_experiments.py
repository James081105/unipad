"""Unit tests for the experiment journal store (PRD 7.2 and 7.3).

Mapping
-------
* PRD 7.3 -- every experiment is one directory holding ``experiment.json`` in
  exactly the documented key set.
* PRD 7.2 "实验失败时保留修改和日志" -- ``save`` refuses to overwrite a record
  whose status is no longer ``planned``; the sanctioned additive paths
  (:meth:`append_change`, :meth:`append_evidence`, :meth:`set_status`) re-read
  the record from disk so a stale copy cannot truncate it.
* PRD 9.2 -- recording an evidence path that is not on disk is refused, so a
  journal cannot claim evidence it does not have.
* PRD appendix A -- a missing record is a ``blocked`` controller state with a
  concrete reason, not an invented experiment.

All tests use ``tmp_path`` for the store root: nothing is written into the
repository working tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unipad.controller.experiments import EXPERIMENT_FILENAME, ID_PATTERN, ExperimentStore
from unipad.errors import ErrorCode, UniPadError
from unipad.models import ExperimentRecord, ExperimentStatus

#: PRD 7.3 key set, verbatim.
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


def _store(tmp_path: Path) -> ExperimentStore:
    """Return a store rooted in ``tmp_path/runs``."""
    return ExperimentStore(tmp_path / "runs")


def _create(store: ExperimentStore, experiment_id: str = "exp-001") -> ExperimentRecord:
    return store.create(
        experiment_id,
        "M4",
        "a fixed TCG CPU model boots UniXNU past DarwinBoot",
        base_commit="a" * 40,
        source_lock_sha256="b" * 64,
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
def test_create_writes_experiment_json_with_the_exact_prd_7_3_key_set(tmp_path: Path) -> None:
    # Arrange.
    store = _store(tmp_path)

    # Act.
    record = _create(store)

    # Assert: PRD 7.3 -- the file exists at runs/<id>/experiment.json with
    # exactly the documented keys and no extras.
    journal = tmp_path / "runs" / "exp-001" / EXPERIMENT_FILENAME
    assert journal.is_file()
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert set(payload) == PRD_7_3_KEYS
    assert payload["experiment_id"] == "exp-001"
    assert payload["milestone"] == "M4"
    assert payload["status"] == ExperimentStatus.PLANNED
    assert payload["schema_version"] == 1
    assert payload["hypothesis"] == record.hypothesis
    assert payload["base_commit"] == "a" * 40
    assert payload["source_lock_sha256"] == "b" * 64
    assert payload["runtime"]["accel"] == "tcg"


def test_create_returns_the_record_it_persisted(tmp_path: Path) -> None:
    # Arrange / act.
    store = _store(tmp_path)
    created = _create(store)
    loaded = store.load("exp-001")

    # Assert: in-memory and on-disk state agree, so a caller cannot act on a
    # record that differs from the journal.
    assert loaded.to_dict() == created.to_dict()


def test_create_refuses_an_existing_id_with_blocked(tmp_path: Path) -> None:
    # PRD 7.2: a second create would discard the first experiment's evidence.
    store = _store(tmp_path)
    _create(store)

    with pytest.raises(UniPadError) as caught:
        _create(store, "exp-001")

    assert caught.value.code == ErrorCode.BLOCKED
    assert caught.value.details["experiment_id"] == "exp-001"
    # The original journal is untouched.
    assert store.load("exp-001").hypothesis.startswith("a fixed TCG")


def test_create_requires_a_falsifiable_hypothesis(tmp_path: Path) -> None:
    # PRD 7.3: one falsifiable hypothesis.
    store = _store(tmp_path)

    for hypothesis in ("", "   "):
        with pytest.raises(UniPadError) as caught:
            store.create("exp-blank", "M4", hypothesis)
        assert caught.value.code == ErrorCode.CONFIG_INVALID

    assert not store.exists("exp-blank")


def test_create_requires_a_milestone(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(UniPadError) as caught:
        store.create("exp-nomilestone", "", "a hypothesis")

    assert caught.value.code == ErrorCode.CONFIG_INVALID


@pytest.mark.parametrize("bad_id", ["", "..", ".", "a/b", "../escape", "with space", "x" * 200])
def test_create_rejects_unusable_ids(tmp_path: Path, bad_id: str) -> None:
    # Ids become directory names, so traversal and separator forms are refused
    # rather than normalised (Windows-correct).
    store = _store(tmp_path)

    with pytest.raises(UniPadError) as caught:
        store.create(bad_id, "M4", "a hypothesis")

    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_id_pattern_accepts_documented_id_shape() -> None:
    # PRD 7.3: ``exp-<unique-id>``.
    assert ID_PATTERN.match("exp-0001")
    assert ID_PATTERN.match("exp-M4.tcg_1")


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------
def test_missing_record_gives_blocked(tmp_path: Path) -> None:
    # PRD appendix A: a missing journal is a blocked controller state.
    store = _store(tmp_path)

    with pytest.raises(UniPadError) as caught:
        store.load("exp-does-not-exist")

    assert caught.value.code == ErrorCode.BLOCKED
    assert caught.value.details["experiment_id"] == "exp-does-not-exist"


def test_malformed_journal_gives_config_invalid(tmp_path: Path) -> None:
    # Arrange: a journal that is not JSON.
    store = _store(tmp_path)
    journal = tmp_path / "runs" / "exp-broken" / EXPERIMENT_FILENAME
    journal.parent.mkdir(parents=True)
    journal.write_text("{not json", encoding="utf-8")

    # Act / assert.
    with pytest.raises(UniPadError) as caught:
        store.load("exp-broken")
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_journal_missing_mandatory_fields_gives_config_invalid(tmp_path: Path) -> None:
    # Arrange: a JSON object without experiment_id / milestone.
    store = _store(tmp_path)
    journal = tmp_path / "runs" / "exp-thin" / EXPERIMENT_FILENAME
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"status": "planned"}), encoding="utf-8")

    # Act / assert.
    with pytest.raises(UniPadError) as caught:
        store.load("exp-thin")
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_exists_reports_the_journal_on_disk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.exists("exp-001") is False
    _create(store)
    assert store.exists("exp-001") is True


# ---------------------------------------------------------------------------
# save and evidence preservation (PRD 7.2)
# ---------------------------------------------------------------------------
def test_save_updates_a_planned_record(tmp_path: Path) -> None:
    # Arrange.
    store = _store(tmp_path)
    record = _create(store)

    # Act: a planned record is still mutable.
    record.next_action = "probe the GIC"
    store.save(record)

    # Assert.
    assert store.load("exp-001").next_action == "probe the GIC"


def test_save_refuses_to_overwrite_a_non_planned_record(tmp_path: Path) -> None:
    # PRD 7.2: "实验失败时保留修改和日志" -- a finished record survives a
    # careless full-record rewrite.
    store = _store(tmp_path)
    record = _create(store)
    store.set_status("exp-001", ExperimentStatus.RUNNING, result="boot hung at DarwinBoot")

    # Act: a stale in-memory copy tries to write the old status back.
    with pytest.raises(UniPadError) as caught:
        store.save(record)

    # Assert: BLOCKED, and the journal on disk is unchanged.
    assert caught.value.code == ErrorCode.BLOCKED
    assert caught.value.details["existing_status"] == ExperimentStatus.RUNNING
    on_disk = store.load("exp-001")
    assert on_disk.status == ExperimentStatus.RUNNING
    assert on_disk.result == "boot hung at DarwinBoot"


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


def test_save_rejects_a_non_record_and_an_unknown_status(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(UniPadError) as caught:
        store.save({"experiment_id": "exp-001"})  # type: ignore[arg-type]
    assert caught.value.code == ErrorCode.CONFIG_INVALID

    record = ExperimentRecord(experiment_id="exp-bad", milestone="M4", status="not-a-status")
    with pytest.raises(UniPadError) as caught:
        store.save(record)
    assert caught.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# additive updates
# ---------------------------------------------------------------------------
def test_append_change_accumulates(tmp_path: Path) -> None:
    # Arrange.
    store = _store(tmp_path)
    _create(store)

    # Act.
    store.append_change("exp-001", "patch: pin cortex-a57")
    record = store.append_change("exp-001", "patch: raise GIC to v2")

    # Assert.
    assert record.changes == ["patch: pin cortex-a57", "patch: raise GIC to v2"]
    assert store.load("exp-001").changes == record.changes


def test_append_change_is_allowed_on_a_failed_record(tmp_path: Path) -> None:
    # PRD 7.2: the failure journal keeps accumulating its changes.
    store = _store(tmp_path)
    _create(store)
    store.set_status("exp-001", ExperimentStatus.FAILED)

    record = store.append_change("exp-001", "post-mortem: keep the serial log")

    assert record.changes == ["post-mortem: keep the serial log"]
    assert record.status == ExperimentStatus.FAILED


def test_append_change_requires_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    with pytest.raises(UniPadError) as caught:
        store.append_change("exp-001", "   ")
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_append_evidence_accumulates_and_stores_relative_paths(tmp_path: Path) -> None:
    # Arrange.
    store = _store(tmp_path)
    _create(store)
    run_dir = tmp_path / "runs" / "exp-001"
    (run_dir / "serial.log").write_text("DarwinBoot handoff\n", encoding="utf-8")
    (run_dir / "qemu-args.txt").write_text("-accel tcg\n", encoding="utf-8")

    # Act.
    store.append_evidence("exp-001", "serial.log")
    record = store.append_evidence("exp-001", run_dir / "qemu-args.txt")

    # Assert: the journal records run-relative POSIX paths, not host paths.
    assert record.evidence == ["serial.log", "qemu-args.txt"]
    assert store.load("exp-001").evidence == record.evidence


def test_append_evidence_refuses_a_path_that_is_not_on_disk(tmp_path: Path) -> None:
    # PRD 9.2: a journal cannot claim evidence that does not exist.
    store = _store(tmp_path)
    _create(store)

    with pytest.raises(UniPadError) as caught:
        store.append_evidence("exp-001", "screenshots/boot.png")

    assert caught.value.code == ErrorCode.ARTIFACT_MISSING
    assert store.load("exp-001").evidence == []


def test_append_evidence_can_record_a_planned_path(tmp_path: Path) -> None:
    # The explicit opt-out exists for a path the caller will create later.
    store = _store(tmp_path)
    _create(store)

    record = store.append_evidence("exp-001", "verification.json", require_exists=False)

    assert record.evidence == ["verification.json"]


def test_additive_updates_do_not_lose_earlier_entries(tmp_path: Path) -> None:
    # Arrange: a stale copy that would truncate the journal if it were written.
    store = _store(tmp_path)
    _create(store)
    store.append_change("exp-001", "patch: one")
    stale = store.load("exp-001")
    store.append_change("exp-001", "patch: two")

    # Act: the sanctioned paths re-read from disk.
    store.set_status("exp-001", ExperimentStatus.FAILED, result="no progress")
    record = store.load("exp-001")

    # Assert.
    assert record.changes == ["patch: one", "patch: two"]
    assert stale.changes == ["patch: one"]


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------
def test_set_status_moves_the_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    record = store.set_status("exp-001", ExperimentStatus.RUNNING)

    assert record.status == ExperimentStatus.RUNNING
    assert store.load("exp-001").status == ExperimentStatus.RUNNING


def test_set_status_only_overwrites_result_and_next_action_when_given(tmp_path: Path) -> None:
    # Arrange.
    store = _store(tmp_path)
    _create(store)
    store.set_status(
        "exp-001",
        ExperimentStatus.FAILED,
        result="H04 not met",
        next_action="implement a persistent Data volume",
    )

    # Act: a plain status move must not erase the recorded result.
    record = store.set_status("exp-001", ExperimentStatus.BLOCKED)

    # Assert.
    assert record.status == ExperimentStatus.BLOCKED
    assert record.result == "H04 not met"
    assert record.next_action == "implement a persistent Data volume"


def test_set_status_rejects_an_unknown_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    with pytest.raises(UniPadError) as caught:
        store.set_status("exp-001", "succeeded")

    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert store.load("exp-001").status == ExperimentStatus.PLANNED


def test_set_status_on_a_missing_record_is_blocked(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(UniPadError) as caught:
        store.set_status("exp-nope", ExperimentStatus.RUNNING)

    assert caught.value.code == ErrorCode.BLOCKED


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------
def test_list_returns_every_readable_id_sorted(tmp_path: Path) -> None:
    # Arrange.
    store = _store(tmp_path)
    for experiment_id in ("exp-003", "exp-001", "exp-002"):
        _create(store, experiment_id)

    # Act.
    records = store.list()

    # Assert.
    assert [record.experiment_id for record in records] == ["exp-001", "exp-002", "exp-003"]


def test_list_on_an_absent_root_returns_nothing(tmp_path: Path) -> None:
    # A missing store root is not an error until a record is written.
    store = _store(tmp_path)

    assert store.list() == []


def test_scan_surfaces_a_broken_journal_instead_of_hiding_it(tmp_path: Path) -> None:
    # PRD 9.2: a broken run must not silently disappear from the listing.
    store = _store(tmp_path)
    _create(store, "exp-good")
    broken = tmp_path / "runs" / "exp-broken" / EXPERIMENT_FILENAME
    broken.parent.mkdir(parents=True)
    broken.write_text("{oops", encoding="utf-8")

    scan = store.scan()

    assert [record.experiment_id for record in scan.records] == ["exp-good"]
    assert [error["experiment_id"] for error in scan.errors] == ["exp-broken"]
    assert scan.errors[0]["code"] == ErrorCode.CONFIG_INVALID


def test_list_skips_directories_without_a_journal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "exp-real")
    (tmp_path / "runs" / "not-an-experiment").mkdir(parents=True)

    assert [record.experiment_id for record in store.list()] == ["exp-real"]


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------
def test_path_helpers_point_at_the_documented_layout(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.dir_for("exp-001") == tmp_path / "runs" / "exp-001"
    assert store.path_for("exp-001") == tmp_path / "runs" / "exp-001" / "experiment.json"
    assert store.root == tmp_path / "runs"
