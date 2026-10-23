"""Image hashing, verification, creation and checkpointing (PRD 4.1, FR10, 7.2).

Responsibilities:

- **Hash and verify** every artifact before it is used; a mismatch is a hard
  ``image_hash_mismatch`` failure carrying *both* digests so the operator can
  see which side is wrong (PRD 7.4: "download failure must not continue with an
  unlabelled old package").
- **Create** the writable Data image as a raw sparse file, never overwriting an
  existing image unless the caller explicitly forces it.
- **Track the known-good pointer** (``artifacts/known-good.json``) that PRD 8.2
  step 6 says may only be updated after a gate passes.
- **Back up and restore** checkpoints.  Restoring never writes into the user's
  working directory in place: it materialises a *new* directory (PRD 7.2).

Everything is stdlib-only, chunked (so multi-GiB images do not need RAM), and
raises :class:`~unipad.errors.UniPadError` with a stable code on failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from unipad.errors import ErrorCode, UniPadError, blocked

__all__ = [
    "ImageManager",
    "KnownGoodPointer",
    "ScopeRequired",
    "declare_scope",
    "read_scope",
    "sha256_file",
]

#: Default chunk size for hashing (1 MiB): small enough to report progress,
#: large enough to keep the syscall count low on multi-GiB images.
DEFAULT_CHUNK_SIZE = 1 << 20

#: Pointer file name inside the artifact root (config/machine.json).
KNOWN_GOOD_FILENAME = "known-good.json"

#: Schema version of ``known-good.json``.
KNOWN_GOOD_SCHEMA_VERSION = 1

#: Schema version of a checkpoint ``manifest.json``.  2 adds the mandatory
#: scope declaration (PRD 3.2 P1 (5)); 1 predates it, so a reader that meets a
#: version-1 manifest knows it cannot state what it covers.
CHECKPOINT_SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# checkpoint scope (PRD 3.2 P1 (5))
#
# P1 (5) reads: 真正经过验证的快照/恢复；**必须说明保存的是磁盘还是整机状态**.
#
# The second clause is the requirement, and it is the one that is easy to miss.
# A restore that silently covers one disk file looks identical to a restore
# that covers the whole machine until someone relies on it: an operator who
# believes a checkpoint captured the machine and gets only a disk back has lost
# state they were told was safe.
#
# So the scope is a *declared field*, not something a reader infers from the
# entry list, and an absent scope is refused rather than defaulted -- assuming
# the narrow reading would let an operator believe they restored less than they
# did, and assuming the wide one would let them believe they restored more.
# ---------------------------------------------------------------------------


class ScopeRequired:
    """The closed vocabulary of checkpoint scopes."""

    #: These image files, and nothing else.
    DISK = "disk"
    #: The whole runnable state, which is strictly more.
    MACHINE = "machine"

    ALL = (DISK, MACHINE)


#: What a ``disk`` checkpoint does *not* cover.  Naming the exclusions is the
#: useful half of the declaration: it is what tells an operator that guest RAM,
#: device state and the install history are not in here.
DISK_SCOPE_EXCLUDES: tuple[str, ...] = (
    "guest_ram",
    "device_state",
    "fw_cfg",
    "install_history",
    "network_state",
)


def declare_scope(scope: str, *, covered: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """Return the manifest fields that declare what a checkpoint covers.

    A ``machine`` scope must name what it covers: a manifest that claims the
    whole machine while listing one image file is a lie the reader cannot
    detect, which is worse than claiming ``disk``.

    Raises:
        ValueError: for a scope outside the vocabulary, or a ``machine`` scope
            with no ``covered`` list.
    """
    if scope not in ScopeRequired.ALL:
        raise ValueError(
            f"unknown checkpoint scope {scope!r}; expected one of {list(ScopeRequired.ALL)}"
        )
    if scope == ScopeRequired.MACHINE and not covered:
        raise ValueError(
            "machine scope requires the list of state it covers "
            "(e.g. images, guest_ram, device_state, install_history)"
        )
    declaration: dict[str, Any] = {
        "scope": scope,
        "scope_note": (
            "This checkpoint covers disk images only. Guest RAM, device state, "
            "the fw_cfg geometry, network state and the install history are NOT "
            "in it; restoring it does not roll those back."
            if scope == ScopeRequired.DISK
            else "This checkpoint covers the whole runnable machine state named "
            "in scope_includes."
        ),
    }
    if scope == ScopeRequired.DISK:
        declaration["scope_excludes"] = list(DISK_SCOPE_EXCLUDES)
    else:
        declaration["scope_includes"] = list(covered)
    return declaration


def read_scope(document: Any) -> Optional[dict[str, Any]]:
    """Return the scope declaration of a checkpoint manifest.

    ``None`` means *there is no manifest to read* -- which is not the same as a
    manifest that failed to declare.  A checkpoint directory holding images but
    no manifest is restored and reported unverified (PRD 9.2: an unverifiable
    restore is reported, never passed off as ok); a manifest that exists and
    does not say what it covers is refused, because that is a claim someone
    made and got wrong.

    Raises:
        UniPadError: ``artifact_manifest_invalid`` when a manifest is present
            but its scope is absent or outside the vocabulary.
    """
    if document is None:
        return None
    if not isinstance(document, dict):
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "checkpoint manifest must be a JSON object",
            manifest_type=type(document).__name__,
        )
    scope = document.get("scope")
    if not scope:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "checkpoint manifest does not declare its scope; PRD 3.2 P1 (5) "
            "requires a checkpoint to state whether it saved disk or "
            "whole-machine state",
            expected=list(ScopeRequired.ALL),
        )
    if scope not in ScopeRequired.ALL:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "checkpoint manifest declares an unknown scope",
            scope=scope,
            expected=list(ScopeRequired.ALL),
        )
    return {
        "scope": scope,
        "scope_note": str(document.get("scope_note", "")),
        "scope_excludes": list(document.get("scope_excludes", [])),
        "scope_includes": list(document.get("scope_includes", [])),
    }

#: Progress callback signature: ``(bytes_done, bytes_total)``.
ProgressCallback = Callable[[int, int], None]

PathLike = Union[str, Path]


def sha256_file(
    path: PathLike,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    progress: Optional[ProgressCallback] = None,
) -> str:
    """Return the hex SHA-256 of a file, read in bounded chunks.

    Args:
        path: File to hash.
        chunk_size: Read size in bytes (default 1 MiB).
        progress: Optional callback invoked after each chunk with
            ``(bytes_done, bytes_total)``.  Exceptions from the callback are
            propagated: a broken progress reporter must not silently produce a
            wrong hash.

    Returns:
        The lowercase hex digest.

    Raises:
        UniPadError: ``image_not_found`` when the path is missing or is not a
            regular file, ``artifact_manifest_invalid`` for an unusable
            ``chunk_size``.
    """
    if chunk_size <= 0:
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "chunk_size must be a positive number of bytes",
            chunk_size=chunk_size,
        )
    target = Path(path)
    if not target.exists():
        raise UniPadError(
            ErrorCode.IMAGE_NOT_FOUND,
            "file to hash does not exist",
            path=str(target),
        )
    if not target.is_file():
        raise UniPadError(
            ErrorCode.IMAGE_NOT_FOUND,
            "path to hash is not a regular file",
            path=str(target),
        )
    total = target.stat().st_size
    digest = hashlib.sha256()
    done = 0
    with open(target, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    return digest.hexdigest()


class KnownGoodPointer:
    """The ``artifacts/known-good.json`` record of last-known-bootable builds.

    PRD 8.2 step 6: the local bootable-version pointer is only updated after a
    gate passes, and it is deliberately separate from source branch management
    (PRD 7.2).

    The caller supplies every ``path``/``sha256``/``timestamp``; this class
    never invents a digest and never stamps its own clock unless asked to.
    """

    def __init__(self, artifact_root: PathLike) -> None:
        """Remember the artifact root that owns the pointer file."""
        self.artifact_root = Path(artifact_root)

    @property
    def path(self) -> Path:
        """Absolute path of the pointer file."""
        return self.artifact_root / KNOWN_GOOD_FILENAME

    def load(self) -> dict[str, Any]:
        """Load the pointer file.

        Returns:
            The parsed document, or ``{"schema_version": 1, "entries": []}``
            when the file does not exist yet (a fresh clone has no pointer).

        Raises:
            UniPadError: ``artifact_manifest_invalid`` when the file exists but
                is not a JSON object with ``schema_version`` 1.
        """
        if not self.path.exists():
            return {
                "schema_version": KNOWN_GOOD_SCHEMA_VERSION,
                "entries": [],
                "note": "no known-good pointer recorded yet",
            }
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "known-good pointer is unreadable",
                path=str(self.path),
                reason=type(exc).__name__,
            ) from exc
        if not isinstance(document, dict):
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "known-good pointer must be a JSON object",
                path=str(self.path),
            )
        if document.get("schema_version") != KNOWN_GOOD_SCHEMA_VERSION:
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "known-good pointer has an unsupported schema_version",
                path=str(self.path),
                schema_version=document.get("schema_version"),
                expected=KNOWN_GOOD_SCHEMA_VERSION,
            )
        if not isinstance(document.get("entries", []), list):
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "known-good pointer entries must be a list",
                path=str(self.path),
            )
        return document

    def save(
        self,
        entries: list[dict[str, Any]],
        note: str,
        *,
        timestamp: Optional[str] = None,
    ) -> Path:
        """Write the pointer file.

        Args:
            entries: Pointer entries supplied by the caller.  Each entry is
                expected to carry ``path``, ``sha256`` and its own timestamp;
                they are stored verbatim so the caller stays the source of
                truth for what was actually verified.
            note: Human-readable provenance (experiment id, gate, operator).
            timestamp: Optional ISO-8601 stamp for the pointer itself.  When
                omitted a UTC stamp is generated; callers that need
                reproducibility should pass their own.

        Returns:
            The path written.

        Raises:
            UniPadError: ``artifact_manifest_invalid`` when ``entries`` is not a
                list of objects, or when any entry lacks ``path``/``sha256``.
        """
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "known-good entries must be a list of objects",
                received_type=type(entries).__name__,
            )
        for index, entry in enumerate(entries):
            for field in ("path", "sha256"):
                if not isinstance(entry.get(field), str) or not entry[field].strip():
                    raise UniPadError(
                        ErrorCode.ARTIFACT_MANIFEST_INVALID,
                        f"known-good entry {index} is missing a non-empty {field!r}",
                        entry_index=index,
                    )
        stamp = timestamp or _utc_now_iso()
        document = {
            "schema_version": KNOWN_GOOD_SCHEMA_VERSION,
            "updated_at": stamp,
            "note": note,
            "entries": [dict(entry) for entry in entries],
        }
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return self.path


class ImageManager:
    """Verify, create, back up and restore disk images under an artifact root.

    Args:
        artifact_root: Root directory for build artifacts
            (``config/machine.json`` ``disks.artifact_root``).  Created on
            demand; never assumed to exist.
    """

    def __init__(self, artifact_root: PathLike) -> None:
        self.artifact_root = Path(artifact_root)

    # -- hashing ---------------------------------------------------------

    def sha256_file(
        self,
        path: PathLike,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        progress: Optional[ProgressCallback] = None,
    ) -> str:
        """Chunked SHA-256 of ``path`` (see :func:`sha256_file`)."""
        return sha256_file(path, chunk_size=chunk_size, progress=progress)

    def verify_image(
        self,
        path: PathLike,
        expected_sha256: str,
        *,
        role: Optional[str] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> dict[str, Any]:
        """Hash ``path`` and compare it against ``expected_sha256``.

        Args:
            path: Image to verify.
            expected_sha256: Expected lowercase hex digest.
            role: Optional artifact role recorded in the result
                (``firmware`` / ``kernel`` / ``system_image`` / ``data_image``).
            progress: Optional chunk progress callback.

        Returns:
            ``{"path", "sha256", "expected_sha256", "size_bytes", "role", "ok"}``
            with ``ok`` always ``True`` (a mismatch raises).

        Raises:
            UniPadError: ``image_not_found`` when the file is missing,
                ``config_invalid`` when ``expected_sha256`` is not a 64-character
                hex digest (a caller bug, not a manifest problem),
                ``image_hash_mismatch`` with both digests in ``details`` when
                they differ.
        """
        expected = (expected_sha256 or "").strip().lower()
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "expected_sha256 must be a 64-character lowercase hex digest",
                expected_sha256=expected_sha256,
            )
        actual = self.sha256_file(path, progress=progress)
        if actual != expected:
            raise UniPadError(
                ErrorCode.IMAGE_HASH_MISMATCH,
                "image hash does not match the expected digest",
                path=str(Path(path)),
                role=role,
                expected_sha256=expected,
                actual_sha256=actual,
                size_bytes=Path(path).stat().st_size if Path(path).exists() else None,
            )
        return {
            "path": str(Path(path)),
            "sha256": actual,
            "expected_sha256": expected,
            "size_bytes": Path(path).stat().st_size,
            "role": role,
            "ok": True,
        }

    # -- creation --------------------------------------------------------

    def create_data_image(
        self,
        path: PathLike,
        size_mb: int,
        *,
        force: bool = False,
        timestamp: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a raw sparse Data image of ``size_mb`` mebibytes.

        The file is created with :func:`os.truncate`, which on NTFS leaves the
        unwritten ranges unallocated (sparse) instead of writing zeros.

        Args:
            path: Destination image path.
            size_mb: Size in MiB (must be > 0).
            force: Overwrite an existing file.  Without it an existing image is
                **never** touched: user data outranks convenience (PRD 7.2).
            timestamp: Optional ISO-8601 creation stamp recorded in the result.

        Returns:
            ``{"path", "size_bytes", "size_mb", "created", "existed_before",
            "sha256", "timestamp"}``.

        Raises:
            UniPadError: ``blocked`` when the file exists and ``force`` is not
                set (details carry the existing size), ``config_invalid`` for a
                non-positive ``size_mb``, ``persistence_failed`` when the file
                cannot be written, ``verification_failed`` when the created file
                does not have the requested size.
        """
        if not isinstance(size_mb, int) or isinstance(size_mb, bool) or size_mb <= 0:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "size_mb must be a positive integer number of mebibytes",
                size_mb=size_mb,
            )
        target = Path(path)
        existed_before = target.exists()
        if existed_before and not force:
            raise blocked(
                "refusing to overwrite an existing image; pass force=True to replace it",
                path=str(target),
                existing_size_bytes=target.stat().st_size,
                requested_size_bytes=size_mb * 1024 * 1024,
                policy="PRD 7.2: never overwrite existing user data by default",
            )
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(target, "wb") as handle:
                handle.truncate(size_mb * 1024 * 1024)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise UniPadError(
                ErrorCode.PERSISTENCE_FAILED,
                "could not create the data image",
                path=str(target),
                size_mb=size_mb,
                reason=type(exc).__name__,
            ) from exc
        size_bytes = target.stat().st_size
        if size_bytes != size_mb * 1024 * 1024:
            raise UniPadError(
                ErrorCode.VERIFICATION_FAILED,
                "data image has an unexpected size after creation",
                path=str(target),
                expected_bytes=size_mb * 1024 * 1024,
                actual_bytes=size_bytes,
            )
        return {
            "path": str(target),
            "size_bytes": size_bytes,
            "size_mb": size_mb,
            "created": not existed_before,
            "existed_before": existed_before,
            "sha256": self.sha256_file(target),
            "timestamp": timestamp or _utc_now_iso(),
        }

    # -- checkpoints (FR10) ---------------------------------------------

    def backup(
        self,
        path: PathLike,
        backup_dir: PathLike,
        *,
        timestamp: Optional[str] = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Copy ``path`` into ``backup_dir`` and hash the copy.

        A ``manifest.json`` is written next to the copy recording the source,
        digest and size so :meth:`restore_checkpoint` can prove it restored the
        right bytes.

        Args:
            path: File to back up.
            backup_dir: Destination directory (created on demand).
            timestamp: Optional ISO-8601 stamp recorded in the manifest.
            note: Optional provenance note.

        Returns:
            ``{"path", "backup_path", "sha256", "size_bytes", "manifest",
            "timestamp"}``.

        Raises:
            UniPadError: ``image_not_found`` when the source is missing.
        """
        source = Path(path)
        if not source.is_file():
            raise UniPadError(
                ErrorCode.IMAGE_NOT_FOUND,
                "backup source does not exist",
                path=str(source),
            )
        destination_dir = Path(backup_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source.name
        if destination.exists():
            raise blocked(
                "backup destination already exists; choose a fresh checkpoint directory",
                path=str(destination),
                policy="PRD 7.2: a checkpoint must not silently replace an earlier one",
            )
        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "could not copy the image into the backup directory",
                path=str(source),
                backup_dir=str(destination_dir),
                reason=type(exc).__name__,
            ) from exc
        digest = self.sha256_file(destination)
        stamp = timestamp or _utc_now_iso()
        manifest_path = destination_dir / "manifest.json"
        # PRD 3.2 P1 (5): a checkpoint must say what it covers.  backup() copies
        # files, so the honest declaration is ``disk`` -- and the exclusions are
        # named, because that is the half an operator needs.
        scope_fields = declare_scope(ScopeRequired.DISK)
        manifest = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "created_at": stamp,
            "note": note,
            "entries": [
                {
                    "path": str(source),
                    "backup_path": str(destination),
                    "sha256": digest,
                    "size_bytes": destination.stat().st_size,
                }
            ],
            **scope_fields,
        }
        _merge_manifest(manifest_path, manifest["entries"], stamp, note, scope_fields)
        return {
            "path": str(source),
            "backup_path": str(destination),
            "sha256": digest,
            "size_bytes": destination.stat().st_size,
            "manifest": str(manifest_path),
            "timestamp": stamp,
            **scope_fields,
        }

    def restore_checkpoint(
        self,
        backup_dir: PathLike,
        *,
        target_dir: Optional[PathLike] = None,
        allow_existing: bool = False,
        timestamp: Optional[str] = None,
    ) -> dict[str, Any]:
        """Restore a checkpoint into a **new** directory.

        PRD 7.2 requires that disk recovery never overwrites the user's working
        directory in place, so the restore target is always a fresh directory:
        either caller-supplied and empty, or derived from the backup directory
        name plus the restore timestamp.

        Args:
            backup_dir: Directory produced by :meth:`backup`.
            target_dir: Explicit restore destination.  Must not exist, or must
                be an empty directory unless ``allow_existing`` is set.
            allow_existing: Permit restoring into an existing directory.
            timestamp: Optional ISO-8601 stamp used to derive the default
                target directory name.

        Returns:
            ``{"backup_dir", "target_dir", "restored": [...], "verified": bool,
            "skipped": [...], "timestamp"}``.

        Raises:
            UniPadError: ``image_not_found`` when the backup directory is
                missing, ``blocked`` when the target already holds files,
                ``restore_failed`` when a restored file does not match the
                manifest digest.
        """
        source_dir = Path(backup_dir)
        if not source_dir.is_dir():
            raise UniPadError(
                ErrorCode.IMAGE_NOT_FOUND,
                "checkpoint directory does not exist",
                backup_dir=str(source_dir),
            )
        stamp = timestamp or _utc_now_iso()
        if target_dir is None:
            destination = source_dir.parent / f"restored-{_slug(stamp)}"
        else:
            destination = Path(target_dir)
            if destination.exists() and any(destination.iterdir()) and not allow_existing:
                raise blocked(
                    "restore target is not empty; refusing to overwrite it",
                    target_dir=str(destination),
                    policy="PRD 7.2: restore into a new directory, never in place",
                )
        destination.mkdir(parents=True, exist_ok=True)

        manifest_path = source_dir / "manifest.json"
        entries = _read_manifest_entries(manifest_path)
        expected = {Path(e["backup_path"]).name: e for e in entries if isinstance(e, dict)}
        # PRD 3.2 P1 (5): refuse to restore a checkpoint that does not say what
        # it covers.  This is the gate that stops an operator from believing
        # they got the machine back when they got a disk.
        manifest_document, manifest_error = _read_manifest_document(manifest_path)
        if manifest_error is not None:
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "checkpoint manifest is unreadable; refusing to restore from a "
                "checkpoint whose contents cannot be verified",
                path=str(manifest_path),
                reason=manifest_error,
            )
        scope = read_scope(manifest_document)
        if scope is None:
            # No manifest at all: PRD 9.2 says report it, do not refuse it and
            # do not call it verified.  The scope is explicitly unknown rather
            # than silently assumed to be the narrow one.
            scope = {
                "scope": "unknown",
                "scope_note": (
                    "This checkpoint has no manifest, so what it covers cannot "
                    "be determined; every restored file is reported unverified."
                ),
                "scope_excludes": [],
                "scope_includes": [],
            }

        restored: list[dict[str, Any]] = []
        skipped: list[str] = []
        for candidate in sorted(source_dir.iterdir()):
            if not candidate.is_file() or candidate.name == "manifest.json":
                continue
            target = destination / candidate.name
            try:
                shutil.copy2(candidate, target)
            except OSError as exc:
                raise UniPadError(
                    ErrorCode.RESTORE_FAILED,
                    "could not copy a checkpoint file back",
                    path=str(candidate),
                    target_dir=str(destination),
                    reason=type(exc).__name__,
                ) from exc
            record = expected.get(candidate.name)
            actual = self.sha256_file(target)
            if record and record.get("sha256") and record["sha256"] != actual:
                raise UniPadError(
                    ErrorCode.RESTORE_FAILED,
                    "restored image does not match the checkpoint digest",
                    path=str(target),
                    expected_sha256=record.get("sha256"),
                    actual_sha256=actual,
                )
            restored.append(
                {
                    "name": candidate.name,
                    "path": str(target),
                    "sha256": actual,
                    "size_bytes": target.stat().st_size,
                    "verified": bool(record and record.get("sha256") == actual),
                }
            )
        if not restored:
            skipped.append(f"no image files found in {source_dir}")
        return {
            **scope,
            "backup_dir": str(source_dir),
            "target_dir": str(destination),
            "restored": restored,
            "verified": all(item["verified"] for item in restored) if restored else False,
            "skipped": skipped,
            "timestamp": stamp,
        }

    # -- pointer ---------------------------------------------------------

    def known_good(self) -> KnownGoodPointer:
        """Return the :class:`KnownGoodPointer` bound to the artifact root."""
        return KnownGoodPointer(self.artifact_root)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the manager configuration."""
        return {"artifact_root": str(self.artifact_root)}


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    import datetime as _dt

    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _slug(value: str) -> str:
    """Turn a timestamp into a filesystem-safe slug."""
    return "".join(c if c.isalnum() else "-" for c in value).strip("-") or "restore"


def _read_manifest_document(manifest_path: Path) -> tuple[Optional[Any], Optional[str]]:
    """Read the whole backup manifest.

    Returns ``(document, error)``.  ``document`` is ``None`` when there is no
    manifest to read; ``error`` names *why* it could not be read when the file
    exists but is broken.  The two are deliberately distinguished: an absent
    manifest means "nothing was claimed", while an unreadable one means "a claim
    was made and cannot be checked" -- the second must not pass as the first.
    """
    if not manifest_path.is_file():
        return None, None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__


def _read_manifest_entries(manifest_path: Path) -> list[dict[str, Any]]:
    """Read the ``entries`` list of a backup manifest (empty when absent)."""
    if not manifest_path.is_file():
        return []
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _merge_manifest(
    manifest_path: Path,
    entries: list[dict[str, Any]],
    stamp: str,
    note: str,
    scope_fields: Optional[dict[str, Any]] = None,
) -> None:
    """Append ``entries`` to an existing manifest, keeping history.

    ``scope_fields`` is re-applied on every merge rather than inherited from
    what is already on disk: a manifest written before the scope existed would
    otherwise keep a *new* checkpoint undeclared, and the checkpoint being
    written now is the one an operator will rely on.
    """
    document: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                document = loaded
        except (OSError, json.JSONDecodeError):
            document = {}
    history = document.get("entries")
    if not isinstance(history, list):
        history = []
    document["schema_version"] = CHECKPOINT_SCHEMA_VERSION
    document["updated_at"] = stamp
    if note:
        document["note"] = note
    document["entries"] = [*history, *entries]
    if scope_fields is not None:
        document.update(scope_fields)
    manifest_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
