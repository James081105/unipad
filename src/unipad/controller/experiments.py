"""Experiment journal store (PRD sections 7.2 and 7.3).

Every experiment is one directory under the store root (default ``runs``):

.. code-block:: text

    runs/<experiment-id>/experiment.json     # PRD 7.3 format, verbatim
    runs/<experiment-id>/<evidence files>    # logs, screenshots, manifests

The journal format is *exactly* :class:`unipad.models.ExperimentRecord`
(PRD 7.3).  No extra keys are written into ``experiment.json`` so the file
stays diffable against the PRD; machine-readable run state that the PRD asks
to keep separately (phase pointer, budget, disk copies) lives in the other
controller modules, not inside the journal.

Error-code contract (documented decision)
-----------------------------------------
* missing experiment record      -> ``ErrorCode.BLOCKED``
  (``ARTIFACT_MISSING`` is reserved for build/run artifacts per PRD 8.1/9.2;
  a missing journal is a controller-state problem, not a missing artifact.)
* referenced evidence file absent -> ``ErrorCode.ARTIFACT_MISSING``
  (the evidence *is* the artifact; recording a path that does not exist would
  let missing evidence be laundered into "verified", PRD 9.2.)
* malformed id / record / status  -> ``ErrorCode.CONFIG_INVALID`` (usage)
* overwriting a finished record   -> ``ErrorCode.BLOCKED`` (evidence
  preservation, PRD 7.2: "实验失败时保留修改和日志")

Evidence preservation
---------------------
:meth:`ExperimentStore.save` refuses to overwrite an existing record whose
status is no longer ``planned``: that is how a failure journal survives a
careless full-record rewrite.  Status moves and *additive* appends
(:meth:`append_change`, :meth:`append_evidence`) go through the sanctioned
paths, which re-read the record from disk before writing so a stale in-memory
copy can never truncate ``changes`` or ``evidence``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..errors import ErrorCode, UniPadError
from ..models import ExperimentRecord, ExperimentStatus

__all__ = ["ExperimentStore", "StoreScan", "EXPERIMENT_FILENAME", "ID_PATTERN"]

#: File name of the journal inside a run directory (PRD 7.3 / 9.2).
EXPERIMENT_FILENAME = "experiment.json"

#: Experiment ids become directory names: keep them boring and traversal-free.
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_KNOWN_STATUSES = (
    ExperimentStatus.PLANNED,
    ExperimentStatus.RUNNING,
    ExperimentStatus.PASSED,
    ExperimentStatus.FAILED,
    ExperimentStatus.BLOCKED,
)


class StoreScan:
    """Result of scanning the store root.

    ``records`` holds every readable journal; ``errors`` holds one entry per
    unreadable or malformed journal so a broken run is surfaced instead of
    silently disappearing from :meth:`ExperimentStore.list`.
    """

    __slots__ = ("records", "errors")

    def __init__(
        self,
        records: list[ExperimentRecord] | None = None,
        errors: list[dict[str, str]] | None = None,
    ) -> None:
        self.records: list[ExperimentRecord] = records or []
        self.errors: list[dict[str, str]] = errors or []

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the scan."""
        return {
            "records": [record.to_dict() for record in self.records],
            "errors": list(self.errors),
        }


def _validate_id(experiment_id: str) -> str:
    """Return ``experiment_id`` unchanged, or raise for an unusable id.

    Ids are used as directory names, so path separators, ``..`` and absolute
    paths are rejected rather than normalised (Windows-correct: both ``/`` and
    ``\\`` are separators here).
    """
    if not isinstance(experiment_id, str) or not ID_PATTERN.match(experiment_id):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "invalid experiment id",
            experiment_id=repr(experiment_id),
            expected=f"1-128 chars matching {ID_PATTERN.pattern}",
        )
    if experiment_id in {".", ".."}:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "invalid experiment id",
            experiment_id=experiment_id,
        )
    return experiment_id


def _relativise(path: Path, run_dir: Path) -> str:
    """Return ``path`` relative to ``run_dir`` (POSIX form) when inside it."""
    try:
        return path.resolve().relative_to(run_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


class ExperimentStore:
    """Filesystem-backed experiment journal (PRD 7.3).

    Parameters
    ----------
    root:
        Store root directory (default ``runs``).  Created lazily; a missing
        root is not an error until a record is actually written.
    """

    __slots__ = ("_root",)

    def __init__(self, root: str | Path = "runs") -> None:
        self._root = Path(root)

    # --- locations ---------------------------------------------------------
    @property
    def root(self) -> Path:
        """Store root directory."""
        return self._root

    def dir_for(self, experiment_id: str) -> Path:
        """Return the run directory for ``experiment_id`` (validated, not created)."""
        return self._root / _validate_id(experiment_id)

    def path_for(self, experiment_id: str) -> Path:
        """Return the ``experiment.json`` path for ``experiment_id``."""
        return self.dir_for(experiment_id) / EXPERIMENT_FILENAME

    def exists(self, experiment_id: str) -> bool:
        """Return ``True`` when a journal exists for ``experiment_id``."""
        return self.path_for(experiment_id).is_file()

    # --- create / load / save ---------------------------------------------
    def create(
        self,
        experiment_id: str,
        milestone: str,
        hypothesis: str,
        **fields: Any,
    ) -> ExperimentRecord:
        """Create a new journal and return it.

        Parameters
        ----------
        experiment_id:
            Caller-assigned unique id (``exp-<unique-id>`` by convention).
        milestone:
            Milestone the experiment targets, e.g. ``M4``.
        hypothesis:
            One falsifiable hypothesis (PRD 7.3).  Must be non-empty.
        **fields:
            Optional :class:`ExperimentRecord` fields (``base_commit``,
            ``source_lock_sha256``, ``runtime`` ...).

        Raises
        ------
        UniPadError
            ``CONFIG_INVALID`` for an unusable id / empty milestone or
            hypothesis; ``BLOCKED`` when the id already exists (a second
            ``create`` would discard the first experiment's evidence).
        """
        _validate_id(experiment_id)
        if not milestone or not str(milestone).strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "experiment milestone is required",
                experiment_id=experiment_id,
            )
        if not hypothesis or not str(hypothesis).strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "experiment hypothesis is required (PRD 7.3: one falsifiable hypothesis)",
                experiment_id=experiment_id,
            )
        target = self.path_for(experiment_id)
        if target.exists():
            raise UniPadError(
                ErrorCode.BLOCKED,
                "experiment already exists; create a new id instead of overwriting evidence",
                experiment_id=experiment_id,
                path=target.as_posix(),
            )
        record = ExperimentRecord(
            experiment_id=experiment_id,
            milestone=str(milestone),
            hypothesis=str(hypothesis),
            **fields,
        )
        self._write(record)
        return record

    def load(self, experiment_id: str) -> ExperimentRecord:
        """Load and return the journal for ``experiment_id``.

        Raises
        ------
        UniPadError
            ``BLOCKED`` when no journal exists, ``CONFIG_INVALID`` when the
            file is not valid JSON or lacks the required fields.
        """
        path = self.path_for(experiment_id)
        if not path.is_file():
            raise UniPadError(
                ErrorCode.BLOCKED,
                "experiment record not found",
                experiment_id=experiment_id,
                path=path.as_posix(),
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "experiment record is not valid JSON",
                experiment_id=experiment_id,
                path=path.as_posix(),
                error=str(exc),
            ) from exc
        if not isinstance(raw, dict):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "experiment record is not a JSON object",
                experiment_id=experiment_id,
                path=path.as_posix(),
            )
        try:
            record = ExperimentRecord.from_dict(raw)
        except TypeError as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "experiment record is missing required fields",
                experiment_id=experiment_id,
                path=path.as_posix(),
                error=str(exc),
            ) from exc
        if not record.experiment_id or not record.milestone:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "experiment record lacks experiment_id or milestone",
                experiment_id=experiment_id,
                path=path.as_posix(),
            )
        return record

    def save(self, record: ExperimentRecord) -> ExperimentRecord:
        """Persist ``record`` verbatim.

        Refuses to overwrite an existing journal whose status is no longer
        ``planned``: that is the evidence-preservation rule of PRD 7.2.  Use
        :meth:`set_status`, :meth:`append_change` and :meth:`append_evidence`
        to advance a running experiment.
        """
        self._validate_record(record)
        path = self.path_for(record.experiment_id)
        if path.is_file():
            existing = self.load(record.experiment_id)
            if existing.status != ExperimentStatus.PLANNED:
                raise UniPadError(
                    ErrorCode.BLOCKED,
                    "refusing to overwrite a non-planned experiment record "
                    "(evidence preservation, PRD 7.2); use set_status/append_*",
                    experiment_id=record.experiment_id,
                    path=path.as_posix(),
                    existing_status=existing.status,
                )
        self._write(record)
        return record

    # --- additive updates --------------------------------------------------
    def append_change(self, experiment_id: str, text: str) -> ExperimentRecord:
        """Append one change description to ``changes`` and return the record.

        Appending is additive, so it is allowed on a running or failed
        experiment: the failure journal must keep accumulating its changes
        (PRD 7.2).  The record is re-read from disk first, so concurrent
        full-record writes cannot truncate earlier entries.
        """
        if not text or not str(text).strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "change text is required",
                experiment_id=experiment_id,
            )
        record = self.load(experiment_id)
        record.changes.append(str(text))
        return self._write(record)

    def append_evidence(
        self,
        experiment_id: str,
        path: str | Path,
        require_exists: bool = True,
    ) -> ExperimentRecord:
        """Append one evidence path to ``evidence`` and return the record.

        Parameters
        ----------
        require_exists:
            When ``True`` (default) the referenced file must exist, resolved
            against the run directory when relative.  A path that does not
            exist raises ``ARTIFACT_MISSING`` so a journal cannot claim
            evidence that is not on disk (PRD 9.2).
        """
        record = self.load(experiment_id)
        run_dir = self.dir_for(experiment_id)
        candidate = Path(path)
        resolved = candidate if candidate.is_absolute() else run_dir / candidate
        stored = _relativise(resolved, run_dir)
        if require_exists and not resolved.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "evidence file not found",
                experiment_id=experiment_id,
                path=stored,
                run_dir=run_dir.as_posix(),
            )
        record.evidence.append(stored)
        return self._write(record)

    def set_status(
        self,
        experiment_id: str,
        status: str,
        result: str | None = None,
        next_action: str | None = None,
    ) -> ExperimentRecord:
        """Move the journal to ``status`` and return the updated record.

        This is the sanctioned status path: it re-reads the record from disk,
        touches only ``status``/``result``/``next_action`` and writes it back,
        so ``changes`` and ``evidence`` are never truncated.  ``result`` and
        ``next_action`` are only overwritten when explicitly provided.
        """
        if status not in _KNOWN_STATUSES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown experiment status",
                experiment_id=experiment_id,
                status=status,
                known_statuses=list(_KNOWN_STATUSES),
            )
        record = self.load(experiment_id)
        record.status = status
        if result is not None:
            record.result = result
        if next_action is not None:
            record.next_action = next_action
        return self._write(record)

    # --- listing -----------------------------------------------------------
    def scan(self) -> StoreScan:
        """Scan the store root and return every readable journal plus errors."""
        scan = StoreScan()
        if not self._root.is_dir():
            return scan
        for child in sorted(self._root.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            journal = child / EXPERIMENT_FILENAME
            if not journal.is_file():
                continue
            try:
                scan.records.append(self.load(child.name))
            except UniPadError as exc:
                scan.errors.append(
                    {
                        "experiment_id": child.name,
                        "path": journal.as_posix(),
                        "code": exc.code,
                        "message": exc.message,
                    }
                )
        scan.records.sort(key=lambda record: record.experiment_id)
        return scan

    def list(self) -> list[ExperimentRecord]:
        """Return every readable experiment record, sorted by id."""
        return self.scan().records

    # --- internals ---------------------------------------------------------
    def _validate_record(self, record: ExperimentRecord) -> None:
        if not isinstance(record, ExperimentRecord):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "expected an ExperimentRecord",
                got=type(record).__name__,
            )
        _validate_id(record.experiment_id)
        if not record.milestone:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "experiment milestone is required",
                experiment_id=record.experiment_id,
            )
        if record.status not in _KNOWN_STATUSES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown experiment status",
                experiment_id=record.experiment_id,
                status=record.status,
                known_statuses=list(_KNOWN_STATUSES),
            )

    def _write(self, record: ExperimentRecord) -> ExperimentRecord:
        """Atomically write ``record`` to ``runs/<id>/experiment.json``."""
        self._validate_record(record)
        target = self.path_for(record.experiment_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(record.to_json(), encoding="utf-8")
        temporary.replace(target)
        return record

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ExperimentStore(root={self._root.as_posix()!r})"
