"""The workflows' own structural contract.

A job that calls a reusable workflow (``uses: ./.github/workflows/x.yml``) may
only carry a fixed set of keys.  ``timeout-minutes`` is not one of them, and
the rejection happens while GitHub builds the job graph, which means the
failure is *invisible in the usual places*: the run completes in zero seconds,
``jobs`` is empty, and there is no log to read.  Measured on 2026-10-24 --
``publish-artifacts`` failed that way on every run since the reusable call was
introduced (40107079873 and its predecessors), and each time the only symptom
was a red run with nothing to click into.

So the contract is checked here, where a failure names the offending key
instead of producing an empty run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# GitHub's documented allowlist for a job whose ``uses:`` names a reusable
# workflow.  Anything else -- notably ``timeout-minutes``, ``runs-on`` and
# ``steps`` -- is a validation error, not a runtime one.
CALLER_JOB_ALLOWED_KEYS = frozenset(
    {
        "name",
        "uses",
        "with",
        "secrets",
        "needs",
        "if",
        "permissions",
        "strategy",
        "concurrency",
    }
)


def _workflows() -> list[Path]:
    return sorted(p for p in WORKFLOW_DIR.glob("*.yml") if p.is_file())


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} did not parse as a mapping"
    return data


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "workflow has no jobs mapping"
    return jobs


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """The workflow's ``on:`` block, whichever key YAML 1.1 turned it into.

    ``on`` is a YAML 1.1 boolean, so ``yaml.safe_load`` hands back the mapping
    under the key ``True`` and not under ``"on"``.  Reading ``workflow["on"]``
    therefore silently returns ``None`` for every workflow, which is exactly the
    kind of thing that makes a contract test pass for the wrong reason.
    """
    for key in ("on", True):  # noqa: E501 - the literal True is the YAML 1.1 quirk
        if key in workflow:
            value = workflow[key]
            return value if isinstance(value, dict) else {}
    return {}


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_the_workflow_parses_and_declares_jobs(path: Path) -> None:
    """A workflow that does not parse produces an empty run, not an error."""
    workflow = _load(path)
    assert _jobs(workflow), f"{path.name} declares no jobs"


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_a_reusable_workflow_caller_only_uses_allowed_keys(path: Path) -> None:
    """The caller's key set must stay inside GitHub's allowlist."""
    offending: list[str] = []
    for job_id, job in _jobs(_load(path)).items():
        if not isinstance(job, dict) or "uses" not in job:
            continue
        extra = sorted(set(job) - CALLER_JOB_ALLOWED_KEYS)
        if extra:
            offending.append(f"{path.name}: job '{job_id}' has {extra}")
    assert not offending, (
        "a job calling a reusable workflow uses keys GitHub rejects, so the "
        "whole workflow fails validation and creates no jobs at all: "
        + "; ".join(offending)
    )


@pytest.mark.parametrize("path", _workflows(), ids=lambda p: p.name)
def test_every_input_a_caller_passes_is_declared_by_the_callee(path: Path) -> None:
    """``with:`` against an input the callee never declared is the same trap."""
    by_name = {p.name: _load(p) for p in _workflows()}
    problems: list[str] = []
    for job_id, job in _jobs(_load(path)).items():
        if not isinstance(job, dict) or "uses" not in job:
            continue
        reference = str(job["uses"])
        if not reference.startswith("./"):
            continue  # an external reusable workflow is not ours to check
        callee = by_name.get(Path(reference[2:]).name)
        if callee is None:
            problems.append(f"{path.name}: '{reference}' does not resolve")
            continue
        call = _triggers(callee).get("workflow_call") or {}
        declared = set((call.get("inputs") or {}))
        passed = set((job.get("with") or {}))
        missing = sorted(passed - declared)
        if missing:
            problems.append(f"{path.name}: job '{job_id}' passes undeclared input(s) {missing}")
    assert not problems, "; ".join(problems)


def test_a_callee_that_is_called_is_actually_reusable() -> None:
    """``uses:`` against a workflow without ``workflow_call`` never runs."""
    by_name = {p.name: _load(p) for p in _workflows()}
    problems: list[str] = []
    for path in _workflows():
        for job_id, job in _jobs(_load(path)).items():
            if not isinstance(job, dict) or "uses" not in job:
                continue
            reference = str(job["uses"])
            if not reference.startswith("./"):
                continue
            callee = by_name.get(Path(reference[2:]).name)
            if callee is None:
                continue  # reported by the test above
            if "workflow_call" not in _triggers(callee):
                problems.append(f"{path.name}: job '{job_id}' calls {reference}, which has no workflow_call trigger")
    assert not problems, "; ".join(problems)
