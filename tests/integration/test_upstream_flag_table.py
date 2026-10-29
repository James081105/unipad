"""Checks that need the real pinned upstream tool, and nothing else.

Why this is a separate stage
----------------------------
``tests/unit`` runs in a clean checkout: ``vendor/`` is excluded from git by
``.git/info/exclude`` and holds nothing on a fresh clone, so a unit test that
opens a file there dies with ``FileNotFoundError`` before any of its own
assertions run.  That is what happened on 2026-10-24: ``preflight`` run
40107080802 went red with two failures from ``tests/unit/test_host_adt.py``,
both at ``source = (REAL_DEVICE / ADT_TOOL).read_text(...)``.

The assertions themselves are correct and worth keeping -- they compare
:data:`~unipad.host.adt.ADT_FLAGS` against the tool's *own* parser, which is
the only authority on what each key becomes.  What was wrong was where they
ran.  So they live here, in a stage that runs where the pinned vendor checkout
has actually been prepared.

Two things keep this stage honest:

* the existence check comes *before* the read (the original guard had it the
  other way round, which made it dead code -- see :func:`_adt_tool_source`);
* the stage refuses to read as green when everything in it skips, which is
  enforced by the ``integration (pinned upstream)`` job in
  ``.github/workflows/preflight.yml``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from unipad.host import adt

ROOT = Path(__file__).resolve().parents[2]
REAL_DEVICE = ROOT / "vendor" / "UniXNU-qemu"

#: The device repository's ADT generator, relative to its root.
ADT_TOOL = "scripts/make-qemu-adt.py"


def _adt_tool_source() -> str:
    """The tool's source, or a skip naming the exact path that is missing.

    The existence check runs *before* the read.  The version of this guard that
    ran the read first is why ``tests/unit`` was red in CI (module docstring):
    ``Path.read_text`` raised ``FileNotFoundError`` and ``if not source`` was
    never reached, so the skip could only ever fire for a file that exists and
    is empty.
    """
    tool = REAL_DEVICE / ADT_TOOL
    if not tool.is_file():
        pytest.skip(
            f"{tool} is not present: this stage needs the pinned vendor "
            "checkout from config/sources.lock.json, prepared by the "
            "'integration (pinned upstream)' job in "
            ".github/workflows/preflight.yml"
        )
    return tool.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tool_source() -> str:
    return _adt_tool_source()


def test_the_flag_table_matches_the_tools_own_argument_names(tool_source: str) -> None:
    """A flag the tool does not spell is a flag it will refuse.

    The table is the one place the mapping lives, and the tool's parser is the
    authority on what each key becomes -- so the two are compared here rather
    than trusted.
    """
    for key, flag in adt.ADT_FLAGS:
        assert flag in tool_source, (key, flag)
    for switch in adt.ADT_SWITCHES:
        assert f"--{switch.replace('_', '-')}" in tool_source, switch
    # ``--dram-size`` and ``--output`` are emitted unconditionally and are not in
    # the table, so they are checked here rather than by the loop above.
    for unconditional in ("--dram-size", "--output", "--unique-chip-id", "--chip-id"):
        assert unconditional in tool_source, unconditional


def test_the_ramdisk_and_trust_cache_flags_are_the_tools_own(tool_source: str) -> None:
    """``--packed-data-ramdisk`` and ``--trust-cache`` exist in the tool's parser."""
    for flag in ("--packed-data-ramdisk", "--data-ramdisk", "--trust-cache", "--ipad-identity"):
        assert flag in tool_source, flag


def test_the_gpu_addon_flag_is_the_tools_own(tool_source: str) -> None:
    """``--gpu-addon`` exists in the tool's parser, and it publishes the node.

    The driver already sends this flag: unixnu/scripts/unixnu/payload.py:161
    appends it to the make-qemu-adt.py command line whenever the payload was
    given a blob.  The device repository did not implement it, so a build
    carrying one would have died on argparse's "unrecognized arguments" rather
    than on anything meaningful -- which is the failure mode where the message
    names the wrong cause.

    The node it must publish is asserted too, because a flag that parses and
    then puts nothing would be worse than the missing flag: the loader
    (DarwinBoot.c:235-237) looks for GpuAddon by name and demands exactly 16
    zero bytes, and NativeRamBlock.cpp:434 reads back the range the loader
    fills.  Without the entry the kernel creates no /private/var/ngpu and the
    addon mount fails its first vnode_lookup with ENOENT -- measured as
    native_gpu_addon_stage 1 / status 2.
    """
    assert "--gpu-addon" in tool_source, "the driver sends --gpu-addon; the tool must parse it"
    assert "GpuAddon" in tool_source, (
        "the tool must publish /chosen/memory-map/GpuAddon, or the loader has "
        "nowhere to put the blob"
    )
    # The placeholder contract, not just the name: 16 zero bytes, exactly as
    # PackedRAMDisk two lines above it.
    assert "bytes(16)" in tool_source, tool_source


def test_the_pinned_device_repository_is_the_one_the_lock_names() -> None:
    """The checkout on disk is the pin, not a later upstream revision.

    A stage that silently tested ``main`` would pass today and stop meaning
    anything after an upstream rename, so the commit on disk is compared with
    ``config/sources.lock.json``.  When git cannot answer, the check is skipped
    rather than guessed at.
    """
    lock_path = ROOT / "config" / "sources.lock.json"
    if not lock_path.is_file():
        pytest.skip("config/sources.lock.json is not present")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    pinned = str((lock.get("device_repo") or {}).get("commit") or "")
    if not pinned:
        pytest.skip("config/sources.lock.json has no device_repo.commit to compare against")
    if not REAL_DEVICE.is_dir():
        pytest.skip(f"{REAL_DEVICE} is not present, so there is no HEAD to compare")

    try:
        head = subprocess.run(
            ["git", "-C", str(REAL_DEVICE), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - no git
        pytest.skip(f"cannot read the checkout's HEAD: {exc.__class__.__name__}")
    if head.returncode != 0:
        pytest.skip(f"the device checkout is not a git repository: {head.stderr.strip()[:120]}")
    assert head.stdout.strip() == pinned, (
        f"vendor/UniXNU-qemu is at {head.stdout.strip()} but config/sources.lock.json "
        f"pins {pinned}: the integration stage would be testing a different tool"
    )
