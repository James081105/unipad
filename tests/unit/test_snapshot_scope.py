"""A checkpoint must declare what it covers (PRD 3.2 P1 (5)).

P1 (5) reads: 真正经过验证的快照/恢复；**必须说明保存的是磁盘还是整机状态**.

The second clause is the requirement, and it is the one that is easy to miss:
a restore that silently covers one disk file looks identical to a restore that
covers the whole machine until someone relies on it.  An operator who thinks a
checkpoint captured the machine and gets only a disk back has lost state they
were told was safe.

So the scope is a *declared field*, not something a reader infers from the
entry list:

* ``disk`` -- these image files, and nothing else.  Guest RAM, device state,
  the fw_cfg geometry and the install history are NOT in it.
* ``machine`` -- the whole runnable state, which is strictly more.

A checkpoint whose scope is absent is refused rather than assumed, and a
``machine`` checkpoint is required to name everything it claims to cover --
otherwise the declaration is a claim with nothing behind it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unipad.errors import ErrorCode
from unipad.host.images import (
    KNOWN_GOOD_SCHEMA_VERSION,
    ImageManager,
    ScopeRequired,
    declare_scope,
)
from unipad.models import MachineConfig



@pytest.fixture
def manager(tmp_path) -> ImageManager:
    return ImageManager(tmp_path / "artifacts")


@pytest.fixture
def image(tmp_path) -> Path:
    path = tmp_path / "data.img"
    path.write_bytes(b"disk-bytes" * 512)
    return path


def _manifest(directory: Path) -> dict:
    return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the declaration itself
# ---------------------------------------------------------------------------


def test_a_backup_declares_its_scope(manager, image, tmp_path):
    """PRD 3.2 P1 (5): the manifest says what it covers."""
    result = manager.backup(image, tmp_path / "cp")

    manifest = _manifest(tmp_path / "cp")
    assert manifest["scope"] == "disk"
    assert manifest["scope_note"]
    assert result["scope"] == "disk"


def test_the_scope_is_one_of_a_closed_vocabulary():
    assert ScopeRequired.DISK == "disk"
    assert ScopeRequired.MACHINE == "machine"
    with pytest.raises(ValueError):
        declare_scope("everything")


def test_the_disk_scope_names_what_it_excludes(manager, image, tmp_path):
    """A declaration that only says what is included invites the wrong guess.

    The exclusion list is the useful half: it is what tells an operator that
    guest RAM and the install history are not in here.
    """
    manager.backup(image, tmp_path / "cp")

    manifest = _manifest(tmp_path / "cp")
    excluded = manifest["scope_excludes"]
    for missing in ("guest_ram", "device_state", "fw_cfg", "install_history"):
        assert missing in excluded, f"{missing} must be named as not covered"


def test_a_machine_scope_has_to_name_what_it_covers(manager, image, tmp_path):
    """``machine`` is a claim about more than disks, so it needs evidence.

    A manifest that says ``machine`` while listing one image file is a lie the
    reader cannot detect, which is worse than saying ``disk``.
    """
    with pytest.raises(ValueError, match="machine scope requires"):
        declare_scope("machine")


def test_a_machine_scope_with_a_full_list_is_accepted():
    declaration = declare_scope(
        "machine",
        covered=["data.img", "system.img", "guest_ram", "device_state", "install_history"],
    )
    assert declaration["scope"] == "machine"
    assert "guest_ram" in declaration["scope_includes"]


# ---------------------------------------------------------------------------
# restore refuses an undeclared checkpoint
# ---------------------------------------------------------------------------


def test_restoring_a_checkpoint_without_a_scope_is_refused(manager, image, tmp_path):
    """An absent scope is not ``disk`` by default: it is unknown.

    Assuming the narrow reading would let an operator believe they restored
    less than they did; assuming the wide one would let them believe they
    restored more.  Neither is safe, so the run stops.
    """
    checkpoint = tmp_path / "cp"
    manager.backup(image, checkpoint)
    manifest_path = checkpoint / "manifest.json"
    manifest = _manifest(checkpoint)
    del manifest["scope"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        manager.restore_checkpoint(checkpoint, target_dir=tmp_path / "restored")

    assert getattr(excinfo.value, "code", None) == ErrorCode.ARTIFACT_MANIFEST_INVALID
    assert "scope" in str(excinfo.value)


def test_restoring_a_checkpoint_with_an_unknown_scope_is_refused(manager, image, tmp_path):
    checkpoint = tmp_path / "cp"
    manager.backup(image, checkpoint)
    manifest_path = checkpoint / "manifest.json"
    manifest = _manifest(checkpoint)
    manifest["scope"] = "whole-universe"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        manager.restore_checkpoint(checkpoint, target_dir=tmp_path / "restored")

    assert getattr(excinfo.value, "code", None) == ErrorCode.ARTIFACT_MANIFEST_INVALID


# ---------------------------------------------------------------------------
# the declaration reaches the operator
# ---------------------------------------------------------------------------


def test_the_restore_result_reports_the_scope_it_restored(manager, image, tmp_path):
    """The CLI prints this, so the scope has to be in the result, not just the
    manifest -- an operator who never opens the file still learns what they got."""
    checkpoint = tmp_path / "cp"
    manager.backup(image, checkpoint)

    result = manager.restore_checkpoint(checkpoint, target_dir=tmp_path / "restored")

    assert result["scope"] == "disk"
    assert result["scope_note"]
    assert "guest_ram" in result["scope_excludes"]


def test_a_machine_scope_is_reported_as_covering_more(manager, tmp_path):
    """The two scopes must be distinguishable in the output, not just stored."""
    system = tmp_path / "system.img"
    system.write_bytes(b"system" * 256)
    data = tmp_path / "data.img"
    data.write_bytes(b"data" * 256)

    checkpoint = tmp_path / "cp-machine"
    manager.backup(system, checkpoint)
    manager.backup(data, checkpoint)
    declaration = declare_scope(
        "machine",
        covered=["system.img", "data.img", "guest_ram", "device_state"],
    )
    manifest_path = checkpoint / "manifest.json"
    manifest = _manifest(checkpoint)
    manifest.update(declaration)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = manager.restore_checkpoint(checkpoint, target_dir=tmp_path / "restored")

    assert result["scope"] == "machine"
    assert result["scope"] != "disk"
    assert "guest_ram" in result["scope_includes"]


def test_the_schema_version_is_bumped_for_the_new_field(manager, image, tmp_path):
    """A reader that knows the old schema must not silently accept a manifest
    it cannot interpret."""
    manager.backup(image, tmp_path / "cp")
    manifest = _manifest(tmp_path / "cp")
    assert manifest["schema_version"] > KNOWN_GOOD_SCHEMA_VERSION - 1


def test_a_checkpoint_with_no_manifest_is_reported_unknown_not_refused(manager, tmp_path):
    """PRD 9.2 and P1 (5) pull in opposite directions here, and both must hold.

    P1 (5) wants the scope declared; 9.2 wants an unverifiable restore reported
    as unverified rather than refused.  A directory of images with no manifest
    satisfies neither "declared" nor "verified", so it restores with the scope
    explicitly ``unknown`` -- not assumed narrow, not assumed wide.
    """
    checkpoint = tmp_path / "cp-no-manifest"
    checkpoint.mkdir(parents=True)
    (checkpoint / "data.img").write_bytes(b"no manifest for me")

    result = manager.restore_checkpoint(checkpoint, target_dir=tmp_path / "restored")

    assert result["scope"] == "unknown"
    assert result["verified"] is False
    assert result["restored"][0]["verified"] is False
    assert "cannot be determined" in result["scope_note"]


def test_a_manifest_that_is_not_json_is_refused(manager, tmp_path):
    """A present-but-unreadable manifest is a claim someone made and broke."""
    checkpoint = tmp_path / "cp-broken"
    checkpoint.mkdir(parents=True)
    (checkpoint / "data.img").write_bytes(b"bytes")
    (checkpoint / "manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        manager.restore_checkpoint(checkpoint, target_dir=tmp_path / "restored")

    assert getattr(excinfo.value, "code", None) == ErrorCode.ARTIFACT_MANIFEST_INVALID
