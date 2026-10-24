"""Run orchestrator: bind a set of gate conclusions to one acceptance verdict.

This module owns the *aggregation* half of the acceptance gate.  It never
re-implements a condition: every gate is still judged by
:meth:`unipad.controller.verifier.Verifier.evaluate_detailed`, called exactly
once per gate, and its :class:`~unipad.controller.verifier.GateOutcome` is
mapped onto axis A by :func:`verdict_from_outcome`.  What is added here is
everything the verifier has no room for:

* **tiering** -- blocking gates (``M0``-``M12`` plus the sub-gates) decide the
  release; advisory gates (``M13``-``M15``) are recorded and never block it
  (PRD 227);
* **dependency order** -- a gate whose prerequisites did not pass reads
  ``BLOCKED`` with ``blocked_by:<dep>``, never ``PASS`` (PRD 202);
* **the four fields** -- derived per check and per milestone, never assigned
  (project CLAUDE.md);
* **the bundle** -- ``runs/<run_id>/acceptance.json`` plus its evidence index
  and append-only ledger, written atomically and re-verified on load.

Exit codes
----------
``FAIL`` exits :data:`unipad.errors.ExitCode.FAILURE` (1) and nothing else
does: exit 1 means something actually failed.  ``BLOCKED``, ``UNVERIFIED`` and
``INCOMPLETE`` exit :data:`unipad.errors.ExitCode.BLOCKED` (3) -- an unfinished
gate is neither a success nor a failure, and PRD 7.5 forbids marking it
successful.  The same distinction is in the bundle, so a script can branch on
``aggregate.verdict`` without parsing text.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Sequence

from ..controller.verifier import VERIFIER_ID as _VERIFIER_ID
from ..controller.verifier import VERIFIER_VERSION as _VERIFIER_VERSION
from ..errors import ErrorCode, ExitCode, UniPadError, blocked
from .record import (
    CODE_STATES,
    ONSITE_STATUSES,
    WINDOWS_STATUSES,
    EvidenceRef,
    EvidenceScope,
    EnvironmentStamp,
    FourFields,
    Measurement,
    Verdict,
    VerdictKind,
    assert_payload_redacted,
    assert_verdict_wellformed,
    relativise,
    scope_at_least,
    worst_of,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..controller.verifier import GateOutcome

__all__ = [
    "ACCEPTANCE_GATE_ID",
    "ACCEPTANCE_GATE_VERSION",
    "BUNDLE_FILENAME",
    "BUNDLE_SCHEMA_VERSION",
    "AcceptanceBundle",
    "AcceptanceRunner",
    "CaseResult",
    "CaseSpec",
    "CheckDef",
    "CheckResult",
    "MilestoneResult",
    "MilestoneSpec",
    "RunOutcome",
    "detect_regressions",
    "exit_code_for",
    "load_bundle",
    "propagate_dependencies",
    "release_verdict",
    "roll_up_fields",
    "verdict_from_outcome",
    "write_bundle",
]

#: Identity of this gate, stamped into every bundle.
ACCEPTANCE_GATE_ID = "unipad.acceptance.gate"
ACCEPTANCE_GATE_VERSION = "1.0.0"

#: Schema of ``acceptance.json``.  Bump on any layout change and record the
#: reason in ``docs/decisions/`` (PRD 7.4: verification logic changes are
#: recorded independently of feature changes).
BUNDLE_SCHEMA_VERSION = 1

BUNDLE_FILENAME = "acceptance.json"
LEDGER_FILENAME = "acceptance-ledger.jsonl"
LOCK_FILENAME = "acceptance.lock"
EVIDENCE_DIRNAME = "acceptance"
EVIDENCE_INDEX_FILENAME = "evidence-index.json"

#: Aggregate verdict of a run that did not finish.  Never a pass.
INCOMPLETE = "INCOMPLETE"

#: Verdict -> process exit code.  Exit 1 is reserved for a real failure; an
#: unfinished run exits 3 so the two are distinguishable without parsing text.
_EXIT_CODES: dict[str, ExitCode] = {
    VerdictKind.PASS: ExitCode.OK,
    VerdictKind.FAIL: ExitCode.FAILURE,
    VerdictKind.BLOCKED: ExitCode.BLOCKED,
    VerdictKind.UNVERIFIED: ExitCode.BLOCKED,
    INCOMPLETE: ExitCode.BLOCKED,
}

#: Gate tiers (PRD 227).  ``blocking`` decides the release; ``advisory`` is
#: recorded and reported but never blocks it.
BLOCKING = "blocking"
ADVISORY = "advisory"
_TIERS: tuple[str, ...] = (BLOCKING, ADVISORY)

#: Field roll-up order: worst last, so a roll-up can only go down (design 7.3).
#: The vocabularies themselves are the canonical ones from
#: :mod:`unipad.acceptance.record` -- see the note there on why four modules
#: re-declaring their own words was a defect (F9).
_FIELD_ORDER: dict[str, tuple[str, ...]] = {
    "implementation": CODE_STATES,
    "windows_status": WINDOWS_STATUSES,
    "onsite_status": ONSITE_STATUSES,
}

#: What a column reads as when there is nothing to roll up.  An empty set is
#: "nothing established", which is not a pass and is not a blockage either --
#: a blockage has to name what blocks it, and there is nothing here to name.
#: Note that this is *not* ``_FIELD_ORDER[column][-1]`` for ``onsite_status``:
#: seeding a roll-up with the worst value would make every milestone read
#: ``BLOCKED`` no matter what its checks said.
_EMPTY_FIELD: dict[str, str] = {
    "implementation": "UNVERIFIED",
    "windows_status": "NOT RUN",
    "onsite_status": "UNVERIFIED",
}


def exit_code_for(verdict: str) -> ExitCode:
    """Return the process exit code for an aggregate verdict.

    Raises
    ------
    UniPadError
        ``CONFIG_INVALID`` when ``verdict`` is not one of the five aggregate
        values -- an unknown verdict must not fall back to "success".
    """
    try:
        return _EXIT_CODES[verdict]
    except KeyError as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown aggregate verdict",
            verdict=verdict,
            known_verdicts=sorted(_EXIT_CODES),
        ) from exc


def _now() -> str:
    """Return the current UTC timestamp in the verifier's format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(payload: Any) -> str:
    """Return the canonical JSON text used for digests."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bundle_digest(payload: Mapping[str, Any]) -> str:
    """Return the sha256 of ``payload`` with its ``bundle_id`` excluded."""
    body = {key: value for key, value in payload.items() if key != "bundle_id"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


# --- check definitions and results ---------------------------------------


@dataclass(frozen=True)
class CheckDef:
    """One registered acceptance check: pure data, no verdict.

    This is the registry row.  There is deliberately no probe *name* here: the
    probe is bound separately by :class:`AcceptanceRunner`, and a check with no
    bound probe reads ``BLOCKED`` with ``probe_not_registered:<id>`` rather
    than defaulting to a pass (PRD 7.4: there is no default probe).
    """

    check_id: str
    prd_citation: str
    requirement: str
    rejection: str
    tier: str
    gate_id: str
    milestone: str
    case_id: str | None = None
    depends_on: tuple[str, ...] = ()
    required_scope: str = EvidenceScope.SIMULATION
    metrics: tuple[str, ...] = ()
    code_state: str = "UNVERIFIED"
    blocker: str | None = None
    implementation_evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "depends_on", tuple(str(dep) for dep in self.depends_on))
        object.__setattr__(self, "metrics", tuple(str(metric) for metric in self.metrics))
        object.__setattr__(self, "implementation_evidence", tuple(self.implementation_evidence))
        if self.tier not in _TIERS:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown gate tier",
                check_id=self.check_id,
                tier=self.tier,
                known_tiers=list(_TIERS),
            )
        scope_at_least(self.required_scope, EvidenceScope.SIMULATION)
        if not self.check_id.strip() or not self.prd_citation.strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a check must name itself and cite the PRD",
                check_id=self.check_id,
                prd_citation=self.prd_citation,
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the definition."""
        return {
            "check_id": self.check_id,
            "prd_citation": self.prd_citation,
            "requirement": self.requirement,
            "rejection": self.rejection,
            "tier": self.tier,
            "gate_id": self.gate_id,
            "milestone": self.milestone,
            "case_id": self.case_id,
            "depends_on": list(self.depends_on),
            "required_scope": self.required_scope,
            "metrics": list(self.metrics),
            "code_state": self.code_state,
            "blocker": self.blocker,
            "implementation_evidence": [ref.to_dict() for ref in self.implementation_evidence],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CheckDef":
        """Rebuild a definition from :meth:`to_dict` output."""
        return cls(
            check_id=str(data["check_id"]),
            prd_citation=str(data["prd_citation"]),
            requirement=str(data.get("requirement", "")),
            rejection=str(data.get("rejection", "")),
            tier=str(data["tier"]),
            gate_id=str(data["gate_id"]),
            milestone=str(data["milestone"]),
            case_id=data.get("case_id"),
            depends_on=tuple(str(dep) for dep in data.get("depends_on", ())),
            required_scope=str(data.get("required_scope", EvidenceScope.SIMULATION)),
            metrics=tuple(str(metric) for metric in data.get("metrics", ())),
            code_state=str(data.get("code_state", "UNVERIFIED")),
            blocker=data.get("blocker"),
            implementation_evidence=tuple(
                EvidenceRef.from_dict(ref) for ref in data.get("implementation_evidence", ())
            ),
        )


@dataclass(frozen=True)
class CheckResult:
    """One evaluated check: its definition, its verdict and its environment.

    ``fields`` is derived, not stored.  Storing it would let a check claim a
    field its evidence does not support; deriving it means the four-field row
    in the bundle is a *consequence* of the verdict and the evidence scopes.
    """

    check: CheckDef
    verdict: Verdict
    environment: EnvironmentStamp
    started_at: str
    finished_at: str
    measurements: tuple[Measurement, ...] = ()
    gate_outcome: dict[str, Any] | None = None

    @property
    def check_id(self) -> str:
        """The id of the underlying definition."""
        return self.check.check_id

    @property
    def tier(self) -> str:
        """The tier of the underlying definition."""
        return self.check.tier

    @property
    def fields(self) -> FourFields:
        """The four-field record of this check (derived, read-only).

        ``blocker`` names what blocks the *on-site* state, so it is reported
        only while that state is actually blocked.  :attr:`CheckDef.blocker` is a
        static declaration of what *would* block the check; once the verdict
        rests on ``ONSITE`` evidence the declared gap is satisfied, and printing
        it anyway would put ``verdict: PASS`` and ``blocker: <a gap that is
        closed>`` on the same row -- a contradiction a bundle reader cannot
        resolve, and one that reads as "the gate passed something it says is
        blocked".  A ``BLOCKED`` verdict keeps its blocker unconditionally,
        because that is the one kind :class:`FourFields` refuses to construct
        without one.
        """
        blocker = self.check.blocker
        if blocker is None and self.verdict.kind == VerdictKind.BLOCKED:
            blocker = self.verdict.reasons[0]
        if self.verdict.kind != VerdictKind.BLOCKED and any(
            ref.scope == EvidenceScope.ONSITE for ref in self.verdict.evidence
        ):
            blocker = None
        return FourFields(
            check_id=self.check.check_id,
            verdict=self.verdict,
            evidence=self.verdict.evidence,
            blocker=blocker,
            implementation_evidence=self.check.implementation_evidence,
            code_state=self.check.code_state,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the result."""
        return {
            "check": self.check.to_dict(),
            "verdict": self.verdict.to_dict(),
            "four_fields": self.fields.to_dict(),
            "environment": self.environment.to_dict(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "measurements": [item.to_dict() for item in self.measurements],
            "gate_outcome": self.gate_outcome,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CheckResult":
        """Rebuild a result from :meth:`to_dict` output.

        The stored ``four_fields`` row is deliberately ignored: it is
        recomputed from the verdict, and the bundle loader compares the two.
        """
        return cls(
            check=CheckDef.from_dict(data["check"]),
            verdict=Verdict.from_dict(data["verdict"]),
            environment=EnvironmentStamp.from_dict(data["environment"]),
            started_at=str(data["started_at"]),
            finished_at=str(data["finished_at"]),
            measurements=tuple(
                Measurement.from_dict(item) for item in data.get("measurements", ())
            ),
            gate_outcome=data.get("gate_outcome"),
        )


@dataclass(frozen=True)
class CaseSpec:
    """One PRD 5.2 end-to-end case and the checks that must hold for it."""

    case_id: str
    prd_citation: str
    check_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_ids", tuple(str(cid) for cid in self.check_ids))
        if not self.case_id.strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID, "a case must have an id", case_id=self.case_id
            )


@dataclass(frozen=True)
class MilestoneSpec:
    """One PRD 6 milestone, its tier and its checks."""

    milestone: str
    tier: str
    gate_id: str
    check_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_ids", tuple(str(cid) for cid in self.check_ids))
        if self.tier not in _TIERS:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown gate tier",
                milestone=self.milestone,
                tier=self.tier,
                known_tiers=list(_TIERS),
            )


@dataclass(frozen=True)
class CaseResult:
    """A PRD 5.2 case rolled up from its checks."""

    case_id: str
    prd_citation: str
    check_ids: tuple[str, ...]
    verdict: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the case result."""
        return {
            "case_id": self.case_id,
            "prd_citation": self.prd_citation,
            "check_ids": list(self.check_ids),
            "verdict": self.verdict,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CaseResult":
        """Rebuild a case result from :meth:`to_dict` output."""
        return cls(
            case_id=str(data["case_id"]),
            prd_citation=str(data["prd_citation"]),
            check_ids=tuple(str(cid) for cid in data.get("check_ids", ())),
            verdict=str(data["verdict"]),
            reasons=tuple(str(reason) for reason in data.get("reasons", ())),
        )


@dataclass(frozen=True)
class MilestoneResult:
    """A PRD 6 milestone rolled up from its checks, four fields included.

    ``fields`` is the milestone row of the four-field table.  Each column is
    the worst value among the milestone's checks, so a milestone can never
    read better than its worst check.
    """

    milestone: str
    tier: str
    gate_id: str
    check_ids: tuple[str, ...]
    verdict: str
    fields: dict[str, Any]
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the milestone result."""
        return {
            "milestone": self.milestone,
            "tier": self.tier,
            "gate_id": self.gate_id,
            "check_ids": list(self.check_ids),
            "verdict": self.verdict,
            "four_fields": dict(self.fields),
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MilestoneResult":
        """Rebuild a milestone result from :meth:`to_dict` output."""
        return cls(
            milestone=str(data["milestone"]),
            tier=str(data["tier"]),
            gate_id=str(data["gate_id"]),
            check_ids=tuple(str(cid) for cid in data.get("check_ids", ())),
            verdict=str(data["verdict"]),
            fields=dict(data.get("four_fields", {})),
            reasons=tuple(str(reason) for reason in data.get("reasons", ())),
        )


# --- aggregation ----------------------------------------------------------


def _worst_field(values: Iterable[str], column: str) -> str:
    """Return the worst value of one four-field column.

    The accumulator starts at ``None`` rather than at the worst value, so an
    all-``UNVERIFIED`` set reads ``UNVERIFIED`` and not the column's floor.
    """
    order = _FIELD_ORDER[column]
    worst: str | None = None
    for value in values:
        if value not in order:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown four-field value",
                field=column,
                value=value,
                known_values=list(order),
            )
        if worst is None or order.index(value) > order.index(worst):
            worst = value
    return worst if worst is not None else _EMPTY_FIELD[column]


def roll_up_fields(records: Sequence[FourFields]) -> dict[str, Any]:
    """Roll a set of four-field records into one row, worst value per column.

    The roll-up only goes down, so a milestone cannot read ``PASS`` while one
    of its checks reads ``UNVERIFIED``.  There is no ``check_id``: a rolled-up
    row belongs to no single check, and inventing one from the first record
    would let a reader attribute a milestone's state to whichever check
    happened to be evaluated first.
    """
    if not records:
        return {
            "implementation": "UNVERIFIED",
            "windows_status": "NOT RUN",
            "onsite_status": "UNVERIFIED",
            "blocker": "no_checks",
        }
    return {
        "implementation": _worst_field(
            (record.implementation for record in records), "implementation"
        ),
        "windows_status": _worst_field(
            (record.windows_status for record in records), "windows_status"
        ),
        "onsite_status": _worst_field(
            (record.onsite_status for record in records), "onsite_status"
        ),
        "blocker": next((record.blocker for record in records if record.blocker), None) or "-",
    }


def release_verdict(
    checks: Sequence[CheckResult],
    *,
    coverage_gaps: Sequence[str] = (),
    run_complete: bool = True,
) -> tuple[str, tuple[str, ...]]:
    """Return the aggregate verdict of a run and the reasons behind it.

    Only the blocking tier decides the outcome; advisory failures are reported
    by the caller and never reach this function (PRD 227).  The last rule is
    the one that makes a green test suite unable to stand in for a missing
    guest: even when every blocking check reads ``PASS``, a run with no
    ``ONSITE`` evidence in the blocking tier is ``BLOCKED``, not ``PASS``.
    """
    if not run_complete:
        return INCOMPLETE, ("run_incomplete",)
    gaps = tuple(str(gap) for gap in coverage_gaps)
    if gaps:
        return VerdictKind.FAIL, tuple(f"coverage_gap:{gap}" for gap in gaps)
    blocking = [check for check in checks if check.tier == BLOCKING]
    for candidate in (VerdictKind.FAIL, VerdictKind.BLOCKED, VerdictKind.UNVERIFIED):
        hit = [check for check in blocking if check.verdict.kind == candidate]
        if hit:
            return candidate, tuple(_blocking_reason(check, candidate) for check in hit)
    if not any(
        ref.scope == EvidenceScope.ONSITE for check in blocking for ref in check.verdict.evidence
    ):
        # The last rule, and the one that makes a green test suite unable to
        # stand in for a missing guest (design 9.9).
        return VerdictKind.BLOCKED, ("no_onsite_evidence",)
    return VerdictKind.PASS, ()


def _blocking_reason(check: CheckResult, kind: str) -> str:
    """Return the reason slug one blocking check contributes to the aggregate."""
    if kind == VerdictKind.FAIL:
        return f"blocking_failed:{check.check_id}"
    if kind == VerdictKind.BLOCKED:
        return f"blocking_by:{check.check_id}:{check.verdict.reasons[0]}"
    return f"blocking_unverified:{check.check_id}"


def _assert_dependencies_known(checks: Mapping[str, CheckDef]) -> None:
    """Refuse a dependency that names no registered check."""
    for check in checks.values():
        for dep in check.depends_on:
            if dep not in checks:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "dependency names an unregistered check",
                    check_id=check.check_id,
                    dependency=dep,
                )


def _topological_order(checks: Mapping[str, CheckDef]) -> tuple[str, ...]:
    """Return the check ids in dependency order (Kahn); a cycle is refused."""
    _assert_dependencies_known(checks)
    remaining = {check_id: set(check.depends_on) for check_id, check in checks.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(check_id for check_id, deps in remaining.items() if not deps)
        if not ready:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "check dependencies form a cycle",
                dependency_cycle=sorted(remaining),
            )
        for check_id in ready:
            order.append(check_id)
            del remaining[check_id]
        for deps in remaining.values():
            deps.difference_update(ready)
    return tuple(order)


def propagate_dependencies(results: Sequence[CheckResult]) -> tuple[CheckResult, ...]:
    """Apply the dependency rules of PRD 202 and return the rewritten results.

    1. a ``PASS`` verdict is never rewritten -- a real result must survive;
    2. ``UNVERIFIED`` with a non-``PASS`` prerequisite becomes ``BLOCKED``
       with ``blocked_by:<dep>``;
    3. ``BLOCKED``/``FAIL`` keep their kind and gain
       ``dependency_not_passed:<dep>:<kind>`` per unsatisfied prerequisite;
    4. propagation never produces a ``PASS``.
    """
    by_id = {result.check_id: result for result in results}
    for check_id in _topological_order({cid: result.check for cid, result in by_id.items()}):
        result = by_id[check_id]
        verdict = result.verdict
        if verdict.kind == VerdictKind.PASS:
            continue
        unsatisfied = [
            dep for dep in result.check.depends_on if by_id[dep].verdict.kind != VerdictKind.PASS
        ]
        if not unsatisfied:
            continue
        notes = tuple(
            f"dependency_not_passed:{dep}:{by_id[dep].verdict.kind}" for dep in unsatisfied
        )
        if verdict.kind == VerdictKind.UNVERIFIED:
            verdict = Verdict(
                VerdictKind.BLOCKED,
                (f"blocked_by:{unsatisfied[0]}",) + verdict.reasons + notes,
                verdict.evidence,
                verdict.observed,
            )
        else:
            verdict = Verdict(
                verdict.kind,
                verdict.reasons + notes,
                verdict.evidence,
                verdict.observed,
            )
        by_id[check_id] = replace(result, verdict=verdict)
    return tuple(by_id[result.check_id] for result in results)


def verdict_from_outcome(
    outcome: "GateOutcome",
    *,
    evidence: Iterable[EvidenceRef] = (),
) -> Verdict:
    """Map one :class:`GateOutcome` onto axis A.

    The mapping lives in exactly one place on purpose:
    ``EVIDENCE_INCOMPLETE`` is *unverified*, not failed and certainly not
    passed (PRD 9.2: 证据缺失属于未验证); ``VERIFICATION_FAILED`` is failed; a
    clean outcome is a pass -- and a pass still needs evidence references,
    which the caller supplies, so a gate that passed without producing an
    artefact cannot be laundered into an acceptance pass here.
    """
    refs = tuple(evidence)
    reasons = tuple(str(reason) for reason in outcome.result.failure_reasons)
    if outcome.code is None:
        return Verdict.pass_(refs, reasons)
    if outcome.code == ErrorCode.EVIDENCE_INCOMPLETE:
        return Verdict(VerdictKind.UNVERIFIED, reasons or ("evidence_incomplete",), refs, True)
    if outcome.code == ErrorCode.VERIFICATION_FAILED:
        return Verdict.fail(reasons, refs)
    return Verdict.fail((f"gate_error:{outcome.code}",) + reasons, refs)


# --- the bundle -----------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceBundle:
    """One run's acceptance evidence: the single source of truth.

    ``four_fields`` and ``field_unverified`` are derived from ``checks`` and
    are serialised so a reader can detect tampering; the loader recomputes
    both and refuses a bundle that disagrees with its own checks.
    """

    run_id: str
    run_complete: bool
    environment: EnvironmentStamp
    checks: tuple[CheckResult, ...]
    written_at: str
    aggregate: dict[str, Any]
    bundle_id: str = ""
    cases: tuple[CaseResult, ...] = ()
    milestones: tuple[MilestoneResult, ...] = ()
    measurements: tuple[Measurement, ...] = ()
    coverage_gaps: tuple[str, ...] = ()
    regressions: tuple[dict[str, Any], ...] = ()
    suite: dict[str, Any] | None = None
    verifier_version_drift: bool = False
    trusted: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "cases", tuple(self.cases))
        object.__setattr__(self, "milestones", tuple(self.milestones))
        object.__setattr__(self, "measurements", tuple(self.measurements))
        object.__setattr__(self, "coverage_gaps", tuple(str(gap) for gap in self.coverage_gaps))
        object.__setattr__(self, "regressions", tuple(dict(item) for item in self.regressions))
        if not self.bundle_id:
            object.__setattr__(self, "bundle_id", "acc-" + bundle_digest(self.to_dict())[:16])

    @property
    def verdict(self) -> str:
        """The aggregate verdict of this run."""
        return str(self.aggregate.get("verdict", ""))

    @property
    def blocking_checks(self) -> tuple[CheckResult, ...]:
        """The checks that decide the release (PRD 227)."""
        return tuple(check for check in self.checks if check.tier == BLOCKING)

    @property
    def advisory_failures(self) -> tuple[str, ...]:
        """Advisory checks that did not pass: recorded, never blocking."""
        return tuple(
            f"{check.check_id}:{check.verdict.kind}"
            for check in self.checks
            if check.tier == ADVISORY and check.verdict.kind != VerdictKind.PASS
        )

    @property
    def four_fields(self) -> tuple[dict[str, Any], ...]:
        """The four-field row of every check, derived from its verdict."""
        return tuple(check.fields.to_dict() for check in self.checks)

    @property
    def field_unverified(self) -> tuple[dict[str, Any], ...]:
        """Every check whose on-site field is not ``PASS`` (CLAUDE.md 四字段)."""
        return tuple(
            {
                "check_id": row["check_id"],
                "milestone": _milestone_for(row["check_id"], self.milestones, self.checks),
                "blocker": row["blocker"],
            }
            for row in self.four_fields
            if row["onsite_status"] != "PASS"
        )

    @property
    def scope_summary(self) -> dict[str, int]:
        """How many evidence references of each scope this run carries."""
        summary = {
            scope: 0
            for scope in (EvidenceScope.SIMULATION, EvidenceScope.HOST_REAL, EvidenceScope.ONSITE)
        }
        for check in self.checks:
            for ref in check.verdict.evidence:
                if ref.scope in summary:
                    summary[ref.scope] += 1
        return summary

    @property
    def evidence_index(self) -> tuple[dict[str, Any], ...]:
        """Every distinct evidence reference, keyed by path."""
        seen: dict[str, dict[str, Any]] = {}
        for check in self.checks:
            for ref in check.verdict.evidence:
                seen.setdefault(ref.path, ref.to_dict())
        return tuple(seen[path] for path in sorted(seen))

    def to_dict(self) -> dict[str, Any]:
        """Return the ``acceptance.json`` payload."""
        return {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_id": self.bundle_id,
            "written_at": self.written_at,
            "run_complete": self.run_complete,
            "run_id": self.run_id,
            "acceptance_gate": {"id": ACCEPTANCE_GATE_ID, "version": ACCEPTANCE_GATE_VERSION},
            "verifier": {"id": _VERIFIER_ID, "version": _VERIFIER_VERSION},
            "environment": self.environment.to_dict(),
            "scope_summary": self.scope_summary,
            "checks": [check.to_dict() for check in self.checks],
            "cases": [case.to_dict() for case in self.cases],
            "milestones": [milestone.to_dict() for milestone in self.milestones],
            "measurements": [item.to_dict() for item in self.measurements],
            "suite": self.suite,
            "four_fields": list(self.four_fields),
            "field_unverified": list(self.field_unverified),
            "aggregate": dict(self.aggregate),
            "coverage_gaps": list(self.coverage_gaps),
            "regressions": [dict(item) for item in self.regressions],
            "verifier_version_drift": self.verifier_version_drift,
            "evidence_index": [dict(item) for item in self.evidence_index],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AcceptanceBundle":
        """Rebuild a bundle from its payload, recomputing every derived value.

        Anything that disagrees with the checks -- a hand-edited aggregate, a
        four-field row, a case or milestone roll-up, the bundle digest, or a
        run marked complete while the aggregate says otherwise -- is refused
        rather than tolerated, so a forged bundle cannot be loaded and
        therefore cannot be "re-checked".
        """
        version = data.get("schema_version")
        if version != BUNDLE_SCHEMA_VERSION:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unsupported acceptance bundle schema",
                schema_version=version,
                supported=BUNDLE_SCHEMA_VERSION,
            )
        for required in ("run_id", "written_at", "environment", "aggregate"):
            if required not in data:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "acceptance bundle is missing a required block",
                    missing=required,
                )
        checks = tuple(CheckResult.from_dict(item) for item in data.get("checks", ()))
        cases = tuple(CaseResult.from_dict(item) for item in data.get("cases", ()))
        milestones = tuple(MilestoneResult.from_dict(item) for item in data.get("milestones", ()))
        by_id = {check.check_id: check for check in checks}
        run_complete = bool(data.get("run_complete", False))
        coverage_gaps = tuple(str(gap) for gap in data.get("coverage_gaps", ()))
        aggregate = dict(data["aggregate"])
        verdict, reasons = release_verdict(
            checks, coverage_gaps=coverage_gaps, run_complete=run_complete
        )
        if aggregate.get("verdict") != verdict:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "stored aggregate does not match the checks",
                aggregate_mismatch={"stored": aggregate.get("verdict"), "recomputed": verdict},
            )
        if tuple(str(reason) for reason in aggregate.get("reasons", ())) != reasons:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "stored aggregate reasons do not match the checks",
                aggregate_mismatch={
                    "stored": list(aggregate.get("reasons", ())),
                    "recomputed": list(reasons),
                },
            )
        if not run_complete and verdict != INCOMPLETE:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "an incomplete run cannot carry a complete verdict",
                verdict=verdict,
            )
        expected_rows = tuple(check.fields.to_dict() for check in checks)
        stored_rows = tuple(dict(row) for row in data.get("four_fields", ()))
        if stored_rows != expected_rows:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "stored four-field rows do not match the checks",
                aggregate_mismatch={"stored": list(stored_rows), "recomputed": list(expected_rows)},
            )
        expected_unverified = tuple(
            {
                "check_id": row["check_id"],
                "milestone": _milestone_for(row["check_id"], milestones, checks),
                "blocker": row["blocker"],
            }
            for row in expected_rows
            if row["onsite_status"] != "PASS"
        )
        if tuple(dict(row) for row in data.get("field_unverified", ())) != expected_unverified:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "stored field-unverified list does not match the checks",
                aggregate_mismatch="field_unverified",
            )
        bundle = cls(
            run_id=str(data["run_id"]),
            run_complete=run_complete,
            environment=EnvironmentStamp.from_dict(data["environment"]),
            checks=checks,
            written_at=str(data["written_at"]),
            aggregate=aggregate,
            bundle_id=str(data.get("bundle_id", "")),
            cases=cases,
            milestones=milestones,
            measurements=tuple(
                Measurement.from_dict(item) for item in data.get("measurements", ())
            ),
            coverage_gaps=coverage_gaps,
            regressions=tuple(dict(item) for item in data.get("regressions", ())),
            suite=dict(data["suite"]) if isinstance(data.get("suite"), dict) else None,
            verifier_version_drift=bool(data.get("verifier_version_drift", False)),
            trusted=bool(data.get("trusted", True)),
        )
        if bundle_digest(data) != bundle_digest(bundle.to_dict()):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "bundle digest does not match its payload",
                bundle_id_mismatch=str(data.get("bundle_id", "")),
            )
        _assert_case_rollups(cases, by_id)
        _assert_milestone_rollups(milestones, by_id)
        return bundle


def _milestone_for(
    check_id: str,
    milestones: Sequence[MilestoneResult],
    checks: Sequence[CheckResult],
) -> str:
    """Return the milestone a check belongs to, or ``""`` when unmapped."""
    for milestone in milestones:
        if check_id in milestone.check_ids:
            return milestone.milestone
    for check in checks:
        if check.check_id == check_id:
            return check.check.milestone
    return ""


def _assert_case_rollups(cases: Sequence[CaseResult], by_id: Mapping[str, CheckResult]) -> None:
    """Refuse a case verdict that is not the worst of its checks."""
    for case in cases:
        expected = worst_of(
            by_id[check_id].verdict.kind for check_id in case.check_ids if check_id in by_id
        )
        if case.verdict != expected:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "stored case verdict does not match its checks",
                case_id=case.case_id,
                aggregate_mismatch={"stored": case.verdict, "recomputed": expected},
            )


def _assert_milestone_rollups(
    milestones: Sequence[MilestoneResult], by_id: Mapping[str, CheckResult]
) -> None:
    """Refuse a milestone verdict or four-field roll-up that its checks contradict."""
    for milestone in milestones:
        members = [by_id[cid] for cid in milestone.check_ids if cid in by_id]
        expected_verdict = worst_of(check.verdict.kind for check in members)
        if milestone.verdict != expected_verdict:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "stored milestone verdict does not match its checks",
                milestone=milestone.milestone,
                aggregate_mismatch={"stored": milestone.verdict, "recomputed": expected_verdict},
            )
        for column in _FIELD_ORDER:
            expected = _worst_field((check.fields.to_dict()[column] for check in members), column)
            stored = milestone.fields.get(column)
            if stored != expected:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "stored milestone four-field roll-up does not match its checks",
                    milestone=milestone.milestone,
                    field=column,
                    aggregate_mismatch={"stored": stored, "recomputed": expected},
                )


# --- writing and reading --------------------------------------------------


def _atomic_write(target: Path, text: str) -> Path:
    """Write ``text`` to ``target`` through a temporary file and a rename.

    The pattern is copied from
    :meth:`unipad.controller.experiments.ExperimentStore._write` rather than
    patched into it: a reader must never see a half-written bundle, and the
    existing store is not edited.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(target)
    return target


def _declared_digests(bundle: AcceptanceBundle) -> frozenset[str]:
    """Return every digest this bundle accounts for."""
    digests = {
        bundle.environment.host_fingerprint,
        bundle.environment.host_inventory_sha256,
    }
    suite = bundle.suite or {}
    for key in ("report_sha256",):
        value = suite.get(key)
        if isinstance(value, str):
            digests.add(value)
    for check in bundle.checks:
        for ref in (*check.verdict.evidence, *check.check.implementation_evidence):
            if ref.sha256:
                digests.add(ref.sha256)
    return frozenset(digests)


def write_bundle(bundle: AcceptanceBundle, run_dir: str | Path) -> Path:
    """Write ``acceptance.json`` and its evidence index into ``run_dir``.

    Gates run before anything is written, and each one refuses the write
    instead of producing a bundle that over-claims:

    1. every verdict re-passes :func:`assert_verdict_wellformed`;
    2. a ``PASS`` check must carry evidence at least as strong as its
       ``required_scope`` -- simulation evidence cannot satisfy an on-site
       requirement;
    3. the stored aggregate must equal the one recomputed from the checks,
       which is also what stops a bundle from claiming ``PASS`` while the
       blocking tier holds no ``ONSITE`` evidence;
    4. the payload must pass redaction (PRD 9.2) -- a bundle that would leak a
       token, a key or a user path is refused, not written and then cleaned.
    """
    for check in bundle.checks:
        assert_verdict_wellformed(check.verdict)
        if check.verdict.kind != VerdictKind.PASS:
            continue
        if not any(
            scope_at_least(ref.scope, check.check.required_scope) for ref in check.verdict.evidence
        ):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a PASS check needs evidence at least as strong as its required scope",
                check_id=check.check_id,
                required_scope=check.check.required_scope,
                scopes=sorted({ref.scope for ref in check.verdict.evidence}),
            )
    verdict, _ = release_verdict(
        bundle.checks, coverage_gaps=bundle.coverage_gaps, run_complete=bundle.run_complete
    )
    if bundle.aggregate.get("verdict") != verdict:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "refusing to write an aggregate the checks do not support",
            aggregate_mismatch={"stored": bundle.aggregate.get("verdict"), "recomputed": verdict},
        )
    payload = bundle.to_dict()
    assert_payload_redacted(payload, declared_digests=_declared_digests(bundle))
    directory = Path(run_dir)
    target = _atomic_write(
        directory / BUNDLE_FILENAME,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    _atomic_write(
        directory / EVIDENCE_DIRNAME / EVIDENCE_INDEX_FILENAME,
        json.dumps(
            list(bundle.evidence_index), ensure_ascii=False, sort_keys=True, indent=2
        )
        + "\n",
    )
    return target


def _read_json(path: Path, *, what: str) -> dict[str, Any]:
    """Read a JSON object from ``path``, failing closed on a broken file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"{what} is not valid JSON",
            path=path.name,
            error=str(exc),
        ) from exc
    if not isinstance(data, dict):
        raise UniPadError(ErrorCode.CONFIG_INVALID, f"{what} is not a JSON object", path=path.name)
    return data


def load_bundle(
    run_dir: str | Path,
    *,
    witness_patterns: Mapping[str, Any] | None = None,
) -> AcceptanceBundle:
    """Load a run's bundle and re-establish everything from disk.

    The point of the re-check is that a claim can be audited later using the
    bundle alone: the digest is recomputed, every evidence file is re-hashed,
    every on-site witness is re-read out of the file it names, and the
    aggregate, the four fields and the roll-ups are recomputed from the
    checks.  A ``witness:<id>`` reference is only accepted when the caller
    supplies the registered pattern, so an on-site claim cannot be asserted
    with an unresolvable witness.

    Raises
    ------
    UniPadError
        ``ARTIFACT_MISSING`` when the bundle is absent, ``CONFIG_INVALID``
        for any forgery, drift or unresolved witness.
    """
    directory = Path(run_dir)
    target = directory / BUNDLE_FILENAME
    if not target.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "acceptance bundle is missing",
            run_id=directory.name,
        )
    payload = _read_json(target, what="acceptance bundle")
    bundle = AcceptanceBundle.from_dict(payload)
    patterns = dict(witness_patterns or {})
    for check in bundle.checks:
        for ref in check.verdict.evidence:
            _verify_evidence_ref(directory, ref, patterns)
    verifier_block = payload.get("verifier") or {}
    if (
        verifier_block.get("id") not in (None, _VERIFIER_ID)
        or verifier_block.get("version") not in (None, _VERIFIER_VERSION)
    ):
        # A bundle written by other verification logic is never silently
        # accepted: it is loaded, marked stale, and reported (PRD 7.4).
        bundle = replace(bundle, verifier_version_drift=True, trusted=False)
    return bundle


def _verify_evidence_ref(directory: Path, ref: EvidenceRef, patterns: Mapping[str, Any]) -> None:
    """Re-hash one evidence file and re-check its witness, if it has one."""
    path = directory / ref.path
    if not path.is_file():
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "evidence file is missing",
            evidence_missing=ref.path,
        )
    if ref.sha256 is not None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != ref.sha256:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "evidence file changed since it was recorded",
                evidence_drift=ref.path,
            )
    if ref.scope != EvidenceScope.ONSITE or not ref.witness:
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if ref.witness.startswith("witness:"):
        pattern = patterns.get(ref.witness[len("witness:") :])
        if pattern is None or not pattern.search(text):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "on-site witness pattern is unresolvable or not found",
                witness_not_found=ref.path,
                witness=ref.witness,
            )
        return
    _file_name, _, remainder = ref.witness.partition(":")
    lineno_text, _, literal = remainder.partition(":")
    lines = text.splitlines()
    try:
        line = lines[int(lineno_text) - 1]
    except (ValueError, IndexError) as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "on-site witness names a line the evidence does not have",
            witness_not_found=ref.path,
            witness=ref.witness,
        ) from exc
    if literal not in line:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "on-site witness literal is not in the evidence",
            witness_not_found=ref.path,
            witness=ref.witness,
        )


def append_ledger(bundle: AcceptanceBundle, run_dir: str | Path) -> Path:
    """Append one line for this run to the append-only acceptance ledger.

    The ledger is never rewritten: a later success must not be able to hide an
    earlier failure (PRD 180/267), which is what makes regression detection
    possible at all.  Every append is flushed and fsynced before it returns,
    and an unterminated tail is closed off with a newline first so a dead
    writer cannot swallow the next entry.
    """
    entry = {
        "bundle_id": bundle.bundle_id,
        "run_id": bundle.run_id,
        "written_at": bundle.written_at,
        "verdict": bundle.verdict,
        "reasons": list(bundle.aggregate.get("reasons", ())),
        "check_ids": [check.check_id for check in bundle.checks],
        "checks": {check.check_id: check.verdict.kind for check in bundle.checks},
    }
    target = Path(run_dir) / LEDGER_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    prefix = "\n" if _tail_is_unterminated(target) else ""
    with target.open("a", encoding="utf-8") as handle:
        handle.write(prefix + json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return target


def _tail_is_unterminated(path: Path) -> bool:
    """True when ``path`` exists, holds bytes, and does not end a line."""
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            return False
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) != b"\n"


def _read_ledger(run_dir: str | Path) -> list[dict[str, Any]]:
    """Return every readable ledger entry; an unreadable line is skipped."""
    target = Path(run_dir) / LEDGER_FILENAME
    if not target.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def detect_regressions(
    checks: Sequence[CheckResult], run_dir: str | Path
) -> tuple[dict[str, Any], ...]:
    """Return the checks that got worse since the last run of the same set."""
    current = {check.check_id: check.verdict.kind for check in checks}
    check_ids = frozenset(current)
    previous: dict[str, Any] | None = None
    for entry in _read_ledger(run_dir):
        if frozenset(entry.get("check_ids", ())) == check_ids:
            previous = entry
    if previous is None:
        return ()
    before = previous.get("checks") or {}
    regressions: list[dict[str, Any]] = []
    for check_id in sorted(current):
        was = before.get(check_id)
        if not isinstance(was, str) or worst_of((was, current[check_id])) == was:
            continue
        regressions.append(
            {
                "kind": "verdict_regressed",
                "check_id": check_id,
                "from_run": previous.get("run_id"),
                "from_verdict": was,
                "to_verdict": current[check_id],
            }
        )
    return tuple(regressions)


# --- the orchestrator -----------------------------------------------------

#: A probe is a zero-argument callable returning a :class:`Verdict`.
Probe = Callable[[], Verdict]


@dataclass(frozen=True)
class RunOutcome:
    """What one acceptance run produced, and how the process should exit."""

    verdict: str
    reasons: tuple[str, ...]
    exit_code: ExitCode
    bundle: AcceptanceBundle
    bundle_path: Path

    @property
    def field_unverified(self) -> tuple[dict[str, Any], ...]:
        """The on-site unverified list the final report must carry (CLAUDE.md)."""
        return self.bundle.field_unverified

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the outcome."""
        return {
            "run_id": self.bundle.run_id,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "exit_code": int(self.exit_code),
            "bundle_path": self.bundle_path.name,
            "field_unverified": [dict(item) for item in self.field_unverified],
        }


@contextmanager
def _run_lock(run_dir: Path) -> Iterator[None]:
    """Hold ``acceptance.lock`` for the duration of one evaluation.

    A second *concurrent* evaluation of the same run is refused with
    ``BLOCKED`` instead of interleaving two writers into one bundle.

    A lock left behind by a process that no longer exists is a different
    thing, and refusing on it would wedge the run directory forever: the
    only recovery would be knowing to delete the file by hand, which is not
    something an operator can be expected to guess.  Measured on this host
    on 2026-10-24: two background runs were stopped, their lock files
    survived, and every later evaluation of those two run ids read BLOCKED
    with "another acceptance evaluation holds this run" -- a message that
    names a holder that is not there.

    So the recorded PID is checked before refusing, and a dead holder's lock
    is removed and re-acquired.  The PID is reused check, not a liveness
    proof: a recycled PID belonging to an unrelated process would let two
    evaluations interleave.  That is accepted because the alternative is a
    permanent wedge, and because the write itself is the last resort -- the
    bundle is rewritten atomically by its own writer, so a collision corrupts
    a bundle rather than the run.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    lock = run_dir / LOCK_FILENAME
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            if not _lock_holder_is_alive(lock):
                # A holder that is gone.  Remove and retry once; if the removal
                # loses a race the loop simply re-reads the new holder's PID.
                try:
                    lock.unlink()
                except FileNotFoundError:  # pragma: no cover - removed underneath us
                    pass
                continue
            raise blocked(
                "another acceptance evaluation holds this run",
                run_id=run_dir.name,
                lock=LOCK_FILENAME,
            ) from exc
    try:
        os.write(handle, f"{os.getpid()}\n".encode("utf-8"))
        os.close(handle)
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:  # pragma: no cover - removed underneath us
            pass


def _lock_holder_is_alive(lock: Path) -> bool:
    """Whether the process named in ``lock`` still exists.

    An unreadable or PID-less lock is treated as held: refusing is the safe
    answer when the file says something this process cannot interpret, and
    the message still names the lock so the operator can look at it.
    """
    try:
        text = lock.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return True
    if not text.isdigit():
        return True
    pid = int(text)
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists but belongs to someone else: a live holder, not a stale one.
        return True
    except OSError:  # pragma: no cover - platform without signals
        return True
    return True


class AcceptanceRunner:
    """Evaluate every registered check of a run and write its bundle.

    Parameters
    ----------
    run_dir:
        The run directory.  The bundle, the evidence index and the ledger are
        written inside it; no other file in it is touched.
    checks:
        The complete check universe.  Every entry gets a result row, so a
        registered check can never be silently absent from the report.
    probes:
        Zero-argument callables returning a :class:`Verdict`, keyed by check
        id.  A check with no probe reads ``BLOCKED`` with
        ``probe_not_registered:<id>`` -- there is no default probe.
    environment:
        The stamp of the host this run happened on.
    cases, milestones:
        The PRD 5.2 case and PRD 6 milestone rows; their verdicts and
        four-field roll-ups are recomputed here, never taken on trust.
    measurements:
        PRD 5.3 metrics, indexed by ``metric_id`` and attached to the checks
        that declare them.
    coverage_gaps:
        Registry-level coverage gaps (an unmapped case, an unknown gate).  A
        non-empty list fails the run: a missing case is a failure, not a
        disappearance.
    gate_outcomes:
        Embedded :meth:`GateOutcome.to_dict` payloads keyed by check id, with
        any absolute path expressed relative to ``run_dir`` first.
    suite:
        The caller's pytest summary, recorded as simulation-scoped evidence.
    witness_patterns:
        Compiled patterns for ``witness:<id>`` references, keyed by id.  The
        runner re-loads what it wrote, and an on-site claim whose witness names
        a registered pattern can only be re-established with the pattern in
        hand -- so the runner has to carry them, or such a check could never
        finish a run.
    checked_at:
        Injected timestamp for determinism; defaults to now (UTC).
    """

    def __init__(
        self,
        *,
        run_dir: str | Path,
        checks: Sequence[CheckDef],
        probes: Mapping[str, Probe],
        environment: EnvironmentStamp,
        cases: Sequence[CaseSpec] = (),
        milestones: Sequence[MilestoneSpec] = (),
        measurements: Sequence[Measurement] = (),
        coverage_gaps: Sequence[str] = (),
        gate_outcomes: Mapping[str, dict[str, Any]] | None = None,
        suite: dict[str, Any] | None = None,
        witness_patterns: Mapping[str, Any] | None = None,
        checked_at: str | None = None,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._checks: dict[str, CheckDef] = {}
        for check in checks:
            if check.check_id in self._checks:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "duplicate check id",
                    check_id=check.check_id,
                )
            self._checks[check.check_id] = check
        self._probes = dict(probes)
        self._environment = environment
        self._cases = tuple(cases)
        self._milestones = tuple(milestones)
        self._measurements = {item.metric_id: item for item in measurements}
        self._coverage_gaps = tuple(str(gap) for gap in coverage_gaps)
        self._gate_outcomes = dict(gate_outcomes or {})
        self._suite = suite
        self._witness_patterns = dict(witness_patterns or {})
        self._stamp = checked_at or _now()

    # -- driving -----------------------------------------------------------

    def run(self) -> RunOutcome:
        """Evaluate every check, write the bundle, and report how to exit.

        A ``BLOCKED`` check is an ordinary outcome and never raises.  A
        ``BaseException`` escaping a probe is not: the bundle is still
        written, marked incomplete, and the exception is re-raised so an
        interrupted run can never be read as a finished one.
        """
        with _run_lock(self._run_dir):
            results: list[CheckResult] = []
            try:
                for check_id in _topological_order(self._checks):
                    results.append(self._evaluate(check_id))
            except BaseException:
                self._finalise(tuple(results), run_complete=False)
                raise
            return self._finalise(tuple(results), run_complete=True)

    def _evaluate(self, check_id: str) -> CheckResult:
        """Run one check's probe and wrap the result."""
        check = self._checks[check_id]
        probe = self._probes.get(check_id)
        if probe is None:
            verdict = Verdict.blocked(f"probe_not_registered:{check_id}")
        else:
            try:
                verdict = probe()
            except Exception as exc:  # noqa: BLE001 - a crashed probe fails closed
                verdict = Verdict.fail((f"probe_error:{check_id}:{type(exc).__name__}",))
            else:
                if not isinstance(verdict, Verdict):
                    raise UniPadError(
                        ErrorCode.CONFIG_INVALID,
                        "a probe must return a Verdict",
                        check_id=check_id,
                        returned=type(verdict).__name__,
                    )
        return CheckResult(
            check=check,
            verdict=self._apply_metrics(check, verdict),
            environment=self._environment,
            started_at=self._stamp,
            finished_at=self._stamp,
            measurements=self._measurements_for(check),
            gate_outcome=self._embedded_outcome(check_id),
        )

    def _embedded_outcome(self, check_id: str) -> dict[str, Any] | None:
        """Return this check's gate outcome with run-relative paths."""
        outcome = self._gate_outcomes.get(check_id)
        if not isinstance(outcome, dict):
            return None
        embedded = dict(outcome)
        raw = embedded.get("verification_path")
        if isinstance(raw, str) and raw:
            embedded["verification_path"] = relativise(raw, run_dir=self._run_dir.as_posix())
        return embedded

    def _measurements_for(self, check: CheckDef) -> tuple[Measurement, ...]:
        """Return the measurements this check declares, in declaration order."""
        return tuple(
            self._measurements[metric_id]
            for metric_id in check.metrics
            if metric_id in self._measurements
        )

    def _apply_metrics(self, check: CheckDef, verdict: Verdict) -> Verdict:
        """Fold this check's metrics into its verdict (PRD 5.3).

        An unmeasured metric turns a ``PASS`` into ``UNVERIFIED``: a target
        nobody measured is not a target that was met.  A measured metric that
        missed its *initial* target is recorded as a reason but does not fail
        the check, because PRD 187 declares these numbers initial product
        goals rather than acceptance conditions.
        """
        unmeasured: list[str] = []
        missed: list[str] = []
        for metric_id in check.metrics:
            measurement = self._measurements.get(metric_id)
            if measurement is None:
                unmeasured.append(f"metric_not_measured:{metric_id}")
                continue
            reason = measurement.reason()
            if reason.startswith("metric_not_measured:"):
                unmeasured.append(reason)
            elif reason:
                missed.append(reason)
        if verdict.kind == VerdictKind.PASS and unmeasured:
            return Verdict(
                VerdictKind.UNVERIFIED,
                tuple(unmeasured) + verdict.reasons,
                verdict.evidence,
                verdict.observed,
            )
        if missed:
            return Verdict(
                verdict.kind, verdict.reasons + tuple(missed), verdict.evidence, verdict.observed
            )
        return verdict

    # -- finishing ---------------------------------------------------------

    def _finalise(self, results: tuple[CheckResult, ...], *, run_complete: bool) -> RunOutcome:
        """Propagate, roll up, write the bundle, and re-read what was written."""
        checks = propagate_dependencies(results)
        cases = tuple(self._case_result(spec, checks) for spec in self._cases)
        milestones = tuple(self._milestone_result(spec, checks) for spec in self._milestones)
        verdict, reasons = release_verdict(
            checks, coverage_gaps=self._coverage_gaps, run_complete=run_complete
        )
        bundle = AcceptanceBundle(
            run_id=self._run_dir.name,
            run_complete=run_complete,
            environment=self._environment,
            checks=checks,
            written_at=self._stamp,
            aggregate={
                "verdict": verdict,
                "reasons": list(reasons),
                "blocking_failures": [
                    f"{check.check_id}:{check.verdict.kind}"
                    for check in checks
                    if check.tier == BLOCKING and check.verdict.kind == VerdictKind.FAIL
                ],
                "advisory_failures": [
                    f"{check.check_id}:{check.verdict.kind}"
                    for check in checks
                    if check.tier == ADVISORY and check.verdict.kind != VerdictKind.PASS
                ],
            },
            cases=cases,
            milestones=milestones,
            measurements=tuple(
                self._measurements[metric_id]
                for metric_id in sorted(self._measurements)
                if any(metric_id in check.check.metrics for check in checks)
            ),
            coverage_gaps=self._coverage_gaps,
            regressions=detect_regressions(checks, self._run_dir),
            suite=self._suite,
        )
        path = write_bundle(bundle, self._run_dir)
        append_ledger(bundle, self._run_dir)
        # Re-read what was written: a bundle that cannot be re-established
        # from disk has not been recorded, only claimed.
        reloaded = load_bundle(self._run_dir, witness_patterns=self._witness_patterns)
        return RunOutcome(
            verdict=verdict,
            reasons=reasons,
            exit_code=exit_code_for(verdict),
            bundle=reloaded,
            bundle_path=path,
        )

    def _case_result(self, spec: CaseSpec, checks: Sequence[CheckResult]) -> CaseResult:
        """Roll one PRD 5.2 case up from its checks."""
        by_id = {check.check_id: check for check in checks}
        members = [by_id[cid] for cid in spec.check_ids if cid in by_id]
        return CaseResult(
            case_id=spec.case_id,
            prd_citation=spec.prd_citation,
            check_ids=spec.check_ids,
            verdict=worst_of(check.verdict.kind for check in members),
            reasons=_member_reasons(members),
        )

    def _milestone_result(
        self, spec: MilestoneSpec, checks: Sequence[CheckResult]
    ) -> MilestoneResult:
        """Roll one PRD 6 milestone up from its checks, four fields included."""
        by_id = {check.check_id: check for check in checks}
        members = [by_id[cid] for cid in spec.check_ids if cid in by_id]
        fields = roll_up_fields([check.fields for check in members])
        fields["check_id"] = spec.milestone
        return MilestoneResult(
            milestone=spec.milestone,
            tier=spec.tier,
            gate_id=spec.gate_id,
            check_ids=spec.check_ids,
            verdict=worst_of(check.verdict.kind for check in members),
            fields=fields,
            reasons=_member_reasons(members),
        )


def _member_reasons(members: Sequence[CheckResult]) -> tuple[str, ...]:
    """Return the first reason slug of every member that did not pass."""
    return tuple(
        f"{check.check_id}:{check.verdict.reasons[0]}"
        for check in members
        if check.verdict.kind != VerdictKind.PASS and check.verdict.reasons
    )
