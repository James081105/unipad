"""``build-os-root`` on the macOS runner: the chain, and what a second pass owes it.

Why this file exists
--------------------
``step_build_os_root`` was unreachable on the runner.  The plan puts it before
``build-kernel`` and ``build-payload``, but its own first sub-step boots the
kernel step's ``kernel.macho`` (``build-storage-abi.py:18,31``) -- so on a fresh
workspace it recorded ``not_run``, ``build-kernel`` built the board's
``qemu_defconfig``, and the runner never produced a kernel carrying
``CONFIG_XNU_NATIVE_VIRTIO_BLOCK``.  Measured on this host against the real
guest: such a kernel reaches ``mountroot``, matches the board's UUID and has APFS
loaded, and enumerates nothing.

The vendor chain documents the sequence (README:96-98): run the script once, let
``build-kernel`` produce the baseline kernel, then run it *again* -- the second
invocation finds the chain's outputs on disk and builds ``qemu_root_defconfig``.
The workflow made exactly one invocation, so the chain could never run.

What this file pins
-------------------
* ``test_the_chain_already_on_disk_is_recorded_skipped_not_ran`` -- the
  load-bearing one.  A second invocation must not claim work it did not do, and
  must not re-spend the two volumes' 7.88 GiB to arrive at the same place.  If
  this ever reports ``ran``, the ledger is lying about who ran the chain.
* ``test_no_chain_on_disk_is_still_not_run`` -- the guard is a short-circuit, not
  a green light.  Without the chain's three inputs the stage still records
  ``not_run`` with its reason, so a missing chain can never read as a pass.
* ``test_the_workflow_runs_the_chain_between_two_build_passes`` -- the defect
  itself, pinned as data.  The workflow has to invoke ``build-macos.sh`` twice
  and hand the chain the members it reads, or every kernel it produces is a
  ``qemu_defconfig`` kernel again.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "macos" / "build-macos.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "build-macos.yml"
REAL_LOCK = ROOT / "config" / "sources.lock.json"

pytestmark = pytest.mark.acceptance

UNIXNU_COMMIT = "b881ac7dc1695ce676c856873051a487c6a9f963"
DEVICE_COMMIT = "97b75b1f63c6b7e895cd7646029db741d5fe5488"

#: The three files ``root_chain_present()`` checks, spelled the way
#: ``build-macos.sh`` spells them: the Data seed (which carries the volume UUID
#: the kernel build exports) and the two archives ``qemu_root_defconfig`` links.
CHAIN_FILES = (
    ".cache/firmware/owner-data/seed.json",
    "tree/build/native-virtio-block-verified-bundle/libNativeVirtioBlock.a",
    "tree/build/verified-root-os-xnu-bundle/libVerifiedRootProbe.a",
)

VOLUME_UUID = "04769D26-2CA0-4825-B443-E2E5F88300F2"


def _bash() -> str:
    for candidate in ("bash", "bash.exe"):
        located = shutil.which(candidate)
        if located:
            return located
    pytest.skip("no bash on this host")


def _tool_shims(tmp_path: Path) -> dict[str, str]:
    """An environment whose ``xcodebuild``/``clang``/``python3`` all resolve.

    ``require_tools()`` refuses the whole run when ``xcodebuild`` or ``clang`` is
    absent, and on the Windows development host both are.  They are shimmed
    rather than skipped because the thing under test is *after* the preflight:
    the plan has to run for ``build-os-root`` to be reached at all.

    Extension-less executables, not ``.cmd`` and not a ``#!/bin/sh`` wrapper: this
    host's ``bash`` does not resolve a ``.cmd`` through ``command -v``, and a shell
    wrapper that ``exec``s the Windows interpreter deadlocks Cygwin's heap on the
    next fork ("child_copy: cygheap read copy failed").  A copy of the interpreter
    itself is the one spelling that survives both.

    Nothing here makes the host look like macOS -- ``apple_step_gate`` reads
    ``uname -s`` and still records ``not_run`` for every Apple stage -- so the
    shims buy the plan, not a green stage.
    """
    import shutil as _shutil
    import sys

    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    for tool in ("xcodebuild", "clang"):
        path = shim / tool
        path.write_text(
            f'#!/bin/sh\necho "synthetic {tool}: no macOS toolchain on this host"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
    interpreter = Path(sys.executable)
    if interpreter.suffix.lower() == ".exe":
        _shutil.copyfile(interpreter, shim / "python3.exe")
    else:  # pragma: no cover - a host whose interpreter is not a Windows binary
        (shim / "python3").write_text(
            f'#!/bin/sh\nexec "{interpreter}" "$@"\n', encoding="utf-8"
        )
        (shim / "python3").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _python_on_path(tmp_path: Path) -> dict[str, str]:
    return _tool_shims(tmp_path)


def _lock_payload() -> dict:
    """A lock shaped like the real one, with no submodules and no feeds.

    The empty lists are load-bearing: ``offline_missing()`` walks exactly those
    two lists, so an empty pair reads as "every pinned source is already on
    disk" and ``prepare-sources`` records ``ran`` instead of setting
    ``SOURCES_INCOMPLETE`` -- which would make every build stage record
    ``not_run`` before any of them was reached, and this test would prove
    nothing about ``build-os-root``.
    """
    payload = (
        json.loads(REAL_LOCK.read_text(encoding="utf-8"))
        if REAL_LOCK.is_file()
        else {"schema_version": 1}
    )
    payload["frozen"] = True
    payload.setdefault("unixnu", {})["commit"] = UNIXNU_COMMIT
    payload.setdefault("device_repo", {})["commit"] = DEVICE_COMMIT
    payload["submodules"] = []
    payload["feeds"] = []
    return payload


def _scratch(tmp_path: Path, *, chain: bool) -> tuple[Path, Path, Path]:
    """A workspace the build script can run its plan in, with or without the chain.

    ``chain=True`` places the three files ``root_chain_present()`` checks, which
    is the state a second invocation of the script is in after the workflow has
    run ``build-os-root.py`` between the two passes.
    """
    workspace = tmp_path / "ws"
    device = workspace / "device"
    # The pinned UniXNU checkout.  ``prepare-sources`` looks for ``.git`` and
    # nothing else, so an empty directory with one is enough: the lock declares no
    # submodules and no feeds, so nothing is ever read out of either checkout.
    for checkout in ("unixnu", "device"):
        (workspace / checkout / ".git").mkdir(parents=True, exist_ok=True)
    (device / "configs").mkdir(parents=True, exist_ok=True)
    (device / "board.toml").write_text(
        "[board]\n"
        'name = "qemu"\n'
        "\n"
        "[kernel]\n"
        'defconfig = "qemu_defconfig"\n'
        "\n"
        "[payload]\n"
        'volume = "UNIPAD"\n'
        'size_mib = 192\n'
        'loader = "firmware"\n',
        encoding="utf-8",
    )
    (device / "configs" / "qemu_defconfig").write_text(
        "# a board defconfig, for the stage's own existence check\n", encoding="utf-8"
    )
    # The defconfig the kernel stage switches to when the chain is on disk.  Its
    # existence is checked before anything else in that stage, so without it the
    # run would die on a missing file rather than reach the ledger.
    (device / "configs" / "qemu_root_defconfig").write_text(
        "# the root-chain defconfig, for the kernel stage's own check\n",
        encoding="utf-8",
    )
    config = workspace / "config"
    config.mkdir(parents=True, exist_ok=True)
    lock = config / "sources.lock.json"
    lock.write_text(json.dumps(_lock_payload(), indent=2), encoding="utf-8")

    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    if chain:
        seed = device / ".cache/firmware/owner-data/seed.json"
        seed.parent.mkdir(parents=True, exist_ok=True)
        seed.write_text(json.dumps({"volume_uuid": VOLUME_UUID}), encoding="utf-8")
        # Relative to tree_dir(), which is "$UNIXNU_OUT/$BOARD_NAME/tree" -- so
        # "tree/build/..." would put them one directory below where the stage
        # looks, and the short-circuit would silently never fire.
        tree = out / "unixnu-out" / "qemu" / "tree"
        for relative in CHAIN_FILES[1:]:
            target = tree / relative.removeprefix("tree/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic archive\n")
    return lock, device, out


def _run(tmp_path: Path, *, chain: bool) -> subprocess.CompletedProcess:
    lock, device, out = _scratch(tmp_path, chain=chain)
    return subprocess.run(
        [
            _bash(),
            SCRIPT.as_posix(),
            "--stage",
            "M2",
            "--experiment",
            "os-root-gate",
            "--lock",
            lock.as_posix(),
            "--out",
            out.as_posix(),
            "--unixnu",
            (tmp_path / "ws" / "unixnu").as_posix(),
            "--device",
            device.as_posix(),
        ],
        capture_output=True,
        text=True,
        env=_python_on_path(tmp_path),
        cwd=str(tmp_path),
        timeout=300,
    )


def _stages(result: subprocess.CompletedProcess, out: Path) -> dict[str, dict[str, str]]:
    report = json.loads((out / "build-report.json").read_text(encoding="utf-8"))
    return {
        entry["name"]: {"status": entry["status"], "reason": entry.get("reason", "")}
        for entry in report["steps"]
    }


def test_the_chain_already_on_disk_is_recorded_skipped_not_ran(tmp_path: Path) -> None:
    """A second invocation must not claim it ran the chain.

    The workflow runs ``build-os-root.py`` itself, between the two passes, and
    deletes the volumes afterwards -- 7.88 GiB of DMG that a second invocation
    would have to re-extract to reach the same place.  So the stage short-circuits
    on ``root_chain_present()``, and it says ``skipped`` rather than ``ran``:
    ``ran`` would be this script taking credit for work a different step did,
    which is precisely the kind of ledger entry nobody can audit later.
    """
    result = _run(tmp_path, chain=True)
    stages = _stages(result, tmp_path / "out")
    entry = stages.get("build-os-root", {})
    assert entry.get("status") == "skipped", (entry, result.stdout, result.stderr)
    # ...and it names what it found, so a reader can tell inherited work from
    # work this run did.
    assert "seed.json" in entry.get("reason", ""), entry
    assert "libNativeVirtioBlock.a" in entry.get("reason", ""), entry
    assert "libVerifiedRootProbe.a" in entry.get("reason", ""), entry
    assert "qemu_root_defconfig" in entry.get("reason", ""), entry
    # It must not stop the plan: the kernel stage is the consumer.
    assert "build-kernel" in stages, stages


def test_no_chain_on_disk_is_still_not_run(tmp_path: Path) -> None:
    """The short-circuit is not a green light.

    Without the chain's three inputs the stage still records ``not_run`` with a
    reason that names what is missing -- so a workspace where the chain never ran
    reads as unverified rather than as a pass.  This is the guard that makes the
    ``skipped`` above trustworthy.
    """
    result = _run(tmp_path, chain=False)
    stages = _stages(result, tmp_path / "out")
    entry = stages.get("build-os-root", {})
    assert entry.get("status") == "not_run", (entry, result.stdout, result.stderr)
    assert entry.get("reason"), entry
    assert "skipped" not in entry.get("reason", ""), entry


def test_the_workflow_runs_the_chain_between_two_build_passes() -> None:
    """The defect itself, pinned as data.

    One invocation means the chain can never run: the plan puts ``build-os-root``
    before the kernel and payload it consumes, so the first pass records it
    ``not_run`` and only a second pass can build ``qemu_root_defconfig``.  The
    workflow has to (a) invoke the script twice, (b) publish the chain's members
    out of the image it already downloads, (c) run ``build-os-root.py`` with an
    ``--ipsw``, and (d) hand the second pass the same inputs as the first.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    invocations = text.count("bash scripts/macos/build-macos.sh")
    assert invocations >= 2, (
        "the workflow invokes build-macos.sh once, so build-os-root can never "
        "run: its first sub-step needs the kernel and payload the plan builds "
        "after it, and only a second invocation sees the chain on disk. "
        f"found {invocations} invocation(s)"
    )
    # The chain's members, published from the image rather than the image kept.
    for flag in ("--os-volume", "--systemos-volume", "--static-trustcache", "--refer-ipsw"):
        assert flag in text, f"the workflow never asks for {flag}"
    # The chain itself, run the way README:96 runs it.
    assert "scripts/build-os-root.py" in text, (
        "the workflow never runs build-os-root.py, so the owner root chain is "
        "never built and every kernel is a qemu_defconfig kernel"
    )
    assert "--ipsw" in text, (
        "the workflow never passes --ipsw to anything, so the OS and SystemOS "
        "images and the System trust cache have no source"
    )
    # The volumes are Apple material and must not outlive the job (PRD 12).
    assert "target-volumes" in text, (
        "the workflow never removes the OS and SystemOS volumes it placed; "
        "Apple firmware material would survive the job"
    )
