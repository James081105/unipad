"""``scripts/macos/provision-data-seed.py`` against seeds built on this host.

Why this exists
---------------
The script runs on the macOS runner, where ``hdiutil`` is the only thing that
can attach an APFS volume, so the half that actually provisions cannot be run
here.  What CAN be run here is the half that decides whether the run is worth
starting: the unpack/repack cycle, the safety property, and the directory list.
Those are the parts where a bug costs a 30-minute runner rather than a second,
and the unpack cursor is the part that already had one.

MEASURED, and the reason ``test_unpack_reads_the_payload_not_the_extent_table``
exists: ``HEADER_SIZE`` is 32 and the payload starts after the extent table, at
``32 + count * 16``.  A cursor starting at ``HEADER_SIZE`` reads the extent
table itself as the payload, which produces a raw image whose offset 32 holds
an extent descriptor instead of ``NXSB``.  The script's own ``NXSB`` check
caught it; this test pins it so a future edit cannot quietly reintroduce it.

No real seed is used anywhere here: one is Apple-derived material that does not
belong in the repository (PRD 12), and ``prepare-owner-data-fixture.py`` needs
macOS to make one anyway.  Every seed in this file is built byte by byte.
"""

from __future__ import annotations

import hashlib
import os
import importlib.util
import struct
from pathlib import Path

import pytest

from unipad.host import packed_ramdisk as pr

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/macos/provision-data-seed.py"


def _load():
    spec = importlib.util.spec_from_file_location("provision_data_seed", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pds = _load()

PAGE = pr.PAGE_SIZE


def synthetic_seed(
    path: Path, *, pages: int = 8, payload: bytes = b"", at: int = 0
) -> bytes:
    """A valid DBRDISK1 seed carrying ``payload`` at page ``at``, rest zeroed.

    ``payload`` defaults to a minimal APFS container header, because
    ``unpack`` refuses anything whose offset 32 is not ``NXSB`` -- which is the
    guard that caught the real bug, so every seed here has to satisfy it.
    """
    if not payload:
        header = bytearray(PAGE)
        header[32:36] = b"NXSB"
        payload = bytes(header)
    disk = bytearray(pages * PAGE)
    disk[at * PAGE:at * PAGE + len(payload)] = payload[: pages * PAGE - at * PAGE]
    packed = pr.pack(bytes(disk))
    path.write_bytes(packed)
    return packed


#: A container header good enough for ``unpack``'s ``NXSB`` check, with a
#: recognisable container UUID at ``nx_uuid`` (0x60) so the round-trip test can
#: assert the UUID survived rather than only that the magic did.
CONTAINER = bytearray(PAGE)
CONTAINER[32:36] = b"NXSB"
CONTAINER[0x60:0x70] = bytes(range(16))


# ---- the directory list -------------------------------------------------
#
# The point of the whole change.  These are the paths the guest's own sandbox
# named, verbatim from the boot-31 log, and a list that does not contain them
# provisions a seed that still fails.

WANTED_BY_THE_GUEST = (
    "containers",
    "containers/Data",
    "containers/Data/System",
    "containers/Shared",
    "containers/Shared/SystemGroup",
    "containers/Bundle",
    "containers/Temp",
    "mobile/Containers",
)


@pytest.mark.parametrize("wanted", WANTED_BY_THE_GUEST)
def test_the_skeleton_has_the_paths_the_sandbox_named(wanted: str) -> None:
    assert wanted in pds.DIRECTORIES, (
        f"{wanted} is a path the guest's sandbox reported it could not find; "
        "a seed provisioned without it fails the same way"
    )


def test_parents_are_created_by_mkdir_parents() -> None:
    """Order is NOT guaranteed, and must not be relied on.

    ``mobile/Library/Preferences`` is listed before ``mobile/Library`` in
    upstream's own ``DIRECTORIES`` -- measured, not assumed -- so the script
    cannot depend on the list being topologically sorted.  What it depends on is
    ``path.mkdir(parents=True, exist_ok=True)``, which makes the order
    irrelevant.  This asserts that dependency is real, because a future edit
    that dropped ``parents=True`` would work for the seed this repository has
    and fail for one that lacks ``mobile/``.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    assert "path.mkdir(parents=True, exist_ok=True)" in text
    # And the two lists are not individually sorted either, which is the same
    # fact from the other side.
    assert list(pds.OFFLINE_DIRECTORIES) != sorted(pds.OFFLINE_DIRECTORIES)


def test_the_two_upstream_lists_are_both_present() -> None:
    """The union, not one of them.  Each list has entries the other lacks."""
    assert set(pds.OFFLINE_DIRECTORIES) - set(pds.SKIP_SETUP_DIRECTORIES)
    assert set(pds.SKIP_SETUP_DIRECTORIES) - set(pds.OFFLINE_DIRECTORIES)
    assert set(pds.DIRECTORIES) == (
        set(pds.OFFLINE_DIRECTORIES) | set(pds.SKIP_SETUP_DIRECTORIES)
    )


def test_no_duplicate_directory() -> None:
    assert len(pds.DIRECTORIES) == len(set(pds.DIRECTORIES))


# ---- the unpack cursor --------------------------------------------------

def test_unpack_reads_the_payload_not_the_extent_table(tmp_path: Path) -> None:
    """The measured bug, pinned.

    A cursor at ``HEADER_SIZE`` reads the extent table as the payload.  With a
    one-extent seed the table is 16 bytes and the first descriptor is
    ``(offset=0, length=PAGE)``, so the raw image's first eight bytes come out
    zero either way and the mistake is invisible until the ``NXSB`` check --
    which is exactly why the check exists and why this test asserts on bytes
    that only a correct cursor produces.
    """
    seed = tmp_path / "seed.dbr"
    synthetic_seed(seed, pages=8, payload=bytes(CONTAINER))
    raw = tmp_path / "seed.raw"

    logical, resident = pds.unpack(seed, raw)

    assert logical == 8 * PAGE
    assert resident == PAGE
    assert raw.read_bytes()[:PAGE] == bytes(CONTAINER)


def test_unpack_writes_the_payload_at_its_logical_offset(tmp_path: Path) -> None:
    """Extent 0 is at logical 0 and extent 1 is not; both must land right."""
    disk = bytearray(8 * PAGE)
    disk[0:PAGE] = CONTAINER
    disk[3 * PAGE:3 * PAGE + 16] = b"SECOND-EXTENT!!!"
    # Two separated dirty regions, so the packed form has two extents and the
    # second one's payload is not the bytes immediately after the first's.
    seed = tmp_path / "seed.dbr"
    seed.write_bytes(pr.pack(bytes(disk)))

    _magic, _version, _page, _logical, count = struct.unpack_from(
        "<8sIIQQ", seed.read_bytes(), 0
    )
    assert count == 2, "a two-region disk must pack to two extents"

    raw = tmp_path / "seed.raw"
    _logical, resident = pds.unpack(seed, raw)
    out = raw.read_bytes()
    assert out[:PAGE] == bytes(CONTAINER)
    assert out[3 * PAGE:3 * PAGE + 16] == b"SECOND-EXTENT!!!"
    assert resident == 2 * PAGE


def test_unpack_refuses_a_non_apfs_payload(tmp_path: Path) -> None:
    """The guard that caught the real bug, checked against its own trigger."""
    seed = tmp_path / "seed.dbr"
    synthetic_seed(seed, payload=b"NOT-AN-APFS-CONTAINER" + bytes(PAGE - 22))
    raw = tmp_path / "seed.raw"
    with pytest.raises(SystemExit, match="not an APFS container"):
        pds.unpack(seed, raw)


def test_unpack_reports_the_logical_size_the_seed_describes(tmp_path: Path) -> None:
    seed = tmp_path / "seed.dbr"
    synthetic_seed(seed, pages=8, payload=bytes(CONTAINER))
    raw = tmp_path / "seed.raw"
    logical, _resident = pds.unpack(seed, raw)
    assert raw.stat().st_size == logical


# ---- the round trip -----------------------------------------------------

def test_unpack_then_pack_is_byte_identical(tmp_path: Path) -> None:
    """The property the whole step rests on.

    Provisioning has to survive the cycle unchanged, or the seed the guest is
    handed is not the seed the chain produced.  Verified on the real 1 GiB seed
    on 2026-10-27 as well; this is the synthetic version, so it runs here.
    """
    seed = tmp_path / "seed.dbr"
    original = synthetic_seed(seed, pages=8, payload=bytes(CONTAINER))
    raw = tmp_path / "seed.raw"
    pds.unpack(seed, raw)
    assert pr.pack_file(raw) == original


def test_the_round_trip_preserves_the_apfs_container_uuid(tmp_path: Path) -> None:
    """A cycle that moved the container UUID would be a different container."""
    seed = tmp_path / "seed.dbr"
    synthetic_seed(seed, pages=4, payload=bytes(CONTAINER))
    raw = tmp_path / "seed.raw"
    pds.unpack(seed, raw)
    assert raw.read_bytes()[0x60:0x70] == CONTAINER[0x60:0x70]

    # And again through the packed form, because that is the form the seed
    # travels in: unpack the repacked seed and the container has to be
    # byte-identical.  (``pack_file(raw)`` itself starts with DBRDISK1, so
    # comparing it to the raw container would be comparing two different
    # formats -- which is what an earlier version of this test did.)
    again = tmp_path / "again.dbr"
    again.write_bytes(pr.pack_file(raw))
    twice = tmp_path / "twice.raw"
    pds.unpack(again, twice)
    assert twice.read_bytes()[:PAGE] == bytes(CONTAINER)


# ---- the safety property ------------------------------------------------

#: Windows refuses ``os.symlink`` without elevation or Developer Mode
#: (``WinError 1314``), and the inventory's symlink half is what this whole
#: script is guarding.  Skipped rather than faked: a fake symlink would test
#: the inventory against something the guest's seed never contains.
needs_symlinks = pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="this platform has no symlink support at all",
)


def _can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / ".probe").symlink_to(".", target_is_directory=True)
    except OSError:
        return False
    finally:
        (tmp_path / ".probe").unlink(missing_ok=True)
    return True


@needs_symlinks
def test_inventory_separates_symlinks_from_files(tmp_path: Path) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("symlinks need elevation or Developer Mode on this host")
    (tmp_path / "a").write_bytes(b"one")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "c").write_bytes(b"two")
    (tmp_path / "link").symlink_to("a", target_is_directory=False)

    found = pds.inventory(tmp_path)

    assert found["a"] == ["file", hashlib.sha256(b"one").hexdigest()]
    assert found["b/c"] == ["file", hashlib.sha256(b"two").hexdigest()]
    assert found["link"][0] == "symlink"
    assert "b" not in found, "directories are not inventoried; creating them is the point"


def test_inventory_notices_a_changed_file(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"one")
    before = pds.inventory(tmp_path)
    (tmp_path / "a").write_bytes(b"two")
    after = pds.inventory(tmp_path)
    assert before != after


@needs_symlinks
def test_inventory_notices_a_retargeted_symlink(tmp_path: Path) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("symlinks need elevation or Developer Mode on this host")
    (tmp_path / "a").write_bytes(b"one")
    (tmp_path / "b").write_bytes(b"two")
    (tmp_path / "link").symlink_to("a", target_is_directory=False)
    before = pds.inventory(tmp_path)
    (tmp_path / "link").unlink()
    (tmp_path / "link").symlink_to("b", target_is_directory=False)
    after = pds.inventory(tmp_path)
    assert before != after


# ---- the CLI contract ---------------------------------------------------

def test_the_script_refuses_to_overwrite_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard that keeps a half-written seed out of the real role's name.

    Reached before any macOS tool runs, so it is testable here: the overwrite
    check is the second thing ``main()`` does, after confirming ``--seed``
    exists.
    """
    seed = tmp_path / "seed.dbr"
    synthetic_seed(seed)
    out = tmp_path / "out.dbr"
    out.write_bytes(b"already here")
    monkeypatch.setattr(
        "sys.argv",
        ["provision-data-seed.py", "--seed", str(seed), "--out", str(out)],
    )
    assert pds.main() == 2
    assert out.read_bytes() == b"already here", "the refusal must not touch it"


def test_the_script_documents_what_it_does_not_do() -> None:
    """No preferences, no keys, no user records.

    ``seed-data-skip-setup.py`` merges five preference plists as well as
    creating directories.  This script deliberately does not, so that a
    provisioning step has one effect that can be attributed.  The docstring is
    where that is promised, and a reader of the run's evidence needs it to be
    true -- so it is asserted here rather than trusted.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    assert "adds empty directories" in text
    assert "No keys, no user records, no persona, no Apple" in text
    assert "com.apple.purplebuddy" not in text, (
        "a preference merge in this script would make its two effects "
        "impossible to tell apart when something breaks"
    )


def test_the_volume_uuid_is_checked_not_assumed() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "uuid_before" in text and "uuid_after" in text
    assert "the volume UUID changed" in text
