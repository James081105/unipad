"""Controller state machine conformance tests (PRD section 7.2).

Mapping
-------
* **PRD 7.2 state diagram** -- every documented edge is implemented and fires;
  every other ``(state, event)`` pair raises ``experiment_state_invalid``.  The
  sweep in :func:`test_every_undocumented_edge_is_refused` is exhaustive over
  the whole ``states x events`` matrix, so a new edge cannot be added to the
  code without a test noticing.
* **PRD 7.1** -- one serial controller: an illegal fire leaves the state *and*
  the history untouched, so the caller reports the illegal request without
  losing progress.
* **PRD 7.2 "实验失败时保留修改和日志"** -- ``history`` is append-only and is
  handed out as a copy.
* **PRD 7.2 ``Blocked --> Ready: 输入或方案已更新``** -- a blocked path only
  resumes through ``reset``.

These tests are pure data/logic checks: no process, no clock, no filesystem.
"""

from __future__ import annotations

import pytest

from unipad.controller.state_machine import (
    ACCEPTED,
    BLOCKED,
    BUILD,
    DIAGNOSE,
    FAILED,
    INITIAL_STATE,
    PATCH,
    READY,
    RUN,
    STATES,
    TRANSITIONS,
    TRANSITION_EDGES,
    VERIFY,
    ExperimentStateMachine,
    Transition,
    describe,
    legal_events,
)
from unipad.errors import ErrorCode, UniPadError

#: The PRD 7.2 diagram, verbatim: ``(from, event, to)``.
PRD_7_2_EDGES: tuple[tuple[str, str, str], ...] = (
    ("Ready", "diagnose", "Diagnose"),
    ("Diagnose", "patch", "Patch"),
    ("Patch", "build", "Build"),
    ("Build", "run", "Run"),
    ("Build", "build_failed", "Failed"),
    ("Run", "verify", "Verify"),
    ("Verify", "accept", "Accepted"),
    ("Verify", "reject", "Failed"),
    ("Failed", "diagnose", "Diagnose"),
    ("Failed", "block", "Blocked"),
    ("Accepted", "reset", "Ready"),
    ("Blocked", "reset", "Ready"),
)

#: Every event name the PRD 7.2 diagram uses (one per edge).
PRD_7_2_EVENTS: frozenset[str] = frozenset(edge[1] for edge in PRD_7_2_EDGES)


def _illegal_pairs() -> list[tuple[str, str]]:
    """Return every ``(state, event)`` pair the PRD 7.2 diagram does not allow."""
    illegal: list[tuple[str, str]] = []
    for state in STATES:
        allowed = set(TRANSITIONS.get(state, {}))
        for event in sorted(PRD_7_2_EVENTS):
            if event not in allowed:
                illegal.append((state, event))
    return illegal


# ---------------------------------------------------------------------------
# PRD 7.2: the table *is* the contract
# ---------------------------------------------------------------------------


def test_the_transition_table_is_the_prd_7_2_diagram_exactly() -> None:
    implemented = {(edge.from_state, edge.event, edge.to_state) for edge in TRANSITION_EDGES}

    assert implemented == set(PRD_7_2_EDGES)
    assert len(TRANSITION_EDGES) == len(PRD_7_2_EDGES)


def test_the_state_names_are_the_prd_7_2_state_names() -> None:
    assert set(STATES) == {
        "Ready",
        "Diagnose",
        "Patch",
        "Build",
        "Run",
        "Verify",
        "Accepted",
        "Failed",
        "Blocked",
    }
    # PRD 7.2: [*] --> Ready
    assert INITIAL_STATE == READY
    assert ExperimentStateMachine().state == READY


def test_describe_reports_the_diagram_as_data_for_audits() -> None:
    payload = describe()

    assert payload["initial_state"] == READY
    assert set(payload["states"]) == set(STATES)
    assert set(payload["events"]) == PRD_7_2_EVENTS
    assert len(payload["transitions"]) == len(PRD_7_2_EDGES)
    assert payload["transitions"][0] == {"from": "Ready", "event": "diagnose", "to": "Diagnose"}


# ---------------------------------------------------------------------------
# PRD 7.2: every legal transition fires
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "event", "target"), PRD_7_2_EDGES)
def test_every_legal_transition_fires(source: str, event: str, target: str) -> None:
    machine = ExperimentStateMachine(source)

    assert machine.can(event) is True
    assert machine.targets(event) == target
    assert machine.fire(event) == target
    assert machine.state == target


def test_the_happy_path_walks_ready_through_accepted_in_order() -> None:
    # PRD 7.2: Ready -> Diagnose -> Patch -> Build -> Run -> Verify -> Accepted.
    machine = ExperimentStateMachine()

    for event, expected in (
        ("diagnose", DIAGNOSE),
        ("patch", PATCH),
        ("build", BUILD),
        ("run", RUN),
        ("verify", VERIFY),
        ("accept", ACCEPTED),
    ):
        assert machine.fire(event) == expected


def test_a_build_failure_reaches_failed_and_then_blocked() -> None:
    # PRD 7.2: Build --> Failed (build failed); Failed --> Blocked (repeated
    # failure or budget gone).
    machine = ExperimentStateMachine()
    machine.fire("diagnose")
    machine.fire("patch")
    machine.fire("build")

    assert machine.fire("build_failed") == FAILED
    assert machine.fire("block") == BLOCKED


def test_a_verify_rejection_reaches_failed_without_skipping_the_gate() -> None:
    # PRD 7.2: Verify --> Failed (failure or insufficient evidence).
    machine = ExperimentStateMachine()
    machine.fire("diagnose")
    machine.fire("patch")
    machine.fire("build")
    machine.fire("run")
    machine.fire("verify")

    assert machine.state == VERIFY
    assert machine.fire("reject") == FAILED


def test_failed_loops_back_to_diagnose_within_budget() -> None:
    # PRD 7.2: Failed --> Diagnose (within budget and a new hypothesis).
    machine = ExperimentStateMachine()
    machine.fire("diagnose")
    machine.fire("patch")
    machine.fire("build")

    assert machine.fire("build_failed") == FAILED
    assert machine.fire("diagnose") == DIAGNOSE


@pytest.mark.parametrize("source", [ACCEPTED, BLOCKED])
def test_accepted_and_blocked_reset_back_to_ready(source: str) -> None:
    # PRD 7.2: Accepted --> Ready (checkpoint saved); Blocked --> Ready (input
    # or plan updated).
    machine = ExperimentStateMachine(source)

    assert machine.fire("reset") == READY


def test_blocked_only_resumes_after_the_input_or_plan_is_updated() -> None:
    # PRD 7.2 "Blocked --> Ready: 输入或方案已更新": nothing else may resume the
    # path, so a controller cannot quietly unblock itself.
    machine = ExperimentStateMachine()
    machine.fire("diagnose")
    machine.fire("patch")
    machine.fire("build")
    machine.fire("build_failed")
    machine.fire("block")

    assert machine.state == BLOCKED
    assert machine.allowed_events() == ("reset",)
    assert machine.fire("reset") == READY


# ---------------------------------------------------------------------------
# PRD 7.2: every undocumented edge is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "event"), _illegal_pairs())
def test_every_undocumented_edge_is_refused(source: str, event: str) -> None:
    machine = ExperimentStateMachine(source)

    with pytest.raises(UniPadError) as caught:
        machine.fire(event)

    error = caught.value
    assert error.code == ErrorCode.EXPERIMENT_STATE_INVALID
    assert error.details["state"] == source
    assert error.details["event"] == event
    assert sorted(error.details["allowed_events"]) == sorted(TRANSITIONS[source])
    # The machine never moves and never rewrites its history on a refusal.
    assert machine.state == source
    assert machine.history == []


@pytest.mark.parametrize("state", STATES)
def test_no_gate_can_be_skipped_from_any_state(state: str) -> None:
    # The specific skip the PRD cares about: reaching Accepted without Verify.
    machine = ExperimentStateMachine(state)

    assert machine.can("accept") is (state == VERIFY)
    if state != VERIFY:
        with pytest.raises(UniPadError):
            machine.fire("accept")


def test_an_unknown_event_name_is_illegal_but_probeable() -> None:
    machine = ExperimentStateMachine()

    assert machine.can("not_an_event") is False
    assert machine.targets("not_an_event") is None
    with pytest.raises(UniPadError) as caught:
        machine.fire("not_an_event")
    assert caught.value.code == ErrorCode.EXPERIMENT_STATE_INVALID


def test_an_unknown_initial_state_is_a_usage_error() -> None:
    with pytest.raises(UniPadError) as caught:
        ExperimentStateMachine("Running")

    assert caught.value.code == ErrorCode.EXPERIMENT_STATE_INVALID
    assert caught.value.details["state"] == "Running"
    assert "Ready" in caught.value.details["known_states"]


def test_legal_events_helper_agrees_with_the_table() -> None:
    assert legal_events(BUILD) == ("build_failed", "run")
    assert legal_events(READY) == ("diagnose",)
    assert legal_events("NotAState") == ()


# ---------------------------------------------------------------------------
# PRD 7.2: history
# ---------------------------------------------------------------------------


def test_history_records_from_event_to_for_every_fired_edge() -> None:
    machine = ExperimentStateMachine()
    machine.fire("diagnose")
    machine.fire("patch")
    machine.fire("build")
    machine.fire("build_failed")

    assert machine.history == [
        Transition(READY, "diagnose", DIAGNOSE),
        Transition(DIAGNOSE, "patch", PATCH),
        Transition(PATCH, "build", BUILD),
        Transition(BUILD, "build_failed", FAILED),
    ]
    # A Transition is a real tuple, so a journal entry unpacks directly.
    for entry in machine.history:
        source, event, target = entry
        assert (source, event, target) == (entry.from_state, entry.event, entry.to_state)
        assert entry.to_dict() == {"from": source, "event": event, "to": target}


def test_history_starts_empty_and_grows_only_on_a_successful_fire() -> None:
    machine = ExperimentStateMachine()

    assert machine.history == []
    with pytest.raises(UniPadError):
        machine.fire("accept")
    assert machine.history == []


def test_history_is_a_copy_so_a_caller_cannot_rewrite_it() -> None:
    # PRD 7.2: the journal of what happened is append-only.
    machine = ExperimentStateMachine()
    machine.fire("diagnose")

    snapshot = machine.history
    snapshot.append(Transition(READY, "forged", ACCEPTED))

    assert machine.history == [Transition(READY, "diagnose", DIAGNOSE)]


def test_to_dict_snapshot_carries_state_allowed_events_and_history() -> None:
    machine = ExperimentStateMachine()
    machine.fire("diagnose")

    payload = machine.to_dict()

    assert payload["state"] == DIAGNOSE
    assert payload["allowed_events"] == ["patch"]
    assert payload["history"] == [{"from": "Ready", "event": "diagnose", "to": "Diagnose"}]
    assert len(payload["transitions"]) == len(PRD_7_2_EDGES)
