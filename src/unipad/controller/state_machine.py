"""Experiment state machine (PRD section 7.2).

The controller drives experiments serially: one evidence-backed blocked point
is advanced at a time (PRD 7.1).  The mermaid diagram of PRD 7.2 is rendered
here as *data* so the controller, the CLI and the tests share a single table.

States
------
``Ready`` / ``Diagnose`` / ``Patch`` / ``Build`` / ``Run`` / ``Verify`` /
``Accepted`` / ``Failed`` / ``Blocked``

Documented edges (exactly these; nothing else is legal)
-------------------------------------------------------
==================  ===============  =========================================
from state          event            to state
==================  ===============  =========================================
Ready               diagnose         Diagnose
Diagnose            patch            Patch
Patch               build            Build
Build               run              Run       (build artifacts合格)
Build               build_failed     Failed    (build failed)
Run                 verify           Verify
Verify              accept           Accepted  (gate evidence passed)
Verify              reject           Failed    (failure or insufficient evidence)
Failed              diagnose         Diagnose  (within budget, new hypothesis)
Failed              block            Blocked   (repeated failure / budget gone)
Accepted            reset            Ready     (checkpoint saved)
Blocked             reset            Ready     (input or plan updated)
==================  ===============  =========================================

Any ``(state, event)`` pair that is absent from :data:`TRANSITIONS` is an
illegal transition and raises
``UniPadError(ErrorCode.EXPERIMENT_STATE_INVALID)``.  The machine never skips
a gate and never rewrites its own history, so a controller cannot silently
declare ``Accepted`` without walking ``Verify`` first.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from ..errors import ErrorCode, UniPadError

__all__ = [
    "STATES",
    "INITIAL_STATE",
    "TRANSITIONS",
    "TRANSITION_EDGES",
    "EVENTS",
    "Transition",
    "ExperimentStateMachine",
]


class Transition(NamedTuple):
    """One legal edge of the PRD 7.2 state machine.

    A ``NamedTuple`` is a real tuple, so ``history`` entries can be unpacked
    as ``(from_state, event, to_state)`` or read by attribute name.
    """

    from_state: str
    event: str
    to_state: str

    def to_dict(self) -> dict[str, str]:
        """Return the edge as a JSON-serialisable mapping."""
        return {"from": self.from_state, "event": self.event, "to": self.to_state}


# Canonical state names (PRD 7.2).  Kept as plain strings: new sub-gates appear
# upstream and an enum would force a contract change for every addition.
READY = "Ready"
DIAGNOSE = "Diagnose"
PATCH = "Patch"
BUILD = "Build"
RUN = "Run"
VERIFY = "Verify"
ACCEPTED = "Accepted"
FAILED = "Failed"
BLOCKED = "Blocked"

STATES: tuple[str, ...] = (
    READY,
    DIAGNOSE,
    PATCH,
    BUILD,
    RUN,
    VERIFY,
    ACCEPTED,
    FAILED,
    BLOCKED,
)

#: The machine always starts here (PRD 7.2 ``[*] --> Ready``).
INITIAL_STATE: str = READY

#: Event names, one per documented edge.  Stable identifiers: scripts branch on them.
EVENT_DIAGNOSE = "diagnose"
EVENT_PATCH = "patch"
EVENT_BUILD = "build"
EVENT_RUN = "run"
EVENT_BUILD_FAILED = "build_failed"
EVENT_VERIFY = "verify"
EVENT_ACCEPT = "accept"
EVENT_REJECT = "reject"
EVENT_BLOCK = "block"
EVENT_RESET = "reset"

#: ``TRANSITIONS[state][event] -> next_state``.  This table *is* the contract.
TRANSITIONS: dict[str, dict[str, str]] = {
    READY: {EVENT_DIAGNOSE: DIAGNOSE},
    DIAGNOSE: {EVENT_PATCH: PATCH},
    PATCH: {EVENT_BUILD: BUILD},
    BUILD: {EVENT_RUN: RUN, EVENT_BUILD_FAILED: FAILED},
    RUN: {EVENT_VERIFY: VERIFY},
    VERIFY: {EVENT_ACCEPT: ACCEPTED, EVENT_REJECT: FAILED},
    FAILED: {EVENT_DIAGNOSE: DIAGNOSE, EVENT_BLOCK: BLOCKED},
    ACCEPTED: {EVENT_RESET: READY},
    BLOCKED: {EVENT_RESET: READY},
}

#: Flat, ordered projection of :data:`TRANSITIONS` for reports and audits.
TRANSITION_EDGES: tuple[Transition, ...] = tuple(
    Transition(source, event, target)
    for source, targets in TRANSITIONS.items()
    for event, target in targets.items()
)

#: Every legal event name across all states.
EVENTS: tuple[str, ...] = tuple(sorted({edge.event for edge in TRANSITION_EDGES}))


class ExperimentStateMachine:
    """Serial experiment driver implementing PRD 7.2 exactly.

    Parameters
    ----------
    state:
        Initial state.  Defaults to :data:`INITIAL_STATE` (``Ready``).  An
        unknown state is a usage error, not a silent fallback.

    Attributes
    ----------
    state:
        Current state name (see :data:`STATES`).
    history:
        Ordered ``(from_state, event, to_state)`` transitions actually fired.

    Notes
    -----
    The instance is deliberately *not* thread-safe: PRD 7.1 mandates one
    serial controller, so concurrent firing would itself be a contract
    violation.  Callers that need concurrency must own one machine per worker.
    """

    __slots__ = ("_state", "_history")

    def __init__(self, state: str = INITIAL_STATE) -> None:
        if state not in STATES:
            raise UniPadError(
                ErrorCode.EXPERIMENT_STATE_INVALID,
                "unknown initial experiment state",
                state=state,
                known_states=list(STATES),
            )
        self._state = state
        self._history: list[Transition] = []

    # --- introspection -----------------------------------------------------
    @property
    def state(self) -> str:
        """Current state name."""
        return self._state

    @property
    def history(self) -> list[Transition]:
        """Ordered transitions fired so far as ``(from_state, event, to_state)``."""
        return list(self._history)

    def allowed_events(self) -> tuple[str, ...]:
        """Return the events legal in the current state (sorted, deterministic)."""
        return tuple(sorted(TRANSITIONS.get(self._state, {})))

    def can(self, event: str) -> bool:
        """Return ``True`` when ``event`` is legal from the current state.

        Unknown event names are simply not legal; no exception is raised so
        callers can probe without ``try``.
        """
        return event in TRANSITIONS.get(self._state, {})

    def targets(self, event: str) -> str | None:
        """Return the state ``event`` would reach, or ``None`` when illegal."""
        return TRANSITIONS.get(self._state, {}).get(event)

    # --- driving -----------------------------------------------------------
    def fire(self, event: str) -> str:
        """Fire ``event`` and return the new state.

        Raises
        ------
        UniPadError
            ``ErrorCode.EXPERIMENT_STATE_INVALID`` when the edge is not in
            :data:`TRANSITIONS`.  The machine state is left untouched so the
            caller can report the illegal request without losing progress.
        """
        targets = TRANSITIONS.get(self._state)
        if not targets or event not in targets:
            raise UniPadError(
                ErrorCode.EXPERIMENT_STATE_INVALID,
                "illegal experiment state transition",
                state=self._state,
                event=event,
                allowed_events=sorted(targets or {}),
                known_states=list(STATES),
            )
        previous = self._state
        self._state = targets[event]
        self._history.append(Transition(previous, event, self._state))
        return self._state

    # --- machine-readable output ------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot (state, history, legal events)."""
        return {
            "state": self._state,
            "allowed_events": list(self.allowed_events()),
            "history": [edge.to_dict() for edge in self._history],
            "transitions": [edge.to_dict() for edge in TRANSITION_EDGES],
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ExperimentStateMachine(state={self._state!r}, steps={len(self._history)})"


def describe() -> dict[str, Any]:
    """Return the whole state machine as data (states, events, edges).

    Useful for ``doctor``-style reports and for auditing that the code still
    matches the PRD 7.2 diagram.
    """
    return {
        "initial_state": INITIAL_STATE,
        "states": list(STATES),
        "events": list(EVENTS),
        "transitions": [edge.to_dict() for edge in TRANSITION_EDGES],
    }


def legal_events(state: str) -> tuple[str, ...]:
    """Return the events legal from ``state`` (empty tuple for an unknown state)."""
    return tuple(sorted(TRANSITIONS.get(state, {})))
