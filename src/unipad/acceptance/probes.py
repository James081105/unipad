"""Probe adapters: four result shapes, one ``Callable[[], Verdict]``.

Why this file exists
--------------------
``runner.Probe`` is ``Callable[[], Verdict]`` and the runner enforces it with an
``isinstance`` check at ``runner.py:1427``.  Every producer in this package
already computes a real verdict, and **not one of them has that signature**:

======================  ===============================  =============================
producer                actual shape                    returns
======================  ===============================  =============================
``milestones.evaluate`` ``(check_id, view, snapshot)``   ``MilestoneResult``
``e2e.CASE_PROBES``     ``(CaseSession)``                ``CaseVerdict``
``p1.CHECKS``           ``(root: Path)``                 ``P1Check``
======================  ===============================  =============================

``MilestoneResult.verdict`` and ``CaseVerdict`` are already ``record.Verdict``
instances (``CaseVerdict`` is a subclass, which the ``isinstance`` accepts), so
those two need only an arity change.  ``P1Check`` is neither a ``Verdict`` nor a
``FourFields`` -- it carries its own ``verdict`` string and derives its field
columns itself -- so it needs a real conversion, and that conversion is where
the loss has to happen in the safe direction.

The conversion rules, and why each is the safe direction
-------------------------------------------------------
* **p1 evidence gains an explicit ``digest_reason``.**  ``p1.Evidence`` has no
  ``sha256``, and ``Verdict.__post_init__`` refuses a PASS carrying undigested
  evidence (``record.py:591-597``).  So a p1 check whose evidence was never
  hashed cannot report PASS -- which is correct: it records what a check opened,
  not a re-hashable artifact.
* **p1 never emits ``onsite``.**  p1's ``SCOPES`` vocabulary is
  simulation/host_real only; there is no onsite scope to report.  That matters
  because ``release_verdict`` needs at least one ONSITE reference in the
  blocking tier, so a p1 row can never be the thing that turns a release green.
* **An exception in a probe is a FAIL, never a crash.**  The runner already
  catches exceptions and turns them into a FAIL row, so a probe that raises
  loses its check rather than the run.  These adapters do not add a second
  opinion about that; they let the runner's own handling stand.
* **Nothing is upgraded.**  A blocked producer stays blocked, an unverified
  producer stays unverified.  The only mapping that could go wrong is the one
  that makes a verdict look better than the producer said, and there is no such
  mapping here.

What this file deliberately does not do
--------------------------------------
It does not decide *which* checks exist, what tier they sit in, or what they
depend on.  That is :mod:`unipad.acceptance.registry`'s job, and keeping the two
apart is what stops the adapter from quietly dropping a check it finds
inconvenient to call.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from . import e2e, milestones, p1, record
from .record import EvidenceRef, Verdict, VerdictKind
from .runner import EVIDENCE_DIRNAME

__all__ = [
    "Probe",
    "case_probe",
    "milestone_probe",
    "p1_probe",
    "probes_for",
]

#: The runner's contract, spelled once.  ``runner.Probe`` is the same alias; this
#: one exists so an adapter author does not have to import the runner to learn
#: the signature.
Probe = Callable[[], Verdict]

#: How a producer's verdict word maps onto the runner's closed vocabulary.  p1
#: spells its verdict as a plain string and is the only producer that needs this;
#: milestones and e2e already emit ``record.Verdict``.
_P1_KINDS = {
    "pass": VerdictKind.PASS,
    "fail": VerdictKind.FAIL,
    "blocked": VerdictKind.BLOCKED,
    "unverified": VerdictKind.UNVERIFIED,
}


def _as_verdict(
    kind: str, reasons: tuple[str, ...], evidence: tuple[EvidenceRef, ...]
) -> Verdict:
    """A ``Verdict`` from a producer's (kind, reasons, evidence) triple.

    An unrecognised kind becomes UNVERIFIED rather than PASS.  That is the only
    safe reading of "I do not know what this producer meant": a verdict the
    adapter cannot classify must not be able to read as a success.

    The four factories do **not** share a signature, and this is the one place
    that has to know it:

    ==============  ======================================
    factory         signature
    ==============  ======================================
    ``pass_``       ``(evidence, reasons=())``
    ``fail``        ``(reasons, evidence=())``
    ``blocked``     ``(reason, evidence=())``
    ``unverified``  ``(reason, evidence=())``
    ==============  ======================================

    ``pass_`` takes evidence *first* and the other three take reasons first; the
    last two take a single reason string, not an iterable.  Calling them
    uniformly -- which the first draft of this function did -- puts a tuple where
    a string belongs, and ``Verdict.__post_init__`` then stores the tuple's repr
    as the reason.  The row still reads BLOCKED, so nothing here fails loudly;
    it just carries ``"('a_reason',)"`` where an operator expects ``a_reason``.
    A refusal that is hard to read is a refusal nobody acts on.
    """
    resolved = _P1_KINDS.get(str(kind).strip().lower())
    if resolved is None:
        return Verdict.unverified(
            "unclassified_verdict:%s" % kind, evidence=evidence
        )
    if resolved is VerdictKind.PASS:
        return Verdict.pass_(evidence, reasons=reasons)
    if resolved is VerdictKind.FAIL:
        return Verdict.fail(reasons, evidence=evidence)
    if resolved is VerdictKind.BLOCKED:
        return Verdict.blocked(_one_reason(reasons), evidence=evidence)
    return Verdict.unverified(_one_reason(reasons), evidence=evidence)


def _stub_name(path: str) -> str:
    """A flat, collision-resistant filename for an absent artifact.

    The path is hashed rather than flattened to its leaf, because two checks can
    name ``artifacts/kernel.macho`` from different directories and a leaf-only
    name would silently overwrite one with the other.
    """
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return "%s-%s.txt" % (Path(path).name or "unnamed", digest)


def _inside(candidate: Path, directory: Path) -> bool:
    """``True`` when ``candidate`` is ``directory`` or lives inside it."""
    try:
        candidate.resolve().relative_to(directory.resolve())
    except (ValueError, OSError):
        return False
    return True


def _legal_kind(kind: str) -> str:
    """Map a producer's evidence kind onto one ``record.EvidenceRef`` accepts.

    This exists because of a measured defect, not a hypothetical one.
    ``milestones`` widens ``EvidenceRef.ALLOWED_KINDS`` with ``trace_log``
    (``milestones.py:169-171``) and ``e2e`` widens it further, but
    ``record.EvidenceRef.from_dict`` -- the classmethod the **reload** path uses
    -- validates against the base class's narrow ``EVIDENCE_KINDS``.  So a
    ``trace_log`` reference is accepted when the verdict is built and refused
    when the bundle is read back: the run writes its bundle and then dies
    verifying the thing it just wrote.

    Measured: ``AcceptanceRunner.run()`` raised
    ``CONFIG_INVALID: unknown evidence kind`` from ``record.py:348`` via
    ``Verdict.from_dict`` on the reload, after every probe had already run.

    ``probe_log`` is the fallback because it is the honest one: a log is what
    these are, and a kind this function does not recognise must not be able to
    pass validation by being spelled in a way some other module accepts.
    """
    text = str(kind or "").strip()
    return text if text in record.EVIDENCE_KINDS else "probe_log"


def _one_reason(reasons: tuple[str, ...]) -> str:
    """The first reason, or a placeholder when there are none.

    ``blocked`` and ``unverified`` take one string.  A producer with several
    reasons and a single-string slot has to choose, and the first one is the
    choice that matches the rest of this package -- the runner's own aggregate
    reports ``blocking_by:<id>:<reasons[0]>``.
    """
    for reason in reasons:
        if str(reason).strip():
            return str(reason)
    return "no_reason_given"


# ---------------------------------------------------------------------------
# milestones
# ---------------------------------------------------------------------------
def milestone_probe(
    check_id: str,
    repo_root: Path,
    evidence_dir: Path,
    view: milestones.HostView,
) -> Probe:
    """A zero-arg probe for one registered milestone check.

    ``milestones.evaluate`` already orders its branches blockers -> PRD veto ->
    pass evidence, so a guest-less milestone reaches BLOCKED with its blocker
    named and never PASS.  This adapter changes nothing about that; it only
    drops the three arguments the runner cannot supply.

    ``view`` is required rather than defaulted, and that is the whole point of
    the parameter.  ``HostView`` caches its file index on the instance and
    ``_snapshot`` caches its observations on the view, so the caches only pay
    off if one view is shared -- and a view that is *constructed* per check
    throws both away every time.  When this adapter called ``evaluate`` with no
    view, ``evaluate`` built a fresh ``default_view()`` for each check, and one
    evaluation over the real registry measured **21 snapshots, 168 walks (42 of
    ``local-private/out/real-boot`` at 1.35 s each) and 163 s** on this host --
    the same work, forty-two times over.  ``probes_for`` builds the view once
    and hands the same object to every milestone probe; a caller with exactly
    one check to run may pass ``milestones.default_view()`` itself.

    The snapshot is therefore computed once and shared, which is what the
    previous docstring already claimed -- the claim was simply not true of the
    code, because the thing doing the caching was being rebuilt underneath it.

    Evidence is adopted into the run directory for the same reason p1's is: the
    runner resolves every reference against the run dir (``runner.py:1116``),
    while a milestone spells its paths relative to the repository.  A milestone
    that cites ``src/unipad/host/qemu.py`` would otherwise be looked up at
    ``runs/<id>/src/unipad/host/qemu.py`` and refused as missing -- which would
    turn a real observation into ``evidence_missing`` and a red row for a reason
    that has nothing to do with the milestone.
    """
    cache: dict[str, Any] = {}

    def probe() -> Verdict:
        if "verdict" not in cache:
            result = milestones.evaluate(check_id, view=view)
            verdict = result.verdict
            adopted = tuple(
                _adopt(item.path, repo_root, evidence_dir)
                or _carry(item, evidence_dir=evidence_dir)
                for item in verdict.evidence
            )
            # The verdict's evidence, not the result's.  ``MilestoneResult``
            # carries its own ``evidence`` field *and* a ``verdict`` that carries
            # a separate tuple, and the runner reads the verdict's.  Mutating the
            # result's -- which the first draft did -- left three ``trace_log``
            # references in the bundle, and the run died reloading it.
            cache["verdict"] = replace(verdict, evidence=adopted)
        return cache["verdict"]

    return probe


# ---------------------------------------------------------------------------
# p1
# ---------------------------------------------------------------------------
def _adopt(path: str, repo_root: Path, evidence_dir: Path) -> EvidenceRef | None:
    """Copy one repo file into the run's evidence directory and hash it.

    This is the piece that makes a PASS *constructible* for a p1 check.  p1's
    ``Evidence`` carries no digest, and ``Verdict.__post_init__`` refuses a PASS
    whose evidence cannot be re-verified (``record.py:591-597``).  Without this
    the first p1 check that legitimately passes on Windows would raise inside
    its probe, the runner would catch it, and the row would read FAIL -- a
    genuinely green check reported red, for a reason that has nothing to do with
    the check.

    The runner resolves every evidence path against the **run directory**
    (``runner.py:1116``: ``path = directory / ref.path``), while p1 spells its
    paths relative to the repository.  Copying is what reconciles the two, and
    it is also what makes the bundle self-contained: ``--verify`` re-hashes what
    is inside the run directory, so a bundle that referenced the live repository
    could not be re-checked after the repository moved.

    Returns ``None`` when there is no file to adopt, so the caller can fall back
    to a digestless reference rather than crashing on a path a check named but
    did not produce.
    """
    source = repo_root / path
    if not source.is_file():
        return None
    # Two paths are never adopted, and both are the same rule: the bundle has to
    # stand on what THIS run saw.
    #
    # 1. A file already in this run's evidence directory -- the destination would
    #    be inside the source, and the copy would feed itself.
    # 2. A file under any directory named ``runs`` -- another run's evidence.
    #    ``runs/`` is inside the repository, so a milestone can legitimately find
    #    a file there, and adopting it writes ``evidence/runs/<other>/...``.  The
    #    *next* run then finds that path, adopts it again, and the nesting
    #    compounds until the OS refuses it (measured: WinError 206, with a path
    #    400 characters deep).  A previous run's evidence is not evidence.
    #
    #    The test is a path *component*, not a resolved prefix: an earlier
    #    ``_run_root()`` bug put runs under ``src/runs/`` and the directories it
    #    left behind were still being found and re-adopted.  Checking one
    #    specific root would have missed them.
    if _inside(source, evidence_dir) or "runs" in source.parts:
        return None
    destination = evidence_dir / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return EvidenceRef(
        path=(Path(evidence_dir.name) / path).as_posix(),
        kind="probe_log",
        sha256=digest,
        scope=record.EvidenceScope.SIMULATION,
    )


def _p1_evidence(
    items: Any, repo_root: Path, evidence_dir: Path
) -> tuple[EvidenceRef, ...]:
    """``p1.Evidence`` -> ``record.EvidenceRef``, digested where it can be.

    A file that exists is adopted and hashed (see :func:`_adopt`).  One that does
    not is still reported, with an explicit ``digest_reason`` -- because a check
    that named an artifact it did not produce is saying something, and dropping
    the reference would hide it.
    """
    refs: list[EvidenceRef] = []
    for item in items or ():
        path = getattr(item, "path", None)
        if not isinstance(path, str):
            continue
        adopted = _adopt(path, repo_root, evidence_dir)
        if adopted is not None:
            refs.append(adopted)
            continue
        refs.append(_carry(item, path=path, evidence_dir=evidence_dir))
    return tuple(refs)


def _carry(item: Any, path: str | None = None, evidence_dir: Path | None = None) -> EvidenceRef:
    """Carry a producer's evidence reference, with its kind made legal.

    Used when a file cannot be adopted -- normally because it does not exist.
    The reference is still reported, because a check that named an artifact it
    did not produce is saying something and dropping the reference would hide it.

    But it is reported **as a file that exists**.  Citing the original path
    verbatim makes the runner resolve it against the run directory, fail to find
    it, and abort the entire run with ``evidence file is missing`` -- which is
    the shipped behaviour and the right one for a bundle that references
    something absent.  So a stub is written recording what was named and why it
    is not here, and that stub is what gets cited.  The information survives, the
    bundle reloads, and the row still says the artifact was not produced.
    """
    resolved = path if path is not None else str(getattr(item, "path", "") or "")
    if evidence_dir is not None:
        stub = evidence_dir / "absent" / _stub_name(resolved)
        stub.parent.mkdir(parents=True, exist_ok=True)
        note = (
            "named by the check but not present to hash: %s\n"
            "the check cited this path and the file did not exist at run time\n"
            % resolved
        )
        # write_bytes, not write_text: text mode on Windows rewrites every
        # newline as CRLF, so the file on disk would not be the bytes the
        # digest above is taken over, and the runner's reload would refuse
        # the run with 'evidence file changed since it was recorded'.
        # Measured: exactly one ref drifted, and it was this stub.
        stub.write_bytes(note.encode("utf-8"))
        return EvidenceRef(
            path=(
                Path(evidence_dir.name) / "absent" / _stub_name(resolved)
            ).as_posix(),
            kind=_legal_kind(getattr(item, "kind", "probe_log")),
            sha256=hashlib.sha256(note.encode("utf-8")).hexdigest(),
            scope=record.EvidenceScope.SIMULATION,
        )
    scope = str(getattr(item, "scope", record.EvidenceScope.SIMULATION))
    if scope == record.EvidenceScope.ONSITE:  # no producer here has an onsite scope
        scope = record.EvidenceScope.HOST_REAL
    return EvidenceRef(
        path=resolved,
        kind=_legal_kind(getattr(item, "kind", "probe_log")),
        sha256=None,
        digest_reason=(
            "named by the check but not present to hash: %s" % Path(resolved).name
        ),
        scope=scope,
    )


def p1_probe(
    check: Callable[[Path], p1.P1Check], root: Path, evidence_dir: Path
) -> Probe:
    """A zero-arg probe wrapping one ``check_p1_N_*`` function.

    ``root`` and ``evidence_dir`` are bound here rather than passed by the
    runner, because the runner's contract is zero-argument and both are facts
    about where the gate was invoked, not about the check.
    """

    def probe() -> Verdict:
        result = check(root)
        return _as_verdict(
            result.verdict,
            tuple(result.verdict_reasons),
            _p1_evidence(result.evidence, root, evidence_dir),
        )

    return probe


# ---------------------------------------------------------------------------
# e2e
# ---------------------------------------------------------------------------
def case_probe(case_id: str, env: e2e.E2EEnvironment) -> Probe:
    """A zero-arg probe for one PRD 5.2 case.

    Each case gets its **own** session.  The ten cases are a stateful sequence in
    PRD order -- case 6 reads case 5's ``tap_count``, case 8 reads case 3's
    install -- so sharing one session across independent probes would make the
    verdict of a case depend on which other cases happened to run first.  That
    is precisely the coupling the sequence exists to express, and it has to be
    expressed by running the sequence, not by a shared mutable dict.

    The consequence, stated plainly: a case whose prerequisite case cannot run
    reports its own prerequisite as unmet.  It does not silently pass because the
    prerequisite's fact was absent.
    """
    probe_of = e2e.CASE_PROBES.get(case_id)

    def probe() -> Verdict:
        if probe_of is None:
            return Verdict.blocked(
                ("probe_not_registered:%s" % case_id,), observed=False
            )
        facts = e2e.observe_host(env)
        session = e2e.CaseSession(env, facts, env.runtime, env.run_dir / EVIDENCE_DIRNAME)
        return probe_of(session)

    return probe


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
def probes_for(
    repo_root: Path,
    run_dir: Path,
    *,
    milestone_ids: tuple[str, ...] = (),
    case_ids: tuple[str, ...] = (),
    p1_checks: tuple[Callable[[Path], p1.P1Check], ...] = (),
) -> dict[str, Probe]:
    """Every probe the registry needs, keyed by check id.

    One place, so a check id that appears in ``CHECKS`` but not here is visible
    as a missing key rather than as a probe the runner invents.  The runner
    synthesises ``probe_not_registered:<id>`` -> BLOCKED for an absent probe, so
    an omission is a red row and not a silent pass -- but a red row nobody
    notices is still worse than a table that is complete.

    The host view is built here, once, and handed to every milestone probe.
    ``HostView`` caches its file index on the instance and ``_snapshot`` caches
    its observations on the view, so a view constructed per check throws both
    caches away -- which is what this used to do, at a measured 163 s and 168
    walks per evaluation.  Constructing the view costs nothing (it stats four
    directories and walks nothing; the first ``find()`` is what walks), so it is
    built unconditionally rather than guarded on ``milestone_ids``.

    ``milestones.default_view()`` locates the repository itself rather than
    taking ``repo_root``; the two agree because ``registry.build_inputs``
    defaults its own root the same way, and no in-tree caller overrides it.  A
    caller that passes a different ``repo_root`` should pass its own view too.
    """
    # One view for the whole run.  This line is the fix; everything above it is
    # the reason it has to be here rather than inside ``milestone_probe``.
    view = milestones.default_view()
    probes: dict[str, Probe] = {}
    for check_id in milestone_ids:
        probes[check_id] = milestone_probe(
            check_id, repo_root, run_dir / EVIDENCE_DIRNAME, view
        )
    env = e2e.E2EEnvironment.create(run_dir, repo_root=repo_root)
    for case_id in case_ids:
        probes[case_id] = case_probe(case_id, env)
    for index, check in enumerate(p1_checks, start=1):
        probes["P1-%d" % index] = p1_probe(
            check, repo_root, run_dir / EVIDENCE_DIRNAME
        )
    return probes
