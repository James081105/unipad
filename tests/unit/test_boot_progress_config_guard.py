"""A defconfig flag that is on requires the script its build branch runs.

Why this exists
---------------
``vendor/UniXNU-qemu/configs/*_defconfig`` looks like inert configuration.  It is
not: ``scripts/unixnu/kernel.py:10-19`` turns every ``CONFIG_XNU_*`` key into an
``XNU_*`` environment variable for the kernel build script, so a line in a
defconfig is a live switch on the runner.

That mapping was not obvious and it was not measured until 2026-10-30.  The
task-#26 investigation reported the wiring as "NOT MEASURED" and concluded that
enabling the console would be a one-line config change.  It is not, and the reason
is worth a test:

``build-xnu-4k-baseline.sh:59`` reads ``XNU_BOOT_PROGRESS``; ``:64-70`` runs
``python3 scripts/build-boot-progress.py`` when it is 1; ``:728-730`` force-loads
the ``libBootProgress.a`` that script is supposed to produce.  And
``scripts/build-boot-progress.py`` **does not exist in the pinned tree** --
measured: it is referenced exactly once in the whole repository, at that line 70,
by nothing else.

So the "one-line change" is a build that dies at line 70 with a missing file, and
line 70 is downstream of acquisition, the IPSW, the Mesa bootstrap, the GPU addon
chain and the first build pass.  It is the most expensive place in the pipeline to
discover that a file is absent, and the check costs nothing here.

This test states the contract for every ``CONFIG_XNU_*`` key that has a build-time
script dependency, and it is skipped for the keys that have none.  Adding the flag
without the script fails here instead of on the runner.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO / "vendor" / "UniXNU-qemu" / "configs"
BUILD_SCRIPT = REPO / "vendor" / "UniXNU" / "scripts" / "build-xnu-4k-baseline.sh"
XNU_SCRIPTS = REPO / "vendor" / "UniXNU" / "scripts"

#: CONFIG_XNU_ key -> the script its ``= 1`` branch runs, relative to the UniXNU
#: tree.  Only keys whose branch invokes a script are listed; a key that merely
#: sets a CFLAG or an archive path has no such dependency and needs no entry.
#:
#: The values are transcribed from ``build-xnu-4k-baseline.sh`` by hand, so the
#: test below reads every one of them back out of the script and fails if the
#: transcription drifted.  A table that no longer describes the build is worse
#: than no table: it would guard a dependency that does not exist.
BUILD_SCRIPT_DEPENDENCIES = {
    "CONFIG_XNU_BOOT_PROGRESS": "build-boot-progress.py",
}


def _defconfigs() -> list[Path]:
    if not CONFIG_DIR.is_dir():
        pytest.skip(f"{CONFIG_DIR} is not present; the device tree is not acquired")
    found = sorted(CONFIG_DIR.glob("*_defconfig"))
    assert found, f"no *_defconfig under {CONFIG_DIR}"
    return found


def _enabled_keys(path: Path) -> set[str]:
    """The ``CONFIG_XNU_*`` keys a defconfig turns on."""
    on = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("CONFIG_XNU_") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.strip() in ("y", "1", "true"):
            on.add(key.strip())
    return on


def test_the_dependency_table_matches_the_build_script():
    """Every key in the table is really gated by the script it is mapped to.

    A table that drifts from the build script is worse than no table: it would
    report a dependency that does not exist and miss the ones that do.  So each
    entry is read back out of ``build-xnu-4k-baseline.sh`` in two steps, because
    the script does not test the environment variable directly:

    1. it renames it -- ``BOOT_PROGRESS=${XNU_BOOT_PROGRESS:-0}`` (line 59);
    2. it tests the rename -- ``if [ "$BOOT_PROGRESS" = 1 ]`` (line 64), and the
       body of that branch runs the script (line 70).

    Asserting only the second step is what my first revision did, and it failed on
    its own transcription: the branch spells ``BOOT_PROGRESS``, not
    ``XNU_BOOT_PROGRESS``.  That is the failure mode this test exists for, caught
    by the test itself rather than by a runner cycle.
    """
    if not BUILD_SCRIPT.is_file():
        pytest.skip(f"{BUILD_SCRIPT} is not present; the UniXNU tree is not acquired")
    lines = BUILD_SCRIPT.read_text(encoding="utf-8", errors="replace").splitlines()

    stale = []
    for key, script in BUILD_SCRIPT_DEPENDENCIES.items():
        # The build script spells the variable XNU_*, not CONFIG_XNU_*.
        variable = key[len("CONFIG_"):]
        # Find `NAME=${XNU_<suffix>:-...}` -- the rename of the env variable into a
        # shorter shell variable the rest of the script tests.
        renamed = None
        for line in lines:
            head, sep, tail = line.partition("=")
            if not sep or "${" not in tail:
                continue
            if f"${{{variable}" not in tail:
                continue
            candidate = head.strip()
            if candidate and candidate.replace("_", "").isalnum():
                renamed = candidate
                break
        if renamed is None:
            stale.append(
                f"{key}: the build script never renames ${variable} into a shell "
                f"variable, so nothing tests it"
            )
            continue
        guard = f'if [ "${renamed}" = 1 ]'
        # The branch continues with `; then` on the same line, so match the prefix
        # rather than the whole line.
        start = next(
            (i for i, line in enumerate(lines) if line.strip().startswith(guard)),
            None,
        )
        if start is None:
            stale.append(f"{key}: no `{guard}` branch in the build script")
            continue
        branch = []
        for line in lines[start + 1:]:
            if line.startswith("fi"):
                break
            branch.append(line)
        if not any(script in line for line in branch):
            stale.append(
                f"{key}: `{guard}` exists but never runs {script}; the mapping "
                f"is stale"
            )
    assert not stale, (
        "the dependency table no longer describes the build script:\n  "
        + "\n  ".join(stale)
    )


def test_no_defconfig_enables_a_flag_whose_build_script_is_absent():
    """The guard itself: an enabled flag cannot require a missing file.

    This is the state the task-#26 investigation proposed to move INTO.  It is
    recorded here rather than acted on, because acting on it costs a runner cycle
    to discover that ``scripts/build-boot-progress.py`` is not in the pinned tree.
    """
    missing = []
    for path in _defconfigs():
        enabled = _enabled_keys(path)
        for key, script in BUILD_SCRIPT_DEPENDENCIES.items():
            if key not in enabled:
                continue
            if not (XNU_SCRIPTS / script).is_file():
                missing.append(
                    f"{path.name} sets {key}=y, but {BUILD_SCRIPT.relative_to(REPO)}"
                    f" runs scripts/{script} for that value and "
                    f"scripts/{script} is not in the pinned tree"
                )
    assert not missing, (
        f"{len(missing)} defconfig(s) enable a flag the build cannot satisfy:\n  "
        + "\n  ".join(missing)
        + "\n\nEither add the script to vendor/UniXNU/scripts/ or turn the flag "
        "off.  Enabling it as-is fails at build-xnu-4k-baseline.sh:70, after "
        "acquisition, the IPSW, Mesa, the GPU addon chain and the first build "
        "pass have all run."
    )
