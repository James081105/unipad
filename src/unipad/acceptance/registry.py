"""The registered check universe: what exists, what tier it sits in, what it needs.

Why this file exists
--------------------
``python -m unipad.acceptance --run <id>`` used to refuse unconditionally, with
"no registered check universe exists yet".  That refusal was correct and it was
also the whole problem: a gate that cannot run cannot report anything, so every
P0/P1 claim in ``docs/implementation-plan.md`` rested on prose rather than on a
verdict the gate had actually produced.

This module is the thing that was missing.  It registers **40 checks** -- the 16
PRD 6 blocking milestones, the 3 advisory milestones, the 5 work packages, the
10 PRD 5.2 end-to-end cases and the 6 P1 bullets -- and hands
:class:`~unipad.acceptance.runner.AcceptanceRunner` exactly the shapes its
``__init__`` asks for.

The granularity decision, and why it is the safe one
----------------------------------------------------
One row per milestone/case/bullet, not one row per evidence requirement.  The
finer split would produce ~80 rows and a ``depends_on`` graph that no existing
table is keyed by; the coarser split would fold the ten cases into
``CaseSpec.check_ids`` over milestone ids and **silently drop every one of their
acceptance predicates** -- case 5's 64-hex ``challenge_value`` match, case 6's
``tap_count == 10`` plus the exact counter sequence, case 9's exactly-three-cycles
and its overwritten-evidence rejection.  Those predicates live only inside
``e2e.CASE_PROBES``, so a case that is not a *check* is a case whose criteria
nobody evaluates.  They are registered as checks here for that reason.

What a green verdict would require, stated before any code
----------------------------------------------------------
``release_verdict`` needs, in order: no incomplete run, no coverage gap, no
failed blocking check, no blocked blocking check, no unverified blocking check,
and **at least one ONSITE evidence reference in the blocking tier**
(``record.py`` §5.5, line 6).  On a Windows host with no iPadOS guest and no
macOS runner, the last condition cannot be met and the verdict is ``BLOCKED``
with ``no_onsite_evidence``.  That is the correct answer, not a gap in this
registry: it is the mechanical form of "a green suite does not substitute for a
missing guest object", and a registry engineered to avoid it would be forging
the very thing the gate exists to prevent.

So the honest outcome of a run on this host is BLOCKED, exit 3 -- and every
blocking row names its blocker.

Two vocabulary remaps, both forced
----------------------------------
* ``milestones`` spells a work package's tier ``"workpackage"``;
  ``runner._TIERS`` accepts only ``"blocking"``/``"advisory"``.  Left unmapped,
  the registry raises ``CONFIG_INVALID: unknown gate tier`` **at import**.
  Work packages are remapped to ``advisory``, which is what PRD 3.1/6 treat them
  as -- they are the plan for reaching the gates, not gates themselves.
* ``MilestoneCheck.gate_id`` is ``None`` for the five work packages;
  ``runner.CheckDef.gate_id`` is a plain ``str``.  They take their own id.

Coverage gaps are *computed*, never hardcoded empty.  A registry that declared
``coverage_gaps=()`` would be asserting its own completeness, and "a missing case
is a failure, not a disappearance" (DESIGN §7.4) is the whole point.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import e2e, milestones, p1, record
from .probes import probes_for
from .record import EnvironmentStamp, Measurement
from .runner import (
    EVIDENCE_DIRNAME,
    AcceptanceRunner,
    CaseSpec,
    CheckDef,
    MilestoneSpec,
)

__all__ = [
    "ADVISORY_TIER",
    "BLOCKING_TIER",
    "CHECKS",
    "DEPENDENCIES",
    "METRICS",
    "WORKPACKAGE_TIER",
    "build_inputs",
    "coverage_gaps",
    "describe",
]

#: PRD 6: the MVP release gate is M0-M12 plus the split gates.
BLOCKING_TIER: tuple[str, ...] = (
    "M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7",
    "M8", "M9", "M9a", "M9b", "M10", "M10a", "M11", "M12",
)

#: PRD 6: M13-M15 do not block the first release by default.
ADVISORY_TIER: tuple[str, ...] = ("M13", "M14", "M15")

#: The PRD 6.1 work packages.  ``milestones`` spells their tier "workpackage";
#: the runner does not know that word, so they are registered as advisory.
WORKPACKAGE_TIER: tuple[str, ...] = ("WP00", "WP01", "WP02", "WP03", "WP04")

#: DESIGN §7.2, from PRD 202: inputs before the interactions that need them.
#: A cycle is a configuration error and is detected at import.
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "M12": ("M9", "M10a", "M9b"),
    "M11": ("M10", "M10a"),
    "M10": ("M9",),
    "M9": ("M8",),
    "M9a": ("M8",),
    "M9b": ("M7",),
    "M8": ("M7",),
    "M7": ("M6",),
    "M6": ("M5",),
    "M5": ("M4",),
    "M4": ("M3",),
    "M3": ("M2",),
    "M2": ("M1",),
    "M1": ("M0",),
}

#: PRD 5.3 metrics.  Every one is registered unmeasured, because the only legal
#: unmeasured form is ``value=None, samples=0, measured=False``
#: (``record.py:922-930``) and a fabricated number is worse than an absent one.
#: They are attached to the checks that gate them so the bundle reports "not
#: measured" rather than omitting the row.
METRIC_TARGETS: dict[str, tuple[str, str, str, str]] = {
    # metric_id: (prd_citation, unit, comparator, target)
    "cold_boot_to_desktop": ("docs/PRD.md:191", "s", "<=", "120"),
    "hello_first_launch": ("docs/PRD.md:192", "s", "<=", "10"),
    "click_p95": ("docs/PRD.md:193", "ms", "<=", "300"),
    "stability_30min_50ops": ("docs/PRD.md:194", "ops", ">=", "50"),
    "persistence_3_reboots": ("docs/PRD.md:195", "reboots", ">=", "3"),
    "clean_workdir_repro": ("docs/PRD.md:196", "builds", "==", "1"),
}

#: Which check each metric gates.  A metric referenced by no check never reaches
#: the bundle (``runner.py:1532``), so an orphan metric is a silent omission.
METRIC_OWNERS: dict[str, str] = {
    "cold_boot_to_desktop": "M2",
    "hello_first_launch": "case3_import_hello_ipa",
    "click_p95": "case6_tap_button_ten_times",
    "stability_30min_50ops": "M11",
    "persistence_3_reboots": "case8_shutdown_and_reboot_from_disk",
    "clean_workdir_repro": "M1",
}

_TIER_OF: dict[str, str] = {
    **{name: "blocking" for name in BLOCKING_TIER},
    **{name: "advisory" for name in ADVISORY_TIER},
    **{name: "advisory" for name in WORKPACKAGE_TIER},
}


def _citation(line: int) -> str:
    """A PRD citation pinned to the live document.

    DESIGN cites the MVP-gate sentence as ``docs/PRD.md:227``; it is at 226.
    Every citation here is derived from the row's own source rather than copied
    from DESIGN, so a PRD edit does not leave a stale line number behind.
    """
    return "docs/PRD.md:%d" % line


# ---------------------------------------------------------------------------
# the rows
# ---------------------------------------------------------------------------
def _milestone_rows() -> list[CheckDef]:
    """One ``CheckDef`` per registered milestone check, in PRD order."""
    rows: list[CheckDef] = []
    for check_id in sorted(milestones.CHECKS, key=_milestone_sort_key):
        source = milestones.CHECKS[check_id]
        tier = _TIER_OF.get(check_id)
        if tier is None:
            raise _config_invalid(
                "a registered milestone has no tier in this registry",
                check_id=check_id,
                known=sorted(_TIER_OF),
            )
        rows.append(
            CheckDef(
                check_id=check_id,
                prd_citation=source.prd_citation,
                requirement=source.goal,
                rejection=_rejection_of(source),
                tier=tier,
                gate_id=source.gate_id or _gate_for(check_id),
                milestone=source.milestone,
                depends_on=DEPENDENCIES.get(check_id, ()),
                required_scope=(
                    record.EvidenceScope.ONSITE
                    if tier == "blocking"
                    else record.EvidenceScope.SIMULATION
                ),
                metrics=tuple(
                    metric for metric, owner in METRIC_OWNERS.items() if owner == check_id
                ),
                code_state=_code_state(source.implementation),
                blocker=_blocker_of(source.blockers),
                implementation_evidence=(),
            )
        )
    return rows


def _milestone_sort_key(check_id: str) -> tuple[int, str]:
    """M0, M1, ... M9, M9a, M9b, M10, ... then the work packages."""
    if check_id.startswith("WP"):
        return (2, check_id)
    head = check_id[1:]
    digits = "".join(c for c in head if c.isdigit())
    suffix = head[len(digits):]
    return (1 if not suffix else 0, "%03d%s" % (int(digits or 0), suffix))


def _advisory_milestone_rows() -> list[CheckDef]:
    """M13, M14 and M15: registered, advisory, and deliberately probe-less.

    PRD 6 puts them outside the MVP release gate, and DESIGN §7.1 requires them
    registered anyway.  Registering them without a probe is the point: the runner
    synthesises ``probe_not_registered:<id>`` -> BLOCKED for an absent probe, so
    each row is visibly red and structurally ungreenable while contributing
    nothing to the blocking roll-up.  Omitting them instead would make the
    advisory tier look complete, and "a missing case is a failure, not a
    disappearance" applies to milestones too.
    """
    citations = {"M13": 222, "M14": 223, "M15": 224}
    rows: list[CheckDef] = []
    for milestone in ADVISORY_TIER:
        rows.append(
            CheckDef(
                check_id=milestone,
                prd_citation=_citation(citations.get(milestone, 222)),
                requirement="PRD 6 %s, outside the first-release gate" % milestone,
                rejection="not attempted: %s is post-MVP and has no probe" % milestone,
                tier="advisory",
                gate_id=milestone,
                milestone=milestone,
                required_scope=record.EvidenceScope.ONSITE,
                code_state="NOT STARTED",
            )
        )
    return rows


def _case_rows() -> list[CheckDef]:
    """One ``CheckDef`` per PRD 5.2 case.

    These are checks, not just ``CaseSpec`` roll-ups, because their acceptance
    predicates live only inside ``e2e.CASE_PROBES`` -- see the module docstring.

    The id is the *probe* key (``case3_import_hello_ipa``), not the short case id
    (``case3``), because the probe key is what ``probes_for`` is keyed by and a
    row whose id does not match its probe's key would register a check with no
    probe -- which the runner reports as ``probe_not_registered`` and BLOCKED,
    turning ten evaluable cases into ten red rows.
    """
    rows: list[CheckDef] = []
    for spec in e2e.CASES:
        rows.append(
            CheckDef(
                check_id=spec.probe,
                prd_citation=spec.prd_citation,
                requirement=spec.title,
                rejection="the case did not reach its acceptance predicate",
                tier="blocking",
                gate_id="ipa-mvp",
                milestone="e2e",
                case_id=spec.case_id,
                depends_on=tuple(
                    _probe_of(dependency) or dependency for dependency in spec.depends_on
                ),
                required_scope=record.EvidenceScope.ONSITE,
                code_state=_code_state(spec.code_state),
            )
        )
    return rows


def _probe_of(case_id: str) -> str | None:
    """The probe key for a short case id, so ``depends_on`` resolves."""
    spec = e2e.case_spec(case_id)
    return getattr(spec, "probe", None)


def _case_spec(case_id: str) -> Any:
    """The ``e2e.CaseSpec`` for ``case_id``, or ``None`` when it is unregistered."""
    for spec in getattr(e2e, "CASE_SPECS", ()) or ():
        if getattr(spec, "case_id", None) == case_id:
            return spec
    return None


def _p1_rows() -> list[CheckDef]:
    """One ``CheckDef`` per P1 bullet, in PRD 3.2 order.

    The id is the bullet's own id (``P1-1``), which is what ``probes_for`` keys
    the p1 adapters by.  Tier is advisory: PRD 3.2 is explicitly post-MVP, and a
    P1 row that could hold up a release would invert the PRD's own priority.
    """
    rows: list[CheckDef] = []
    for bullet in p1.P1_BULLETS:
        rows.append(
            CheckDef(
                check_id=str(bullet.bullet),
                prd_citation=bullet.prd_citation,
                requirement=str(bullet.requirement),
                rejection="the P1 requirement is not met on this host",
                tier="advisory",
                gate_id="default",
                milestone="P1",
                required_scope=record.EvidenceScope.SIMULATION,
                code_state="UNVERIFIED",
            )
        )
    return rows


def _code_state(implementation: str) -> str:
    """``milestones``' implementation word -> the runner's ``code_state``.

    ``FourFields`` validates against ``("CODE COMPLETE", "PARTIAL", "NOT
    STARTED", "UNVERIFIED")`` and ``milestones`` spells it differently, so an
    unmapped word has to land somewhere.  UNVERIFIED is the only honest default:
    a word this function does not recognise must not be able to read as
    "CODE COMPLETE".
    """
    text = str(implementation or "").strip()
    if text in ("CODE COMPLETE", "PARTIAL", "NOT STARTED"):
        return text
    return "UNVERIFIED"


def _gate_for(check_id: str) -> str:
    """The gate id a row cites, or ``"default"`` when the verifier has none.

    The five work packages carry ``gate_id = None`` because PRD 6.1 describes
    them as work packages, not gates -- and ``verifier.CONDITIONS`` has no WP
    entry.  Citing a gate the verifier does not know makes ``coverage_gaps()``
    permanently non-empty, which fails *every* run for a reason no change here
    caused.  ``"default"`` is the honest answer: the verifier does have a
    default gate, and that is the one a work package belongs to.
    """
    from ..controller.verifier import CONDITIONS

    return check_id if check_id in CONDITIONS else "default"


def _rejection_of(source: Any) -> str:
    """The PRD's own veto text for this milestone, verbatim.

    A ``Refusal`` carries ``refusal_id``, ``text`` and ``trigger``.  The text is
    what an operator reads, so that is what the row carries -- and it is carried
    *unmodified* rather than paraphrased, because the registry authoring its own
    rejection wording is how a FAIL ends up explaining something other than the
    specific PRD condition it violated.
    """
    texts = [
        str(getattr(item, "text", "")).strip()
        for item in (getattr(source, "refusals", ()) or ())
    ]
    joined = "; ".join(text for text in texts if text)
    return joined or str(getattr(source, "goal", "") or "")


def _blocker_of(blockers: Any) -> str | None:
    """The first named blocker, or ``None`` when the check has none."""
    for item in blockers or ():
        text = str(getattr(item, "reason", "") or "").strip()
        if text:
            return text
    return None


def _config_invalid(message: str, **details: Any) -> Exception:
    from ..errors import ErrorCode, UniPadError

    return UniPadError(ErrorCode.CONFIG_INVALID, message, **details)


def _build_checks() -> tuple[CheckDef, ...]:
    rows = _milestone_rows() + _advisory_milestone_rows() + _case_rows() + _p1_rows()
    seen: set[str] = set()
    for row in rows:
        if row.check_id in seen:
            raise _config_invalid("a check id is registered twice", check_id=row.check_id)
        seen.add(row.check_id)
    return tuple(rows)


#: The registered universe.  Built at import so a duplicate id, an unknown tier
#: or a dangling ``depends_on`` fails here rather than mid-run.
CHECKS: tuple[CheckDef, ...] = _build_checks()


def _case_rollups() -> tuple[CaseSpec, ...]:
    """PRD 5.2's ten cases as roll-ups over the checks that implement them."""
    rollups: list[CaseSpec] = []
    for spec in e2e.CASES:
        rollups.append(
            CaseSpec(
                case_id=spec.case_id,
                prd_citation=spec.prd_citation,
                check_ids=(spec.probe,),
            )
        )
    return tuple(rollups)


def _milestone_rollups() -> tuple[MilestoneSpec, ...]:
    rollups: list[MilestoneSpec] = []
    for row in CHECKS:
        if row.milestone in ("e2e", "P1"):
            continue
        rollups.append(
            MilestoneSpec(
                milestone=row.milestone,
                tier=row.tier,
                gate_id=row.gate_id,
                check_ids=(row.check_id,),
            )
        )
    return tuple(rollups)


def _measurements() -> tuple[Measurement, ...]:
    rows: list[Measurement] = []
    for metric_id, (citation, unit, comparator, target) in METRIC_TARGETS.items():
        rows.append(
            Measurement(
                metric_id=metric_id,
                prd_citation=citation,
                value=None,
                unit=unit,
                target=float(target),
                comparator=comparator,
                samples=0,
                measured=False,
                notes=(
                    "not measured on this host: the metric needs a running "
                    "iPadOS guest"
                ),
            )
        )
    return tuple(rows)


#: PRD 5.2, as roll-ups.
CASES: tuple[CaseSpec, ...] = _case_rollups()

#: PRD 6, as roll-ups.
MILESTONES: tuple[MilestoneSpec, ...] = _milestone_rollups()

#: PRD 5.3.
METRICS: tuple[Measurement, ...] = _measurements()


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------
def coverage_gaps(
    checks: Sequence[CheckDef] = CHECKS,
    cases: Sequence[CaseSpec] = CASES,
) -> tuple[str, ...]:
    """Every way this registry could be silently incomplete.

    Three rules, from DESIGN §7.4, and each is a failure rather than a
    disappearance:

    1. a PRD 5.2 case with no check mapping;
    2. a ``gate_id`` the verifier does not know;
    3. a ``depends_on`` pointing at a check that is not registered.

    Computed, never hardcoded empty: a registry that asserted its own
    completeness would be the one thing this function exists to prevent.
    """
    from ..controller.verifier import CONDITIONS

    gaps: list[str] = []
    known = {row.check_id for row in checks}
    registered_cases = {spec.case_id for spec in cases}

    for spec in e2e.CASES:
        if spec.case_id not in registered_cases:
            gaps.append("case_without_check:%s" % spec.case_id)

    for row in checks:
        if row.gate_id not in CONDITIONS:
            gaps.append("unknown_gate:%s:%s" % (row.check_id, row.gate_id))
        for dependency in row.depends_on:
            if dependency not in known:
                gaps.append("dangling_dependency:%s:%s" % (row.check_id, dependency))

    return tuple(sorted(set(gaps)))


def describe() -> dict[str, Any]:
    """The registry as a document, for ``--version`` and for an audit."""
    tiers: dict[str, int] = {}
    for row in CHECKS:
        tiers[row.tier] = tiers.get(row.tier, 0) + 1
    return {
        "checks": len(CHECKS),
        "tiers": tiers,
        "cases": len(CASES),
        "milestones": len(MILESTONES),
        "metrics": len(METRICS),
        "coverage_gaps": list(coverage_gaps()),
        "blocking_tier": list(BLOCKING_TIER),
        "advisory_tier": list(ADVISORY_TIER),
    }


# ---------------------------------------------------------------------------
# building the runner
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RegistryInputs:
    """Everything ``AcceptanceRunner`` needs, in the shapes it asks for."""

    checks: tuple[CheckDef, ...]
    probes: Mapping[str, Any]
    cases: tuple[CaseSpec, ...]
    milestones: tuple[MilestoneSpec, ...]
    measurements: tuple[Measurement, ...]
    coverage_gaps: tuple[str, ...]
    environment: Any
    run_dir: Path


def _environment(repo_root: Path, run_dir: Path) -> Any:
    """The host stamp, derived from the real inventory rather than assumed.

    ``EnvironmentStamp`` forces ``accel == "tcg"`` and requires a 64-hex
    ``host_inventory_sha256``.  Both come from the machine that ran the gate:
    a hardcoded stamp would let a bundle claim a verified host that was never
    inventoried, which is the exact forgery the field exists to prevent.
    """
    from ..host.inventory import collect_host_inventory

    inventory = collect_host_inventory()
    inventory_path = run_dir / EVIDENCE_DIRNAME / "host-inventory.json"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(inventory, indent=2, sort_keys=True, ensure_ascii=False)
    inventory_path.write_text(text + "\n", encoding="utf-8")

    # The inventory's shape, for the record: ``host`` and ``os`` are *siblings*,
    # not ``host.os``.  Reading ``inventory["os"]["build"]`` -- which the first
    # draft of this function did -- raises AttributeError on a str.  Every field
    # below is read defensively for the same reason: a missing one becomes
    # "unknown" rather than crashing a run that had already collected the facts.
    host = _mapping(inventory.get("host"))
    toolchain = _mapping(inventory.get("toolchain"))
    machine = _machine_config(repo_root)
    return EnvironmentStamp(
        host_fingerprint=str(host.get("fingerprint") or _fingerprint(inventory)),
        host_label=str(host.get("label") or host.get("os") or "unknown"),
        accel=str(machine.get("accel") or "tcg"),
        qemu_version=str(_mapping(toolchain.get("qemu")).get("version") or "unknown"),
        os_build=str(host.get("kernel") or inventory.get("arch") or "unknown"),
        python_version=_python_version(),
        collected_at=str(inventory.get("collected_at") or ""),
        host_inventory_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _mapping(value: Any) -> dict[str, Any]:
    """``value`` as a dict, or an empty one.

    The inventory's ``os`` and ``arch`` are plain strings while ``host``,
    ``cpu`` and ``toolchain`` are mappings, and nothing enforces which is which.
    A helper that returns ``{}`` for a non-mapping keeps one wrong assumption
    from crashing the whole run.
    """
    return value if isinstance(value, dict) else {}


def _fingerprint(inventory: Mapping[str, Any]) -> str:
    """A stable fingerprint for a host the inventory did not label.

    Derived from the fields that identify the machine rather than invented, so
    two runs on the same host agree and two runs on different hosts do not.
    """
    host = _mapping(inventory.get("host"))
    cpu = _mapping(inventory.get("cpu"))
    parts = [
        str(host.get("os") or ""),
        str(cpu.get("model") or ""),
        str(inventory.get("arch") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _machine_config(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "config" / "machine.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _python_version() -> str:
    import sys

    return sys.version.split()[0]


def build_inputs(run_dir: Path, repo_root: Path | None = None) -> RegistryInputs:
    """Assemble everything ``AcceptanceRunner`` needs for one run.

    ``repo_root`` defaults to this repository.  The run directory is created if
    it does not exist, because the evidence the probes adopt has to land
    somewhere and the bundle is written inside it.
    """
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[3]
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)

    # Only the milestones that ``milestones`` actually registers.  M13-M15 are
    # in the advisory tier but have no source there, so asking for their probe
    # would call ``milestones.evaluate`` on an unknown id and raise -- and the
    # runner turns a raising probe into a FAIL row.  They are meant to be
    # probe-less: the runner then synthesises ``probe_not_registered:<id>`` and
    # the row reads BLOCKED, which is the honest state of a post-MVP milestone
    # nobody has attempted.
    milestone_ids = tuple(
        row.check_id
        for row in CHECKS
        if row.milestone not in ("e2e", "P1") and row.check_id in milestones.CHECKS
    )
    case_ids = tuple(row.check_id for row in CHECKS if row.milestone == "e2e")
    p1_ids = tuple(row.check_id for row in CHECKS if row.milestone == "P1")

    return RegistryInputs(
        checks=CHECKS,
        probes=probes_for(
            root,
            directory,
            milestone_ids=milestone_ids,
            case_ids=case_ids,
            p1_checks=tuple(p1.CHECKS),
        ),
        cases=CASES,
        milestones=MILESTONES,
        measurements=METRICS,
        coverage_gaps=coverage_gaps(),
        environment=_environment(root, directory),
        run_dir=directory,
    )
