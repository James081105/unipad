"""A bundle is read back without re-judging the kind its producer already judged.

Why this exists
---------------
MEASURED 2026-11-03.  Every case in ``test_acceptance_registry.py`` was red on
this host, and the acceptance registry could not report a verdict at all:

    UniPadError: unknown evidence kind
    kind: 'precheck_report'

``Verdict.from_dict`` hardcoded ``EvidenceRef.from_dict(ref)`` -- the BASE class,
whose kind set is the BUNDLE vocabulary.  A bundle is written by producers whose
subclasses carry wider sets (``e2e`` alone declares 28 more, for the things PRD
5.2 asks for and a bundle does not store).  So a kind the write side accepted was
rejected by the read side.

The fix is not a union list.  ``LoadedEvidenceRef`` declines exactly the kind
check and keeps every other rule, because the kind was already validated -- by
whichever producer wrote the reference, against that producer's own set.  A union
would mean re-listing 28 e2e kinds here and updating them whenever a case gains
one, for no gain.

A note on how this was found, because the first diagnosis was wrong
------------------------------------------------------------------
I first concluded the opposite -- that ``record.EVIDENCE_KINDS`` was too narrow
and should be widened -- and very nearly did it.  The instrument that told me so
patched ``record.EvidenceRef.__post_init__`` and compared the kind against
``rec.EvidenceRef.ALLOWED_KINDS``: the BASE set.  Every kind a subclass
legitimately widened to therefore looked rejected, including ``trace_log``, which
``milestones`` has accepted since 2026-10-24 via ``MILESTONE_EVIDENCE_KINDS``.

Widening the base set would have "fixed" the symptom and destroyed a deliberate
design that two existing tests pin -- ``test_the_evidence_reference_is_shared_
not_reimplemented`` asserts the sets differ on purpose, with a documented reason.
The correct instrument compares against ``self.ALLOWED_KINDS``, which is what the
real ``__post_init__`` uses, and that points straight at ``Verdict.from_dict``.
Sixth instrument this session that had to be validated before its answer could
be used, and the only one that would have caused damage rather than delay.

What is NOT checked here
------------------------
* that a producer chose the RIGHT kind for its evidence.  A probe labelling a
  screenshot ``serial_log`` passes everything below.
* ``__main__.py``'s silent coercion of an unknown kind to ``probe_log``.  That
  turns a usage error into a mislabel and is a separate hole.
"""

from __future__ import annotations

import pytest

from unipad.acceptance import e2e, record
from unipad.acceptance.record import EvidenceRef, LoadedEvidenceRef


def test_the_read_side_accepts_what_every_producer_declares() -> None:
    """The invariant that was broken, over the real producers.

    The producers are imported explicitly rather than discovered through
    ``__subclasses__()``: a discovered set depends on which modules happen to
    have been imported, and ``load_bundle`` is exactly the path where that is not
    knowable.  Importing them here is what makes the check mean something.
    """
    for producer in (e2e.EvidenceRef, record.EvidenceRef):
        for kind in producer.ALLOWED_KINDS:
            ref = LoadedEvidenceRef(path="a.log", kind=kind, sha256="0" * 64)
            assert ref.kind == kind


def test_the_bundle_vocabulary_is_unchanged() -> None:
    """The base set is the bundle's, and the fix must not widen it.

    Two existing tests pin that the producer sets differ from the bundle set on
    purpose (``test_the_evidence_reference_is_shared_not_reimplemented``).  This
    is the assertion that keeps this fix from quietly becoming the widening I
    first reached for.
    """
    assert set(record.EVIDENCE_KINDS) == {
        "serial_log", "guest_log", "qmp_log", "screenshot", "artifact",
        "suite_report", "verification_json", "manifest", "ledger", "probe_log",
    }
    assert "trace_log" not in record.EVIDENCE_KINDS
    assert "precheck_report" not in record.EVIDENCE_KINDS


def test_construction_still_refuses_an_unknown_kind() -> None:
    """The write side is unchanged, and this is the rule that has teeth.

    Without it the fix could be satisfied by deleting the check everywhere.
    """
    with pytest.raises(Exception) as caught:
        EvidenceRef(path="a.log", kind="not_a_kind_at_all", sha256="0" * 64)
    assert "unknown evidence kind" in str(caught.value)


def test_the_read_side_still_enforces_every_other_rule() -> None:
    """Declining the KIND check must not become declining all of them.

    A subclass that skipped ``__post_init__`` entirely would accept a malformed
    digest, a missing reason and a witness-less on-site claim -- each of which is
    a hole the closed set was never the only guard for.
    """
    # A digest that is not 64 lowercase hex is still refused.
    with pytest.raises(Exception) as caught:
        LoadedEvidenceRef(path="a.log", kind="precheck_report", sha256="nope")
    assert "digest" in str(caught.value)

    # Evidence with no digest and no reason is still refused.
    with pytest.raises(Exception) as caught:
        LoadedEvidenceRef(path="a.log", kind="precheck_report", sha256=None,
                          digest_reason="   ")
    assert "digest" in str(caught.value)

    # An on-site claim with no witness is still refused.
    with pytest.raises(Exception) as caught:
        LoadedEvidenceRef(path="a.log", kind="precheck_report", sha256="0" * 64,
                          scope="onsite")
    assert "witness" in str(caught.value)


def test_the_read_side_round_trips_through_a_verdict() -> None:
    """End to end: a verdict carrying a producer's kind survives to_dict/from_dict.

    That is the exact path ``load_bundle`` takes, and the one that was raising.
    """
    from unipad.acceptance.record import Verdict

    ref = e2e.EvidenceRef(path="acceptance-e2e/sample.precheck.json",
                          kind="precheck_report", sha256="0" * 64)
    verdict = Verdict.fail(reasons=("sample_precheck_unavailable:blocked",),
                           evidence=(ref,))
    reloaded = Verdict.from_dict(verdict.to_dict())
    assert [r.kind for r in reloaded.evidence] == ["precheck_report"]
    assert reloaded.evidence[0].sha256 == "0" * 64
