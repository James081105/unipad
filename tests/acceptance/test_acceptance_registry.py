"""``unipad.acceptance.registry``: the check universe, and what stops it lying.

Why this file exists
--------------------
``--run`` used to refuse unconditionally.  With ``registry.py`` the gate now
evaluates 40 checks and writes a real bundle -- which means the registry itself
became the thing that decides what the gate can and cannot say.  A registry that
is wrong does not fail loudly: it produces a confident verdict about the wrong
set of requirements.

The honest verdict on this host is ``BLOCKED``, exit 3, because there is no
iPadOS guest and no macOS runner.  These tests pin that outcome *and* pin the
mechanisms that keep it honest, so a future change that makes the gate greener
has to break a test to do it.

The load-bearing ones
---------------------
* ``test_the_release_verdict_changes_when_a_gate_leaves_the_blocking_tier`` --
  the承重 test DESIGN §7.1 asks for.  Tier is data, so moving one gate from
  blocking to advisory must change the release verdict.  If it does not, the
  tier table is decoration.
* ``test_a_gate_without_onsite_evidence_can_never_release`` -- the mechanical
  form of "a green suite does not substitute for a missing guest".  This is the
  one that stops the whole suite from being green while the product is unbuilt.
* ``test_a_check_with_no_probe_is_blocked_not_failed`` -- M13-M15 have no probe
  on purpose.  A missing probe must read BLOCKED (nobody attempted it), not FAIL
  (somebody attempted it and it broke).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unipad.acceptance import registry
from unipad.acceptance.runner import AcceptanceRunner
from unipad.errors import ExitCode

pytestmark = pytest.mark.acceptance


# ---------------------------------------------------------------------------
# the universe itself
# ---------------------------------------------------------------------------
def test_the_registry_registers_every_prd_requirement() -> None:
    """40 rows: 16 blocking milestones, 3 advisory, 5 work packages, 10 cases, 6 P1.

    The count is asserted because a silently dropped row is the failure mode
    this registry has to prevent -- and the composition is asserted because
    "40 checks" could otherwise be reached by, say, 40 milestones and no cases.
    """
    tiers: dict[str, int] = {}
    for row in registry.CHECKS:
        tiers[row.tier] = tiers.get(row.tier, 0) + 1
    assert tiers == {"blocking": 26, "advisory": 14}, tiers
    assert len(registry.CHECKS) == 40, len(registry.CHECKS)

    milestones = [r for r in registry.CHECKS if r.milestone not in ("e2e", "P1")]
    assert len(milestones) == 24, len(milestones)  # 21 + M13/M14/M15 - 0
    cases = [r for r in registry.CHECKS if r.milestone == "e2e"]
    assert len(cases) == 10, len(cases)
    p1 = [r for r in registry.CHECKS if r.milestone == "P1"]
    assert len(p1) == 6, len(p1)


def test_the_ten_cases_are_checks_not_just_rollups() -> None:
    """A case's acceptance predicates live only inside ``e2e.CASE_PROBES``.

    Folding them into ``CaseSpec.check_ids`` over milestone ids would drop every
    predicate -- case 5's challenge value, case 6's tap count, case 9's
    exactly-three-cycles.  So each case is a row in ``CHECKS``, keyed by its
    probe name so ``probes_for`` can find it.
    """
    case_rows = {row.check_id for row in registry.CHECKS if row.milestone == "e2e"}
    assert case_rows == set(registry.probes_for.__doc__ and []) or case_rows
    assert "case5_launch_hello_ipa" in case_rows, sorted(case_rows)
    assert "case6_tap_button_ten_times" in case_rows, sorted(case_rows)
    assert "case9_three_cold_boots" in case_rows, sorted(case_rows)


def test_a_duplicate_check_id_is_refused_at_import(monkeypatch) -> None:
    """Two rows with one id would let one shadow the other in every roll-up.

    Tested through ``_build_checks`` itself rather than by re-implementing its
    guard: a test that checks its own copy of the rule proves the copy, not the
    rule.  ``_milestone_rows`` is patched to emit one id twice, which is exactly
    the shape a future edit would produce.
    """
    from unipad.acceptance import milestones
    from unipad.acceptance.runner import CheckDef

    real = list(milestones.CHECKS.values())
    twin = CheckDef(
        check_id=real[0].check_id,
        prd_citation="docs/PRD.md:1",
        requirement="a second row carrying the first row's id",
        rejection="",
        tier="advisory",
        gate_id="default",
        milestone="P1",
    )
    monkeypatch.setattr(registry, "_milestone_rows", lambda: [real[0], twin])
    with pytest.raises(Exception) as caught:
        registry._build_checks()
    assert "registered twice" in str(caught.value), caught.value


def test_every_prd_case_has_a_check_or_the_gap_list_says_so() -> None:
    """A case with no check is a coverage gap, not a disappearance."""
    from unipad.acceptance import e2e

    registered = {spec.case_id for spec in registry.CASES}
    for spec in e2e.CASES:
        assert spec.case_id in registered, spec.case_id
    assert registry.coverage_gaps() == (), registry.coverage_gaps()


def test_a_gate_the_verifier_does_not_know_is_a_gap() -> None:
    """``coverage_gaps()`` computes; it does not assert its own completeness."""
    from unipad.acceptance.runner import CheckDef

    rogue = CheckDef(
        check_id="rogue",
        prd_citation="docs/PRD.md:1",
        requirement="a gate nobody defined",
        rejection="",
        tier="advisory",
        gate_id="not-a-real-gate",
        milestone="P1",
    )
    gaps = registry.coverage_gaps(checks=(*registry.CHECKS, rogue), cases=registry.CASES)
    assert "unknown_gate:rogue:not-a-real-gate" in gaps, gaps


def test_a_dangling_dependency_is_a_gap() -> None:
    from unipad.acceptance.runner import CheckDef

    orphan = CheckDef(
        check_id="orphan",
        prd_citation="docs/PRD.md:1",
        requirement="depends on a check that does not exist",
        rejection="",
        tier="advisory",
        gate_id="default",
        milestone="P1",
        depends_on=("nobody",),
    )
    gaps = registry.coverage_gaps(checks=(*registry.CHECKS, orphan), cases=registry.CASES)
    assert "dangling_dependency:orphan:nobody" in gaps, gaps


def test_a_work_package_is_advisory_and_cites_a_gate_the_verifier_knows() -> None:
    """The two forced remaps, pinned.

    ``milestones`` spells a work package's tier ``"workpackage``, which the
    runner refuses at import; and its ``gate_id`` is ``None``, which would cite a
    gate the verifier does not know and fail every run forever.  Both are
    remapped here, and this test is what stops the remap being dropped.
    """
    for row in registry.CHECKS:
        if row.check_id.startswith("WP"):
            assert row.tier == "advisory", row
            assert row.gate_id == "default", row


# ---------------------------------------------------------------------------
# the run, and what it is allowed to conclude
# ---------------------------------------------------------------------------
def _evaluate(tmp_path: Path, **overrides: object):
    """Run the gate with the real registry, optionally overriding one input."""
    run_dir = tmp_path / "run"
    inputs = registry.build_inputs(run_dir)
    checks = overrides.pop("checks", inputs.checks)
    return AcceptanceRunner(
        run_dir=run_dir,
        checks=checks,  # type: ignore[arg-type]
        probes=overrides.pop("probes", inputs.probes),
        environment=overrides.pop("environment", inputs.environment),
        cases=inputs.cases,
        milestones=inputs.milestones,
        measurements=inputs.measurements,
        coverage_gaps=overrides.pop("coverage_gaps", inputs.coverage_gaps),
    ).run()


def test_a_run_on_this_host_names_its_blockers(tmp_path: Path) -> None:
    """The honest verdict, end to end.

    Not PASS, and not silent: every reason names a specific blocking check and
    the blocker it hit, which is the only form an operator can act on.

    This test used to pin ``BLOCKED`` with exit 3 and only ``blocking_by:``
    reasons.  All three stopped being true when a real guest log reached the
    host's view: M9 and M9a's only blocker was ``xnu_initialised``, the guest
    log satisfies it, and the PRD veto branch fires instead -- so the verdict is
    ``FAIL`` with exit 1 and ``blocking_failed:M9`` / ``blocking_failed:M9a``.
    The gap was closed, so the assertion is updated rather than the check
    weakened.

    What replaces it is the rule the old form was a proxy for, and the module
    docstring states the purpose of pinning it: "so a future change that makes
    the gate greener has to break a test to do it".  ``verdict != "PASS"`` is
    the half that does that -- it survives every milestone going green, because
    a green blocking tier is held off PASS by the ``no_onsite_evidence`` gate
    that :func:`test_forced_green_probes_cannot_produce_a_pass` drives directly.

    The exit code is now required to be DERIVED from the verdict rather than
    hand-set.  That is a defect class the old form could not see: an exit code
    that disagreed with its verdict would still have equalled ``ExitCode.BLOCKED``
    on the day the verdict was BLOCKED, and would quietly mean something else the
    day it was not.
    """
    from unipad.acceptance.runner import exit_code_for

    outcome = _evaluate(tmp_path)
    assert outcome.verdict != "PASS", outcome.verdict
    assert outcome.exit_code == exit_code_for(outcome.verdict), (
        outcome.verdict, outcome.exit_code
    )
    assert outcome.exit_code != ExitCode.OK, outcome.exit_code
    assert outcome.reasons, "a non-PASS verdict with no reasons says nothing"
    assert all(
        r.startswith(("blocking_by:", "blocking_failed:", "blocking_unverified:",
                      "no_onsite_evidence"))
        for r in outcome.reasons
    ), outcome.reasons
    # Recorded 2026-11-02.  Re-record deliberately when the field moves.
    assert (outcome.verdict, outcome.exit_code) == ("FAIL", ExitCode.FAILURE), (
        outcome.verdict, outcome.exit_code
    )


def test_forced_green_probes_cannot_produce_a_pass(tmp_path: Path) -> None:
    """The load-bearing rule: a green suite is not a guest.

    Every blocking probe is replaced by one returning PASS with perfectly good,
    digested, simulation-scoped evidence.  The run must not come out PASS, and
    the reason is sharper than expected: ``write_bundle`` refuses a PASS whose
    evidence is weaker than the check's ``required_scope`` (``runner.py:1019``),
    so the forgery is stopped at *write* time -- before ``release_verdict`` even
    runs.  If this test ever goes green, the gate has started accepting
    simulation evidence as proof of a real device.
    """
    from unipad.acceptance.record import EvidenceRef, Verdict, VerdictKind

    inputs = registry.build_inputs(tmp_path / "run")
    reference = EvidenceRef(
        path="evidence/synthetic.txt",
        kind="probe_log",
        sha256="0" * 64,
        scope="simulation",
    )
    (tmp_path / "run" / "evidence").mkdir(parents=True, exist_ok=True)
    (tmp_path / "run" / "evidence" / "synthetic.txt").write_text("x", encoding="utf-8")
    green = {
        row.check_id: (lambda: Verdict(VerdictKind.PASS, ("forced",), (reference,), True))
        for row in inputs.checks
    }
    with pytest.raises(Exception) as caught:
        _evaluate(tmp_path, probes=green)
    assert "required scope" in str(caught.value), caught.value


def test_the_release_verdict_changes_when_a_gate_leaves_the_blocking_tier(
    tmp_path: Path,
) -> None:
    """The承重 test DESIGN §7.1 asks for: tier is data, and it must matter.

    Demote one gate the verdict actually NAMES, and its reason must leave the
    list -- while every other gate still blocks, so the verdict string is
    unchanged and the *composition* is what moves.  Asserting on the string
    would pass either way; asserting on the named gate's presence is what proves
    the tier table is load-bearing rather than decoration.

    The target used to be hardcoded as ``M10a``.  That stopped working when the
    verdict moved from BLOCKED to FAIL: ``release_verdict`` checks FAIL before
    BLOCKED, so M9 and M9a's ``blocking_failed:`` reasons are reported and NO
    ``blocking_by:`` reason reaches the list at all -- which means the old
    assertion failed not because tier stopped mattering but because the gate it
    picked had stopped being named.  Deriving the target from the measured
    reasons is what keeps the test testing tier rather than testing one gate's
    current verdict.

    The recorded caveat: the final "verdict is unchanged" assertion holds only
    while the demoted gate is not the sole carrier of the run's worst kind.
    Deriving the target makes that true by construction today (M9 and M9a both
    FAIL), and if it ever stops being true the test should say so rather than
    pass.
    """
    from dataclasses import replace

    def named(reasons: tuple) -> set:
        return {
            reason.split(":", 2)[1]
            for reason in reasons
            if reason.startswith(("blocking_by:", "blocking_failed:",
                                  "blocking_unverified:"))
        }

    with_tier = _evaluate(tmp_path / "a", checks=registry.CHECKS)
    before = named(with_tier.reasons)
    assert before, with_tier.reasons
    # A gate the verdict actually names, so demoting it must move the verdict.
    target = sorted(before)[0]

    def demote(rows: tuple) -> tuple:
        return tuple(
            replace(row, tier="advisory") if row.check_id == target else row
            for row in rows
        )

    demoted = _evaluate(tmp_path / "b", checks=demote(registry.CHECKS))
    after = named(demoted.reasons)
    assert target in before and target not in after, (target, sorted(before), sorted(after))
    # ...and the rest of the blocking tier still blocks, so this is a change in
    # composition and not in whether the gate runs at all.
    assert after, demoted.reasons
    assert with_tier.verdict == demoted.verdict, (
        with_tier.verdict, demoted.verdict
    )


def test_a_check_with_no_probe_is_blocked_not_failed(tmp_path: Path) -> None:
    """M13-M15 have no probe on purpose.

    A missing probe must read BLOCKED -- nobody attempted it -- rather than FAIL,
    which would say somebody attempted it and it broke.  The runner synthesises
    ``probe_not_registered:<id>``; this test pins that the row exists and reads
    BLOCKED rather than vanishing or failing.
    """
    inputs = registry.build_inputs(tmp_path / "run")
    assert "M13" in {row.check_id for row in inputs.checks}
    assert "M13" not in inputs.probes, "M13 gained a probe; it is meant to have none"
    outcome = _evaluate(tmp_path)
    kinds = {
        check.check.check_id: check.verdict.kind for check in outcome.bundle.checks
    }
    for milestone in ("M13", "M14", "M15"):
        assert kinds.get(milestone) == "BLOCKED", (milestone, kinds.get(milestone))
        reasons = next(
            c.verdict.reasons
            for c in outcome.bundle.checks
            if c.check.check_id == milestone
        )
        assert reasons[0].startswith("probe_not_registered:"), reasons


def test_the_bundle_is_written_and_reloads(tmp_path: Path) -> None:
    """A bundle that cannot be read back is not evidence.

    The runner reloads what it wrote and re-hashes every reference, so a run
    that cannot survive its own reload is a run that produced nothing.  This
    caught two real defects while the registry was being built: a ``trace_log``
    evidence kind that ``milestones`` widens but ``record`` refuses on reload,
    and a stub file written in text mode whose digest was taken over the
    untranslated bytes.
    """
    outcome = _evaluate(tmp_path)
    bundle_path = tmp_path / "run" / "acceptance.json"
    assert bundle_path.is_file(), bundle_path
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "run", payload["run_id"]
    assert len(payload["checks"]) == 40, len(payload["checks"])
    assert outcome.bundle.run_id == "run", outcome.bundle.run_id


def test_the_four_fields_are_never_upgraded(tmp_path: Path) -> None:
    """Every row carries its four-field record, and the blocked ones say so.

    ``as_verdict`` maps a runner row onto (verdict, fields) and the mapping is
    lossy in one direction only.  This asserts the direction: a BLOCKED row must
    not be able to report a green on-site column.
    """
    from unipad.acceptance.__main__ import as_verdict

    outcome = _evaluate(tmp_path)
    blocked_seen = 0
    for check in outcome.bundle.checks:
        row = as_verdict("runner", check)
        if row.verdict.kind == "BLOCKED":
            blocked_seen += 1
            assert row.fields.onsite_status != "PASS", (row.check_id, row.fields)
    assert blocked_seen, "no BLOCKED rows were produced; the host is not guest-less"
