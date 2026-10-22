"""Unit tests for :mod:`unipad.host.images` (PRD 4.1, 4.2, 7.2, 7.4, 8.2, FR10).

Image handling is where PRD 7.4 gets teeth: "构建产物存在、尺寸非零、哈希/架构/清单正确后
才进入运行；下载失败不能继续使用未标记的旧包".  These tests pin the observable
contracts that enforce it:

- ``sha256_file`` is chunked (a >1 MiB file must not be read in one gulp) and
  matches the published SHA-256 test vectors;
- ``verify_image`` refuses a tampered artifact with ``image_hash_mismatch`` and
  names **both** digests so the operator can see which side is wrong;
- ``create_data_image`` never overwrites an existing image without ``force``
  (PRD 7.2: never overwrite user data by default);
- ``restore_checkpoint`` materialises a **new** directory, never writes in place
  (PRD 7.2), and refuses a checkpoint whose bytes no longer match its manifest.

Field status for every real image is UNVERIFIED: these are host-side contracts
proved against temporary files (docs/testing-strategy.md D2/D3).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host.images import (
    KNOWN_GOOD_SCHEMA_VERSION,
    ImageManager,
    KnownGoodPointer,
    sha256_file,
)

# Published SHA-256 vectors (FIPS 180-4 / NIST examples).
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def _pattern_bytes(size: int) -> bytes:
    """Deterministic, non-repeating content (no clock, no randomness)."""
    return bytes((index * 31 + (index >> 8) * 7) % 256 for index in range(size))


# ---------------------------------------------------------------------------
# sha256_file
# ---------------------------------------------------------------------------


def test_sha256_file_matches_the_empty_vector(tmp_path):
    """PRD 7.4: the digest of an empty artifact is the published one."""
    target = tmp_path / "empty.img"
    target.write_bytes(b"")

    assert sha256_file(target) == EMPTY_SHA256


def test_sha256_file_matches_the_hello_vector(tmp_path):
    """PRD 7.4: hashing is exact, not approximate."""
    target = tmp_path / "hello.bin"
    target.write_bytes(b"hello")

    assert sha256_file(target) == HELLO_SHA256


def test_sha256_file_chunks_files_larger_than_one_mib(tmp_path):
    """PRD 7.4: multi-GiB images are hashed in bounded chunks, never in RAM.

    A 1 MiB + 12345-byte file forces at least two reads at the default chunk
    size, and re-hashing with a 4 KiB chunk must produce the same digest.
    """
    size = (1 << 20) + 12345
    target = tmp_path / "big.img"
    target.write_bytes(_pattern_bytes(size))

    expected = hashlib.sha256(_pattern_bytes(size)).hexdigest()

    assert sha256_file(target) == expected
    assert sha256_file(target, chunk_size=4096) == expected


def test_sha256_file_progress_reports_every_chunk(tmp_path):
    """The chunked reader really is chunked: the callback fires per chunk."""
    size = (1 << 20) + 12345
    target = tmp_path / "big.img"
    target.write_bytes(_pattern_bytes(size))
    seen: list[tuple[int, int]] = []

    sha256_file(target, progress=lambda done, total: seen.append((done, total)))

    assert len(seen) >= 2, "a >1 MiB file must be read in more than one chunk"
    assert [total for _, total in seen] == [size] * len(seen)
    assert seen[-1] == (size, size)
    assert all(earlier < later for (earlier, _), (later, _) in zip(seen, seen[1:]))


def test_sha256_file_missing_path_raises_image_not_found(tmp_path):
    """PRD 7.4: a missing artifact is a distinct, named failure."""
    with pytest.raises(UniPadError) as excinfo:
        sha256_file(tmp_path / "absent.img")

    assert excinfo.value.code == ErrorCode.IMAGE_NOT_FOUND


def test_sha256_file_directory_raises_image_not_found(tmp_path):
    """PRD 7.4: only regular files are hashable artifacts."""
    with pytest.raises(UniPadError) as excinfo:
        sha256_file(tmp_path)

    assert excinfo.value.code == ErrorCode.IMAGE_NOT_FOUND
    assert "regular file" in excinfo.value.message


def test_sha256_file_rejects_a_non_positive_chunk_size(tmp_path):
    """A zero/negative chunk would silently hash nothing."""
    target = tmp_path / "x.bin"
    target.write_bytes(b"x")

    with pytest.raises(UniPadError) as excinfo:
        sha256_file(target, chunk_size=0)

    assert excinfo.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID


# ---------------------------------------------------------------------------
# verify_image
# ---------------------------------------------------------------------------


def test_verify_image_accepts_a_matching_digest(tmp_path):
    """PRD 7.4: a verified artifact reports its role and size for the manifest."""
    target = tmp_path / "firmware.bin"
    target.write_bytes(b"firmware-bytes")
    manager = ImageManager(tmp_path / "artifacts")

    result = manager.verify_image(target, hashlib.sha256(b"firmware-bytes").hexdigest(), role="firmware")

    assert result["ok"] is True
    assert result["role"] == "firmware"
    assert result["size_bytes"] == len(b"firmware-bytes")
    assert result["sha256"] == result["expected_sha256"]


def test_verify_image_tamper_raises_image_hash_mismatch_with_both_hashes(tmp_path):
    """PRD 7.4: a mismatch is fatal and names both digests."""
    target = tmp_path / "system.img"
    target.write_bytes(b"original-system-image")
    manager = ImageManager(tmp_path / "artifacts")
    expected = hashlib.sha256(b"original-system-image").hexdigest()

    target.write_bytes(b"original-system-image" + b"tampered")  # different bytes now

    with pytest.raises(UniPadError) as excinfo:
        manager.verify_image(target, expected, role="system_image")

    error = excinfo.value
    assert error.code == ErrorCode.IMAGE_HASH_MISMATCH
    assert error.details["expected_sha256"] == expected
    assert error.details["actual_sha256"] == hashlib.sha256(
        b"original-system-image" + b"tampered"
    ).hexdigest()
    assert error.details["expected_sha256"] != error.details["actual_sha256"]
    assert error.details["role"] == "system_image"


def test_verify_image_rejects_a_malformed_expected_digest(tmp_path):
    """PRD 7.4: an unlabelled expectation is a caller bug, not a manifest issue."""
    target = tmp_path / "system.img"
    target.write_bytes(b"data")
    manager = ImageManager(tmp_path / "artifacts")

    with pytest.raises(UniPadError) as excinfo:
        manager.verify_image(target, "not-a-digest")

    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_verify_image_missing_file_raises_image_not_found(tmp_path):
    """PRD 7.4: absent artifact, not a silent pass."""
    manager = ImageManager(tmp_path / "artifacts")

    with pytest.raises(UniPadError) as excinfo:
        manager.verify_image(tmp_path / "absent.img", "a" * 64)

    assert excinfo.value.code == ErrorCode.IMAGE_NOT_FOUND


# ---------------------------------------------------------------------------
# create_data_image (PRD 7.2)
# ---------------------------------------------------------------------------


def test_create_data_image_makes_a_sparse_raw_file(tmp_path):
    """PRD 4.2: the Data volume is a raw, sparsely allocated image."""
    manager = ImageManager(tmp_path / "artifacts")
    target = tmp_path / "data.img"

    result = manager.create_data_image(target, 1)

    assert result["size_bytes"] == 1024 * 1024
    assert result["created"] is True
    assert result["existed_before"] is False
    assert target.read_bytes() == bytes(1024 * 1024)
    assert result["sha256"] == hashlib.sha256(bytes(1024 * 1024)).hexdigest()


def test_create_data_image_refuses_to_overwrite_without_force(tmp_path):
    """PRD 7.2: user data outranks convenience; no overwrite unless asked."""
    manager = ImageManager(tmp_path / "artifacts")
    target = tmp_path / "data.img"
    target.write_bytes(b"precious user data")

    with pytest.raises(UniPadError) as excinfo:
        manager.create_data_image(target, 4)

    assert excinfo.value.code == ErrorCode.BLOCKED
    assert excinfo.value.details["existing_size_bytes"] == len(b"precious user data")
    # The bytes on disk are untouched.
    assert target.read_bytes() == b"precious user data"


def test_create_data_image_force_replaces_the_image(tmp_path):
    """PRD 7.2: ``force`` is the explicit, auditable opt-in."""
    manager = ImageManager(tmp_path / "artifacts")
    target = tmp_path / "data.img"
    target.write_bytes(b"old")

    result = manager.create_data_image(target, 2, force=True)

    assert result["existed_before"] is True
    assert result["created"] is False
    assert target.stat().st_size == 2 * 1024 * 1024


def test_create_data_image_rejects_non_positive_sizes(tmp_path):
    """A zero-sized Data volume would silently lose every write."""
    manager = ImageManager(tmp_path / "artifacts")

    for bad in (0, -1, True, "4"):
        with pytest.raises(UniPadError) as excinfo:
            manager.create_data_image(tmp_path / f"data-{bad}.img", bad)  # type: ignore[arg-type]
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_create_data_image_creates_missing_parent_directories(tmp_path):
    """PRD 4.1: the artifact root is created on demand."""
    manager = ImageManager(tmp_path / "artifacts")
    target = tmp_path / "deep" / "nested" / "data.img"

    manager.create_data_image(target, 1)

    assert target.is_file()


# ---------------------------------------------------------------------------
# known-good pointer (PRD 8.2 step 6)
# ---------------------------------------------------------------------------


def test_known_good_pointer_round_trips(tmp_path):
    """PRD 8.2 step 6: the pointer stores what the caller verified, verbatim."""
    pointer = KnownGoodPointer(tmp_path / "artifacts")
    entries = [
        {
            "path": "F:/iosvirtual/artifacts/system.img",
            "sha256": "b" * 64,
            "timestamp": "2026-10-22T00:00:00Z",
            "run_id": "exp-1",
        }
    ]

    written = pointer.save(entries, "exp-1 gate passed", timestamp="2026-10-22T00:00:00Z")
    document = pointer.load()

    assert written == pointer.path
    assert document["schema_version"] == KNOWN_GOOD_SCHEMA_VERSION
    assert document["note"] == "exp-1 gate passed"
    assert document["updated_at"] == "2026-10-22T00:00:00Z"
    assert document["entries"] == entries


def test_known_good_pointer_is_empty_on_a_fresh_clone(tmp_path):
    """PRD 8.2: no pointer recorded yet is an empty pointer, not an error."""
    document = KnownGoodPointer(tmp_path / "artifacts").load()

    assert document["schema_version"] == KNOWN_GOOD_SCHEMA_VERSION
    assert document["entries"] == []


def test_known_good_pointer_rejects_an_unsupported_schema(tmp_path):
    """PRD 7.4: an unlabelled pointer is not evidence of a bootable build."""
    path = tmp_path / "artifacts" / "known-good.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 2, "entries": []}), encoding="utf-8")

    with pytest.raises(UniPadError) as excinfo:
        KnownGoodPointer(tmp_path / "artifacts").load()

    assert excinfo.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID
    assert excinfo.value.details["schema_version"] == 2


def test_known_good_pointer_rejects_unreadable_json(tmp_path):
    """PRD 7.4: a corrupt pointer must not be silently treated as empty."""
    path = tmp_path / "artifacts" / "known-good.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(UniPadError) as excinfo:
        KnownGoodPointer(tmp_path / "artifacts").load()

    assert excinfo.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID


@pytest.mark.parametrize("bad_entries", [["not-an-object"], [{"path": "x"}], [{"sha256": "y" * 64}]])
def test_known_good_pointer_rejects_entries_without_path_and_digest(tmp_path, bad_entries):
    """PRD 8.2 step 6: an entry without path+digest names nothing verifiable."""
    with pytest.raises(UniPadError) as excinfo:
        KnownGoodPointer(tmp_path / "artifacts").save(bad_entries, "note")  # type: ignore[arg-type]

    assert excinfo.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID


# ---------------------------------------------------------------------------
# backup / restore (PRD 7.2, FR10)
# ---------------------------------------------------------------------------


def test_restore_checkpoint_copies_and_never_deletes_the_source(tmp_path):
    """PRD 7.2 / FR10: a restore is a copy; it consumes nothing.

    Both the live image and the checkpoint must still be there afterwards, with
    their bytes unchanged -- a restore that moved (instead of copied) the
    checkpoint would leave the operator with no second known-good version.
    """
    manager = ImageManager(tmp_path / "artifacts")
    source = tmp_path / "live" / "data.img"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_pattern_bytes(4096))
    backup_dir = tmp_path / "checkpoints" / "cp-1"

    manager.backup(source, backup_dir, timestamp="2026-10-22T00:00:00Z")
    restored = manager.restore_checkpoint(backup_dir, target_dir=tmp_path / "restored")

    target = Path(restored["target_dir"]) / "data.img"
    assert target.read_bytes() == _pattern_bytes(4096)
    # The source image survives, byte for byte.
    assert source.is_file()
    assert source.read_bytes() == _pattern_bytes(4096)
    # The checkpoint survives too: it is still a usable rollback point.
    assert (backup_dir / "data.img").is_file()
    assert (backup_dir / "manifest.json").is_file()
    assert (backup_dir / "data.img").read_bytes() == _pattern_bytes(4096)


def test_backup_then_restore_into_a_fresh_directory(tmp_path):
    """PRD 7.2: recovery materialises a new directory, never writes in place."""
    manager = ImageManager(tmp_path / "artifacts")
    source = tmp_path / "live" / "data.img"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_pattern_bytes(5000))
    original_digest = hashlib.sha256(_pattern_bytes(5000)).hexdigest()
    backup_dir = tmp_path / "checkpoints" / "cp-1"
    restore_dir = tmp_path / "restored-cp-1"

    backup = manager.backup(source, backup_dir, timestamp="2026-10-22T00:00:00Z", note="gate M9b")
    restored = manager.restore_checkpoint(backup_dir, target_dir=restore_dir)

    # The checkpoint holds a verified copy plus a manifest.
    assert backup["sha256"] == original_digest
    assert (backup_dir / "manifest.json").is_file()
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["entries"][0]["sha256"] == original_digest

    # The restore lands in a brand-new directory and verifies every file.
    assert restored["target_dir"] == str(restore_dir)
    assert Path(restored["target_dir"]) != backup_dir
    assert restored["verified"] is True
    assert [item["name"] for item in restored["restored"]] == ["data.img"]
    assert restored["restored"][0]["sha256"] == original_digest

    # The live image was never touched by either operation.
    assert source.read_bytes() == _pattern_bytes(5000)


def test_restore_checkpoint_default_target_is_a_new_sibling_directory(tmp_path):
    """PRD 7.2: without an explicit target the restore still avoids in place."""
    manager = ImageManager(tmp_path / "artifacts")
    source = tmp_path / "live" / "data.img"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"payload")
    backup_dir = tmp_path / "checkpoints" / "cp-1"

    manager.backup(source, backup_dir, timestamp="2026-10-22T00:00:00Z")
    restored = manager.restore_checkpoint(backup_dir, timestamp="2026-10-22T00:00:00Z")

    target = Path(restored["target_dir"])
    assert target != backup_dir
    assert target.parent == backup_dir.parent
    assert (target / "data.img").read_bytes() == b"payload"


def test_restore_checkpoint_refuses_a_non_empty_target(tmp_path):
    """PRD 7.2: an existing target is never silently overwritten."""
    manager = ImageManager(tmp_path / "artifacts")
    source = tmp_path / "live" / "data.img"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"payload")
    backup_dir = tmp_path / "checkpoints" / "cp-1"
    target = tmp_path / "restore-here"
    target.mkdir(parents=True, exist_ok=True)
    (target / "keep-me.txt").write_text("do not clobber", encoding="utf-8")

    manager.backup(source, backup_dir)

    with pytest.raises(UniPadError) as excinfo:
        manager.restore_checkpoint(backup_dir, target_dir=target)

    assert excinfo.value.code == ErrorCode.BLOCKED
    assert (target / "keep-me.txt").read_text(encoding="utf-8") == "do not clobber"


def test_restore_checkpoint_missing_directory_raises_image_not_found(tmp_path):
    """PRD 7.2: no checkpoint means nothing to restore, not an empty success."""
    manager = ImageManager(tmp_path / "artifacts")

    with pytest.raises(UniPadError) as excinfo:
        manager.restore_checkpoint(tmp_path / "no-such-checkpoint")

    assert excinfo.value.code == ErrorCode.IMAGE_NOT_FOUND


def test_restore_checkpoint_detects_a_tampered_checkpoint(tmp_path):
    """PRD 7.4: a checkpoint whose bytes changed must not be restored silently.

    The failure names both digests, so the operator can see whether the copy or
    the manifest is the side that moved.  ``restore_failed`` is the taxonomy
    code for this boundary (``unipad.errors``); the hash-comparison contract is
    additionally pinned by ``verify_image`` below.
    """
    manager = ImageManager(tmp_path / "artifacts")
    source = tmp_path / "live" / "data.img"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_pattern_bytes(2048))
    backup_dir = tmp_path / "checkpoints" / "cp-1"

    manager.backup(source, backup_dir)
    (backup_dir / "data.img").write_bytes(b"corrupted checkpoint bytes")

    with pytest.raises(UniPadError) as excinfo:
        manager.restore_checkpoint(backup_dir, target_dir=tmp_path / "restored")

    error = excinfo.value
    assert error.code == ErrorCode.RESTORE_FAILED
    assert error.details["expected_sha256"] == hashlib.sha256(_pattern_bytes(2048)).hexdigest()
    assert error.details["actual_sha256"] == hashlib.sha256(b"corrupted checkpoint bytes").hexdigest()
    assert error.details["expected_sha256"] != error.details["actual_sha256"]


def test_tampered_checkpoint_bytes_are_a_hash_mismatch_by_any_route(tmp_path):
    """PRD 7.4: the same corruption is ``image_hash_mismatch`` via verify_image."""
    manager = ImageManager(tmp_path / "artifacts")
    good = tmp_path / "data.img"
    good.write_bytes(_pattern_bytes(2048))
    expected = hashlib.sha256(_pattern_bytes(2048)).hexdigest()

    good.write_bytes(b"corrupted checkpoint bytes")

    with pytest.raises(UniPadError) as excinfo:
        manager.verify_image(good, expected)

    assert excinfo.value.code == ErrorCode.IMAGE_HASH_MISMATCH


def test_restore_checkpoint_reports_an_unverified_file_without_a_manifest(tmp_path):
    """PRD 9.2: an unverifiable restore is reported as unverified, never as ok."""
    manager = ImageManager(tmp_path / "artifacts")
    backup_dir = tmp_path / "checkpoints" / "cp-1"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "data.img").write_bytes(b"no manifest entry for me")

    restored = manager.restore_checkpoint(backup_dir, target_dir=tmp_path / "restored")

    assert restored["verified"] is False
    assert restored["restored"][0]["verified"] is False


def test_backup_refuses_to_replace_an_existing_checkpoint(tmp_path):
    """PRD 7.2: a checkpoint must not silently replace an earlier one."""
    manager = ImageManager(tmp_path / "artifacts")
    source = tmp_path / "live" / "data.img"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"payload")
    backup_dir = tmp_path / "checkpoints" / "cp-1"
    manager.backup(source, backup_dir)

    with pytest.raises(UniPadError) as excinfo:
        manager.backup(source, backup_dir)

    assert excinfo.value.code == ErrorCode.BLOCKED


def test_backup_missing_source_raises_image_not_found(tmp_path):
    """PRD 7.4: nothing to back up is an error, not an empty checkpoint."""
    manager = ImageManager(tmp_path / "artifacts")

    with pytest.raises(UniPadError) as excinfo:
        manager.backup(tmp_path / "absent.img", tmp_path / "checkpoints" / "cp-1")

    assert excinfo.value.code == ErrorCode.IMAGE_NOT_FOUND


def test_image_manager_known_good_pointer_is_bound_to_the_artifact_root(tmp_path):
    """PRD 8.2: the pointer lives under the configured artifact root."""
    manager = ImageManager(tmp_path / "artifacts")

    assert manager.known_good().path == tmp_path / "artifacts" / "known-good.json"
    assert manager.to_dict() == {"artifact_root": str(tmp_path / "artifacts")}
