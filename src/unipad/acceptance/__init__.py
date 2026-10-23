"""Executable acceptance gate for UniPad (PRD 6, 7.4 and 9.2).

The gate answers one question the existing verifier cannot: *did this run
clear PRD P0 and P1, and in which environment?*  It binds a set of gate
conclusions, the PRD 5.2 end-to-end cases, the PRD 5.3 measurements and the
environment every piece of evidence was produced in into a single verdict that
carries the four fields (实现 / Windows/模拟 / 现场 / 阻断).

Two axes, deliberately separate:

* :data:`~unipad.acceptance.record.VerdictKind` -- the condition axis: did the
  registered condition hold *for the evidence supplied*?
* :class:`~unipad.acceptance.record.FourFields` -- the environment axis: where
  was that evidence produced?

``SIMULATION PASS`` therefore cannot become an on-site ``PASS``: the two read
different evidence, and the on-site field is a derived read-only property
rather than something a caller can assign.

Public API
----------
- :mod:`unipad.acceptance.record` -- the record types and their invariants
- :mod:`unipad.acceptance.runner` -- the run orchestrator and the bundle

Only :mod:`unipad.acceptance.record` is re-exported here.  The record types
are the frozen contract the rest of the package is written against, so this
module never needs to change when the orchestrator does.
"""

from __future__ import annotations

from .record import (
    EvidenceRef,
    EvidenceScope,
    EnvironmentStamp,
    FourFields,
    Measurement,
    Verdict,
    VerdictKind,
    assert_payload_redacted,
    assert_verdict_wellformed,
    redact,
    relativise,
    scope_at_least,
    worst_of,
)

__all__ = [
    "EvidenceRef",
    "EvidenceScope",
    "EnvironmentStamp",
    "FourFields",
    "Measurement",
    "Verdict",
    "VerdictKind",
    "assert_payload_redacted",
    "assert_verdict_wellformed",
    "redact",
    "relativise",
    "scope_at_least",
    "worst_of",
]
