"""Controller budget tests (PRD section 7.5, docs/testing-strategy.md D5).

Mapping
-------
* **PRD 7.5** -- "初始建议每个假设最多 3 次受控实验": the fourth controlled
  experiment under one hypothesis raises ``budget_exceeded``.
* **PRD 7.5** -- "相同失败签名连续 3 次且没有新增证据时停止该路径": the fourth
  record of the same failure signature raises ``budget_exceeded``.
* **PRD 7.5** -- "可以创建新的有依据假设，不能靠改名绕过限制": the bucket is
  derived from the hypothesis *text*, so a renamed hypothesis does not reset the
  quota -- while a genuinely different hypothesis has its own budget.
* **PRD 7.5** -- "预算按真实 Actions 用量、模型用量和本地运行时间累计，跨会话保留":
  usage accumulates across sessions in the usage file; a fresh
  :class:`BudgetTracker` over the same file continues the count and never
  silently resets.
* **PRD 7.5 / 8.4** -- ``max_actions_minutes_per_week: 0`` means *not yet
  authorized*: any paid minute is refused instead of silently running.
* **PRD 7.4** -- ``max_local_run_minutes_per_experiment`` is reported so the
  launcher can enforce a per-experiment run timeout.

The clock is injected, so the rolling weekly window is deterministic and the
suite never sleeps.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from unipad.controller.budget import (
    USAGE_SCHEMA_VERSION,
    BudgetLimits,
    BudgetTracker,
)
from unipad.errors import ErrorCode, UniPadError

#: ``config/budget.json`` as shipped (PRD 7.5 initial recommendations).
LIMITS_CONFIG: dict[str, object] = {
    "schema_version": 1,
    "per_hypothesis_max_experiments": 3,
    "same_failure_signature_max": 3,
    "max_actions_minutes_per_week": 0,
    "max_model_tokens_per_session": None,
    "max_local_run_minutes_per_experiment": 45,
}

HYPOTHESIS = "a fixed TCG CPU model boots UniXNU past DarwinBoot"
OTHER_HYPOTHESIS = "the GICv2 distributor is mis-programmed before M4"
FAILURE_SIGNATURE = "no XNU banner on serial within 300s"


@pytest.fixture()
def budget_paths(tmp_path: Path) -> tuple[Path, Path]:
    """A ``config/budget.json`` and an (initially absent) usage file."""
    config = tmp_path / "budget.json"
    config.write_text(json.dumps(LIMITS_CONFIG), encoding="utf-8")
    return config, tmp_path / "runs" / "budget-usage.json"


@pytest.fixture()
def clock() -> datetime:
    """A fixed instant, so the 7-day rolling window is deterministic."""
    return datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _tracker(paths: tuple[Path, Path], clock: datetime) -> BudgetTracker:
    config, usage = paths
    return BudgetTracker(config_path=config, usage_path=usage, now=lambda: clock)


# ---------------------------------------------------------------------------
# PRD 7.5: three controlled experiments per hypothesis
# ---------------------------------------------------------------------------


def test_three_experiments_are_allowed_and_the_fourth_is_budget_exceeded(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    tracker = _tracker(budget_paths, clock)

    for _ in range(3):
        report = tracker.check("M4", hypothesis=HYPOTHESIS, new_experiment=True)
        assert report.ok is True
        tracker.record(milestone="M4", hypothesis=HYPOTHESIS, experiment_minutes=1.0)

    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", hypothesis=HYPOTHESIS, new_experiment=True)

    error = caught.value
    assert error.code == ErrorCode.BUDGET_EXCEEDED
    exceeded = error.details["exceeded"]
    assert [item["rule"] for item in exceeded] == ["per_hypothesis_max_experiments"]
    assert exceeded[0]["limit"] == 3
    assert exceeded[0]["used"] == 3
    assert exceeded[0]["requested"] == 1


def test_a_step_that_is_not_a_new_experiment_still_runs(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # Collecting evidence for the experiment already spent is not a fourth one.
    tracker = _tracker(budget_paths, clock)
    for _ in range(3):
        tracker.record(milestone="M4", hypothesis=HYPOTHESIS)

    report = tracker.check("M4", hypothesis=HYPOTHESIS, new_experiment=False)

    assert report.ok is True


def test_the_quota_is_per_hypothesis_not_global(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # PRD 7.5: a new, justified hypothesis may be tried; the old one is spent.
    tracker = _tracker(budget_paths, clock)
    for _ in range(3):
        tracker.record(milestone="M4", hypothesis=HYPOTHESIS)

    report = tracker.check("M4", hypothesis=OTHER_HYPOTHESIS, new_experiment=True)

    assert report.ok is True


def test_renaming_the_hypothesis_does_not_bypass_the_limit(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # PRD 7.5: "不能靠改名绕过限制" -- whitespace/case changes are the same
    # hypothesis, because the bucket is derived from the text itself.
    tracker = _tracker(budget_paths, clock)
    for _ in range(3):
        tracker.record(milestone="M4", hypothesis=HYPOTHESIS)

    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", hypothesis=f"  {HYPOTHESIS.upper()}  ", new_experiment=True)

    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED


# ---------------------------------------------------------------------------
# PRD 7.5: the same failure signature stops the path
# ---------------------------------------------------------------------------


def test_the_same_failure_signature_stops_the_path_after_three(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    tracker = _tracker(budget_paths, clock)

    for _ in range(3):
        report = tracker.check("M4", hypothesis=HYPOTHESIS, failure_signature=FAILURE_SIGNATURE)
        assert report.ok is True
        tracker.record(
            milestone="M4", hypothesis=HYPOTHESIS, failure_signature=FAILURE_SIGNATURE
        )

    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", hypothesis=HYPOTHESIS, failure_signature=FAILURE_SIGNATURE)

    error = caught.value
    assert error.code == ErrorCode.BUDGET_EXCEEDED
    rules = [item["rule"] for item in error.details["exceeded"]]
    assert "same_failure_signature_max" in rules
    signature_rule = next(
        item for item in error.details["exceeded"] if item["rule"] == "same_failure_signature_max"
    )
    assert signature_rule["limit"] == 3
    assert signature_rule["used"] == 3


def test_a_different_failure_signature_has_its_own_budget(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    tracker = _tracker(budget_paths, clock)
    for _ in range(3):
        tracker.record(milestone="M4", hypothesis=HYPOTHESIS, failure_signature=FAILURE_SIGNATURE)

    # The same hypothesis quota is spent, so this step is not a new experiment;
    # the point is that the *signature* bucket for a different failure is fresh.
    report = tracker.check(
        "M4",
        hypothesis=HYPOTHESIS,
        failure_signature="panic: unmapped device tree node",
        new_experiment=False,
    )

    assert report.ok is True


# ---------------------------------------------------------------------------
# PRD 7.5: usage accumulates across sessions
# ---------------------------------------------------------------------------


def test_usage_accumulates_across_sessions_via_the_usage_file(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    config, usage = budget_paths

    first = BudgetTracker(config_path=config, usage_path=usage, now=lambda: clock)
    first.record(
        experiment_minutes=12.5,
        actions_minutes=0.0,
        model_tokens=1000,
        milestone="M4",
        hypothesis=HYPOTHESIS,
    )

    # A new session (a fresh process, a fresh tracker) over the same file.
    later = clock + timedelta(hours=6)
    second = BudgetTracker(config_path=config, usage_path=usage, now=lambda: later)
    second.record(
        experiment_minutes=7.25,
        actions_minutes=0.0,
        model_tokens=500,
        milestone="M4",
        hypothesis=HYPOTHESIS,
    )

    document = json.loads(usage.read_text(encoding="utf-8"))
    assert document["schema_version"] == USAGE_SCHEMA_VERSION
    assert document["totals"]["experiments"] == 2
    assert document["totals"]["experiment_minutes"] == pytest.approx(19.75)
    assert document["totals"]["model_tokens"] == 1500
    assert document["records"] == 2
    assert document["first_recorded_at"]
    assert document["last_recorded_at"]
    # The quota the *second* session sees already includes the first session's
    # experiments, which is what makes the limit survive a restart.
    hypothesis_key = next(iter(document["per_hypothesis"]))
    assert document["per_hypothesis"][hypothesis_key]["experiments"] == 2
    assert document["per_hypothesis"][hypothesis_key]["milestones"] == ["M4"]
    assert document["per_milestone"]["M4"]["experiments"] == 2


def test_an_absent_usage_file_starts_at_zero_and_is_never_reset(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    _config, usage = budget_paths
    assert not usage.exists()

    tracker = _tracker(budget_paths, clock)
    report = tracker.report("M4")

    assert report.ok is True
    assert report.usage["totals"]["experiments"] == 0
    assert report.usage["usage_file"] == usage.as_posix()
    assert report.usage["config_source"] == budget_paths[0].as_posix()


def test_a_corrupt_usage_file_is_config_invalid_not_a_reset(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # PRD 7.5: a broken budget file must not silently restart the allowance.
    _config, usage = budget_paths
    usage.parent.mkdir(parents=True, exist_ok=True)
    usage.write_text("{ truncated", encoding="utf-8")

    with pytest.raises(UniPadError) as caught:
        _tracker(budget_paths, clock)

    assert caught.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# PRD 7.5 / 8.4: paid Actions minutes
# ---------------------------------------------------------------------------


def test_paid_actions_minutes_are_refused_while_the_budget_is_zero(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    # config/budget.json ships 0 = "not yet authorized" (PRD 8.4).
    tracker = _tracker(budget_paths, clock)

    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", actions_minutes=1.0, new_experiment=False)

    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED
    rule = caught.value.details["exceeded"][0]
    assert rule["rule"] == "max_actions_minutes_per_week"
    assert rule["limit"] == 0.0
    assert rule["requested"] == 1.0


def test_a_free_step_is_allowed_under_a_zero_paid_budget(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    tracker = _tracker(budget_paths, clock)

    report = tracker.check("M4", actions_minutes=0.0, new_experiment=False)

    assert report.ok is True


def test_model_tokens_are_capped_per_session_when_a_limit_is_set(
    tmp_path: Path, clock: datetime
) -> None:
    config = tmp_path / "budget.json"
    config.write_text(
        json.dumps({**LIMITS_CONFIG, "max_model_tokens_per_session": 100}), encoding="utf-8"
    )
    tracker = BudgetTracker(config_path=config, usage_path=tmp_path / "usage.json", now=lambda: clock)

    tracker.record(model_tokens=90, milestone="M4")

    with pytest.raises(UniPadError) as caught:
        tracker.check("M4", model_tokens=20, new_experiment=False)

    assert caught.value.code == ErrorCode.BUDGET_EXCEEDED
    assert caught.value.details["exceeded"][0]["rule"] == "max_model_tokens_per_session"


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------


def test_report_reflects_the_recorded_usage_without_enforcing(
    budget_paths: tuple[Path, Path], clock: datetime
) -> None:
    tracker = _tracker(budget_paths, clock)
    tracker.record(experiment_minutes=3.0, milestone="M4", hypothesis=HYPOTHESIS)
    tracker.record(failure_signature=FAILURE_SIGNATURE, milestone="M4")

    report = tracker.report("M4")

    assert report.milestone == "M4"
    assert report.ok is True
    assert report.exceeded == []
    assert report.limits["per_hypothesis_max_experiments"] == 3
    assert report.limits["same_failure_signature_max"] == 3
    assert report.limits["max_actions_minutes_per_week"] == 0.0
    assert report.usage["totals"]["experiments"] == 2
    assert report.usage["totals"]["experiment_minutes"] == pytest.approx(3.0)
    assert report.usage["experiments_this_week"] == 2
    assert len(report.usage["per_signature"]) == 1
    assert report.usage["per_signature"][next(iter(report.usage["per_signature"]))]["failures"] == 1
    # The launcher reads the per-experiment run timeout from the report (PRD 7.4).
    assert report.remaining["local_run_minutes_per_experiment"] == 45.0
    assert report.remaining["actions_minutes_this_week"] == 0.0


def test_report_is_one_json_object(budget_paths: tuple[Path, Path], clock: datetime) -> None:
    tracker = _tracker(budget_paths, clock)
    tracker.record(milestone="M4", hypothesis=HYPOTHESIS)

    document, end = json.JSONDecoder().raw_decode(tracker.report("M4").to_json())

    assert tracker.report("M4").to_json()[end:].strip() == ""
    assert document["usage"]["records"] == 1
    assert document["usage"]["session"]["model_tokens"] == 0


def test_limits_are_loaded_from_the_config_file(budget_paths: tuple[Path, Path], clock: datetime) -> None:
    tracker = _tracker(budget_paths, clock)

    assert isinstance(tracker.limits, BudgetLimits)
    assert tracker.limits.per_hypothesis_max_experiments == 3
    assert tracker.limits.same_failure_signature_max == 3
    assert tracker.limits.max_actions_minutes_per_week == 0.0
    assert tracker.limits.max_local_run_minutes_per_experiment == 45.0
