"""The Data-mount verification rule, exercised in both directions.

Why this test exists
--------------------
``local-private/firmware/verify-data-mount.py`` answers "did the Data volume
mount and is the original launchd PID 1" for one boot.  It lives in
``local-private/`` because the UUID it compares against and the globals it reads
are Apple-derived material that does not belong in the repository (PRD 12), so
the *rule* is unprovable there and untested here unless it is loaded across the
boundary.

That boundary is the reason this test is written the way it is.  A harness that
can only ever print "not verified" is indistinguishable from a working one on
the boot that is actually blocked -- which is the only boot anyone has.  So the
fixtures below are synthetic *log text* and *global readings*, not an APFS
image: no container is created, nothing is booted, and the only thing measured
is whether the rule flips when the numbers do.

The teeth check is the important half.  ``test_every_criterion_is_load_bearing``
mutates one thing at a time and requires that the *named* criterion be the one
that fails.  A rule where a criterion could be dropped without anything noticing
is a rule that will be dropped, and the boot that exposes it will be the real
one.

Lives in ``tests/unit`` rather than ``tests/acceptance`` because it needs no
host binary and no boot: everything it exercises is in-memory parsing and
comparison.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "local-private" / "firmware" / "verify-data-mount.py"

#: The globals a boot that mounted the Data volume reports.  Every value except
#: ``_native_owner_data_device`` is one the measured baseline or the patch's own
#: semantics fixes; the dev_t is illustrative, because the mapping from a dev_t
#: to a ``diskNsM`` name is not something this harness asserts on -- the console
#: carries the identity, the global only has to be a real device.
PASSING_GLOBALS: dict[str, Any] = {
    "_native_owner_data_stage": 4,
    "_native_owner_data_status": 0,
    "_native_owner_data_device": 16777226,
    "_native_owner_data_readonly": 0,
    "_native_owner_data_seed_base": 0,
    "_native_owner_data_seed_size": 1073741824,
    "_native_owner_cache_stage": 6,
    "_native_owner_cache_status": 0,
    "_native_ram_block_ready": 1,
    "_native_ram_block_reads": 41,
    "_native_ram_block_bytes": 1073741824,
    "_native_ram_block_resident_bytes": 73728,
    "_native_ram_block_sparse": 1,
    "_native_ram_block_failures": 0,
    "_native_virtio_read_requests": 306,
    "_native_virtio_verified_blocks": 482,
    "_native_virtio_read_failures": 0,
    "_native_virtio_integrity_failures": 0,
}

#: The console lines a boot that mounted the Data volume prints.  The shapes are
#: the ones the measured baseline prints for its *real* containers
#: (``local-private/boot25/run-real/kernel.log`` lines 260-263 for disk3s1 and
#: 280-283 for disk1s1), with the Data volume's device and UUID substituted:
#: the whole point of the harness is that the Data volume's lines look like
#: these and are absent today.
PASSING_CONSOLE = """\
[   71.240328]: apfs_log_op_with_proc:3091: disk3s1 mounting volume DawnUpdate21H461.J120J121J207J208OS, requested by: kernel_task (pid 0); parent: kernel_task (pid 0)
[   71.247614]: handle_mount:817: disk3s1 vol-uuid: 04769D26-2CA0-4825-B443-E2E5F88300F2 block size: 4096 block count: 1483776 (unencrypted; flags: 0x1; features: 8.0.12)
[   71.517960]: apfs_log_op_with_proc:3091: disk3s1 mount-complete volume DawnUpdate21H461.J120J121J207J208OS, requested by: kernel_task (pid 0); parent: kernel_task (pid 0)
[   98.297331]: apfs_log_op_with_proc:3091: disk1s1 mounting volume DawnUpdate21H461.J120J121J207J208SystemCryptex, requested by: init (pid 1); parent: kernel_task (pid 0)
[   98.300354]: handle_mount:817: disk1s1 vol-uuid: 8846FE78-2910-4297-AF97-D59BB6F8FC16 block size: 4096 block count: 806400 (unencrypted; flags: 0x1; features: 28.0.2)
[  100.170360]: apfs_log_op_with_proc:3091: disk1s1 mount-complete volume DawnUpdate21H461.J120J121J207J208SystemCryptex, requested by: init (pid 1); parent: kernel_task (pid 0)
[  152.759056]: dev_init:303: disk4 device accelerated crypto: 0 (compiled @ Jul 11 2025 17:34:16)
[  152.760960]: dev_init:306: disk4 device_handle block size 4096 block count 262144 features 0 internal
[  153.010000]: apfs_log_op_with_proc:3091: disk4s1 mounting volume Data, requested by: init (pid 1); parent: kernel_task (pid 0)
[  153.020000]: handle_mount:817: disk4s1 vol-uuid: 18AB5557-DC2D-469B-8758-855E60F5B0DF block size: 4096 block count: 262144 (unencrypted; flags: 0x1; features: 28.0.2)
[  153.200000]: apfs_log_op_with_proc:3091: disk4s1 mount-complete volume Data, requested by: init (pid 1); parent: kernel_task (pid 0)
load_init_program: attempting to load /sbin/launchd
"""

#: The process list of that boot.  ``p_comm`` of PID 1 is the discriminator PRD
#: M8 asks for; ``init`` is what the same slot holds before the exec, which is
#: what the measured baseline's PID 1 reads as.
PASSING_PROCESSES = [
    {"pid": 0, "state": "idle", "name": "kernel_task"},
    {"pid": 1, "state": "run", "name": "launchd"},
]


@pytest.fixture(scope="module")
def harness():
    """The local-private harness as a module, or a skip.

    Skipped rather than failed when ``local-private/`` is absent, because that
    directory is gitignored (PRD 12) and a clean checkout has nothing to load.
    Every other acceptance test that reaches into it does the same.
    """
    if not HARNESS.is_file():
        pytest.skip("local-private/firmware/verify-data-mount.py is absent "
                    "(gitignored, PRD 12)")
    spec = importlib.util.spec_from_file_location("verify_data_mount", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_rule(harness, globals_map=None, console_text=None, processes=None):
    """The rule's verdict over one set of readings, as ``{criterion: status}``."""
    readings = harness.evaluate(
        globals_map,
        harness.Console(console_text, "test") if console_text is not None else None,
        processes,
    )
    return {item.name: item.status for item in readings}


# ---------------------------------------------------------------------------
# the pass direction
# ---------------------------------------------------------------------------
def test_a_boot_that_mounted_the_data_volume_verifies(harness):
    """The whole rule passes on a boot that did everything it asks for.

    Without this, the harness could only ever say "not verified" and would be
    indistinguishable from a correct one on the only boot anyone has.
    """
    statuses = run_rule(harness, PASSING_GLOBALS, PASSING_CONSOLE,
                        PASSING_PROCESSES)
    failed = {name: status for name, status in statuses.items()
              if status != harness.PASS}
    assert not failed, f"a boot that mounted the Data volume did not verify: {failed}"


def test_the_recorded_baseline_is_not_verified(harness):
    """The measured blocker, replayed, is not verified -- and says why.

    The readings are the ones ``local-private/boot25/run-real/summary.json`` and
    ``kernel.log`` hold: data stage 0, data status 2, and a console with two
    ``vol-uuid`` lines, neither of them the Data volume's.  A harness that
    reports this boot as anything else is broken.
    """
    statuses = run_rule(
        harness,
        globals_map={
            "_native_owner_data_stage": 0,
            "_native_owner_data_status": 2,
            "_native_owner_data_device": -1,
            "_native_owner_data_readonly": -1,
            "_native_owner_data_seed_base": 0,
            "_native_owner_data_seed_size": 0,
            "_native_ram_block_ready": 1,
            "_native_ram_block_failures": 0,
            "_native_ram_block_bytes": 1073741824,
            "_native_virtio_read_failures": 0,
            "_native_virtio_integrity_failures": 0,
        },
        console_text=PASSING_CONSOLE.replace(
            "disk4s1 vol-uuid: 18AB5557-DC2D-469B-8758-855E60F5B0DF",
            "disk4s1 vol-uuid: 04769D26-2CA0-4825-B443-E2E5F88300F2"),
        processes=None,
    )
    assert statuses["_native_owner_data_stage == 4"] == harness.FAIL
    assert statuses["_native_owner_data_status == 0"] == harness.FAIL
    assert statuses["the console's Data volume vol-uuid == "
                    + harness.DATA_VOLUME_UUID] == harness.FAIL
    # The process list is the one channel a read-owner-state run does not
    # record, so it is the one criterion that may be UNVERIFIED rather than
    # FAIL -- and an UNVERIFIED is never a pass.
    assert statuses["PID 1 is the original launchd"] == harness.UNVERIFIED
    assert harness.FAIL in statuses.values()


def test_a_missing_channel_is_never_a_pass(harness):
    """Every criterion of a channel that was not measured is UNVERIFIED.

    The distinction this test pins is the one that keeps a harness honest: a
    channel that cannot answer is reported as unable to answer, which makes the
    verdict "not verified", rather than being treated as satisfied.
    """
    statuses = run_rule(harness, None, None, None)
    assert set(statuses.values()) == {harness.UNVERIFIED}


# ---------------------------------------------------------------------------
# the teeth check
# ---------------------------------------------------------------------------
def _mutate(globals_map, console_text, processes, mutation):
    """Apply one mutation to the passing fixture.

    The mutation is a single mapping rather than keyword arguments, because two
    of the mutations replace the *process list* and a keyword called
    ``processes`` would collide with the fixture's own parameter -- which is how
    the first version of this helper failed, with a ``TypeError`` that had
    nothing to do with the rule.
    """
    readings = dict(globals_map)
    readings.update(mutation.get("globals", {}))
    text = console_text
    for line in mutation.get("drop_lines", ()):
        text = "\n".join(
            kept for kept in text.splitlines() if line not in kept
        ) + "\n"
    for old, new in mutation.get("replace", ()):
        assert old in text, f"the fixture does not carry {old!r}"
        text = text.replace(old, new)
    rows = mutation["processes"] if "processes" in mutation else processes
    return readings, text, rows


#: One entry per criterion: what to change so that criterion alone fails, and
#: the criterion that must then be reported as failing.  A mutation that leaves
#: the verdict passing is a criterion that is not load-bearing.
#:
#: The console mutations are line-level rather than regex-level on purpose: a
#: test that weakened the *pattern* to make a mutation bite would be testing the
#: pattern against itself.
TEETH: list[tuple[str, dict[str, Any], str]] = [
    ("the prepare stopped one stage short",
     {"globals": {"_native_owner_data_stage": 3}},
     "_native_owner_data_stage == 4"),
    ("the prepare returned ENOENT",
     {"globals": {"_native_owner_data_status": 2}},
     "_native_owner_data_status == 0"),
    ("the mounted device was never read",
     {"globals": {"_native_owner_data_device": -1}},
     "_native_owner_data_device != NODEV"),
    ("the Data mount came up read-only",
     {"globals": {"_native_owner_data_readonly": 1}},
     "_native_owner_data_readonly == 0"),
    ("the seed size is not 4096-aligned",
     {"globals": {"_native_owner_data_seed_size": 1073741825}},
     "_native_owner_data_seed_size in (0, 0x10000000000] and 4096-aligned"),
    ("the seed is empty",
     {"globals": {"_native_owner_data_seed_size": 0}},
     "_native_owner_data_seed_size in (0, 0x10000000000] and 4096-aligned"),
    ("the RAM block failed a read",
     {"globals": {"_native_ram_block_failures": 1}},
     "_native_ram_block_failures == 0"),
    ("the RAM block never came up",
     {"globals": {"_native_ram_block_ready": 0}},
     "_native_ram_block_ready == 1"),
    ("the block layer refused a read",
     {"globals": {"_native_virtio_read_failures": 1}},
     "_native_virtio_read_failures == 0"),
    ("the block layer failed a verification",
     {"globals": {"_native_virtio_integrity_failures": 1}},
     "_native_virtio_integrity_failures == 0"),
    ("the volume carries a different UUID",
     {"replace": [("disk4s1 vol-uuid: 18AB5557-DC2D-469B-8758-855E60F5B0DF",
                   "disk4s1 vol-uuid: 18AF0FFB-77A7-4878-B999-70DD8B4076DC")]},
     "the console's Data volume vol-uuid == " + "18AB5557-DC2D-469B-8758-855E60F5B0DF"),
    ("the volume never started mounting",
     {"drop_lines": ["disk4s1 mounting volume"]},
     "the Data volume has a mounting volume line"),
    ("the volume never finished mounting",
     {"drop_lines": ["disk4s1 mount-complete volume"]},
     "the Data volume has a mount-complete volume line"),
    ("the volume is not on the RAM block",
     {"replace": [("dev_init:306: disk4 device_handle block size 4096 block count 262144",
                   "dev_init:306: disk4 device_handle block size 4096 block count 1483776")]},
     "the Data volume's device is the RAM block"),
    ("launchd was never exec'd",
     {"drop_lines": ["load_init_program: attempting to load"]},
     "the console's launchd exec line is present"),
    ("a fallback init program is PID 1",
     {"replace": [("load_init_program: attempting to load /sbin/launchd",
                   "load_init_program: attempting to load /sbin/mach_init")]},
     "the console's launchd exec line is present"),
    ("PID 1 is the pre-exec init process",
     {"processes": [{"pid": 0, "state": "idle", "name": "kernel_task"},
                    {"pid": 1, "state": "run", "name": "init"}]},
     "PID 1 is the original launchd"),
    ("there is no PID 1 at all",
     {"processes": [{"pid": 0, "state": "idle", "name": "kernel_task"}]},
     "PID 1 is the original launchd"),
]


@pytest.mark.parametrize(
    ("why", "mutation", "criterion"),
    TEETH,
    ids=[entry[0] for entry in TEETH],
)
def test_every_criterion_is_load_bearing(harness, why, mutation, criterion):
    """Breaking one thing breaks exactly the criterion that names it.

    This is the test that makes the rule worth having.  Each case starts from a
    boot that verifies, changes one reading, and requires that the named
    criterion fail -- so a criterion that could be removed without this test
    going red is a criterion that is not measuring anything.
    """
    readings, text, rows = _mutate(PASSING_GLOBALS, PASSING_CONSOLE,
                                   PASSING_PROCESSES, mutation)
    statuses = run_rule(harness, readings, text, rows)
    assert statuses.get(criterion) == harness.FAIL, (
        f"{why}: expected {criterion!r} to fail, got {statuses.get(criterion)!r}"
    )
    # And it is not merely "something failed": the verdict is what a caller
    # branches on, so it has to move too.
    assert harness.FAIL in statuses.values(), (
        f"{why}: the verdict still verifies with {criterion!r} broken"
    )


def test_the_launchd_pattern_rejects_a_fallback_path(harness):
    """``attempting to load /sbin/launchd`` is not a substring match.

    ``load_init_program()`` prints one line per candidate it tries, so a pattern
    that matched "launchd" anywhere would pass a boot that fell back.  PRD M8
    forbids exactly that, and this is the test that keeps it forbidden.
    """
    console = harness.Console(
        "load_init_program: attempting to load /sbin/launchd.debug\n", "test")
    assert console.launchd_line is False
    console = harness.Console(
        "load_init_program: attempting to load /sbin/launchd\n", "test")
    assert console.launchd_line is True


def test_the_console_resolves_one_device_for_every_criterion(harness):
    """A Data volume on a *different* device does not satisfy the mount lines.

    The device identity comes from the ``vol-uuid`` line, and the
    ``mounting``/``mount-complete`` criteria are keyed off it.  Two independent
    searches could each find a line on a different device and each succeed,
    which is what this fixture rules out: disk3s1 mounts completely and carries
    a UUID, but it is not the Data volume.
    """
    text = PASSING_CONSOLE.replace(
        "disk4s1 vol-uuid: 18AB5557-DC2D-469B-8758-855E60F5B0DF",
        "disk4s1 vol-uuid: 04769D26-2CA0-4825-B443-E2E5F88300F2")
    console = harness.Console(text, "test")
    assert console.expected_volume is None
    assert console.observed_uuids() == [
        "04769D26-2CA0-4825-B443-E2E5F88300F2",
        "8846FE78-2910-4297-AF97-D59BB6F8FC16",
        "04769D26-2CA0-4825-B443-E2E5F88300F2",
    ]


def test_the_expected_uuid_is_the_one_the_kernel_was_built_with(harness):
    """The constant is the build script's, spelled the same way.

    A typo in the UUID is indistinguishable from a missing Data volume, so the
    value is pinned here against the file that defines it.  The file is under
    ``vendor/`` and is read rather than imported, because it is a Python script
    whose module-level code writes a build environment.
    """
    source = (REPO_ROOT / "vendor" / "UniXNU" / "scripts"
              / "build-cpu-desktop-kernel.py")
    if not source.is_file():
        pytest.skip("vendor/UniXNU is absent (gitignored, PRD 12)")
    assert f"XNU_OWNER_DATA_VOLUME_UUID='{harness.DATA_VOLUME_UUID}'" in \
        source.read_text(encoding="utf-8", errors="replace")
