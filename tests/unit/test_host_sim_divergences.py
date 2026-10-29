"""The host-simulation divergences: what each changes, and what it must not.

Why these tests exist
---------------------
Ring 2 of the addon chain compiles the real driver sources against a stub IOKit
and links them into a dylib.  In the pinned tree that no longer works, and every
gap is in the SIMULATION, not the driver -- measured across two runs:

  * run 40932083150: ``NativeAdrenoMemory.cpp:167: error: unknown type name
    'IODirection'`` and ``:170: error: use of undeclared identifier
    'kIOMemoryPersistent'``
  * run 40939171888: ``Undefined symbols for architecture arm64:
    "_proc_selfpid"``

All of it lives in files the tablet never sees.  But they are still changes to a
PINNED tree, so the properties that make them safe have to be pinned too: the
values added must be inert in the simulation, and the tool must refuse rather
than guess when the upstream file changes shape.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "scripts" / "macos" / "apply-host-sim-divergences.py"
DRIVER = REPO / "vendor" / "UniXNU"
FIXTURE = DRIVER / "drivers" / "NativeAdreno" / "tests" / "XnuMemoryFixture.h"
SHIM = DRIVER / "drivers" / "NativeAdreno" / "tests" / "hostsim" / "host_iokit_shim.cpp"


def _tool() -> Path:
    if not TOOL.is_file():
        pytest.skip(f"{TOOL} is not present")
    return TOOL


def _driver_root(tmp_path: Path) -> Path:
    """A copy of the driver's two divergent files, in a tree the tool accepts.

    Only the files the divergences touch are copied: the tool checks for
    ``scripts/check-turnip-darwin-host.py`` to recognise a driver root, and
    copying 500 MB of Mesa to test a fifteen-line edit would be absurd.
    """
    if not FIXTURE.is_file() or not SHIM.is_file():
        pytest.skip("the pinned vendor checkout is absent")
    root = tmp_path / "driver"
    for relative in (
        "scripts/check-turnip-darwin-host.py",
        "drivers/NativeAdreno/tests/XnuMemoryFixture.h",
        "drivers/NativeAdreno/tests/hostsim/host_iokit_shim.cpp",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DRIVER / relative, target)
    return root


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_tool()), str(root), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def test_the_tool_applies_both_divergences_to_a_pristine_tree(tmp_path):
    """The two measured gaps, closed, on a copy."""
    root = _driver_root(tmp_path)
    result = _run(root)
    assert result.returncode == 0, (result.stdout + result.stderr)[-800:]
    fixture = (root / "drivers/NativeAdreno/tests/XnuMemoryFixture.h").read_text(encoding="utf-8")
    shim = (root / "drivers/NativeAdreno/tests/hostsim/host_iokit_shim.cpp").read_text(encoding="utf-8")

    assert "using IODirection=unsigned;" in fixture
    assert "kIOMemoryPersistent=" in fixture
    assert "int proc_selfpid(void);" in shim
    assert "int proc_selfpid(void) { return getpid(); }" in shim


def test_the_added_constant_is_inert_in_the_simulation(tmp_path):
    """Teeth: the value must not collide with a bit the fixture branches on.

    ``withAddressRange`` branches only on ``kIOMemoryMapperNone`` (0x800) and
    ``kIOMemoryPhysicallyContiguous`` (0x10).  A ``kIOMemoryPersistent`` sharing a
    bit with either would change the simulation's behaviour -- silently, which is
    the one outcome worse than the compile error it fixes.
    """
    root = _driver_root(tmp_path)
    assert _run(root).returncode == 0
    fixture = (root / "drivers/NativeAdreno/tests/XnuMemoryFixture.h").read_text(encoding="utf-8")

    def bit_of(name: str) -> int:
        for line in fixture.splitlines():
            if name + "=" in line:
                tail = line.split(name + "=", 1)[1]
                digits = ""
                for ch in tail:
                    if ch.isdigit() or ch in "xXabcdefABCDEF":
                        digits += ch
                    else:
                        break
                return int(digits, 0)
        raise AssertionError(f"{name} is not declared")

    persistent = bit_of("kIOMemoryPersistent")
    for tested in ("kIOMemoryMapperNone", "kIOMemoryPhysicallyContiguous"):
        assert persistent & bit_of(tested) == 0, (
            f"kIOMemoryPersistent={persistent:#x} shares a bit with "
            f"{tested}={bit_of(tested):#x}; the simulation's branches would change"
        )


def test_the_tool_refuses_when_an_anchor_has_changed_shape(tmp_path):
    """Fail closed: an anchor that is not present exactly once is a refusal.

    The alternative is a fix applied to a file it has never seen.
    """
    root = _driver_root(tmp_path)
    fixture = root / "drivers/NativeAdreno/tests/XnuMemoryFixture.h"
    fixture.write_text(
        fixture.read_text(encoding="utf-8").replace(
            "kIOMemoryPhysicallyContiguous=0x10;", "kIOMemoryPhysicallyContiguous=0x20;", 1),
        encoding="utf-8",
    )
    before = fixture.read_text(encoding="utf-8")

    result = _run(root)
    assert result.returncode != 0, (
        f"the tool applied to a fixture whose anchor had changed: "
        f"{(result.stdout + result.stderr)[-400:]!r}"
    )
    assert "REFUSED" in result.stdout + result.stderr
    assert "re-derived" in result.stdout + result.stderr
    assert fixture.read_text(encoding="utf-8") == before, "a refused run still wrote"
    # And nothing else was written either.
    shim = root / "drivers/NativeAdreno/tests/hostsim/host_iokit_shim.cpp"
    assert "proc_selfpid" not in shim.read_text(encoding="utf-8"), (
        "the shim was patched even though the fixture divergence refused"
    )


def test_a_second_application_is_a_no_op(tmp_path):
    """Idempotent in effect: the result is not stacked."""
    root = _driver_root(tmp_path)
    assert _run(root).returncode == 0
    fixture = root / "drivers/NativeAdreno/tests/XnuMemoryFixture.h"
    shim = root / "drivers/NativeAdreno/tests/hostsim/host_iokit_shim.cpp"
    once_fixture, once_shim = fixture.read_text(encoding="utf-8"), shim.read_text(encoding="utf-8")

    second = _run(root)
    assert second.returncode == 0, (second.stdout + second.stderr)[-400:]
    assert fixture.read_text(encoding="utf-8") == once_fixture
    assert shim.read_text(encoding="utf-8") == once_shim
    assert once_fixture.count("using IODirection=unsigned;") == 1
    assert once_shim.count("int proc_selfpid(void) { return getpid(); }") == 1


def test_the_dry_run_changes_nothing(tmp_path):
    """``--dry-run`` is how a reader learns what would happen without a runner."""
    root = _driver_root(tmp_path)
    fixture = root / "drivers/NativeAdreno/tests/XnuMemoryFixture.h"
    shim = root / "drivers/NativeAdreno/tests/hostsim/host_iokit_shim.cpp"
    before = (fixture.read_text(encoding="utf-8"), shim.read_text(encoding="utf-8"))

    result = _run(root, "--dry-run")
    assert result.returncode == 0, (result.stdout + result.stderr)[-400:]
    assert "would-apply" in result.stdout
    assert "fixes:" in result.stdout and "safe :" in result.stdout, (
        "the dry run does not say what each divergence fixes or why it is safe"
    )
    assert (fixture.read_text(encoding="utf-8"), shim.read_text(encoding="utf-8")) == before


def test_the_chain_calls_the_tool_rather_than_repeating_it():
    """The chain must delegate, so a third divergence is one entry and not a copy."""
    if not TOOL.is_file():
        pytest.skip(f"{TOOL} is not present")
    chain = (REPO / "scripts" / "macos" / "build-gpu-addon.sh").read_text(encoding="utf-8")
    assert "apply-host-sim-divergences.py" in chain, (
        "build-gpu-addon.sh no longer calls the divergence tool; the divergences "
        "have been inlined again, and a third one will be a third copy"
    )
    assert "DIVERGENCE" in chain or "divergence" in chain, (
        "the chain no longer records that it applies divergences"
    )
