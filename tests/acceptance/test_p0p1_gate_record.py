"""Load-bearing tests for the four-field acceptance record and its orchestrator.

What is pinned here
-------------------
Five properties, in the order the briefing names them:

1. **An unevidenced PASS is not constructible.**  Every combination of
   ``Verdict("PASS", ...)`` that lacks evidence or an observation raises, the
   sanctioned factory included, and a hand-edited payload that claims a PASS
   with an empty evidence list is refused on load.  PRD 9.2: 证据缺失属于未验证.
2. **The four fields are independent and cannot be copied into each other.**
   ``implementation`` / ``windows_status`` / ``onsite_status`` are derived
   read-only properties, so fifty simulation references cannot lift the on-site
   field, ``CODE COMPLETE`` cannot either, and neither can be assigned.
3. **BLOCKED and UNVERIFIED are distinct from FAIL and from each other** -- as
   kinds, as observation state, in the badness order every roll-up uses, and in
   that only ``BLOCKED`` must name what blocks it.
4. **The bundle round-trips from disk**, and a forged row, a forged aggregate
   or a replaced artefact is refused on load rather than tolerated.
5. **The exit code distinguishes FAILED from BLOCKED.**  Exit 1 is reserved
   for something that actually failed; ``BLOCKED``, ``UNVERIFIED`` and
   ``INCOMPLETE`` exit 3.  A green blocking tier with no on-site evidence is
   ``BLOCKED`` -- never ``PASS``, and never a failure.

Four more that the five depend on, because a roll-up or a propagation that
lies would quietly undo all of the above:

* a roll-up takes the *worst* of its members and never invents a blocker --
  seeding it with the column's floor would report every milestone ``BLOCKED``
  and, since the loader recomputes with the same helper, accept a forged one;
* a probe's own ``PASS`` is never rewritten by dependency propagation, while an
  ``UNVERIFIED`` dependent of a non-``PASS`` prerequisite becomes ``BLOCKED``
  and names it (design 7.2);
* a probe that raises is a ``FAIL`` with the exception type in the reason, an
  unregistered probe is ``BLOCKED`` with ``probe_not_registered:<id>``, and a
  ``BaseException`` leaves an ``INCOMPLETE`` bundle rather than a finished one;
* the environment stamp refuses a non-TCG accelerator and a host inventory
  digest that is not a digest -- the redaction gate *declares* that field, so
  it has to be one.

No real QEMU is launched, no port is bound and no network is touched: the
probes are plain closures and the only files written live under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

import pytest

from unipad.acceptance.record import (
    EvidenceRef,
    EvidenceScope,
    EnvironmentStamp,
    FourFields,
    Verdict,
    VerdictKind,
    worst_of,
)
from unipad.acceptance.runner import (
    ADVISORY,
    BLOCKING,
    BUNDLE_FILENAME,
    EVIDENCE_DIRNAME,
    EVIDENCE_INDEX_FILENAME,
    LEDGER_FILENAME,
    AcceptanceRunner,
    CaseSpec,
    CheckDef,
    CheckResult,
    INCOMPLETE,
    MilestoneSpec,
    load_bundle,
    propagate_dependencies,
    roll_up_fields,
)
from unipad.errors import ErrorCode, ExitCode, UniPadError

# --- helpers ---------------------------------------------------------------

_FINGERPRINT = hashlib.sha256(b"host-fingerprint").hexdigest()
_INVENTORY = hashlib.sha256(b"host-inventory").hexdigest()


def _stamp() -> EnvironmentStamp:
    """A host stamp that is allowed to be recorded: TCG, no user paths."""
    return EnvironmentStamp(
        host_fingerprint=_FINGERPRINT,
        host_label="win-x86_64-tcg",
        accel="tcg",
        qemu_version="9.2.0",
        os_build="10.0.26200",
        python_version="3.12.9",
        collected_at="2026-01-01T00:00:00+00:00",
        host_inventory_sha256=_INVENTORY,
    )


def _content(path: str) -> bytes:
    """The deterministic content every fixture file is written with."""
    return f"unipad acceptance fixture: {path}\n".encode("utf-8")


def _digest_of(path: str) -> str:
    """Return the digest of the content :func:`_materialise` writes for ``path``."""
    return hashlib.sha256(_content(path)).hexdigest()


def _ref(
    path: str,
    *,
    scope: str = EvidenceScope.SIMULATION,
    kind: str = "serial_log",
    witness: str | None = None,
    sha256: str | None = None,
) -> EvidenceRef:
    """Build one admissible evidence reference.

    The digest defaults to the digest of the content :func:`_materialise`
    writes, so a reference and its file always agree -- the loader re-hashes
    every reference and refuses a mismatch, which is the point.
    """
    return EvidenceRef(
        path=path,
        kind=kind,
        sha256=sha256 or _digest_of(path),
        scope=scope,
        witness=witness,
        observed_at="2026-01-01T00:00:00+00:00",
    )


def _onsite_ref(path: str) -> EvidenceRef:
    """Build one on-site reference, witness included (the only way to get one)."""
    return _ref(path, scope=EvidenceScope.ONSITE, kind="guest_log", witness=f"{path}:1:fixture")


def _simulation_refs(count: int) -> tuple[EvidenceRef, ...]:
    """``count`` distinct simulation references, as many as a caller could hang."""
    return tuple(
        _ref(f"logs/line-{index}.log", scope=EvidenceScope.SIMULATION) for index in range(count)
    )


def _materialise(run_dir: Path, refs: Iterable[EvidenceRef]) -> None:
    """Write every referenced file under ``run_dir`` with its recorded content."""
    for ref in refs:
        target = run_dir / ref.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_content(ref.path))


def _check(
    check_id: str,
    *,
    tier: str = BLOCKING,
    required_scope: str = EvidenceScope.SIMULATION,
    depends_on: tuple[str, ...] = (),
) -> CheckDef:
    """One registered check: pure data, no verdict."""
    return CheckDef(
        check_id=check_id,
        prd_citation="PRD 6",
        requirement="the registered condition holds",
        rejection="the registered condition did not hold",
        tier=tier,
        gate_id=f"gate-{check_id}",
        milestone="M0",
        depends_on=depends_on,
        required_scope=required_scope,
    )


def _run(
    tmp_path: Path,
    *,
    name: str,
    checks: Iterable[CheckDef],
    probes: dict[str, Any],
    **kwargs: Any,
):
    """Evaluate one run in its own directory and return the outcome."""
    return AcceptanceRunner(
        run_dir=tmp_path / name,
        checks=tuple(checks),
        probes=probes,
        environment=_stamp(),
        checked_at="2026-01-01T00:00:00+00:00",
        **kwargs,
    ).run()


# --- 1. an unevidenced PASS is not constructible --------------------------


def test_an_unevidenced_pass_cannot_be_constructed() -> None:
    """Every shape of ``PASS`` without evidence or observation is refused.

    The message is asserted, not just the exception: a sibling guard must not
    be able to refuse an unevidenced PASS for the wrong reason and leave this
    test green while the real rule is gone.
    """
    ref = _ref("serial.log")
    with pytest.raises(UniPadError) as no_evidence:
        Verdict(VerdictKind.PASS, (), (), False)
    assert no_evidence.value.code == ErrorCode.CONFIG_INVALID
    assert "requires at least one evidence reference" in str(no_evidence.value.message)

    with pytest.raises(UniPadError) as not_observed:
        Verdict(VerdictKind.PASS, (), (ref,), False)
    assert "requires an observation" in str(not_observed.value.message)

    with pytest.raises(UniPadError) as observed_only:
        Verdict(VerdictKind.PASS, (), (), True)
    assert "requires at least one evidence reference" in str(observed_only.value.message)

    # The sanctioned factory is bound by the same rule, so the only way in is
    # through it with evidence.
    for factory_args in (((),), ((), ("looks-fine",))):
        with pytest.raises(UniPadError) as factory_refused:
            Verdict.pass_(*factory_args)
        assert "requires at least one evidence reference" in str(factory_refused.value.message)

    passed = Verdict.pass_((ref,))
    assert passed.kind == VerdictKind.PASS
    assert passed.observed is True
    assert passed.evidence == (ref,)


def test_a_non_pass_without_a_reason_is_refused() -> None:
    """A FAIL/BLOCKED/UNVERIFIED that says nothing is not a verdict."""
    for kind in (VerdictKind.FAIL, VerdictKind.BLOCKED, VerdictKind.UNVERIFIED):
        with pytest.raises(UniPadError) as excinfo:
            Verdict(kind, (), (), True)
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    with pytest.raises(UniPadError):
        Verdict("SOMEDAY", ("reason",), (), True)


def test_a_forged_pass_is_refused_on_load() -> None:
    """A payload that claims PASS with no evidence cannot be rebuilt."""
    ref = _ref("serial.log")
    forged = Verdict.pass_((ref,)).to_dict()
    forged["evidence"] = []
    with pytest.raises(UniPadError) as excinfo:
        Verdict.from_dict(forged)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert "requires at least one evidence reference" in str(excinfo.value.message)


def test_evidence_without_a_digest_must_say_why() -> None:
    """F5: pin ``record.py``'s own copy of the "no hash and no explanation" guard.

    The adversarial battery's mutation 14/14 disabled exactly this guard and
    survived all four gate test files: only ``e2e.py``'s parallel copy of the
    same rule was tested.  This is ``record.py``'s, and the message is asserted
    rather than just the exception, so a sibling guard cannot refuse the shape
    for the wrong reason and leave the real rule unpinned.

    The complement is asserted too, so this test cannot be satisfied by a
    constructor that refuses every reference: a ``digest_reason`` is a
    legitimate answer, and a reference that carries one is constructible.
    """
    with pytest.raises(UniPadError) as excinfo:
        EvidenceRef(path="serial.log", kind="serial_log", sha256=None, digest_reason=None)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert "evidence without a digest must say why" in str(excinfo.value.message)

    # An empty or whitespace reason is not a reason.
    for blank in ("", "   ", "\t"):
        with pytest.raises(UniPadError) as blank_exc:
            EvidenceRef(path="serial.log", kind="serial_log", sha256=None, digest_reason=blank)
        assert blank_exc.value.code == ErrorCode.CONFIG_INVALID
        assert "must say why" in str(blank_exc.value.message)

    # A real reason is accepted, and a real digest is accepted without one.
    explained = EvidenceRef(
        path="serial.log", kind="serial_log", sha256=None, digest_reason="unreadable"
    )
    assert explained.sha256 is None
    assert explained.digest_reason == "unreadable"
    digested = EvidenceRef(path="serial.log", kind="serial_log", sha256=_digest_of("serial.log"))
    assert digested.digest_reason is None


def test_a_digestless_reference_cannot_carry_a_pass() -> None:
    """C2 at the construction boundary: an unverifiable PASS is not buildable.

    The audit proved that a PASS resting on ``EvidenceRef(sha256=None,
    digest_reason='unreadable')`` was written as a real bundle and accepted by
    ``load_bundle``, because the loader only re-hashes when ``sha256`` is not
    ``None``.  The fix is at construction rather than at load: a reference
    without a digest is still *representable* -- a ``FAIL`` or ``BLOCKED`` check
    must be able to cite an artefact it could not read -- so the rule belongs to
    the verdict that claims the condition holds, not to the reference.  Every
    boundary (``Verdict`` construction, ``Verdict.pass_``, ``write_bundle`` via
    :func:`assert_verdict_wellformed`, and ``load_bundle`` via
    ``Verdict.from_dict``) re-runs the same ``__post_init__``, so one rule
    closes all four.
    """
    unreadable = EvidenceRef(
        path="serial.log", kind="serial_log", sha256=None, digest_reason="unreadable"
    )

    # A non-PASS verdict may cite it: that is the whole point of digest_reason.
    for verdict in (
        Verdict.fail(("the artefact could not be read",), (unreadable,)),
        Verdict.blocked("no artefact was produced"),
        Verdict.unverified("nobody established it"),
    ):
        assert verdict.kind != VerdictKind.PASS
    carrying = Verdict(VerdictKind.BLOCKED, ("blocked",), (unreadable,), False)
    assert carrying.evidence == (unreadable,)

    # A PASS may not, however it is built.
    for build in (
        lambda: Verdict(VerdictKind.PASS, ("reason",), (unreadable,), True),
        lambda: Verdict.pass_((unreadable,)),
        lambda: Verdict.pass_((_ref("logs/serial.log"), unreadable)),
    ):
        with pytest.raises(UniPadError) as excinfo:
            build()
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID
        assert "can be re-verified" in str(excinfo.value.message)
        assert excinfo.value.details.get("undigested_evidence") == ["serial.log"]

    # A PASS whose references all carry digests is untouched by the new rule.
    assert Verdict.pass_((_ref("logs/serial.log"),)).kind == VerdictKind.PASS


def test_a_pass_on_an_undigested_artefact_is_refused_at_every_boundary(tmp_path: Path) -> None:
    """C2 end to end: no write, and no load of a hand-forged bundle.

    The construction guard already makes the shape unbuildable through the
    sanctioned factories, so the remaining route to a bad bundle is a payload
    edited on disk.  That route is closed by ``Verdict.from_dict`` re-running
    the same ``__post_init__``, which is what this exercises: a bundle whose
    PASS check carries a digestless reference is refused on load, not merely
    recomputed-and-disagreed-with.
    """
    run_dir = tmp_path / "run"
    simulated, onsite = _write_evidence(run_dir)
    checks = (
        _check("m0-onsite", required_scope=EvidenceScope.ONSITE),
        _check("m1-simulation"),
    )
    outcome = _run(
        tmp_path,
        name="run",
        checks=checks,
        probes={
            "m0-onsite": lambda: Verdict.pass_((onsite,)),
            "m1-simulation": lambda: Verdict.pass_((simulated,)),
        },
    )
    assert outcome.verdict == VerdictKind.PASS

    # Route 1: the sanctioned path.  A probe that tries to build the unverifiable
    # PASS raises, and the runner turns a raised probe into a FAIL rather than a
    # pass -- so the run is recorded as a failure and nothing is released.  The
    # artefact is materialised so the *only* thing that can go wrong here is the
    # digest rule: a missing file would refuse the load for an unrelated reason
    # and the assertion would be red for the wrong cause.
    unreadable = EvidenceRef(
        path="logs/serial.log", kind="serial_log", sha256=None, digest_reason="unreadable"
    )
    undigested_dir = tmp_path / "undigested"
    undigested_dir.mkdir(parents=True, exist_ok=True)
    _materialise(undigested_dir, (unreadable,))
    outcome = _run(
        tmp_path,
        name="undigested",
        checks=(_check("m1-undigested"),),
        probes={"m1-undigested": lambda: Verdict.pass_((unreadable,))},
    )
    assert outcome.verdict == VerdictKind.FAIL
    recorded = {check.check_id: check.verdict.kind for check in outcome.bundle.checks}
    assert recorded["m1-undigested"] == VerdictKind.FAIL
    assert "UniPadError" in " ".join(
        reason for check in outcome.bundle.checks for reason in check.verdict.reasons
    )
    assert (undigested_dir / BUNDLE_FILENAME).is_file()
    reloaded = load_bundle(undigested_dir)
    assert reloaded.verdict == VerdictKind.FAIL

    # Route 2: the forged payload.  Strip the digest out of a recorded PASS and
    # reload; the bundle must refuse rather than accept an unverifiable PASS.
    target = run_dir / BUNDLE_FILENAME
    payload = json.loads(target.read_text(encoding="utf-8"))
    row = next(
        check
        for check in payload["checks"]
        if check["check"]["check_id"] == "m1-simulation"
    )
    row["verdict"]["evidence"][0]["sha256"] = None
    row["verdict"]["evidence"][0]["digest_reason"] = "unreadable"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    with pytest.raises(UniPadError) as load_refused:
        load_bundle(run_dir)
    assert load_refused.value.code == ErrorCode.CONFIG_INVALID
    assert "can be re-verified" in str(load_refused.value.message)


def test_onsite_scope_is_decided_by_the_witness_not_the_claimant() -> None:
    """``ONSITE`` without a witness is not constructible (design 9.5/9.7)."""
    with pytest.raises(UniPadError) as excinfo:
        _ref("guest.log", scope=EvidenceScope.ONSITE)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert "witness" in str(excinfo.value.message)

    # A malformed witness is refused too, including one that is only a name.
    for bad in ("", "witness:", "guest.log:DARWIN", "guest.log:1:", "C:/x/guest.log:1:y"):
        with pytest.raises(UniPadError):
            _ref("guest.log", scope=EvidenceScope.ONSITE, witness=bad)

    # A witness does not by itself make something on-site: a firmware banner on
    # a serial log is HOST_REAL however long it is (PRD 7.4).
    host_real = _ref("serial.log", scope=EvidenceScope.HOST_REAL, witness="serial.log:1:EDK2")
    assert host_real.scope == EvidenceScope.HOST_REAL
    fields = FourFields(
        check_id="c-host-real",
        verdict=Verdict.pass_((host_real,)),
        evidence=(host_real,),
    )
    assert fields.windows_status == "SIMULATION PASS"
    assert fields.onsite_status == "UNVERIFIED"


def test_a_pass_without_the_required_scope_is_refused_at_write_time(
    tmp_path: Path,
) -> None:
    """Simulation evidence cannot satisfy an on-site requirement (design 5.3)."""
    check = _check("m0-onsite-required", required_scope=EvidenceScope.ONSITE)
    probes = {"m0-onsite-required": lambda: Verdict.pass_((_ref("serial.log"),))}
    with pytest.raises(UniPadError) as excinfo:
        _run(tmp_path, name="weak-scope", checks=(check,), probes=probes)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert "required scope" in str(excinfo.value.message)


def test_the_environment_stamp_refuses_another_accelerator() -> None:
    """A run that did not happen on this host under TCG is not this run."""
    with pytest.raises(UniPadError) as excinfo:
        replace(_stamp(), accel="whpx")
    assert excinfo.value.code == ErrorCode.ACCELERATOR_NOT_TCG


def test_the_environment_stamp_requires_a_real_inventory_digest() -> None:
    """The inventory digest is declared by the redaction gate, so it must be one."""
    for bad in ("", "to be measured", _FINGERPRINT[:16], _FINGERPRINT.upper()):
        with pytest.raises(UniPadError) as excinfo:
            replace(_stamp(), host_inventory_sha256=bad)
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_a_rolled_up_row_belongs_to_no_single_check() -> None:
    """A roll-up reports the worst of its members and claims no check id."""
    members = (
        FourFields(
            check_id="c-first",
            verdict=Verdict.pass_((_ref("logs/a.log"),)),
            evidence=(_ref("logs/a.log"),),
            code_state="CODE COMPLETE",
            implementation_evidence=(_ref("src/a.py"),),
        ),
        FourFields(
            check_id="c-second",
            verdict=Verdict.unverified("nobody_ran_it"),
            evidence=(),
        ),
    )
    row = roll_up_fields(members)
    assert "check_id" not in row
    # Worst per column, never the first member's value: the second check has no
    # implementation evidence and never ran, so both columns go down.
    assert row["implementation"] == "UNVERIFIED"
    assert row["windows_status"] == "NOT RUN"
    assert row["onsite_status"] == "UNVERIFIED"
    assert roll_up_fields(())["blocker"] == "no_checks"


def test_a_roll_up_does_not_invent_a_blocker() -> None:
    """An all-UNVERIFIED roll-up reads UNVERIFIED, not the column's floor.

    ``BLOCKED`` is the worst ``onsite_status`` value, so seeding the roll-up
    with the floor would report every milestone as blocked -- and, because the
    loader recomputes the roll-up with the same helper, a forged ``BLOCKED``
    row would then be *accepted* as agreeing with its checks.
    """
    members = tuple(
        FourFields(check_id=f"c-{index}", verdict=Verdict.unverified("nobody_ran_it"))
        for index in range(3)
    )
    row = roll_up_fields(members)
    assert row["onsite_status"] == "UNVERIFIED"
    assert row["blocker"] == "-"

    # One genuinely blocked check does drag the column down, and names itself.
    blocked = FourFields(
        check_id="c-blocked",
        verdict=Verdict.blocked("no_system_image"),
        blocker="no_system_image",
    )
    dragged = roll_up_fields((*members, blocked))
    assert dragged["onsite_status"] == "BLOCKED"
    assert dragged["blocker"] == "no_system_image"


# --- 2. the four fields are independent -----------------------------------


def test_the_four_fields_are_independent() -> None:
    """Simulation evidence lifts the simulation field and nothing else."""
    simulated = _simulation_refs(50)
    onsite = _onsite_ref("guest.log")

    # Evidence that the code exists does not double as evidence that it ran.
    implementation_only = FourFields(
        check_id="c-implementation",
        verdict=Verdict.unverified("nobody_ran_it"),
        evidence=(),
        implementation_evidence=simulated,
        code_state="CODE COMPLETE",
    )
    assert implementation_only.implementation == "CODE COMPLETE"
    assert implementation_only.windows_status == "NOT RUN"
    assert implementation_only.onsite_status == "UNVERIFIED"

    # Fifty simulation references cannot lift the on-site field.
    simulation_pass = FourFields(
        check_id="c-simulation",
        verdict=Verdict.pass_(simulated),
        evidence=simulated,
        code_state="CODE COMPLETE",
        implementation_evidence=simulated,
    )
    assert simulation_pass.windows_status == "SIMULATION PASS"
    assert simulation_pass.onsite_status == "UNVERIFIED"

    # Only on-site evidence does, and it needs a witness to exist at all.
    onsite_pass = FourFields(
        check_id="c-onsite",
        verdict=Verdict.pass_((onsite,)),
        evidence=(onsite,),
        code_state="CODE COMPLETE",
        implementation_evidence=simulated,
    )
    assert onsite_pass.windows_status == "NOT RUN"
    assert onsite_pass.onsite_status == "PASS"

    # A FAIL verdict with on-site evidence is a failed on-site check, not an
    # unverified one.
    onsite_fail = FourFields(
        check_id="c-onsite-fail",
        verdict=Verdict.fail(("persistence_marker_mismatch",)),
        evidence=(onsite,),
    )
    assert onsite_fail.onsite_status == "FAIL"

    # The simulation field reads the verdict too: evidence that something ran
    # and failed is "FAIL", not "SIMULATION PASS".  Reading only the evidence
    # would let a failed run report the same row as a passing one.
    simulation_fail = FourFields(
        check_id="c-simulation-fail",
        verdict=Verdict.fail(("persistence_marker_mismatch",)),
        evidence=simulated,
    )
    assert simulation_fail.windows_status == "FAIL"
    assert simulation_fail.onsite_status == "UNVERIFIED"


def test_the_four_fields_cannot_be_assigned() -> None:
    """The environment fields are derived: there is no setter to abuse."""
    fields = FourFields(
        check_id="c-readonly",
        verdict=Verdict.pass_((_ref("serial.log"),)),
        code_state="CODE COMPLETE",
        implementation_evidence=(_ref("impl.py"),),
    )
    for column in ("implementation", "windows_status", "onsite_status"):
        with pytest.raises(AttributeError):
            setattr(fields, column, "PASS")
    # ... and the inputs are frozen too.
    with pytest.raises(AttributeError):
        fields.code_state = "CODE COMPLETE"  # type: ignore[misc]


def test_code_complete_without_implementation_evidence_is_unverified() -> None:
    """A claimed implementation state with nothing behind it reads UNVERIFIED."""
    claimed = FourFields(
        check_id="c-claimed",
        verdict=Verdict.unverified("nobody_ran_it"),
        code_state="CODE COMPLETE",
    )
    assert claimed.implementation == "UNVERIFIED"
    proved = FourFields(
        check_id="c-proved",
        verdict=Verdict.unverified("nobody_ran_it"),
        code_state="CODE COMPLETE",
        implementation_evidence=(_ref("src/unipad/acceptance/record.py"),),
    )
    assert proved.implementation == "CODE COMPLETE"


# --- 3. BLOCKED and UNVERIFIED are distinct -------------------------------


def test_blocked_and_unverified_are_distinct_from_fail_and_from_each_other() -> None:
    """Four kinds, four different statements about what is known."""
    failed = Verdict.fail(("condition_not_met",))
    blocked = Verdict.blocked("no_system_image")
    unverified = Verdict.unverified("nobody_ran_it")

    assert len({failed.kind, blocked.kind, unverified.kind}) == 3
    # FAIL observed a result and reports it; the other two did not observe one.
    assert failed.observed is True
    assert blocked.observed is False
    assert unverified.observed is False
    assert blocked.evidence == () and unverified.evidence == ()
    assert blocked.reasons == ("no_system_image",)
    assert unverified.reasons == ("nobody_ran_it",)

    # Badness order: a definite failure dominates everything.
    assert worst_of([]) == VerdictKind.UNVERIFIED
    assert worst_of([VerdictKind.PASS]) == VerdictKind.PASS
    assert worst_of([VerdictKind.PASS, VerdictKind.UNVERIFIED]) == VerdictKind.UNVERIFIED
    assert (
        worst_of([VerdictKind.PASS, VerdictKind.UNVERIFIED, VerdictKind.BLOCKED])
        == VerdictKind.BLOCKED
    )
    assert (
        worst_of(
            [VerdictKind.UNVERIFIED, VerdictKind.BLOCKED, VerdictKind.FAIL, VerdictKind.PASS]
        )
        == VerdictKind.FAIL
    )


def test_a_pass_check_does_not_report_a_blocker_it_has_satisfied(tmp_path: Path) -> None:
    """F6: a passing check must not name a blocker that is actually satisfied.

    ``CheckResult.fields`` used to report ``CheckDef.blocker`` verbatim whatever
    the verdict was, so a check that PASSed on ``ONSITE`` evidence emitted a
    four-field row reading ``verdict: PASS`` next to ``blocker: <the gap the
    PASS just closed>``.  A reader cannot tell whether the gate passed the thing
    or is still waiting for it.

    The row is now self-consistent: the blocker column is non-empty exactly when
    ``onsite_status`` reads ``BLOCKED``.  Both directions are pinned, because a
    fix that simply always blanked the column would hide the genuine on-site
    blockage that a Windows-only PASS must report.
    """
    # A PASS resting on on-site evidence: the declared blocker is satisfied.
    satisfied_dir = tmp_path / "satisfied"
    _, onsite = _write_evidence(satisfied_dir)
    satisfied = _check("m6-onsite-pass", required_scope=EvidenceScope.ONSITE)
    satisfied = replace(satisfied, blocker="no guest log has ever been witnessed")
    outcome = _run(
        tmp_path,
        name="satisfied",
        checks=(satisfied,),
        probes={"m6-onsite-pass": lambda: Verdict.pass_((onsite,))},
    )
    assert outcome.verdict == VerdictKind.PASS
    row = next(
        item for item in outcome.bundle.four_fields if item["check_id"] == "m6-onsite-pass"
    )
    assert row["onsite_status"] == "PASS"
    assert row["blocker"] == "-", row
    # Noted, not fixed here: ``windows_status`` reads only SIMULATION/HOST_REAL
    # references, so a PASS resting on an ONSITE reference alone reports
    # "NOT RUN" for the Windows column even though the on-site run necessarily
    # happened on this host under TCG.  Changing that would alter every roll-up
    # in the report, so it is recorded as an observation rather than silently
    # adjusted inside the F6 fix.
    assert row["windows_status"] == "NOT RUN", row

    # The same check with simulation-only evidence: the on-site half is still
    # blocked, and the blocker is exactly what must be reported.
    blocked_dir = tmp_path / "still-blocked"
    simulated, _ = _write_evidence(blocked_dir)
    still_blocked = replace(_check("m6-sim-pass"), blocker="no guest log has ever been witnessed")
    outcome = _run(
        tmp_path,
        name="still-blocked",
        checks=(still_blocked,),
        probes={"m6-sim-pass": lambda: Verdict.pass_((simulated,))},
    )
    # The check itself reads PASS; the aggregate is BLOCKED, because a blocking
    # tier with no on-site evidence may not release (the rule the existing
    # green-tier test pins).  The four-field row is what is under test here.
    check_verdicts = {item.check_id: item.verdict.kind for item in outcome.bundle.checks}
    assert check_verdicts["m6-sim-pass"] == VerdictKind.PASS
    assert outcome.verdict == VerdictKind.BLOCKED
    row = next(
        item for item in outcome.bundle.four_fields if item["check_id"] == "m6-sim-pass"
    )
    assert row["onsite_status"] == "BLOCKED"
    assert row["blocker"] == "no guest log has ever been witnessed", row
    assert row["windows_status"] == "SIMULATION PASS", row

    # The invariant, over every row of both runs.
    for run_name in ("satisfied", "still-blocked"):
        reloaded = load_bundle(tmp_path / run_name)
        for item in reloaded.four_fields:
            assert (item["blocker"] != "-") == (item["onsite_status"] == "BLOCKED"), item


def test_only_a_blocked_check_must_name_its_blocker() -> None:
    """A BLOCKED verdict with no blocker is refused; UNVERIFIED is not asked for one."""
    with pytest.raises(UniPadError) as excinfo:
        FourFields(check_id="c-blocked", verdict=Verdict.blocked("no_system_image"))
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    blocked = FourFields(
        check_id="c-blocked",
        verdict=Verdict.blocked("no_system_image"),
        blocker="no_system_image",
    )
    assert blocked.onsite_status == "BLOCKED"
    assert blocked.to_dict()["blocker"] == "no_system_image"

    unverified = FourFields(check_id="c-unverified", verdict=Verdict.unverified("nobody_ran_it"))
    assert unverified.onsite_status == "UNVERIFIED"
    assert unverified.to_dict()["blocker"] == "-"


# --- 4. the bundle round-trips from disk ----------------------------------


def _write_evidence(run_dir: Path) -> tuple[EvidenceRef, EvidenceRef]:
    """Write two evidence files and return references to them."""
    run_dir.mkdir(parents=True, exist_ok=True)
    serial = _ref("logs/serial.log")
    guest = _onsite_ref("guest.log")
    _materialise(run_dir, (serial, guest))
    return serial, guest


def test_the_bundle_round_trips_from_disk(tmp_path: Path) -> None:
    """Write, re-read, and compare: a bundle that cannot be re-read is not recorded."""
    run_dir = tmp_path / "run"
    simulated, onsite = _write_evidence(run_dir)

    checks = (
        _check("m0-onsite", required_scope=EvidenceScope.ONSITE),
        _check("m1-simulation"),
        _check("m13-advisory", tier=ADVISORY),
    )
    probes = {
        "m0-onsite": lambda: Verdict.pass_((onsite,)),
        "m1-simulation": lambda: Verdict.pass_((simulated,)),
        # An advisory failure is recorded and never blocks the release (PRD 227).
        "m13-advisory": lambda: Verdict.fail(("advisory_condition_not_met",)),
    }
    outcome = _run(
        tmp_path,
        name="run",
        checks=checks,
        probes=probes,
        cases=(
            CaseSpec(
                case_id="case1",
                prd_citation="PRD 5.2",
                check_ids=("m0-onsite", "m1-simulation"),
            ),
        ),
        milestones=(
            MilestoneSpec(
                milestone="M0", tier=BLOCKING, gate_id="gate-m0-onsite",
                check_ids=("m0-onsite", "m1-simulation"),
            ),
        ),
    )

    assert outcome.verdict == VerdictKind.PASS
    assert outcome.exit_code is ExitCode.OK
    assert outcome.reasons == ()
    assert outcome.bundle.advisory_failures == ("m13-advisory:FAIL",)

    payload = json.loads((run_dir / BUNDLE_FILENAME).read_text(encoding="utf-8"))
    assert payload == outcome.bundle.to_dict()
    assert (run_dir / EVIDENCE_DIRNAME / EVIDENCE_INDEX_FILENAME).is_file()

    reloaded = load_bundle(run_dir)
    assert reloaded.to_dict() == payload
    assert reloaded.bundle_id == outcome.bundle.bundle_id
    assert reloaded.trusted is True
    assert reloaded.verifier_version_drift is False
    assert reloaded.scope_summary[EvidenceScope.ONSITE] == 1
    assert reloaded.scope_summary[EvidenceScope.SIMULATION] == 1
    assert reloaded.verdict == VerdictKind.PASS

    # The four-field table is a consequence of the evidence, not of a claim:
    # the simulation-only check still reads UNVERIFIED on site, and so does the
    # advisory check, which is why both are listed for the final report.
    rows = {row["check_id"]: row for row in reloaded.four_fields}
    assert rows["m0-onsite"]["onsite_status"] == "PASS"
    assert rows["m1-simulation"]["onsite_status"] == "UNVERIFIED"
    assert rows["m13-advisory"]["onsite_status"] == "UNVERIFIED"
    assert [item["check_id"] for item in reloaded.field_unverified] == [
        "m1-simulation",
        "m13-advisory",
    ]
    assert reloaded.field_unverified[0]["milestone"] == "M0"


def test_a_forged_four_field_row_is_refused_on_load(tmp_path: Path) -> None:
    """Editing the stored row does not edit the verdict it was derived from."""
    run_dir = tmp_path / "run"
    simulated, onsite = _write_evidence(run_dir)
    checks = (
        _check("m0-onsite", required_scope=EvidenceScope.ONSITE),
        _check("m1-simulation"),
    )
    _run(
        tmp_path,
        name="run",
        checks=checks,
        probes={
            "m0-onsite": lambda: Verdict.pass_((onsite,)),
            "m1-simulation": lambda: Verdict.pass_((simulated,)),
        },
    )

    target = run_dir / BUNDLE_FILENAME
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["four_fields"] = [dict(row) for row in payload["four_fields"]]
    forged = next(row for row in payload["four_fields"] if row["check_id"] == "m1-simulation")
    forged["onsite_status"] = "PASS"
    forged["blocker"] = "none"
    payload["aggregate"] = dict(payload["aggregate"], verdict="PASS")
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )

    with pytest.raises(UniPadError) as excinfo:
        load_bundle(run_dir)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_evidence_that_changed_is_refused_on_load(tmp_path: Path) -> None:
    """Replacing a recorded artefact breaks the bundle, not the report."""
    run_dir = tmp_path / "run"
    _, onsite = _write_evidence(run_dir)
    checks = (_check("m0-onsite", required_scope=EvidenceScope.ONSITE),)
    _run(
        tmp_path,
        name="run",
        checks=checks,
        probes={"m0-onsite": lambda: Verdict.pass_((onsite,))},
    )

    (run_dir / "guest.log").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(UniPadError) as excinfo:
        load_bundle(run_dir)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert "evidence_drift" in excinfo.value.details


def test_a_witness_that_is_not_in_the_evidence_is_refused(tmp_path: Path) -> None:
    """The witness is re-read out of the file it names (design 5.4/9.7)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    lying = _onsite_ref("guest.log")
    _materialise(run_dir, (lying,))
    # The format is well-formed, so the reference is constructible; what makes
    # it a lie is that the literal is not in the evidence it names.
    forged = replace(lying, witness="guest.log:1:NOT_PRESENT")
    checks = (_check("m0-onsite", required_scope=EvidenceScope.ONSITE),)
    with pytest.raises(UniPadError) as excinfo:
        _run(
            tmp_path,
            name="run",
            checks=checks,
            probes={"m0-onsite": lambda: Verdict.pass_((forged,))},
        )
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert "witness_not_found" in excinfo.value.details


def test_a_registry_witness_needs_the_registered_pattern(tmp_path: Path) -> None:
    """``witness:<id>`` is only re-established when the caller supplies it."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    registered = EvidenceRef(
        path="guest.log",
        kind="guest_log",
        sha256=_digest_of("guest.log"),
        scope=EvidenceScope.ONSITE,
        witness="witness:launchd",
        observed_at="2026-01-01T00:00:00+00:00",
    )
    _materialise(run_dir, (registered,))
    checks = (_check("m0-onsite", required_scope=EvidenceScope.ONSITE),)
    probes = {"m0-onsite": lambda: Verdict.pass_((registered,))}
    pattern = re.compile(r"fixture: guest\.log")

    outcome = _run(
        tmp_path, name="run", checks=checks, probes=probes, witness_patterns={"launchd": pattern}
    )
    assert outcome.verdict == VerdictKind.PASS
    assert outcome.bundle.trusted is True

    # The same bundle cannot be re-established without the pattern, nor with
    # one that does not match: an on-site claim is not self-certifying.
    for wrong in ({}, {"launchd": re.compile(r"never-here")}):
        with pytest.raises(UniPadError) as excinfo:
            load_bundle(run_dir, witness_patterns=wrong)
        assert "witness_not_found" in excinfo.value.details


def test_the_ledger_is_append_only_across_runs(tmp_path: Path) -> None:
    """A later success must not hide an earlier failure (PRD 180/267)."""
    run_dir = tmp_path / "run"
    first = _ref("logs/a.log")
    _materialise(run_dir, (first,))
    checks = (_check("m0-a"),)

    failing = _run(
        tmp_path, name="run", checks=checks, probes={"m0-a": lambda: Verdict.fail(("not_met",))}
    )
    passing = _run(
        tmp_path, name="run", checks=checks, probes={"m0-a": lambda: Verdict.pass_((first,))}
    )
    worsened = _run(
        tmp_path, name="run", checks=checks, probes={"m0-a": lambda: Verdict.fail(("regressed",))}
    )

    lines = (run_dir / LEDGER_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3, "the ledger is append-only: three runs, three lines"
    entries = [json.loads(line) for line in lines]
    assert [entry["checks"] for entry in entries] == [
        {"m0-a": VerdictKind.FAIL},
        {"m0-a": VerdictKind.PASS},
        {"m0-a": VerdictKind.FAIL},
    ]
    # The first failure is still readable after two later runs.
    assert entries[0]["verdict"] == VerdictKind.FAIL

    # Improving is not a regression; getting worse is, and it is named.
    assert passing.bundle.regressions == ()
    assert [
        (item["check_id"], item["from_verdict"], item["to_verdict"])
        for item in worsened.bundle.regressions
    ] == [("m0-a", VerdictKind.PASS, VerdictKind.FAIL)]
    assert failing.bundle.verdict == VerdictKind.FAIL


# --- 5. the exit code distinguishes FAILED from BLOCKED -------------------


def test_the_exit_code_distinguishes_failed_from_blocked(tmp_path: Path) -> None:
    """Exit 1 means something failed; exit 3 means the gate is unfinished."""
    failed = _run(
        tmp_path,
        name="failed",
        checks=(_check("m0-fails"),),
        probes={"m0-fails": lambda: Verdict.fail(("condition_not_met",))},
    )
    blocked = _run(
        tmp_path,
        name="blocked",
        checks=(_check("m0-blocked"),),
        probes={"m0-blocked": lambda: Verdict.blocked("no_system_image")},
    )
    unverified = _run(
        tmp_path,
        name="unverified",
        checks=(_check("m0-unverified"),),
        probes={"m0-unverified": lambda: Verdict.unverified("nobody_ran_it")},
    )

    assert int(failed.exit_code) == int(ExitCode.FAILURE) == 1
    assert int(blocked.exit_code) == int(ExitCode.BLOCKED) == 3
    assert int(unverified.exit_code) == int(ExitCode.BLOCKED) == 3

    assert failed.exit_code != blocked.exit_code
    assert failed.verdict == VerdictKind.FAIL
    assert blocked.verdict == VerdictKind.BLOCKED
    assert unverified.verdict == VerdictKind.UNVERIFIED
    assert failed.reasons == ("blocking_failed:m0-fails",)
    assert blocked.reasons == ("blocking_by:m0-blocked:no_system_image",)
    assert unverified.reasons == ("blocking_unverified:m0-unverified",)

    # A BLOCKED gate is not a failing gate: it never raises, and the bundle
    # says which of the two it was.
    assert blocked.bundle_path.is_file()
    assert blocked.bundle.verdict == VerdictKind.BLOCKED


def test_a_green_blocking_tier_without_onsite_evidence_is_blocked(tmp_path: Path) -> None:
    """A green suite is not a guest: no on-site evidence means BLOCKED, not PASS."""
    first = _ref("logs/first.log")
    second = _ref("logs/second.log")
    _materialise(tmp_path / "green", (first, second))
    green = _run(
        tmp_path,
        name="green",
        checks=(_check("m0-simulation"), _check("m1-simulation")),
        probes={
            "m0-simulation": lambda: Verdict.pass_((first,)),
            "m1-simulation": lambda: Verdict.pass_((second,)),
        },
        suite={"exit_code": 0, "passed": 1552, "failed": 0, "scope": "simulation"},
    )
    assert green.verdict == VerdictKind.BLOCKED
    assert green.reasons == ("no_onsite_evidence",)
    assert green.exit_code is ExitCode.BLOCKED
    assert green.exit_code is not ExitCode.FAILURE
    assert green.bundle.scope_summary[EvidenceScope.ONSITE] == 0

    # Every check passed, so nothing failed: the distinction is in the verdict.
    assert all(check.verdict.kind == VerdictKind.PASS for check in green.bundle.checks)


def test_an_interrupted_run_is_incomplete_and_not_a_pass(tmp_path: Path) -> None:
    """A probe that crashes the run writes an INCOMPLETE bundle and re-raises."""
    def _boom() -> Verdict:
        raise KeyboardInterrupt

    runner = AcceptanceRunner(
        run_dir=tmp_path / "interrupted",
        checks=(_check("m0-interrupted"),),
        probes={"m0-interrupted": _boom},
        environment=_stamp(),
        checked_at="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(KeyboardInterrupt):
        runner.run()

    bundle = load_bundle(tmp_path / "interrupted")
    assert bundle.run_complete is False
    assert bundle.verdict == INCOMPLETE
    assert bundle.aggregate["reasons"] == ["run_incomplete"]


def test_a_probe_that_raises_fails_closed(tmp_path: Path) -> None:
    """An ordinary exception is a FAIL with the exception type in the reason."""
    def _boom() -> Verdict:
        raise RuntimeError("probe exploded")

    outcome = _run(
        tmp_path,
        name="crashed",
        checks=(_check("m0-crashed"),),
        probes={"m0-crashed": _boom},
    )
    assert outcome.verdict == VerdictKind.FAIL
    assert outcome.reasons == ("blocking_failed:m0-crashed",)
    reasons = outcome.bundle.checks[0].verdict.reasons
    assert reasons[0] == "probe_error:m0-crashed:RuntimeError"


def test_an_unregistered_probe_is_blocked_not_passed(tmp_path: Path) -> None:
    """There is no default probe: a check with no probe reads BLOCKED."""
    outcome = _run(tmp_path, name="unbound", checks=(_check("m0-unbound"),), probes={})
    assert outcome.verdict == VerdictKind.BLOCKED
    assert outcome.exit_code is ExitCode.BLOCKED
    assert outcome.bundle.checks[0].verdict.reasons == ("probe_not_registered:m0-unbound",)


def test_a_dependency_that_did_not_pass_blocks_its_dependents(tmp_path: Path) -> None:
    """Propagation never produces a PASS, and never rewrites a real one."""
    checks = (
        _check("m10a"),
        _check("m12", depends_on=("m10a",)),
    )
    results = propagate_dependencies(
        (
            _result(checks[0], Verdict.blocked("no_system_image")),
            _result(checks[1], Verdict.unverified("nobody_ran_it")),
        )
    )
    by_id = {result.check_id: result for result in results}
    assert by_id["m10a"].verdict.kind == VerdictKind.BLOCKED
    # Rule 2: an UNVERIFIED dependent of something that did not pass reads
    # BLOCKED and names what blocks it -- never PASS (design 7.2 / 9.20).
    assert by_id["m12"].verdict.kind == VerdictKind.BLOCKED
    assert "blocked_by:m10a" in by_id["m12"].verdict.reasons


def test_a_real_pass_is_never_rewritten_by_propagation(tmp_path: Path) -> None:
    """Rule 1: a probe's own PASS survives, so a real failure is not masked."""
    checks = (
        _check("m10a"),
        _check("m12", depends_on=("m10a",)),
    )
    results = propagate_dependencies(
        (
            _result(checks[0], Verdict.fail(("condition_not_met",))),
            _result(checks[1], Verdict.pass_((_ref("logs/x.log"),))),
        )
    )
    by_id = {result.check_id: result for result in results}
    assert by_id["m10a"].verdict.kind == VerdictKind.FAIL
    assert by_id["m12"].verdict.kind == VerdictKind.PASS
    # A FAIL dependent still records why its prerequisite is not behind it.
    failed = propagate_dependencies(
        (
            _result(checks[0], Verdict.fail(("condition_not_met",))),
            _result(checks[1], Verdict.fail(("own_condition_not_met",))),
        )
    )
    dependent = {result.check_id: result for result in failed}["m12"]
    assert "dependency_not_passed:m10a:FAIL" in dependent.verdict.reasons


def _result(check: CheckDef, verdict: Verdict) -> CheckResult:
    """One evaluated check, without running anything."""
    return CheckResult(
        check=check,
        verdict=verdict,
        environment=_stamp(),
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:00+00:00",
    )
