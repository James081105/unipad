"""Experiment and cost budget (PRD section 7.5).

The budget is the guard rail that keeps the AI loop finite:

* at most ``per_hypothesis_max_experiments`` controlled experiments per
  hypothesis (initial recommendation: 3);
* the same failure signature may not repeat ``same_failure_signature_max``
  times without new evidence (initial recommendation: 3);
* weekly GitHub Actions minutes are capped by ``max_actions_minutes_per_week``
  -- the shipped config uses ``0``, which means *not yet authorized*: any paid
  minute is refused until the user confirms account billing (PRD 8.4);
* ``max_model_tokens_per_session`` caps model usage per session when set;
* ``max_local_run_minutes_per_experiment`` is reported so the launcher can
  enforce a per-experiment run timeout (PRD 7.4).

Usage accumulates **across sessions** in ``runs/budget-usage.json``; an absent
file starts at zero and is never silently reset.  A ``null`` limit disables the
corresponding guard; ``0`` means "nothing allowed" (see the Actions note
above).  Only the standard library is used -- ``pip`` is unusable on this host.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..errors import ErrorCode, UniPadError

__all__ = [
    "BudgetLimits",
    "BudgetReport",
    "BudgetTracker",
    "USAGE_SCHEMA_VERSION",
]

#: Schema version of ``runs/budget-usage.json``.
USAGE_SCHEMA_VERSION = 1

#: Rolling window length for the weekly Actions budget (PRD 7.5).
WEEK_DAYS = 7

_DEFAULT_CONFIG: dict[str, Any] = {
    "per_hypothesis_max_experiments": 3,
    "same_failure_signature_max": 3,
    "max_actions_minutes_per_week": 0,
    "max_model_tokens_per_session": None,
    "max_local_run_minutes_per_experiment": 45,
}


def _utc_now_iso() -> str:
    """Return the current UTC time as a second-resolution ISO-8601 string."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalise_key(text: str) -> str:
    """Return a stable digest key for free-form hypothesis/signature text.

    The key is a hash so that the usage file never stores long prose and so
    that whitespace-only differences do not fork the bucket.  PRD 7.5 forbids
    bypassing the limit *by renaming*: the bucket is derived from the
    hypothesis text itself, so retrying the same hypothesis keeps paying into
    the same bucket no matter which experiment id is used.
    """
    collapsed = " ".join(str(text).split()).casefold()
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()[:32]


@dataclass
class BudgetLimits:
    """Budget limits loaded from ``config/budget.json``.

    ``None`` disables a guard; ``0`` means "nothing allowed".
    """

    per_hypothesis_max_experiments: int | None = 3
    same_failure_signature_max: int | None = 3
    max_actions_minutes_per_week: float | None = 0.0
    max_model_tokens_per_session: int | None = None
    max_local_run_minutes_per_experiment: float | None = 45.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetLimits":
        """Build limits from a config mapping, ignoring unknown keys."""
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the limits."""
        return {
            "per_hypothesis_max_experiments": self.per_hypothesis_max_experiments,
            "same_failure_signature_max": self.same_failure_signature_max,
            "max_actions_minutes_per_week": self.max_actions_minutes_per_week,
            "max_model_tokens_per_session": self.max_model_tokens_per_session,
            "max_local_run_minutes_per_experiment": self.max_local_run_minutes_per_experiment,
        }


@dataclass
class BudgetReport:
    """Structured budget snapshot returned by ``check``/``record``/``report``."""

    milestone: str = ""
    ok: bool = True
    limits: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    remaining: dict[str, Any] = field(default_factory=dict)
    exceeded: list[dict[str, Any]] = field(default_factory=list)
    checked_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the report."""
        return {
            "milestone": self.milestone,
            "ok": self.ok,
            "limits": self.limits,
            "usage": self.usage,
            "remaining": self.remaining,
            "exceeded": self.exceeded,
            "checked_at": self.checked_at,
        }

    def to_json(self) -> str:
        """Return the report as indented JSON."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)


class BudgetTracker:
    """Load budget limits, enforce them, and accumulate usage across sessions.

    Parameters
    ----------
    config_path:
        ``config/budget.json``.  A missing file falls back to the PRD 7.5
        initial recommendations and is reported as such.
    usage_path:
        ``runs/budget-usage.json``.  Created on first :meth:`record`.
    now:
        Clock injection for tests; defaults to :func:`datetime.now`.
    """

    __slots__ = ("_config_path", "_usage_path", "_limits", "_usage", "_now", "_config_source")

    def __init__(
        self,
        config_path: str | Path = "config/budget.json",
        usage_path: str | Path = "runs/budget-usage.json",
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._usage_path = Path(usage_path)
        self._now = now or datetime.now
        self._limits, self._config_source = self._load_limits()
        self._usage = self._load_usage()

    # --- configuration -----------------------------------------------------
    @property
    def limits(self) -> BudgetLimits:
        """Effective budget limits."""
        return self._limits

    @property
    def usage_path(self) -> Path:
        """Path of the cumulative usage file."""
        return self._usage_path

    @property
    def config_path(self) -> Path:
        """Path of the budget configuration file."""
        return self._config_path

    def _load_limits(self) -> tuple[BudgetLimits, str]:
        if not self._config_path.is_file():
            return BudgetLimits.from_dict(dict(_DEFAULT_CONFIG)), "defaults"
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "budget configuration is not valid JSON",
                path=self._config_path.as_posix(),
                error=str(exc),
            ) from exc
        if not isinstance(raw, dict):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "budget configuration is not a JSON object",
                path=self._config_path.as_posix(),
            )
        return BudgetLimits.from_dict(raw), self._config_path.as_posix()

    # --- usage persistence -------------------------------------------------
    @staticmethod
    def _empty_usage() -> dict[str, Any]:
        return {
            "schema_version": USAGE_SCHEMA_VERSION,
            "records": 0,
            "first_recorded_at": None,
            "last_recorded_at": None,
            "totals": {
                "experiments": 0,
                "experiment_minutes": 0.0,
                "actions_minutes": 0.0,
                "model_tokens": 0,
            },
            "daily": {},
            "per_milestone": {},
            "per_hypothesis": {},
            "per_signature": {},
            "session": {"session_id": None, "model_tokens": 0, "actions_minutes": 0.0},
        }

    def _load_usage(self) -> dict[str, Any]:
        if not self._usage_path.is_file():
            return self._empty_usage()
        try:
            raw = json.loads(self._usage_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Fail closed: a corrupt usage file must not silently reset the budget.
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "budget usage file is not valid JSON; refusing to reset the budget",
                path=self._usage_path.as_posix(),
                error=str(exc),
            ) from exc
        if not isinstance(raw, dict):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "budget usage file is not a JSON object; refusing to reset the budget",
                path=self._usage_path.as_posix(),
            )
        usage = self._empty_usage()
        usage.update({k: v for k, v in raw.items() if k in usage})
        return usage

    def _save_usage(self) -> None:
        """Atomically write the usage file (Windows-safe replace)."""
        self._usage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._usage, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        handle, temp_name = tempfile.mkstemp(
            dir=str(self._usage_path.parent), prefix=self._usage_path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temp_name, self._usage_path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    # --- derived views -----------------------------------------------------
    def _today(self) -> date:
        return self._now().date()

    def _window_days(self) -> list[str]:
        today = self._today()
        return [(today - timedelta(days=offset)).isoformat() for offset in range(WEEK_DAYS)]

    def _window_actions_minutes(self) -> float:
        daily = self._usage.get("daily", {})
        total = 0.0
        for day in self._window_days():
            bucket = daily.get(day)
            if isinstance(bucket, dict):
                total += float(bucket.get("actions_minutes", 0.0) or 0.0)
        return round(total, 6)

    def _window_experiments(self) -> int:
        daily = self._usage.get("daily", {})
        total = 0
        for day in self._window_days():
            bucket = daily.get(day)
            if isinstance(bucket, dict):
                total += int(bucket.get("experiments", 0) or 0)
        return total

    def _hypothesis_bucket(self, hypothesis: str) -> dict[str, Any]:
        buckets = self._usage.setdefault("per_hypothesis", {})
        key = _normalise_key(hypothesis)
        bucket = buckets.setdefault(
            key,
            {
                "hypothesis_digest": key,
                "hypothesis_preview": " ".join(str(hypothesis).split())[:120],
                "experiments": 0,
                "experiment_minutes": 0.0,
                "model_tokens": 0,
                "milestones": [],
            },
        )
        return bucket

    def _signature_bucket(self, signature: str) -> dict[str, Any]:
        buckets = self._usage.setdefault("per_signature", {})
        key = _normalise_key(signature)
        return buckets.setdefault(
            key,
            {
                "signature_digest": key,
                "signature_preview": " ".join(str(signature).split())[:120],
                "failures": 0,
                "last_seen": None,
            },
        )

    def _usage_view(self) -> dict[str, Any]:
        return {
            "totals": dict(self._usage.get("totals", {})),
            "actions_minutes_this_week": self._window_actions_minutes(),
            "experiments_this_week": self._window_experiments(),
            "records": self._usage.get("records", 0),
            "first_recorded_at": self._usage.get("first_recorded_at"),
            "last_recorded_at": self._usage.get("last_recorded_at"),
            "per_hypothesis": {
                key: {
                    "experiments": value.get("experiments", 0),
                    "experiment_minutes": value.get("experiment_minutes", 0.0),
                    "model_tokens": value.get("model_tokens", 0),
                    "hypothesis_preview": value.get("hypothesis_preview"),
                }
                for key, value in self._usage.get("per_hypothesis", {}).items()
            },
            "per_signature": {
                key: {
                    "failures": value.get("failures", 0),
                    "last_seen": value.get("last_seen"),
                    "signature_preview": value.get("signature_preview"),
                }
                for key, value in self._usage.get("per_signature", {}).items()
            },
            "session": dict(self._usage.get("session", {})),
            "usage_file": self._usage_path.as_posix(),
            "config_source": self._config_source,
        }

    def _remaining_view(self) -> dict[str, Any]:
        limits = self._limits
        used_actions = self._window_actions_minutes()
        remaining: dict[str, Any] = {}
        if limits.max_actions_minutes_per_week is None:
            remaining["actions_minutes_this_week"] = None
        else:
            remaining["actions_minutes_this_week"] = round(
                float(limits.max_actions_minutes_per_week) - used_actions, 6
            )
        session = self._usage.get("session", {})
        if limits.max_model_tokens_per_session is None:
            remaining["model_tokens_session"] = None
        else:
            remaining["model_tokens_session"] = int(limits.max_model_tokens_per_session) - int(
                session.get("model_tokens", 0) or 0
            )
        remaining["local_run_minutes_per_experiment"] = limits.max_local_run_minutes_per_experiment
        return remaining

    # --- enforcement -------------------------------------------------------
    def check(
        self,
        milestone: str,
        *,
        hypothesis: str | None = None,
        failure_signature: str | None = None,
        actions_minutes: float = 0.0,
        model_tokens: int = 0,
        new_experiment: bool = True,
        session_id: str | None = None,
    ) -> BudgetReport:
        """Check whether another step is still inside budget.

        Parameters
        ----------
        milestone:
            Milestone the step belongs to (recorded in the report).
        hypothesis:
            Hypothesis the step runs under.  Required for the per-hypothesis
            experiment quota; without it that quota cannot be attributed and
            is reported instead of enforced.
        failure_signature:
            Failure signature already observed for this path.  When supplied,
            the "same signature three times" stop rule (PRD 7.5) is enforced.
        actions_minutes:
            Paid Actions minutes the step is about to consume.
        model_tokens:
            Model tokens the step is about to consume.
        new_experiment:
            Whether the step counts as one more controlled experiment
            (default ``True``) for the per-hypothesis quota.
        session_id:
            Resets the per-session counters when it differs from the stored
            session.

        Returns
        -------
        BudgetReport
            ``ok=True`` when the step is inside budget.

        Raises
        ------
        UniPadError
            ``ErrorCode.BUDGET_EXCEEDED`` when a limit is hit; ``details``
            carries the full report so the CLI can print machine-readable
            usage instead of prose.
        """
        self._roll_session(session_id)
        exceeded: list[dict[str, Any]] = []

        # 1. Per-hypothesis experiment quota (PRD 7.5).
        if hypothesis is not None and self._limits.per_hypothesis_max_experiments is not None:
            limit = int(self._limits.per_hypothesis_max_experiments)
            bucket = self._hypothesis_bucket(hypothesis)
            used = int(bucket.get("experiments", 0))
            demand = used + (1 if new_experiment else 0)
            if demand > limit:
                exceeded.append(
                    {
                        "rule": "per_hypothesis_max_experiments",
                        "limit": limit,
                        "used": used,
                        "requested": 1 if new_experiment else 0,
                        "hypothesis_digest": bucket.get("hypothesis_digest"),
                        "hypothesis_preview": bucket.get("hypothesis_preview"),
                    }
                )

        # 2. Weekly Actions minutes (PRD 7.5 / 8.4).
        if self._limits.max_actions_minutes_per_week is not None:
            limit = float(self._limits.max_actions_minutes_per_week)
            used = self._window_actions_minutes()
            demand = round(used + float(actions_minutes), 6)
            if demand > limit:
                exceeded.append(
                    {
                        "rule": "max_actions_minutes_per_week",
                        "limit": limit,
                        "used": used,
                        "requested": float(actions_minutes),
                        "window_days": WEEK_DAYS,
                    }
                )

        # 3. Same failure signature repeated without new evidence (PRD 7.5).
        if failure_signature is not None and self._limits.same_failure_signature_max is not None:
            limit = int(self._limits.same_failure_signature_max)
            bucket = self._signature_bucket(failure_signature)
            used = int(bucket.get("failures", 0))
            if used >= limit:
                exceeded.append(
                    {
                        "rule": "same_failure_signature_max",
                        "limit": limit,
                        "used": used,
                        "signature_digest": bucket.get("signature_digest"),
                        "signature_preview": bucket.get("signature_preview"),
                    }
                )

        # 4. Model tokens per session (only enforced when a positive limit is set).
        if self._limits.max_model_tokens_per_session not in (None, 0):
            limit = int(self._limits.max_model_tokens_per_session)
            session = self._usage.get("session", {})
            used = int(session.get("model_tokens", 0) or 0)
            demand = used + int(model_tokens)
            if demand > limit:
                exceeded.append(
                    {
                        "rule": "max_model_tokens_per_session",
                        "limit": limit,
                        "used": used,
                        "requested": int(model_tokens),
                        "session_id": session.get("session_id"),
                    }
                )

        report = BudgetReport(
            milestone=milestone,
            ok=not exceeded,
            limits=self._limits.to_dict(),
            usage=self._usage_view(),
            remaining=self._remaining_view(),
            exceeded=exceeded,
        )
        if exceeded:
            raise UniPadError(
                ErrorCode.BUDGET_EXCEEDED,
                "budget exhausted for this step (PRD 7.5)",
                milestone=milestone,
                exceeded=exceeded,
                report=report.to_dict(),
            )
        return report

    def record(
        self,
        experiment_minutes: float = 0.0,
        actions_minutes: float = 0.0,
        model_tokens: int = 0,
        *,
        milestone: str | None = None,
        hypothesis: str | None = None,
        failure_signature: str | None = None,
        experiments: int = 1,
        session_id: str | None = None,
    ) -> BudgetReport:
        """Accumulate usage into ``runs/budget-usage.json`` and return a report.

        Parameters
        ----------
        experiment_minutes:
            Local experiment wall-clock minutes.
        actions_minutes:
            Paid GitHub Actions minutes.
        model_tokens:
            Model tokens consumed.
        experiments:
            Number of controlled experiments this call accounts for
            (default 1; pass 0 to log resources without spending quota).
        milestone / hypothesis / failure_signature / session_id:
            Attribution buckets.  ``failure_signature`` increments the
            repeat-failure counter used by the PRD 7.5 stop rule.
        """
        self._roll_session(session_id)
        now_iso = _utc_now_iso()
        day = self._today().isoformat()

        totals = self._usage.setdefault("totals", {})
        totals["experiments"] = int(totals.get("experiments", 0)) + int(experiments)
        totals["experiment_minutes"] = round(
            float(totals.get("experiment_minutes", 0.0)) + float(experiment_minutes), 6
        )
        totals["actions_minutes"] = round(
            float(totals.get("actions_minutes", 0.0)) + float(actions_minutes), 6
        )
        totals["model_tokens"] = int(totals.get("model_tokens", 0)) + int(model_tokens)

        daily = self._usage.setdefault("daily", {})
        bucket = daily.setdefault(day, {"experiments": 0, "experiment_minutes": 0.0, "actions_minutes": 0.0, "model_tokens": 0})
        bucket["experiments"] = int(bucket.get("experiments", 0)) + int(experiments)
        bucket["experiment_minutes"] = round(
            float(bucket.get("experiment_minutes", 0.0)) + float(experiment_minutes), 6
        )
        bucket["actions_minutes"] = round(
            float(bucket.get("actions_minutes", 0.0)) + float(actions_minutes), 6
        )
        bucket["model_tokens"] = int(bucket.get("model_tokens", 0)) + int(model_tokens)

        if milestone:
            per_milestone = self._usage.setdefault("per_milestone", {})
            m_bucket = per_milestone.setdefault(
                str(milestone), {"experiments": 0, "experiment_minutes": 0.0}
            )
            m_bucket["experiments"] = int(m_bucket.get("experiments", 0)) + int(experiments)
            m_bucket["experiment_minutes"] = round(
                float(m_bucket.get("experiment_minutes", 0.0)) + float(experiment_minutes), 6
            )

        if hypothesis:
            h_bucket = self._hypothesis_bucket(hypothesis)
            h_bucket["experiments"] = int(h_bucket.get("experiments", 0)) + int(experiments)
            h_bucket["experiment_minutes"] = round(
                float(h_bucket.get("experiment_minutes", 0.0)) + float(experiment_minutes), 6
            )
            h_bucket["model_tokens"] = int(h_bucket.get("model_tokens", 0)) + int(model_tokens)
            if milestone and str(milestone) not in h_bucket["milestones"]:
                h_bucket["milestones"].append(str(milestone))

        if failure_signature:
            s_bucket = self._signature_bucket(failure_signature)
            s_bucket["failures"] = int(s_bucket.get("failures", 0)) + 1
            s_bucket["last_seen"] = now_iso

        session = self._usage.setdefault("session", {})
        session["model_tokens"] = int(session.get("model_tokens", 0)) + int(model_tokens)
        session["actions_minutes"] = round(
            float(session.get("actions_minutes", 0.0)) + float(actions_minutes), 6
        )

        self._usage["records"] = int(self._usage.get("records", 0)) + 1
        if not self._usage.get("first_recorded_at"):
            self._usage["first_recorded_at"] = now_iso
        self._usage["last_recorded_at"] = now_iso
        self._save_usage()

        return self.report(milestone=milestone or "")

    def report(self, milestone: str = "") -> BudgetReport:
        """Return the current usage versus the limits without enforcing them."""
        return BudgetReport(
            milestone=milestone,
            ok=True,
            limits=self._limits.to_dict(),
            usage=self._usage_view(),
            remaining=self._remaining_view(),
            exceeded=[],
        )

    # --- internals ---------------------------------------------------------
    def _roll_session(self, session_id: str | None) -> None:
        """Reset the per-session counters when ``session_id`` changes."""
        session = self._usage.setdefault("session", {})
        if session_id is None:
            return
        if session.get("session_id") != session_id:
            session["session_id"] = session_id
            session["model_tokens"] = 0
            session["actions_minutes"] = 0.0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"BudgetTracker(config={self._config_path.as_posix()!r}, "
            f"usage={self._usage_path.as_posix()!r})"
        )
