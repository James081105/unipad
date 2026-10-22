"""UniPad experiment controller (PRD sections 7 and 9.1).

Public API
----------
- :class:`ExperimentStateMachine` -- PRD 7.2 experiment states/transitions
- :class:`ExperimentStore`       -- PRD 7.3 experiment journal on disk
- :class:`BudgetTracker`         -- PRD 7.5 experiment/cost budget
- :class:`Verifier`              -- PRD 7.4 fixed-condition gate verifier
- :func:`main`                   -- PRD 9.1 machine-readable CLI

The controller owns *orchestration only*.  The real Windows, QEMU, IPA and
guest work lives in :mod:`unipad.host`, :mod:`unipad.ipa` and
:mod:`unipad.guest`; every command here imports those lazily so the controller
keeps working (and keeps reporting honest ``blocked`` results) on a host where
they cannot run yet.
"""

from __future__ import annotations

from .budget import BudgetLimits, BudgetReport, BudgetTracker
from .cli import COMMANDS, CommandOutcome, main
from .experiments import ExperimentStore, StoreScan
from .state_machine import (
    EVENTS,
    INITIAL_STATE,
    STATES,
    TRANSITIONS,
    TRANSITION_EDGES,
    ExperimentStateMachine,
    Transition,
)
from .verifier import (
    CONDITIONS,
    VERIFIER_ID,
    VERIFIER_VERSION,
    Condition,
    GateOutcome,
    Verifier,
)

__all__ = [
    "COMMANDS",
    "CONDITIONS",
    "EVENTS",
    "INITIAL_STATE",
    "STATES",
    "TRANSITIONS",
    "TRANSITION_EDGES",
    "VERIFIER_ID",
    "VERIFIER_VERSION",
    "BudgetLimits",
    "BudgetReport",
    "BudgetTracker",
    "CommandOutcome",
    "Condition",
    "ExperimentStateMachine",
    "ExperimentStore",
    "GateOutcome",
    "StoreScan",
    "Transition",
    "Verifier",
    "main",
]
