"""``python -m unipad.acceptance`` -- the gate's command-line entry point.

Why this module exists
----------------------
``unipad.acceptance.runner`` had no ``__main__``, no ``main()`` and no argparse,
so ``python -m unipad.acceptance.runner`` exited 0 having done nothing.  A gate
that can be invoked and silently succeeds is worse than no gate: an operator
would read the exit code as "the acceptance run passed".  DESIGN.md section 10
assigns this file to owner D and specifies ``python -m unipad.acceptance --run
<id>``, with the explicit constraint that ``unipad/controller/cli.py`` is *not*
to be touched -- the product CLI and the acceptance gate stay separate.

What it does not do
-------------------
It does not invent a check universe.  The registered ``CHECKS`` table DESIGN
section 10 describes has not been built yet, so ``--run`` refuses rather than
writing an empty bundle: a bundle with zero checks would roll up to a
verdict, and "no checks ran" must never be able to read as a pass.  When the
registry lands, ``--run`` gains its probe map and nothing else changes.

The adapter
-----------
Four modules produce a verdict-shaped result and no two of them produced the
same shape:

* ``runner`` / ``record`` -- :class:`~unipad.acceptance.record.Verdict` plus a
  :class:`~unipad.acceptance.record.FourFields` row;
* ``p1`` -- :class:`~unipad.acceptance.p1.P1Check`, which carries its own
  ``verdict`` string and derives ``windows_status`` / ``onsite_status`` itself;
* ``e2e`` -- a :class:`~unipad.acceptance.e2e.SequenceReport` whose aggregate
  lives in a plain dict;
* ``milestones`` -- a :class:`~unipad.acceptance.milestones.MilestoneResult`
  whose verdict is already a ``record.Verdict`` (since F8) but whose field
  columns are its own strings.

:func:`as_verdict` maps all four onto one ``(Verdict, FourFields)`` pair and
:func:`render_rows` prints one table for them.  The mapping is deliberately
lossy in one direction only -- it never upgrades a field -- and it raises on a
shape it does not recognise rather than guessing, because a silently mis-read
row is how a green column gets manufactured.

Which of the four reach that table **today** is a fact about what this entry
point can be handed, and it is stated here rather than implied: ``--run`` is
refused (see above), so the only thing that produces a report is ``--verify``,
which reads a bundle back off disk.  A written bundle carries runner
``CheckResult`` objects, so ``runner`` is the one source the report renders.
The other three are adapters for results nothing writes yet; they are pinned by
``tests/unit/test_acceptance_main_report.py`` and gain their caller when the
check universe lands and ``--run`` stops refusing.  Claiming all four are
rendered now would be a promise nothing keeps.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..errors import ErrorCode, UniPadError, ExitCode
from . import record
from .record import EvidenceRef, FourFields, Verdict, VerdictKind

__all__ = [
    "VerdictFields",
    "as_verdict",
    "render_rows",
    "build_parser",
    "main",
]


# ---------------------------------------------------------------------------
# The adapter: four result shapes, one (verdict, fields) pair
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerdictFields:
    """One row of the unified report: a verdict and its four-field record."""

    source: str
    check_id: str
    verdict: Verdict
    fields: FourFields

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "check_id": self.check_id,
            "verdict": self.verdict.to_dict(),
            "fields": self.fields.to_dict(),
        }


def _evidence_of(items: Iterable[Any]) -> tuple[EvidenceRef, ...]:
    """Return ``items`` as evidence references, refusing a shape it cannot read.

    A reference that is already an :class:`EvidenceRef` passes through.  A p1
    :class:`~unipad.acceptance.p1.Evidence` has no digest, so it is carried with
    an explicit ``digest_reason`` -- which is exactly the case that rule exists
    for, and it can never carry a PASS because of it.
    """
    refs: list[EvidenceRef] = []
    for item in items:
        if isinstance(item, EvidenceRef):
            refs.append(item)
            continue
        path = getattr(item, "path", None)
        if not isinstance(path, str):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "cannot adapt an evidence item with no path",
                received_type=type(item).__name__,
            )
        kind = str(getattr(item, "kind", "probe_log"))
        if kind not in record.EVIDENCE_KINDS:
            kind = "probe_log"
        refs.append(
            EvidenceRef(
                path=path,
                kind=kind,
                sha256=None,
                digest_reason=(
                    "carried from %s, which records what a check opened rather "
                    "than a re-hashable digest" % type(item).__name__
                ),
                scope=str(getattr(item, "scope", record.EvidenceScope.SIMULATION)),
            )
        )
    return tuple(refs)


def _verdict_of(kind: str, reasons: Sequence[str], evidence: Sequence[EvidenceRef]) -> Verdict:
    """Build a canonical :class:`Verdict` for ``kind``, keeping the invariants.

    ``PASS`` is built through :meth:`Verdict.pass_` so the "no evidence, no
    pass" rule is the canonical one and not a second spelling of it.  A ``PASS``
    whose adapted evidence has no digest is refused by the constructor, which
    is the correct outcome: an unevidenced pass must not be constructible, and
    the caller sees the refusal rather than a laundered row.
    """
    slugs = tuple(str(reason) for reason in reasons)
    if kind == VerdictKind.PASS:
        return Verdict.pass_(evidence, slugs)
    if kind == VerdictKind.FAIL:
        return Verdict.fail(slugs, evidence)
    if kind == VerdictKind.BLOCKED:
        return Verdict.blocked(slugs[0] if slugs else "unspecified_blocker", evidence)
    if kind == VerdictKind.UNVERIFIED:
        return Verdict.unverified(slugs[0] if slugs else "nobody_established_it", evidence)
    raise UniPadError(
        ErrorCode.CONFIG_INVALID,
        "cannot adapt an unknown verdict kind",
        kind=kind,
        known_kinds=list(record.VERDICT_KINDS),
    )


def as_verdict(source: str, result: Any) -> VerdictFields:
    """Map any of the four verdict-shaped results onto one canonical row.

    Parameters
    ----------
    source:
        Which module produced ``result`` -- ``"p1"``, ``"e2e"``, ``"milestones"``
        or ``"runner"``.  Recorded on the row so a report can say where a claim
        came from.
    result:
        A ``P1Check``, a ``SequenceReport``, a ``MilestoneResult`` or a
        ``CheckResult``.

    Raises
    ------
    UniPadError
        ``CONFIG_INVALID`` when ``result`` is none of those.  Guessing is not an
        option: a row read from the wrong shape is a fabricated row.
    """
    if source == "p1":
        return _from_p1(result)
    if source == "e2e":
        return _from_e2e(result)
    if source == "milestones":
        return _from_milestones(result)
    if source == "runner":
        return _from_runner(result)
    raise UniPadError(
        ErrorCode.CONFIG_INVALID,
        "unknown verdict source",
        source=source,
        known_sources=["p1", "e2e", "milestones", "runner"],
    )


def _from_p1(check: Any) -> VerdictFields:
    """A ``P1Check``: its own verdict string plus its own derived columns."""
    kind = str(getattr(check, "verdict", ""))
    reasons = tuple(getattr(check, "verdict_reasons", ())) or tuple(getattr(check, "reasons", ()))
    evidence = _evidence_of(getattr(check, "evidence", ()))
    verdict = _verdict_of(kind, reasons, evidence)
    implementation = str(getattr(check, "implementation", "UNVERIFIED"))
    blocker = str(getattr(check, "blocker", "") or "") or None
    fields = FourFields(
        check_id=str(getattr(check, "bullet", "?")),
        verdict=verdict,
        evidence=evidence,
        blocker=blocker,
        code_state=implementation if implementation in record.CODE_STATES else "UNVERIFIED",
    )
    return VerdictFields("p1", fields.check_id, verdict, fields)


def _from_e2e(report: Any) -> VerdictFields:
    """A ``SequenceReport``: its aggregate is a dict, its rows are ``FourFields``."""
    aggregate = getattr(report, "aggregate", None)
    if not isinstance(aggregate, Mapping):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "an e2e report must carry an aggregate mapping",
            received_type=type(aggregate).__name__,
        )
    kind = str(aggregate.get("verdict", ""))
    reasons = tuple(str(item) for item in aggregate.get("reasons", ()))
    evidence = _evidence_of(getattr(report, "evidence_index", ()))
    verdict = _verdict_of(kind, reasons, evidence)
    run_id = str(getattr(report, "run_id", "?"))
    fields = FourFields(
        check_id=run_id,
        verdict=verdict,
        evidence=evidence,
        blocker=(reasons[0] if reasons and kind != VerdictKind.PASS else None),
    )
    return VerdictFields("e2e", run_id, verdict, fields)


def _from_milestones(result: Any) -> VerdictFields:
    """A ``MilestoneResult``: its verdict is already canonical (since F8)."""
    verdict = getattr(result, "verdict", None)
    if not isinstance(verdict, Verdict):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a milestone result must carry a record.Verdict",
            received_type=type(verdict).__name__,
        )
    check_id = str(getattr(result, "check_id", "?"))
    implementation = str(getattr(result, "implementation", "UNVERIFIED"))
    blocker = str(getattr(result, "blocker", "") or "") or None
    fields = FourFields(
        check_id=check_id,
        verdict=verdict,
        evidence=verdict.evidence,
        blocker=blocker,
        code_state=implementation if implementation in record.CODE_STATES else "UNVERIFIED",
    )
    return VerdictFields("milestones", check_id, verdict, fields)


def _from_runner(result: Any) -> VerdictFields:
    """A ``CheckResult``: already a verdict plus a ``FourFields`` row."""
    verdict = getattr(result, "verdict", None)
    fields = getattr(result, "fields", None)
    if not isinstance(verdict, Verdict) or not isinstance(fields, FourFields):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a runner result must carry a Verdict and a FourFields row",
            verdict_type=type(verdict).__name__,
            fields_type=type(fields).__name__,
        )
    return VerdictFields("runner", fields.check_id, verdict, fields)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_rows(rows: Sequence[VerdictFields]) -> str:
    """Render the unified four-field table as text.

    This is the report's one table renderer: ``--verify`` prints what this
    returns, so the gate has a single spelling of its rows rather than one here
    and another in the command path.

    The columns are the four-field vocabulary -- ``implementation``,
    ``windows_status``, ``onsite_status`` -- plus the source that produced the
    row and the verdict kind, so a reader can tell where a claim came from and
    which verdict it rests on.  The ``blocker`` is deliberately **not** a
    column here: it is per-row detail, and printing it for every row would put
    ``verdict: PASS`` and ``blocker: <a gap that is closed>`` on the same line.
    It is reported by the ``field_unverified`` list, which is the surface that
    names what blocks a field.
    """
    lines = [
        "%-10s %-22s %-11s %-15s %-10s %s"
        % ("SOURCE", "CHECK", "IMPLEMENTATION", "WINDOWS", "ONSITE", "VERDICT"),
        "-" * 88,
    ]
    for row in rows:
        fields = row.fields.to_dict()
        lines.append(
            "%-10s %-22s %-11s %-15s %-10s %s"
            % (
                row.source[:10],
                row.check_id[:22],
                fields["implementation"][:11],
                fields["windows_status"][:15],
                fields["onsite_status"][:10],
                row.verdict.kind,
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``python -m unipad.acceptance``."""
    parser = argparse.ArgumentParser(
        prog="python -m unipad.acceptance",
        description=(
            "UniPad acceptance gate.  Reads a written bundle back from disk and "
            "re-establishes every claim in it; it does not run the product."
        ),
    )
    parser.add_argument(
        "--run",
        metavar="RUN_ID",
        help=(
            "run the gate for RUN_ID and write its bundle.  Refused until the "
            "registered check universe exists: an empty bundle must never be "
            "able to read as a pass."
        ),
    )
    parser.add_argument(
        "--verify",
        metavar="RUN_DIR",
        help=(
            "load RUN_DIR's bundle, re-hash every evidence file and re-read "
            "every on-site witness, and report the aggregate"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the text table",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the gate identity and exit",
    )
    return parser


def _verify(run_dir: str) -> int:
    """Load a bundle and report what re-checking it establishes."""
    from .runner import BUNDLE_FILENAME, load_bundle

    directory = Path(run_dir)
    try:
        bundle = load_bundle(directory)
    except UniPadError as exc:
        # A bundle that cannot be loaded is not a verdict: it is a refusal to
        # have one.  Say so plainly rather than printing a traceback an
        # operator would have to read as "the gate crashed".
        message = "cannot verify %s: %s" % (directory.name, exc.message)
        if _ARGS.json:
            print(json.dumps({"refused": True, "reason": message}, indent=2, sort_keys=True))
        else:
            print(message, file=sys.stderr)
        return int(ExitCode.USAGE)
    payload = {
        "run_id": bundle.run_id,
        "run_complete": bundle.run_complete,
        "verdict": bundle.verdict,
        "reasons": list(bundle.aggregate.get("reasons", ())),
        "bundle": BUNDLE_FILENAME,
        "four_fields": list(bundle.four_fields),
        "field_unverified": list(bundle.field_unverified),
    }
    if _ARGS.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("run %s: %s" % (bundle.run_id, bundle.verdict))
        for reason in bundle.aggregate.get("reasons", ()):
            print("  reason: %s" % reason)
        # One table, rendered by the adapter: every check in the bundle crosses
        # ``as_verdict`` and the rows are printed by ``render_rows``.  Printing
        # them here with a second format string would give the gate two
        # spellings of the same table, free to drift apart.
        print(render_rows([as_verdict("runner", check) for check in bundle.checks]))
        if not bundle.field_unverified:
            print("  no field left unverified")
        else:
            print("  fields left unverified on site:")
            for item in bundle.field_unverified:
                print("    %s" % item)
    return int(ExitCode.OK if bundle.verdict == VerdictKind.PASS else ExitCode.BLOCKED)


def _refuse_run(run_id: str) -> int:
    """Refuse ``--run`` until the check universe exists, and say why."""
    message = (
        "refusing to run the gate for %r: no registered check universe exists "
        "yet, and a bundle with zero checks would still roll up to a verdict. "
        "Use --verify <run_dir> to re-check a bundle that was already written."
        % run_id
    )
    if _ARGS.json:
        print(json.dumps({"refused": True, "reason": message}, indent=2, sort_keys=True))
    else:
        print(message, file=sys.stderr)
    return int(ExitCode.BLOCKED)


#: The parsed arguments, set by :func:`main`.  Module-level so the helpers above
#: stay single-argument and testable; there is exactly one parse per process.
_ARGS: argparse.Namespace = argparse.Namespace(json=False)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.  Returns the process exit code; never raises for a refusal."""
    global _ARGS
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _ARGS = args

    if args.version:
        from .runner import ACCEPTANCE_GATE_ID, ACCEPTANCE_GATE_VERSION

        identity = "%s %s" % (ACCEPTANCE_GATE_ID, ACCEPTANCE_GATE_VERSION)
        if args.json:
            print(json.dumps({"gate": identity}, sort_keys=True))
        else:
            print(identity)
        return int(ExitCode.OK)

    if args.run:
        return _refuse_run(args.run)
    if args.verify:
        return _verify(args.verify)

    parser.print_help()
    return int(ExitCode.USAGE)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
