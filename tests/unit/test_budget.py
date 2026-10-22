"""Unit tests for the experiment budget (PRD 7.5).

Mapping
-------
* PRD 7.5 "初始建议每个假设最多 3 次受控实验；相同失败签名连续 3 次且没有新增
  证据时停止该路径" -- the per-hypothesis quota and the same-signature stop
  rule raise ``budget_exceeded`` instead of silently continuing.
* PRD 8.4 -- the shipped ``max_actions_minutes_per_week`` is ``0``, meaning the
  paid Actions path is not yet authorised: any positive minute request is
  refused.
* PRD 7.5 "预算按真实 Actions 用量、模型用量和本地运行时间累计，跨会话保留" --
  usage accumulates across :class:`BudgetTracker` instances through the usage
  file; an absent file starts at zero and is never silently reset.
* PRD 7.4 -- the weekly window is a rolling 7-day window, so a usage entry
  older than the window no longer counts.

The clock is injected (``now=``) so the rolling window is deterministic: no
wall-clock, no sleeps.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unipad.controller.budget import USAGE_SCHEMA_VERSION, BudgetTracker
from unipad.errors import ErrorCode, UniPadError

#: The shipped budget configuration (PRD 7.5 initial recommendations).
BUDGET_CONFIG: dict[str, object] = {
    "schema_version": 1,
    "per_hypothesis_max_experiments": 3,
    "same_failure_signature_max": 3,
    "max_actions_minutes_per_week": 0,
    "max_model_tokens_per_session": None,
    "max_local_run_minutes_per_experiment": 45,
}

_HYPOTHESIS = "a fixed TCG CPU model boots UniXNU past DarwinBoot"
_SIGNATURE = "kernel panic at IOKit probe"


def _write_config(path: Path, **overrides: object) -> Path:
    """Write a budget config to ``path`` and return it."""
    payload = dict(BUDGET_CONFIG)
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fixed_now(moment: datetime):
    """Return a deterministic ``now`` callable pinned to ``moment``."""

    def now() -> datetime:
        return moment

    return now


@pytest.fixture()
def budget_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(config_path, usage_path)`` inside ``tmp_path``."""
    return (
        _write_config(tmp_path / "config" / "budget.json"),
        tmp_path / "runs" / "budget-usage.json",
    )


@pytest.fixture()
def clock() -> datetime:
    """A fixed 'now' so the rolling weekly window is deterministic."""
    return datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)


def _tracker(config: Path, usage: Path, clock: datetime) -> BudgetTracker:
    return BudgetTracker(config, usage, now=_fixed_now(clock))


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------
def test_limits_are_loaded_from_the_config_file(budget_paths: tuple[Path, Path], clock: datetime) -> None:
    # Arrange / act.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    # Assert: the values on disk, not the module defaults.
    assert tracker.limits.per_hypothesis_max_experiments == 3
    assert tracker.limits.same_failure_signature_max == 3
    assert tracker.limits.max_actions_minutes_per_week == 0.0
    assert tracker.config_path == config
    assert tracker.usage_path == usage


def test_missing_config_falls_back_to_the_prd_7_5_recommendations(tmp_path: Path, clock: datetime) -> None:
    # PRD 7.5 initial recommendations are the safe default.
    tracker = _tracker(tmp_path / "absent.json", tmp_path / "usage.json", clock)

    assert tracker.limits.per_hypothesis_max_experiments == 3
    assert tracker.limits.same_failure_signature_max == 3
    assert tracker.limits.max_actions_minutes_per_week == 0.0


def test_malformed_config_is_config_invalid(tmp_path: Path, clock: datetime) -> None:
    # Arrange: a config that is not JSON.
    path = tmp_path / "budget.json"
    path.write_text("{not json", encoding="utf-8")

    # Act / assert: fail closed rather than silently using the defaults.
    with pytest.raises(UniPadError) as caught:
        _tracker(path, tmp_path / "usage.json", clock)
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_config_that_is_not_an_object_is_config_invalid(tmp_path: Path, clock: datetime) -> None:
    path = tmp_path / "budget.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(UniPadError) as caught:
        _tracker(path, tmp_path / "usage.json", clock)
    assert caught.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# zero Actions budget (PRD 8.4)
# ---------------------------------------------------------------------------
def test_positive_actions_minutes_under_a_zero_limit_is_budget_exceeded(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # Arrange: the shipped config authorises no paid Actions minute yet.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    # Act / assert: PRD 8.4 -- the paid path is refused, not silently continued.
    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", actions_minutes=1.0)

    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED
    rules = [entry["rule"] for entry in caught.value.details["exceeded"]]
    assert rules == ["max_actions_minutes_per_week"]
    assert caught.value.details["report"]["exceeded"][0]["limit"] == 0.0


def test_zero_actions_minutes_is_allowed_under_a_zero_limit(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # Local, unpaid diagnostics stay available (PRD 7.5).
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    report = tracker.check("M4", actions_minutes=0.0)

    assert report.ok is True
    assert report.exceeded == []


def test_a_disabled_limit_allows_any_actions_amount(tmp_path: Path, clock: datetime) -> None:
    # Arrange: ``null`` disables the guard (an explicit authorisation).
    config = _write_config(tmp_path / "budget.json", max_actions_minutes_per_week=None)
    tracker = _tracker(config, tmp_path / "usage.json", clock)

    report = tracker.check("M4", actions_minutes=90.0)

    assert report.ok is True
    assert report.remaining["actions_minutes_this_week"] is None


# ---------------------------------------------------------------------------
# per-hypothesis quota (PRD 7.5)
# ---------------------------------------------------------------------------
def test_three_experiments_are_allowed_and_the_fourth_is_budget_exceeded(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # PRD 7.5: at most 3 controlled experiments per hypothesis.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    for index in range(3):
        report = tracker.check("M4", hypothesis=_HYPOTHESIS)
        assert report.ok is True
        tracker.record(experiment_minutes=5.0, milestone="M4", hypothesis=_HYPOTHESIS)

    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", hypothesis=_HYPOTHESIS)

    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED
    exceeded = caught.value.details["exceeded"]
    assert [entry["rule"] for entry in exceeded] == ["per_hypothesis_max_experiments"]
    assert exceeded[0]["limit"] == 3
    assert exceeded[0]["used"] == 3


def test_a_resource_only_record_does_not_spend_the_quota(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # Arrange.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    # Act: ``experiments=0`` logs resources without spending quota.
    tracker.record(experiments=0, milestone="M4", hypothesis=_HYPOTHESIS)

    # Assert.
    assert tracker.check("M4", hypothesis=_HYPOTHESIS).ok is True


def test_the_quota_is_per_hypothesis_not_global(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # Arrange: a different hypothesis has its own bucket.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)
    for _ in range(3):
        tracker.record(milestone="M4", hypothesis=_HYPOTHESIS)

    # Act / assert.
    assert tracker.check("M4", hypothesis="a different falsifiable hypothesis").ok is True


def test_renaming_the_hypothesis_does_not_bypass_the_limit(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # PRD 7.5: "不能靠改名绕过限制" -- the bucket is derived from the text.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)
    for _ in range(3):
        tracker.record(milestone="M4", hypothesis=_HYPOTHESIS)

    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", hypothesis=f"  {_HYPOTHESIS.upper()}  ")
    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED


def test_without_a_hypothesis_the_quota_is_not_enforced(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # The quota cannot be attributed, so it is reported rather than guessed.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    report = tracker.check("M4")

    assert report.ok is True
    assert report.milestone == "M4"


# ---------------------------------------------------------------------------
# same failure signature (PRD 7.5)
# ---------------------------------------------------------------------------
def test_the_same_failure_signature_stops_the_path(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # PRD 7.5: same signature 3 times without new evidence -> stop.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)
    for _ in range(3):
        tracker.record(failure_signature=_SIGNATURE)

    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", failure_signature=_SIGNATURE)

    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED
    exceeded = caught.value.details["exceeded"]
    assert [entry["rule"] for entry in exceeded] == ["same_failure_signature_max"]
    assert exceeded[0]["limit"] == 3


def test_a_different_failure_signature_has_its_own_budget(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)
    for _ in range(3):
        tracker.record(failure_signature=_SIGNATURE)

    assert tracker.check("M4", failure_signature="a different panic").ok is True


# ---------------------------------------------------------------------------
# model tokens per session
# ---------------------------------------------------------------------------
def test_model_tokens_are_capped_per_session(tmp_path: Path, clock: datetime) -> None:
    # Arrange.
    config = _write_config(tmp_path / "budget.json", max_model_tokens_per_session=100)
    tracker = _tracker(config, tmp_path / "usage.json", clock)

    # Act / assert: 60 + 60 exceeds the 100-token session cap.
    assert tracker.check("M4", model_tokens=60, session_id="s1").ok is True
    tracker.record(model_tokens=60, session_id="s1")
    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", model_tokens=60, session_id="s1")
    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED


def test_a_new_session_resets_the_session_counters(tmp_path: Path, clock: datetime) -> None:
    config = _write_config(tmp_path / "budget.json", max_model_tokens_per_session=100)
    tracker = _tracker(config, tmp_path / "usage.json", clock)
    tracker.record(model_tokens=90, session_id="s1")

    assert tracker.check("M4", model_tokens=90, session_id="s2").ok is True


# ---------------------------------------------------------------------------
# cross-session accumulation
# ---------------------------------------------------------------------------
def test_record_accumulates_across_tracker_instances(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # PRD 7.5: usage is preserved across sessions, never silently reset.
    config, usage = budget_paths

    first = _tracker(config, usage, clock)
    first.record(experiment_minutes=12.5, milestone="M4", hypothesis=_HYPOTHESIS)

    # Act: a brand-new controller process reads the same usage file.
    second = _tracker(config, usage, clock)
    second.record(experiment_minutes=7.5, milestone="M4", hypothesis=_HYPOTHESIS)
    report = second.report("M4")

    # Assert: totals are cumulative, not per-instance.
    assert report.usage["totals"]["experiment_minutes"] == 20.0
    assert report.usage["totals"]["experiments"] == 2
    assert report.usage["records"] == 2
    assert usage.is_file()


def test_the_usage_file_is_schema_versioned_and_created_on_first_record(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    # Assert: absent until something is actually recorded.
    assert not usage.exists()

    tracker.record(milestone="M4", hypothesis=_HYPOTHESIS)

    payload = json.loads(usage.read_text(encoding="utf-8"))
    assert payload["schema_version"] == USAGE_SCHEMA_VERSION
    assert payload["totals"]["experiments"] == 1


def test_an_absent_usage_file_starts_at_zero(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    report = tracker.report()

    assert report.usage["totals"]["experiments"] == 0
    assert report.usage["totals"]["experiment_minutes"] == 0.0
    assert report.usage["totals"]["actions_minutes"] == 0.0
    assert report.usage["totals"]["model_tokens"] == 0
    assert report.usage["actions_minutes_this_week"] == 0.0
    assert report.usage["experiments_this_week"] == 0
    assert report.usage["per_hypothesis"] == {}


def test_a_corrupt_usage_file_is_config_invalid_not_a_reset(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # PRD 7.5: fail closed -- a corrupt usage file must not silently reset the
    # budget and re-open the paid path.
    config, usage = budget_paths
    usage.parent.mkdir(parents=True, exist_ok=True)
    usage.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(UniPadError) as caught:
        _tracker(config, usage, clock)
    assert caught.value.code == ErrorCode.CONFIG_INVALID


def test_report_reflects_the_recorded_usage(budget_paths: tuple[Path, Path], clock: datetime) -> None:
    # Arrange.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)
    tracker.record(
        experiment_minutes=10.0,
        actions_minutes=0.0,
        model_tokens=500,
        milestone="M9",
        hypothesis=_HYPOTHESIS,
        failure_signature=_SIGNATURE,
    )

    # Act.
    report = tracker.report("M9")

    # Assert: the report is a machine-readable snapshot of usage vs limits.
    payload = report.to_dict()
    assert payload["milestone"] == "M9"
    assert payload["ok"] is True
    assert payload["exceeded"] == []
    assert payload["usage"]["totals"]["experiment_minutes"] == 10.0
    assert payload["usage"]["totals"]["model_tokens"] == 500
    assert payload["usage"]["experiments_this_week"] == 1
    assert len(payload["usage"]["per_hypothesis"]) == 1
    assert payload["usage"]["per_hypothesis"][next(iter(payload["usage"]["per_hypothesis"]))][
        "experiments"
    ] == 1
    assert payload["usage"]["per_signature"]
    assert payload["limits"]["per_hypothesis_max_experiments"] == 3
    assert payload["remaining"]["actions_minutes_this_week"] == 0.0
    assert payload["usage"]["usage_file"] == usage.as_posix()
    assert payload["usage"]["config_source"] == config.as_posix()

    # The per-milestone attribution is persisted for the audit trail.
    persisted = json.loads(usage.read_text(encoding="utf-8"))
    assert persisted["per_milestone"]["M9"] == {"experiments": 1, "experiment_minutes": 10.0}


def test_report_json_is_one_parseable_object(budget_paths: tuple[Path, Path], clock: datetime) -> None:
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)
    tracker.record(milestone="M4", hypothesis=_HYPOTHESIS)

    document = json.loads(tracker.report("M4").to_json())

    assert set(document) == {
        "milestone",
        "ok",
        "limits",
        "usage",
        "remaining",
        "exceeded",
        "checked_at",
    }


# ---------------------------------------------------------------------------
# rolling weekly window
# ---------------------------------------------------------------------------
def test_usage_outside_the_rolling_window_no_longer_counts(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # Arrange: a record from 10 days ago, outside the 7-day window.
    config, usage = budget_paths
    old = clock - timedelta(days=10)
    stale = _tracker(config, usage, old)
    stale.record(actions_minutes=30.0, milestone="M4")

    # Act: a fresh controller today still sees the 0-minute budget as free.
    tracker = _tracker(config, usage, clock)
    report = tracker.check("M4", actions_minutes=0.0)

    # Assert: the stale usage is retained in totals but not in the window.
    assert report.ok is True
    assert report.usage["totals"]["actions_minutes"] == 30.0
    assert report.usage["actions_minutes_this_week"] == 0.0


def test_a_positive_request_fails_even_when_the_window_is_clear(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # A zero limit means "nothing allowed", independent of the window.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", actions_minutes=0.5)
    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED


def test_the_weekly_window_blocks_a_paid_request_once_the_limit_is_reached(
    tmp_path: Path, clock: datetime
) -> None:
    # Arrange: a real (positive) weekly allowance, partly spent.
    config = _write_config(tmp_path / "budget.json", max_actions_minutes_per_week=60)
    tracker = _tracker(config, tmp_path / "usage.json", clock)
    tracker.record(actions_minutes=55.0, milestone="M4")

    # Act / assert: 55 + 6 exceeds the 60-minute weekly cap.
    assert tracker.check("M4", actions_minutes=5.0).ok is True
    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", actions_minutes=6.0)
    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED


def test_local_run_minutes_are_reported_for_the_launcher(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # PRD 7.4: every experiment has a run timeout.
    config, usage = budget_paths
    tracker = _tracker(config, usage, clock)

    report = tracker.report()

    assert report.limits["max_local_run_minutes_per_experiment"] == 45
    assert report.remaining["local_run_minutes_per_experiment"] == 45
