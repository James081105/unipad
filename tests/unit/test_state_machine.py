"""Unit tests for the experiment state machine (PRD 7.2).

Mapping
-------
* PRD 7.2 state diagram -- the machine implements exactly the documented edges
  and refuses everything else, so a controller can never silently declare
  ``Accepted`` without walking ``Verify`` first.
* PRD 7.1 -- one serial controller: firing an illegal edge leaves the state
  untouched so the caller can report the illegal request without losing
  progress.
* PRD 7.2 "实验失败时保留修改和日志" -- ``history`` is append-only and is
  returned as a copy, so a caller cannot rewrite what happened.
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

#: Every legal edge of the PRD 7.2 diagram as ``(from, event, to)``.
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


def _edge_set() -> set[tuple[str, str, str]]:
    """Return the implemented edges as a set of ``(from, event, to)``."""
    return {(edge.from_state, edge.event, edge.to_state) for edge in TRANSITION_EDGES}


# ---------------------------------------------------------------------------
# the table is the contract
# ---------------------------------------------------------------------------
def test_transition_table_matches_the_prd_7_2_diagram_exactly() -> None:
    # Act / assert: nothing extra, nothing missing.  An undocumented edge would
    # let the controller skip a gate, so this is an equality check.
    assert _edge_set() == set(PRD_7_2_EDGES)
    assert len(TRANSITION_EDGES) == len(PRD_7_2_EDGES)


def test_states_are_the_prd_7_2_state_names() -> None:
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
    assert INITIAL_STATE == READY


def test_every_event_name_is_unique_per_source_state() -> None:
    # A duplicated event inside one state would make the target ambiguous.
    for source, targets in TRANSITIONS.items():
        assert len(set(targets)) == len(targets), source


def test_legal_events_helper_agrees_with_the_table() -> None:
    assert legal_events("Build") == ("build_failed", "run")
    assert legal_events("Ready") == ("diagnose",)
    assert legal_events("Unknown") == ()


def test_describe_returns_the_whole_machine_as_data() -> None:
    payload = describe()

    assert payload["initial_state"] == READY
    assert set(payload["states"]) == set(STATES)
    assert len(payload["transitions"]) == len(PRD_7_2_EDGES)
    assert payload["transitions"][0] == {"from": "Ready", "event": "diagnose", "to": "Diagnose"}


# ---------------------------------------------------------------------------
# legal transitions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("source", "event", "target"), PRD_7_2_EDGES)
def test_every_legal_transition_fires(source: str, event: str, target: str) -> None:
    # Arrange.
    machine = ExperimentStateMachine(source)

    # Act.
    reached = machine.fire(event)

    # Assert.
    assert reached == target
    assert machine.state == target


def test_full_happy_path_walks_every_stage_in_order() -> None:
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


def test_build_failure_path_reaches_failed_then_blocked() -> None:
    # PRD 7.2: Build --> Failed (build failed); Failed --> Blocked (repeated
    # failure or budget gone).
    machine = ExperimentStateMachine()
    machine.fire("diagnose")
    machine.fire("patch")
    machine.fire("build")
    machine.fire("build_failed")

    assert machine.state == FAILED
    assert machine.fire("block") == BLOCKED


def test_failed_loops_back_to_diagnose_within_budget() -> None:
    # PRD 7.2: Failed --> Diagnose (within budget and a new hypothesis).
    machine = ExperimentStateMachine()
    machine.fire("diagnose")
    machine.fire("patch")
    machine.fire("build")
    machine.fire("build_failed")

    assert machine.fire("diagnose") == DIAGNOSE


def test_verify_rejection_reaches_failed() -> None:
    # PRD 7.2: Verify --> Failed (failure or insufficient evidence).
    machine = ExperimentStateMachine()
    machine.fire("diagnose")
    machine.fire("patch")
    machine.fire("build")
    machine.fire("run")
    machine.fire("verify")

    assert machine.fire("reject") == FAILED


@pytest.mark.parametrize("source", [ACCEPTED, BLOCKED])
def test_accepted_and_blocked_reset_back_to_ready(source: str) -> None:
    # PRD 7.2: Accepted --> Ready (checkpoint saved); Blocked --> Ready (input
    # or plan updated).
    machine = ExperimentStateMachine(source)

    assert machine.fire("reset") == READY


# ---------------------------------------------------------------------------
# illegal transitions
# ---------------------------------------------------------------------------
def test_illegal_transition_raises_experiment_state_invalid() -> None:
    # Arrange: Ready only allows "diagnose".
    machine = ExperimentStateMachine()

    # Act / assert.
    with pytest.raises(UniPadError) as caught:
        machine.fire("accept")
    assert caught.value.code == ErrorCode.EXPERIMENT_STATE_INVALID


def test_illegal_transition_leaves_the_state_untouched() -> None:
    # PRD 7.1: report the illegal request without losing progress.
    machine = ExperimentStateMachine()
    machine.fire("diagnose")

    with pytest.raises(UniPadError):
        machine.fire("run")

    assert machine.state == DIAGNOSE
    assert len(machine.history) == 1


def test_gate_skipping_is_refused() -> None:
    # The machine must not jump from Ready straight to Verify/Accepted.
    machine = ExperimentStateMachine()

    for event in ("verify", "accept", "run", "build", "patch", "reset", "block"):
        assert machine.can(event) is False
        with pytest.raises(UniPadError):
            machine.fire(event)

    assert machine.state == READY


def test_accepted_cannot_reenter_the_cycle_without_reset() -> None:
    # Arrange.
    machine = ExperimentStateMachine(ACCEPTED)

    # Act / assert: only "reset" is legal from Accepted.
    assert machine.allowed_events() == ("reset",)
    with pytest.raises(UniPadError) as caught:
        machine.fire("diagnose")
    assert caught.value.code == ErrorCode.EXPERIMENT_STATE_INVALID


def test_unknown_event_name_is_illegal_without_raising_from_can() -> None:
    # Arrange.
    machine = ExperimentStateMachine()

    # Act / assert: probing never raises so callers can ask before firing.
    assert machine.can("not_an_event") is False
    assert machine.targets("not_an_event") is None
    with pytest.raises(UniPadError):
        machine.fire("not_an_event")


def test_illegal_transition_error_details_name_the_state_and_event() -> None:
    # Arrange.
    machine = ExperimentStateMachine(BUILD)

    # Act.
    with pytest.raises(UniPadError) as caught:
        machine.fire("accept")

    # Assert: the report is machine-readable, not prose.
    details = caught.value.details
    assert details["state"] == BUILD
    assert details["event"] == "accept"
    assert sorted(details["allowed_events"]) == ["build_failed", "run"]


def test_unknown_initial_state_is_a_usage_error() -> None:
    # Arrange / act / assert: an unknown state is a usage error, never a silent
    # fallback to Ready.
    with pytest.raises(UniPadError) as caught:
        ExperimentStateMachine("Running")
    assert caught.value.code == ErrorCode.EXPERIMENT_STATE_INVALID
    assert caught.value.details["state"] == "Running"


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------
def test_history_records_from_event_to_for_every_fired_edge() -> None:
    # Arrange.
    machine = ExperimentStateMachine()

    # Act.
    machine.fire("diagnose")
    machine.fire("patch")
    machine.fire("build")
    machine.fire("build_failed")

    # Assert: PRD 7.2 history entries are (from, event, to) and unpack as such.
    assert machine.history == [
        Transition(READY, "diagnose", DIAGNOSE),
        Transition(DIAGNOSE, "patch", PATCH),
        Transition(PATCH, "build", BUILD),
        Transition(BUILD, "build_failed", FAILED),
    ]
    for entry in machine.history:
        source, event, target = entry
        assert (source, event, target) == (entry.from_state, entry.event, entry.to_state)


def test_history_starts_empty_and_grows_only_on_success() -> None:
    # Arrange.
    machine = ExperimentStateMachine()

    # Assert.
    assert machine.history == []

    # Act: an illegal fire must not append anything.
    with pytest.raises(UniPadError):
        machine.fire("accept")

    assert machine.history == []


def test_history_is_a_copy_so_callers_cannot_rewrite_it() -> None:
    # PRD 7.2: the journal of what happened is append-only.
    machine = ExperimentStateMachine()
    machine.fire("diagnose")

    snapshot = machine.history
    snapshot.append(Transition(READY, "forged", ACCEPTED))

    assert machine.history == [Transition(READY, "diagnose", DIAGNOSE)]


def test_transition_to_dict_is_the_documented_shape() -> None:
    # Arrange / act.
    payload = Transition(READY, "diagnose", DIAGNOSE).to_dict()

    # Assert.
    assert payload == {"from": "Ready", "event": "diagnose", "to": "Diagnose"}


def test_to_dict_snapshot_reports_state_allowed_events_and_history() -> None:
    # Arrange.
    machine = ExperimentStateMachine()
    machine.fire("diagnose")

    # Act.
    payload = machine.to_dict()

    # Assert.
    assert payload["state"] == DIAGNOSE
    assert payload["allowed_events"] == ["patch"]
    assert payload["history"] == [{"from": "Ready", "event": "diagnose", "to": "Diagnose"}]
    assert len(payload["transitions"]) == len(PRD_7_2_EDGES)


def test_can_and_targets_report_the_table_without_mutating() -> None:
    # Arrange.
    machine = ExperimentStateMachine(VERIFY)

    # Act / assert.
    assert machine.can("accept") is True
    assert machine.targets("accept") == ACCEPTED
    assert machine.can("reject") is True
    assert machine.targets("reject") == FAILED
    assert machine.state == VERIFY
    assert machine.history == []
