"""The GPU addon chain's position in the macOS build workflow.

Why this contract exists
------------------------
The blob the addon chain produces is consumed by ``build-macos.sh`` through
``--gpu-addon``, which means it has to exist before the build step runs.  That
ordering is not something a reader can take on trust: an earlier revision of
this step sat *after* the build and its own comment claimed it ran before it,
which was false on the very run that comment described.  A step in the wrong
place produces no error -- the build simply runs without the addon and the
payload reports the missing blob, which reads exactly like "the chain is still
blocked upstream".

So the ordering is asserted structurally: producer before consumer, in the same
job, with the consumer naming the producer's step output.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "build-macos.yml"

PRODUCER_ID = "addon"
CONSUMER_ENV = "UNIXNU_GPU_ADDON"


def _build_job() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert "build" in jobs, "the macOS build job is gone"
    return jobs["build"]


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps") or []
    assert steps, "the build job has no steps"
    return steps


def _index_of(steps: list[dict[str, Any]], step_id: str) -> int:
    for index, step in enumerate(steps):
        if step.get("id") == step_id:
            return index
    raise AssertionError(f"no step with id {step_id!r} in the build job")


def _consumers(steps: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Every step whose environment carries the addon, as (index, step name)."""
    found = []
    for index, step in enumerate(steps):
        env = step.get("env") or {}
        if CONSUMER_ENV in env:
            found.append((index, step.get("name", f"step {index}")))
    return found


def test_the_addon_step_exists_in_the_build_job():
    job = _build_job()
    steps = _steps(job)
    assert any(step.get("id") == PRODUCER_ID for step in steps), (
        "no step with id 'addon'; the chain's blob is never built and the "
        "payload step's 'no GPU addon' line fires on every run"
    )


def test_the_addon_step_precedes_every_step_that_consumes_the_blob():
    """Producer before consumer -- the whole point, and the one bug that is silent."""
    job = _build_job()
    steps = _steps(job)
    producer = _index_of(steps, PRODUCER_ID)
    consumers = _consumers(steps)
    assert consumers, f"no step carries {CONSUMER_ENV}; the blob is built and then dropped"
    for index, name in consumers:
        assert index > producer, (
            f"{name!r} consumes {CONSUMER_ENV} at position {index} but the "
            f"producer step 'addon' is at {producer}; the build would run "
            f"without the blob and report it as missing"
        )


def test_every_consumer_reads_the_addon_steps_output():
    """A consumer that only reads the bootstrap's leftover can miss the fresh blob."""
    job = _build_job()
    steps = _steps(job)
    producer = _index_of(steps, PRODUCER_ID)
    consumers = [s for s in steps if CONSUMER_ENV in (s.get("env") or {})]
    for step in consumers:
        value = str(step["env"][CONSUMER_ENV])
        assert f"steps.{PRODUCER_ID}.outputs.addon" in value, (
            f"{step.get('name')!r} reads {CONSUMER_ENV}={value!r}, which never "
            f"consults steps.{PRODUCER_ID}.outputs.addon; a blob this run built "
            f"would be ignored"
        )


def test_the_addon_step_never_fails_the_build():
    """A runner that cannot cross-compile the plugin still owes a kernel and a Data volume.

    This is the same trade the bootstrap step makes, and it is deliberate: the
    addon is one input to the payload, not a precondition for it.  What must
    never happen is the opposite -- a chain that fails silently, or a step whose
    failure is swallowed without a record.
    """
    job = _build_job()
    steps = _steps(job)
    step = steps[_index_of(steps, PRODUCER_ID)]
    run = str(step.get("run", ""))
    assert "exit 0" in run, (
        "the addon step does not end in `exit 0`; its failure would fail the "
        "whole build and trade a known limitation (no addon) for an unknown one "
        "(no kernel, no device tree, no Data volume)"
    )
    assert "GITHUB_OUTPUT" in run, (
        "the addon step writes no step output; the build could not receive the "
        "blob even when the chain succeeds"
    )
    assert "::warning::" in run, (
        "the addon step reports nothing on failure; a failed chain would leave "
        "no trace outside its own log file"
    )


def test_the_addon_step_runs_the_script_that_builds_the_chain():
    job = _build_job()
    steps = _steps(job)
    run = str(steps[_index_of(steps, PRODUCER_ID)].get("run", ""))
    assert "scripts/macos/build-gpu-addon.sh" in run, (
        "the addon step does not invoke scripts/macos/build-gpu-addon.sh; "
        "rings 1..7 of the chain are unbuilt"
    )


def test_the_addon_logs_are_uploaded():
    """A chain whose logs are not in the artifact cannot be diagnosed after the runner dies."""
    job = _build_job()
    uploads = [
        step for step in _steps(job)
        if "upload-artifact" in str(step.get("uses", ""))
    ]
    assert uploads, "the build job uploads no artifact at all"
    paths = "\n".join(str(step.get("with", {}).get("path", "")) for step in uploads)
    assert "build-gpu-addon.log" in paths, (
        "the addon chain's own log is not in any upload path; its failure "
        "reason would be unrecoverable"
    )
    assert "addon-*.log" in paths, (
        "the per-ring logs are not in any upload path; which ring failed "
        "would be unrecoverable"
    )
