"""The Metal device's userspace half has to be built by something.

Measured on 2026-10-31: the kernel half works.  ``native_qemu_metal_stage``
reaches 4, ``native_qemu_metal_grants`` reaches 1, and with ``backboardd``
running ``native_qemu_metal_refusals`` reaches 1 -- something really reached
``openClient()``.  But ``native_qemu_metal_opens`` stays 0, and the reason is
structural rather than timing: ``kOpenType`` (0x514d) is used nowhere in this
repository except ``QemuMetalAccelerator.cpp``'s own check and
``QemuMetalPlugin.mm``'s open, and the latter lives inside ``QemuMetal.bundle``
-- which **no build step produced**.  ``build-qemu-metal.py guest`` existed and
was referenced by exactly one docstring; the workflow and ``board.toml`` both
had zero matches for it.

That is the failure shape this file guards against, and it is worth naming
precisely: a step whose absence is indistinguishable from success.  The boot
succeeded, the accelerator was published, every counter looked plausible, and
the missing half was invisible in all of them.  So the guard does not check
"a step exists" -- it checks the four things that make the step real:

  1. it runs the vendored builder's ``guest`` action, spelled the way the
     builder spells it (read back out of the pinned source, not from memory);
  2. it is gated on the owner root chain, because ``trust()`` refuses to run
     against anything but the original trust bundle the chain produces;
  3. it verifies every output *by name*, because a script that exits 0 having
     written nothing is exactly the silent case;
  4. the second build prefers the Metal trust cache and the Metal Data seed,
     because shipping the bundle against the original trust cache ships
     something AMFI will refuse to load.

Every dependency below is hand-copied, so the last test reads each one back out
of the file it claims to describe -- the same discipline
``test_boot_progress_config_guard.py`` uses, for the same reason.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "build-macos.yml"
BUILDER = REPO / "vendor" / "UniXNU-qemu" / "scripts" / "build-qemu-metal.py"

#: The step that builds the userspace half, by its workflow id.
METAL_STEP_ID = "qemu_metal"

#: The step that rebuilds the payload around the root-capable kernel.  It is the
#: consumer: it chooses which trust cache and which Data seed the boot carries.
SECOND_BUILD_ID = "build_root"

#: The outputs ``build-qemu-metal.py guest`` writes, relative to its ``OUT``.
#: The bundle is a directory, so its two members are named rather than the
#: directory itself -- a directory in the workflow's copy tuple would be counted
#: as one entry instead of as its files.
BUILDER_OUTPUTS = (
    "QemuMetal.bundle/QemuMetal",
    "QemuMetal.bundle/Info.plist",
    "owner-trust.bundle",
    "owner-data-1g-metal.dbr",
    "trust.json",
)


def _text(path: Path) -> str:
    """A source file as text, with the checkout's line endings normalised.

    This repository is checked out CRLF on Windows, so a multi-line pattern
    written with ``\\n`` never matches.  Normalising here is cheaper than
    writing every pattern to tolerate an optional ``\\r``.
    """
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "build-macos.yml did not parse as a mapping"
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "build-macos.yml has no jobs mapping"
    assert len(jobs) == 1, f"expected one job, found {sorted(jobs)}"
    steps = next(iter(jobs.values())).get("steps")
    assert isinstance(steps, list), "the job has no steps list"
    return steps


def _step(step_id: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("id") == step_id:
            return step
    pytest.fail(f"no step with id '{step_id}' in build-macos.yml")


def _body(step_id: str) -> str:
    return str(_step(step_id).get("run") or "")


# --- 1. the step exists and runs the vendored builder's guest action ---------


def test_the_workflow_runs_the_vendored_builders_guest_action() -> None:
    """``build-qemu-metal.py guest`` is invoked, spelled as the builder spells it.

    The action name is read out of the builder's own dispatch table rather than
    written here from memory: a typo in either place produces a step that runs,
    exits non-zero, and is then blamed on the script.
    """
    table = re.search(
        r"\{'host':\s*host,\s*'instruments':\s*instruments,\s*'check':\s*check,"
        r"\s*'guest':\s*guest\}\[action\]\(\)",
        _text(BUILDER),
    )
    assert table, (
        "build-qemu-metal.py's dispatch table no longer maps 'guest' to guest(); "
        "the workflow's step name would be stale"
    )
    body = _body(METAL_STEP_ID)
    assert "scripts/build-qemu-metal.py guest" in body, (
        "no step runs the vendored builder's guest action; the Metal device's "
        "userspace half is not built by anything"
    )


def test_the_step_runs_from_the_assembled_tree_not_the_device_repository() -> None:
    """``ROOT`` is ``parents[1]`` of the script, so the cwd decides what it reads.

    Run from the device repository it would look for the trust bundle under
    ``vendor/UniXNU-qemu/build/`` and find nothing, because the owner root chain
    writes it into the assembled tree.
    """
    assert "ROOT = Path(__file__).resolve().parents[1]" in _text(BUILDER)
    body = _body(METAL_STEP_ID)
    assert re.search(r'cd\s+"\$tree"\s*&&\s*python3 scripts/build-qemu-metal\.py', body), (
        "the step does not run the builder from the assembled tree"
    )


# --- 2. it is gated on the owner root chain --------------------------------


def test_the_step_is_gated_on_the_owner_root_chain() -> None:
    """``trust()`` refuses anything but the original trust bundle's digest.

    That bundle is written by the root chain's last step, so a step that runs
    before the chain would fail on a hash check that says nothing about Metal.
    """
    guard = re.search(
        r"hashlib\.sha256\(original\)\.hexdigest\(\) != '([0-9a-f]{64})'",
        _text(BUILDER),
    )
    assert guard, "build-qemu-metal.py no longer verifies the original trust bundle's digest"
    condition = str(_step(METAL_STEP_ID).get("if") or "")
    assert "os_root" in condition and "pass" in condition, (
        f"the Metal step is not gated on the owner root chain (if: {condition!r}); "
        "trust() would refuse the trust bundle it reads"
    )


# --- 3. every output is verified by name -----------------------------------


@pytest.mark.parametrize("output", BUILDER_OUTPUTS)
def test_every_output_the_builder_writes_is_checked_by_name(output: str) -> None:
    """A script that exits 0 having written nothing is the silent case.

    The names are read back out of the builder: ``bundle()`` writes the bundle
    and ``trust.json``, ``trust()`` writes the owner trust cache, ``seed()``
    writes the Metal Data seed.  If the pinned builder renames one, this test
    says which -- rather than the workflow checking for a file nobody writes.
    """
    source = _text(BUILDER)
    stem = output.split("/")[-1]
    if output.startswith("QemuMetal.bundle"):
        assert "BUNDLE = OUT / 'QemuMetal.bundle'" in source
        assert f"BUNDLE / '{stem}'" in source, (
            f"build-qemu-metal.py no longer writes {output} into the bundle"
        )
    else:
        assert f"OUT / '{stem}'" in source, (
            f"build-qemu-metal.py no longer writes {output}"
        )
    assert f'"$metal/{output}"' in _body(METAL_STEP_ID), (
        f"the Metal step does not verify {output} by name; a builder that wrote "
        "nothing would still pass"
    )


def test_the_step_fails_the_job_rather_than_warning() -> None:
    """A warning is what let the gap survive in the first place.

    The GPU addon chain warns and continues, which is right for it.  This half
    is different: with only a warning the boot succeeds, the accelerator is
    published, and the missing plugin is invisible in every counter.
    """
    body = _body(METAL_STEP_ID)
    assert "exit 1" in body, "the Metal step does not fail the job on a missing output"
    assert "::warning::" not in body, (
        "the Metal step warns; a step whose absence is indistinguishable from "
        "success must not be allowed to be absent quietly"
    )


# --- 4. the second build prefers the Metal trust cache and seed -------------


def test_the_second_build_prefers_the_metal_trust_cache_and_seed() -> None:
    """Shipping the bundle against the original trust cache ships a refusal.

    ``owner-trust.bundle`` is the original two modules plus an owner module
    carrying the bundle's CDHash; without it AMFI refuses the bundle.  And the
    Metal seed *is* the seed -- same container with the bundle in the addon mount
    -- so passing both would stage two ramdisks and the last would win by
    accident.  Both are therefore preferences, not additions.
    """
    body = _body(SECOND_BUILD_ID)
    assert 'if [ -f "$metal/owner-trust.bundle" ]' in body, (
        "the second build does not prefer the Metal trust cache"
    )
    assert 'if [ -f "$metal/owner-data-1g-metal.dbr" ]' in body, (
        "the second build does not prefer the Metal Data seed"
    )
    assert 'trust_cache="$metal/owner-trust.bundle"' in body
    assert 'data_ramdisk="$metal/owner-data-1g-metal.dbr"' in body


def test_the_metal_artifacts_reach_the_boot_inputs_bundle() -> None:
    """Built on the runner is not the same as delivered to the host.

    The bundle itself travels inside the Metal Data seed; what the boot-inputs
    bundle carries is what a verifier needs to check the trust cache without
    unpacking a 1 GiB image.
    """
    body = _body("collect_inputs") if any(
        s.get("id") == "collect_inputs" for s in _steps()
    ) else next(
        str(s.get("run") or "") for s in _steps()
        if "Collect the boot inputs" in str(s.get("name") or "")
    )
    for fragment in (
        "unixnu-out/*/tree/build/qemu-metal/owner-trust.bundle",
        "unixnu-out/*/tree/build/qemu-metal/trust.json",
        "qemu-metal.log",
    ):
        assert fragment in body, f"the boot-inputs bundle does not carry {fragment}"


# --- 5. the self-check: the hand-copied table matches the files -------------


def test_the_metal_steps_log_is_in_the_build_logs_artifact() -> None:
    """The failure message points at the log, so the log has to be uploadable.

    Measured on run 41099656091: the step wrote ``build-output/qemu-metal.log``
    with ``tee`` and its failure message said "its log is in
    build-output/qemu-metal.log and in the build-logs artifact".  It was in the
    first place and **not** in the second -- the upload step names its paths
    explicitly, and this one was not among them.  So the one message that could
    have explained the failure pointed at a file a reader could not open.

    Named explicitly rather than matched by a glob, because a glob wide enough
    to catch it would also change what every other run publishes.
    """
    upload = next(
        str(s.get("with") or "")
        for s in _steps()
        if "Upload the build logs" in str(s.get("name") or "")
    )
    assert "build-output/qemu-metal.log" in upload, (
        "the QemuMetal step's log is not in the build-logs artifact, so its own "
        "failure message points at a file nobody can read"
    )


def test_the_hand_copied_dependencies_are_the_ones_the_files_describe() -> None:
    """Every id and path this module names is read back out of the workflow.

    The table above is hand-copied, so a rename in the workflow would leave
    these tests asserting against a step that no longer exists -- which is the
    same class of blind spot as a dependency table nobody re-reads.
    """
    ids = {str(s.get("id")) for s in _steps() if s.get("id")}
    for expected in (METAL_STEP_ID, SECOND_BUILD_ID):
        assert expected in ids, f"no step with id '{expected}'"
    names = [str(s.get("name") or "") for s in _steps()]
    assert any("Collect the boot inputs" in name for name in names), (
        "the boot-inputs collection step was renamed"
    )
    assert any("QemuMetal userspace plugin" in name for name in names), (
        "the Metal build step was renamed; its id is load-bearing for the gate above"
    )
    assert BUILDER.is_file(), f"{BUILDER} is not in the pinned device repository"
    assert "def guest():" in _text(BUILDER), (
        "build-qemu-metal.py no longer defines guest(); nothing produces the bundle"
    )
