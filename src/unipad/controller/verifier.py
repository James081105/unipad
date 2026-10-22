"""Fixed-condition verifier (PRD sections 6, 7.4 and 9.2).

The model proposes; the verifier disposes.  Gate outcomes are decided by the
**fixed conditions** registered in this module plus whatever extra checks the
caller supplies -- never by prose, and never by the agent that produced the
evidence.

Rules encoded here (PRD 7.4)
----------------------------
1. Evidence completeness first: when :meth:`unipad.models.EvidenceSet.check_complete`
   is non-empty the gate fails with reason ``evidence_incomplete`` and error
   code :data:`unipad.errors.ErrorCode.EVIDENCE_INCOMPLETE`.  Missing evidence
   is *unverified*, never "probably fine".
2. Evidence paths must point at files that exist.  A journal that references a
   log which is not on disk is not evidence.
3. Extra checks are **additive**: they can only add failure reasons, never
   replace a registered condition.  A condition is never dropped because a
   caller did not supply data for it -- absent data fails closed.
4. Every result is stamped with ``verifier_id`` and the verifier version so a
   change in verification logic is traceable independently of feature changes
   (PRD 7.4: "验证逻辑的修改独立记录，不能随功能修改偷偷放宽").

Changing a condition, or adding/removing a gate, is a *verifier logic change*:
edit this module and record the reason in ``docs/decisions/``.  There is
deliberately no runtime API for loosening a condition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..errors import ErrorCode, UniPadError
from ..models import (
    IPA_MVP_GATES,
    M13,
    M14,
    M15,
    EvidenceSet,
    GateResult,
    InstallResult,
)

__all__ = [
    "VERIFIER_ID",
    "VERIFIER_VERSION",
    "VERIFICATION_FILENAME",
    "Condition",
    "CheckContext",
    "GateOutcome",
    "CONDITIONS",
    "Verifier",
]

#: Identity of this verifier, stamped into every gate result.
VERIFIER_ID = "unipad.controller.verifier"

#: Version of the verification logic.  Bump on any condition change and record
#: the reason in ``docs/decisions/`` (PRD 7.4).
VERIFIER_VERSION = "1.1.0"

#: File written into a run directory when ``run_dir`` is supplied.
VERIFICATION_FILENAME = "verification.json"

#: EvidenceSet fields that hold a path (or list of paths) to a file on disk.
#: ``verification_json`` is deliberately excluded: it is written by this
#: verifier, so it cannot be required to pre-exist.
_PATH_FIELDS: tuple[str, ...] = (
    "experiment_json",
    "host_inventory",
    "artifact_manifest",
    "qemu_args",
    "serial_log",
    "guest_log",
    "qmp_log",
    "gpu_bridge_log",
)

#: PRD 9.2 lists ``verification.json`` in the minimum evidence set, but it is
#: an *output* of this verifier: on the first evaluation of a run it cannot
#: exist yet.  When a run directory is supplied these items are satisfied by
#: construction (the verifier writes them) and the fact is recorded in the
#: conditions row, so the one explicit exception to "missing evidence fails
#: closed" is always visible in the report instead of being silently dropped.
_SELF_PROVIDED: tuple[str, ...] = ("verification_json",)


@dataclass(frozen=True)
class Condition:
    """One fixed gate condition.

    ``evaluator`` names a built-in check in :data:`_EVALUATORS`; ``since``
    records the verifier version that introduced the condition so a gate
    result can be read against the logic that produced it.
    """

    condition_id: str
    description: str
    evaluator: str
    since: str = VERIFIER_VERSION

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serialisable view of the condition."""
        return {
            "condition_id": self.condition_id,
            "description": self.description,
            "evaluator": self.evaluator,
            "since": self.since,
        }


@dataclass
class CheckContext:
    """Inputs handed to a built-in condition evaluator."""

    gate_id: str
    milestone: str
    evidence: EvidenceSet
    run_dir: Path | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    #: Evidence items the verifier itself supplies (see _SELF_PROVIDED).
    self_provided: tuple[str, ...] = ()


# --- built-in evaluators ---------------------------------------------------
# Each returns a list of failure reasons; an empty list means the condition holds.
# An evaluator must never raise for missing data -- it reports it as a reason,
# because "no data" is a failure, not a pass.

_Evaluator = Callable[[CheckContext], list[str]]


def _evidence_complete(ctx: CheckContext) -> list[str]:
    """Required evidence items of PRD 9.2 are present."""
    return [
        f"missing:{name}"
        for name in ctx.evidence.check_complete()
        if name not in ctx.self_provided
    ]


def _evidence_files_exist(ctx: CheckContext) -> list[str]:
    """Every referenced evidence path resolves to a file on disk."""
    if ctx.run_dir is None:
        return []
    reasons: list[str] = []
    for name in (*_PATH_FIELDS, "screenshots", "evidence"):
        value = getattr(ctx.evidence, name, None)
        candidates = value if isinstance(value, list) else ([value] if value else [])
        for raw in candidates:
            if not raw:
                continue
            path = Path(raw)
            resolved = path if path.is_absolute() else ctx.run_dir / path
            if not resolved.is_file():
                reasons.append(f"missing_evidence_file:{name}:{raw}")
    return reasons


def _screenshots_present(ctx: CheckContext) -> list[str]:
    """At least one screenshot is recorded (PRD 9.2 timestamped screenshots)."""
    if ctx.evidence.screenshots:
        return []
    return ["missing:screenshots"]


def _guest_log_present(ctx: CheckContext) -> list[str]:
    """A guest-side log is recorded (PRD 7.4: serial text alone is not proof)."""
    if ctx.evidence.guest_log:
        return []
    return ["missing:guest_log"]


def _install_fact(ctx: CheckContext) -> dict[str, Any] | None:
    fact = ctx.facts.get("install")
    return fact if isinstance(fact, dict) else None


def _install_stage_terminal(ctx: CheckContext) -> list[str]:
    """The install reached a terminal *successful* stage.

    A file copy alone is not install, and neither is a failure.  ``failed`` is
    in :data:`InstallResult.TERMINAL_STAGES`, so asking only "did it reach a
    terminal stage" would let a failed install satisfy this condition; the
    installer always pairs ``failed`` with an ``error_code`` today, but this
    module reads ``install.json`` off disk, so a truncated, hand-edited or
    future-producer fact file could report ``failed`` with no error code and
    slip through every install condition.

    Each condition therefore has to hold on its own rather than rely on a
    sibling condition to catch the gap (PRD 7.4: the verifier disposes, and
    missing or contradictory data fails closed).
    """
    install = _install_fact(ctx)
    if install is None:
        return ["facts_missing:install"]
    stage = install.get("stage")
    if stage not in InstallResult.SUCCESS_STAGES:
        return [f"install_stage_not_terminal:{stage}"]
    return []


def _install_bundle_id_recorded(ctx: CheckContext) -> list[str]:
    """A registered install must report its bundle id (PRD 9.1)."""
    install = _install_fact(ctx)
    if install is None:
        return ["facts_missing:install"]
    if not install.get("bundle_id"):
        return ["install_bundle_id_missing"]
    return []


def _install_error_free(ctx: CheckContext) -> list[str]:
    """A passing install carries no error code."""
    install = _install_fact(ctx)
    if install is None:
        return ["facts_missing:install"]
    code = install.get("error_code")
    if code:
        return [f"install_error:{code}"]
    return []


def _persistence_marker_matches(ctx: CheckContext) -> list[str]:
    """A random marker written before shutdown reads back identically (H04)."""
    persistence = ctx.facts.get("persistence")
    if not isinstance(persistence, dict):
        return ["facts_missing:persistence"]
    marker = persistence.get("marker")
    read_back = persistence.get("read_back")
    if not marker or not read_back:
        return ["persistence_evidence_incomplete"]
    if marker != read_back:
        return ["persistence_marker_mismatch"]
    return []


_EVALUATORS: dict[str, _Evaluator] = {
    "evidence.complete": _evidence_complete,
    "evidence.files_exist": _evidence_files_exist,
    "evidence.screenshots_present": _screenshots_present,
    "evidence.guest_log_present": _guest_log_present,
    "install.stage_terminal": _install_stage_terminal,
    "install.bundle_id_recorded": _install_bundle_id_recorded,
    "install.error_free": _install_error_free,
    "persistence.marker_matches": _persistence_marker_matches,
}


def _condition(condition_id: str, description: str) -> Condition:
    """Build a :class:`Condition`, failing closed on an unknown evaluator name."""
    if condition_id not in _EVALUATORS:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "condition has no built-in evaluator",
            condition_id=condition_id,
            known_evaluators=sorted(_EVALUATORS),
        )
    return Condition(condition_id=condition_id, description=description, evaluator=condition_id)


# Condition groups reused across gates (DRY: one definition per requirement).
_CORE: tuple[Condition, ...] = (
    _condition("evidence.complete", "PRD 9.2 minimum evidence set is complete"),
    _condition("evidence.files_exist", "every referenced evidence path exists on disk"),
)
_SCREEN: tuple[Condition, ...] = (
    _condition("evidence.screenshots_present", "timestamped screenshots are recorded"),
)
_GUEST: tuple[Condition, ...] = (
    _condition("evidence.guest_log_present", "a guest-side log is recorded (serial alone is not proof)"),
)
_INSTALL: tuple[Condition, ...] = (
    _condition(
        "install.stage_terminal",
        "install reached a terminal successful stage -- neither a copy nor a failure counts",
    ),
    _condition("install.bundle_id_recorded", "bundle id is recorded"),
    _condition("install.error_free", "no install error code is recorded"),
)
_PERSIST: tuple[Condition, ...] = (
    _condition("persistence.marker_matches", "random marker survives a normal reboot (H04)"),
)

#: Gate registry.  ``default`` applies to every gate; a gate id absent from
#: this table fails closed (``unknown_gate``) so typos cannot pass silently.
CONDITIONS: dict[str, tuple[Condition, ...]] = {
    "default": _CORE,
    "M0": _CORE,
    "M1": _CORE,
    "M2": _CORE,
    "M3": _CORE,
    "M4": _CORE,
    "M5": _CORE,
    "M6": _CORE,
    "M7": _CORE,
    "M8": _CORE,
    "M9": _CORE + _SCREEN + _GUEST,
    "M9a": _CORE + _SCREEN + _GUEST,
    "M9b": _CORE + _SCREEN + _GUEST + _PERSIST,
    "M10": _CORE + _SCREEN + _GUEST + _INSTALL,
    "M10a": _CORE + _SCREEN + _GUEST + _INSTALL,
    "M11": _CORE + _SCREEN + _GUEST + _INSTALL,
    "M12": _CORE + _SCREEN + _GUEST + _INSTALL,
    M13: _CORE + _SCREEN + _GUEST,
    M14: _CORE + _SCREEN + _GUEST,
    M15: _CORE + _SCREEN + _GUEST,
    "ipa-mvp": _CORE + _SCREEN + _GUEST + _INSTALL + _PERSIST,
}

# Guard: every milestone named in models must have a registered gate, otherwise
# a milestone could be "verified" against the default set by accident.
_MISSING_GATES = tuple(
    sorted({*IPA_MVP_GATES, M13, M14, M15} - set(CONDITIONS))
)
if _MISSING_GATES:  # pragma: no cover - import-time self-check
    raise UniPadError(
        ErrorCode.CONFIG_INVALID,
        "milestone without registered gate conditions",
        missing_gates=list(_MISSING_GATES),
    )


@dataclass
class GateOutcome:
    """Full verifier outcome: the PRD gate result plus its error code.

    ``evaluate`` returns only the :class:`unipad.models.GateResult` because
    that is the PRD 6 contract; callers that need the stable error code
    (the CLI, the controller) use ``evaluate_detailed``.
    """

    result: GateResult
    code: str | None = None
    gate_id: str = ""
    missing: list[str] = field(default_factory=list)
    self_provided: list[str] = field(default_factory=list)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    verification_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the outcome."""
        return {
            "gate_id": self.gate_id,
            "code": self.code,
            "missing": list(self.missing),
            "self_provided": list(self.self_provided),
            "conditions": list(self.conditions),
            "verification_path": self.verification_path.as_posix() if self.verification_path else None,
            "result": self.result.to_dict(),
        }


#: An extra check is a zero-argument callable returning failure reasons, or a
#: ``(condition_id, callable)`` pair binding it to a registered condition.
CheckCallable = Callable[[], list[str]]
BoundCheck = tuple[str, CheckCallable]
ExtraCheck = CheckCallable | BoundCheck


class Verifier:
    """Evaluate milestone gates against fixed conditions (PRD 7.4)."""

    __slots__ = ()

    @property
    def verifier_id(self) -> str:
        """Stamped identity: ``<id>@<version>``."""
        return f"{VERIFIER_ID}@{VERIFIER_VERSION}"

    @staticmethod
    def known_gates() -> tuple[str, ...]:
        """Return every registered gate id (excluding ``default``)."""
        return tuple(sorted(name for name in CONDITIONS if name != "default"))

    @staticmethod
    def conditions_for(gate_id: str) -> tuple[Condition, ...]:
        """Return the conditions for ``gate_id`` (``default`` + gate specific).

        Every gate entry in :data:`CONDITIONS` already starts with the ``_CORE``
        group, and the ``default`` entry is that same group, so concatenating
        them naively evaluates each core condition twice and reports a gate as
        having more conditions than it defines.  Duplicates are collapsed by
        ``condition_id``, first occurrence wins: a condition is a named rule,
        and evaluating it twice cannot make it stricter (PRD 7.4).

        Raises
        ------
        UniPadError
            ``CONFIG_INVALID`` when the gate is not registered -- an unknown
            gate must never be evaluated against a weaker default set.
        """
        if gate_id not in CONDITIONS:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown gate id",
                gate_id=gate_id,
                known_gates=Verifier.known_gates(),
            )
        merged: list[Condition] = []
        seen: set[str] = set()
        for condition in (*CONDITIONS["default"], *CONDITIONS[gate_id]):
            if condition.condition_id in seen:
                continue
            seen.add(condition.condition_id)
            merged.append(condition)
        return tuple(merged)

    # --- evaluation --------------------------------------------------------
    def evaluate(
        self,
        gate_id: str,
        milestone: str,
        evidence: EvidenceSet,
        extra_checks: list[ExtraCheck] | None = None,
        *,
        run_dir: str | Path | None = None,
        facts: dict[str, Any] | None = None,
        checked_at: str | None = None,
    ) -> GateResult:
        """Evaluate ``gate_id`` and return the PRD 6 gate result.

        Parameters
        ----------
        gate_id:
            Registered gate (``ipa-mvp`` or a milestone such as ``M10``).
        milestone:
            Milestone recorded on the result.
        evidence:
            Runtime evidence set (PRD 9.2).
        extra_checks:
            Additive checks; each returns a list of failure reasons.  A
            ``(condition_id, callable)`` pair binds the check to a registered
            condition for the report.
        run_dir:
            When given, ``verification.json`` is written there and evidence
            paths are resolved against it.
        facts:
            Structured runtime facts the built-in conditions read, e.g.
            ``{"install": {...}, "persistence": {"marker": ..., "read_back": ...}}``.
        checked_at:
            ISO-8601 timestamp; defaults to now (UTC).
        """
        return self.evaluate_detailed(
            gate_id,
            milestone,
            evidence,
            extra_checks,
            run_dir=run_dir,
            facts=facts,
            checked_at=checked_at,
        ).result

    def evaluate_detailed(
        self,
        gate_id: str,
        milestone: str,
        evidence: EvidenceSet,
        extra_checks: list[ExtraCheck] | None = None,
        *,
        run_dir: str | Path | None = None,
        facts: dict[str, Any] | None = None,
        checked_at: str | None = None,
    ) -> GateOutcome:
        """Evaluate ``gate_id`` and return the result plus its stable code."""
        from datetime import datetime, timezone

        conditions = self.conditions_for(gate_id)
        stamp = checked_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        directory = Path(run_dir) if run_dir is not None else None

        # (a) Evidence completeness short-circuits everything else: without the
        #     minimum set there is nothing to verify (PRD 9.2).  The only
        #     exception is evidence this verifier produces itself, and only
        #     when it is about to write it (see _SELF_PROVIDED).
        missing = evidence.check_complete()
        self_provided = [
            name for name in _SELF_PROVIDED if name in missing and directory is not None
        ]
        if self_provided:
            missing = [name for name in missing if name not in self_provided]
        context = CheckContext(
            gate_id=gate_id,
            milestone=milestone,
            evidence=evidence,
            run_dir=directory,
            facts=dict(facts or {}),
            self_provided=tuple(self_provided),
        )
        condition_rows: list[dict[str, Any]] = []
        failure_reasons: list[str] = []

        if missing:
            failure_reasons.append("evidence_incomplete")
            failure_reasons.extend(f"missing:{name}" for name in missing)
            condition_rows.append(
                {
                    "condition_id": "evidence.complete",
                    "description": "PRD 9.2 minimum evidence set is complete",
                    "source": "builtin",
                    "passed": False,
                    "failure_reasons": [f"missing:{name}" for name in missing],
                }
            )
            outcome = self._finish(
                gate_id=gate_id,
                milestone=milestone,
                evidence=evidence,
                stamp=stamp,
                failure_reasons=failure_reasons,
                condition_rows=condition_rows,
                missing=missing,
                self_provided=self_provided,
                directory=directory,
                code=ErrorCode.EVIDENCE_INCOMPLETE,
            )
            return outcome

        # (b) Fixed conditions.
        for condition in conditions:
            evaluator = _EVALUATORS.get(condition.evaluator)
            if evaluator is None:  # pragma: no cover - guarded at import time
                reasons = [f"condition_without_evaluator:{condition.condition_id}"]
            else:
                reasons = list(evaluator(context))
            failure_reasons.extend(reasons)
            row: dict[str, Any] = {
                "condition_id": condition.condition_id,
                "description": condition.description,
                "source": "builtin",
                "since": condition.since,
                "passed": not reasons,
                "failure_reasons": reasons,
            }
            if condition.condition_id == "evidence.complete" and self_provided:
                # Make the one documented exception visible in every report.
                row["self_provided_by_verifier"] = list(self_provided)
            condition_rows.append(row)

        # (c) Additive caller checks.  Exceptions become failures: a crashed
        #     check must never be read as a pass.
        for index, item in enumerate(extra_checks or []):
            bound_id: str | None = None
            check = item
            if isinstance(item, tuple):
                if len(item) != 2 or not isinstance(item[0], str) or not callable(item[1]):
                    raise UniPadError(
                        ErrorCode.CONFIG_INVALID,
                        "extra check must be a callable or a (condition_id, callable) pair",
                        index=index,
                    )
                bound_id, check = item[0], item[1]
            try:
                reasons = [str(reason) for reason in (check() or [])]
            except Exception as exc:  # noqa: BLE001 - fail closed, never swallow into a pass
                reasons = [f"check_error:{bound_id or index}:{type(exc).__name__}"]
            failure_reasons.extend(reasons)
            condition_rows.append(
                {
                    "condition_id": bound_id or f"extra[{index}]",
                    "description": "caller-supplied additive check",
                    "source": "extra",
                    "passed": not reasons,
                    "failure_reasons": reasons,
                }
            )

        code = None if not failure_reasons else ErrorCode.VERIFICATION_FAILED
        return self._finish(
            gate_id=gate_id,
            milestone=milestone,
            evidence=evidence,
            stamp=stamp,
            failure_reasons=failure_reasons,
            condition_rows=condition_rows,
            missing=[],
            self_provided=self_provided,
            directory=directory,
            code=code,
        )

    # --- internals ---------------------------------------------------------
    def _finish(
        self,
        *,
        gate_id: str,
        milestone: str,
        evidence: EvidenceSet,
        stamp: str,
        failure_reasons: list[str],
        condition_rows: list[dict[str, Any]],
        missing: list[str],
        directory: Path | None,
        code: str | None,
        self_provided: list[str] | None = None,
    ) -> GateOutcome:
        """Build the gate result, optionally persist it, and wrap the outcome."""
        passed = not failure_reasons
        result = GateResult(
            milestone=milestone,
            passed=passed,
            checked_at=stamp,
            evidence=self._evidence_listing(evidence),
            failure_reasons=list(failure_reasons),
            verifier_id=self.verifier_id,
        )
        verification_path: Path | None = None
        if directory is not None:
            verification_path = self._write_verification(
                directory=directory,
                gate_id=gate_id,
                milestone=milestone,
                result=result,
                evidence=evidence,
                condition_rows=condition_rows,
                code=code,
                self_provided=self_provided or [],
            )
            if not evidence.verification_json:
                # Keep the evidence set self-consistent for later runs.
                evidence.verification_json = verification_path.name
        return GateOutcome(
            result=result,
            code=code,
            gate_id=gate_id,
            missing=list(missing),
            self_provided=list(self_provided or []),
            conditions=condition_rows,
            verification_path=verification_path,
        )

    @staticmethod
    def _evidence_listing(evidence: EvidenceSet) -> list[str]:
        """Return the non-empty evidence references recorded for this run."""
        listing: list[str] = []
        for name in (*_PATH_FIELDS, "verification_json", "screenshots", "evidence"):
            value = getattr(evidence, name, None)
            candidates = value if isinstance(value, list) else ([value] if value else [])
            listing.extend(str(item) for item in candidates if item)
        return listing

    @staticmethod
    def _write_verification(
        *,
        directory: Path,
        gate_id: str,
        milestone: str,
        result: GateResult,
        evidence: EvidenceSet,
        condition_rows: list[dict[str, Any]],
        code: str | None,
        self_provided: list[str] | None = None,
    ) -> Path:
        """Write ``verification.json`` into the run directory (PRD 9.2)."""
        payload = {
            "schema_version": 1,
            "gate_id": gate_id,
            "milestone": milestone,
            "verifier_id": VERIFIER_ID,
            "verifier_version": VERIFIER_VERSION,
            "checked_at": result.checked_at,
            "passed": result.passed,
            "error_code": code,
            "failure_reasons": list(result.failure_reasons),
            "self_provided_by_verifier": list(self_provided or []),
            "conditions": list(condition_rows),
            "evidence": evidence.to_dict(),
            "evidence_references": list(result.evidence),
        }
        target = directory / VERIFICATION_FILENAME
        directory.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Verifier(id={self.verifier_id!r}, gates={len(self.known_gates())})"


def load_facts(*paths: str | Path) -> dict[str, Any]:
    """Load optional structured facts (``install.json``, ``persistence.json``).

    Missing files are simply absent from the returned mapping -- the
    conditions that need them then fail closed with ``facts_missing:*``.
    Malformed JSON raises ``CONFIG_INVALID`` so a broken fact file cannot be
    mistaken for "no data".
    """
    facts: dict[str, Any] = {}
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "fact file is not valid JSON",
                path=path.as_posix(),
                error=str(exc),
            ) from exc
        if isinstance(data, dict):
            facts[path.stem] = data
    return facts


def iter_condition_ids(gate_id: str) -> Iterable[str]:
    """Return the condition ids registered for ``gate_id`` (audit helper)."""
    return tuple(condition.condition_id for condition in Verifier.conditions_for(gate_id))


def describe_gate(gate_id: str) -> dict[str, Any]:
    """Return a JSON-serialisable description of a gate for reports."""
    return {
        "gate_id": gate_id,
        "verifier_id": VERIFIER_ID,
        "verifier_version": VERIFIER_VERSION,
        "conditions": [condition.to_dict() for condition in Verifier.conditions_for(gate_id)],
    }
