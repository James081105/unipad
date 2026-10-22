"""Serial log tailing and the runtime evidence bundle (PRD 7.4, 9.2, FR09).

Two things live here:

- :class:`SerialLogTailer` follows the guest serial log without ever blocking a
  caller forever and without inventing lines that the guest has not produced.
- :class:`EvidenceBundle` assembles the PRD 9.2 minimum evidence set
  (``experiment.json``, ``host-inventory.json``, ``artifact-manifest.json``,
  actual QEMU args, serial log, guest log, QMP log, timestamped screenshots,
  ``verification.json``) into a :class:`unipad.models.EvidenceSet` plus an
  ``evidence.json`` file.

:meth:`EvidenceBundle.build` refuses to report a complete set:
:meth:`unipad.models.EvidenceSet.check_complete` is always run and the missing
items are recorded (and, by default, raised as ``evidence_incomplete``).  A
missing artefact is *unverified*, never silently filled in (PRD 9.2, appendix A).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from unipad import models
from unipad.errors import ErrorCode, UniPadError

__all__ = [
    "SerialLogTailer",
    "EvidenceBundle",
    "Screenshotter",
    "read_ppm_header",
    "sha256_file",
]

PathLike = Union[str, Path]


def sha256_file(path: PathLike, *, chunk_size: int = 1 << 20) -> str:
    """Return the hex SHA-256 of a file, or ``""`` when it cannot be read."""
    import hashlib

    target = Path(path)
    if not target.is_file():
        return ""
    digest = hashlib.sha256()
    try:
        with open(target, "rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


class SerialLogTailer:
    """Follow a growing text file and yield complete lines.

    Args:
        path: Log file to follow.  It may not exist yet -- :meth:`follow`
            waits for it, while :meth:`iter_lines` refuses to guess.
        from_end: Start at the end of the file (``tail -f`` semantics) instead
            of the beginning.  The starting position is captured when the
            tailer is **constructed**, not when it is first read, so output the
            guest produces in between is still delivered.  A file that does not
            exist yet is followed from its first byte once it appears, which is
            what makes a boot log readable.
        encoding: Text encoding used for decoding.
        poll_interval: Sleep between polls in :meth:`follow`.

    Note:
        Partial lines are buffered, never yielded: a half-written line is not
        guest evidence.
    """

    def __init__(
        self,
        path: PathLike,
        from_end: bool = True,
        encoding: str = "utf-8",
        poll_interval: float = 0.25,
    ) -> None:
        self.path = Path(path)
        self.from_end = bool(from_end)
        self.encoding = encoding
        self.poll_interval = max(float(poll_interval), 0.01)
        self._offset = 0
        self._buffer = ""
        self._closed = False
        self._missing_at_start = not self.path.is_file()
        if self.from_end and not self._missing_at_start:
            # Snapshot the end position now.  Doing this lazily on the first
            # read would silently drop everything the guest wrote between
            # construction and that read, which looks exactly like a hung boot.
            try:
                self._offset = self.path.stat().st_size
            except OSError:
                self._offset = 0

    def iter_lines(self) -> Iterator[str]:
        """Yield every complete line currently available.

        Raises:
            UniPadError: ``artifact_missing`` when the file does not exist.
                Silently yielding nothing would let a caller believe the guest
                produced no output (PRD 9.2).
        """
        if not self.path.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "serial log does not exist",
                path=str(self.path),
            )
        while not self._closed:
            produced = False
            for line in self._read_new_lines():
                produced = True
                yield line
            if not produced:
                return

    def __iter__(self) -> Iterator[str]:
        """Iterate the lines currently available (see :meth:`iter_lines`)."""
        return self.iter_lines()

    def read_lines(self, max_lines: Optional[int] = None) -> list[str]:
        """Return up to ``max_lines`` newly available lines (``None`` = all)."""
        collected: list[str] = []
        for line in self.iter_lines():
            collected.append(line)
            if max_lines is not None and len(collected) >= max_lines:
                break
        return collected

    def read_all(self) -> list[str]:
        """Return every line from the start of the file (ignores ``from_end``)."""
        if not self.path.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "serial log does not exist",
                path=str(self.path),
            )
        return self.path.read_text(encoding=self.encoding, errors="replace").splitlines()

    def follow(self, timeout: Optional[float] = None, *, stop_on_shutdown: bool = False) -> Iterator[str]:
        """Yield lines as they appear, waiting for the file to be created.

        Args:
            timeout: Total budget in seconds; ``None`` follows forever.
            stop_on_shutdown: Stop when a line containing ``"SHUTDOWN"`` or
                ``"System halted"`` is seen (a boot-loop guard, not a success
                signal).

        Yields:
            Complete lines in arrival order.
        """
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        if self._missing_at_start:
            # The log did not exist when this tailer was built -- the usual case
            # right before QEMU starts.  Wait for it, then read from its first
            # byte: the boot log is the evidence, not something to skip past.
            while not self.path.is_file():
                if self._closed:
                    return
                if deadline is not None and time.monotonic() >= deadline:
                    return
                time.sleep(self.poll_interval)
        while not self._closed:
            emitted = False
            for line in self._read_new_lines():
                emitted = True
                yield line
                if stop_on_shutdown and _looks_like_shutdown(line):
                    return
            if deadline is not None and time.monotonic() >= deadline:
                return
            if not emitted:
                time.sleep(self.poll_interval)

    def close(self) -> None:
        """Stop any in-flight :meth:`follow` iteration."""
        self._closed = True

    def __enter__(self) -> "SerialLogTailer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # -- internals -------------------------------------------------------

    def _read_new_lines(self) -> list[str]:
        """Read newly appended bytes and return the complete lines among them."""
        if not self.path.is_file():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            # The file was truncated or rotated: restart from the beginning.
            self._offset = 0
            self._buffer = ""
        if size == self._offset:
            return []
        lines: list[str] = []
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._offset)
                data = handle.read(size - self._offset)
        except OSError:
            return []
        self._offset += len(data)
        self._buffer += data.decode(self.encoding, errors="replace")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            lines.append(line.rstrip("\r"))
        return lines


def _looks_like_shutdown(line: str) -> bool:
    """Heuristic boot-loop guard used by :meth:`SerialLogTailer.follow`."""
    lowered = line.lower()
    return "shutdown" in lowered or "system halted" in lowered


def read_ppm_header(path: PathLike) -> dict[str, int]:
    """Parse a binary PPM (``P6``) header.

    Args:
        path: Screenshot produced by QMP ``screendump``.

    Returns:
        ``{"width", "height", "maxval", "header_bytes"}``.

    Raises:
        UniPadError: ``render_failed`` when the file is missing or is not a
            readable ``P6`` PPM.
    """
    target = Path(path)
    if not target.is_file():
        raise UniPadError(
            ErrorCode.RENDER_FAILED,
            "screenshot file does not exist",
            path=str(target),
        )
    try:
        with open(target, "rb") as handle:
            magic = handle.readline().strip()
            if magic != b"P6":
                raise UniPadError(
                    ErrorCode.RENDER_FAILED,
                    "screenshot is not a binary PPM (P6)",
                    path=str(target),
                    magic=magic[:16].decode("ascii", errors="replace"),
                )
            dimensions = handle.readline()
            while dimensions.startswith(b"#"):
                dimensions = handle.readline()
            parts = dimensions.split()
            if len(parts) < 2:
                raise UniPadError(
                    ErrorCode.RENDER_FAILED,
                    "screenshot PPM header has no dimensions",
                    path=str(target),
                )
            width, height = int(parts[0]), int(parts[1])
            maxval = int(handle.readline().strip())
            header_bytes = handle.tell()
    except UniPadError:
        raise
    except (OSError, ValueError) as exc:
        raise UniPadError(
            ErrorCode.RENDER_FAILED,
            "screenshot PPM header could not be parsed",
            path=str(target),
            reason=type(exc).__name__,
        ) from exc
    return {
        "width": width,
        "height": height,
        "maxval": maxval,
        "header_bytes": header_bytes,
    }


class Screenshotter:
    """Capture guest framebuffer dumps through QMP ``screendump``.

    Args:
        qmp: A connected :class:`unipad.host.qmp.QmpClient`.
        run_dir: Run directory; screenshots land in ``<run_dir>/screenshots/``.

    Note:
        A capture is only reported as successful when the PPM file exists, is
        non-empty and parses.  A QMP ``return`` with no file is a failure, not
        a screenshot.
    """

    def __init__(self, qmp: Any, run_dir: PathLike) -> None:
        self.qmp = qmp
        self.run_dir = Path(run_dir)
        self.screenshot_dir = self.run_dir / "screenshots"

    def capture(self, name: Optional[str] = None) -> dict[str, Any]:
        """Take one screenshot and return a structured result.

        Args:
            name: Optional base name (defaults to a monotonic sequence).

        Returns:
            ``{"ok", "path", "name", "bytes", "sha256", "width", "height",
            "timestamp", "error_code", "error_message"}``.  ``ok`` is ``False``
            with an ``error_code`` when the dump failed; nothing is claimed
            without a verified file on disk.
        """
        stamp = _utc_now_iso()
        base = name or f"screenshot-{int(time.time() * 1000)}"
        filename = f"{base}.ppm"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        target = self.screenshot_dir / filename
        result: dict[str, Any] = {
            "ok": False,
            "path": str(target),
            "name": filename,
            "bytes": None,
            "sha256": None,
            "width": None,
            "height": None,
            "timestamp": stamp,
            "error_code": None,
            "error_message": None,
        }
        try:
            dump = self.qmp.screendump(target)
        except UniPadError as exc:
            result["error_code"] = exc.code
            result["error_message"] = exc.message
            return result
        if not isinstance(dump, dict) or not dump.get("exists"):
            result["error_code"] = ErrorCode.RENDER_FAILED
            result["error_message"] = "qmp screendump reported success but no file was produced"
            return result
        size = target.stat().st_size if target.is_file() else 0
        if size <= 0:
            result["error_code"] = ErrorCode.RENDER_FAILED
            result["error_message"] = "screendump produced an empty file"
            return result
        try:
            header = read_ppm_header(target)
        except UniPadError as exc:
            result["error_code"] = exc.code
            result["error_message"] = exc.message
            return result
        result.update(
            {
                "ok": True,
                "bytes": size,
                "sha256": sha256_file(target),
                "width": header["width"],
                "height": header["height"],
            }
        )
        return result

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the screenshotter."""
        return {"run_dir": str(self.run_dir), "screenshot_dir": str(self.screenshot_dir)}


class EvidenceBundle:
    """Assemble the PRD 9.2 minimum runtime evidence set for one run.

    Args:
        run_dir: ``runs/<experiment-id>/`` directory; created on demand.
        run_id: Experiment identifier recorded in the evidence set.

    Example:
        >>> bundle = EvidenceBundle("runs/exp-1", "exp-1")      # doctest: +SKIP
        >>> bundle.add_qemu_args(args)                          # doctest: +SKIP
        >>> bundle.add_serial_log("runs/exp-1/serial.log")      # doctest: +SKIP
        >>> bundle.set_verification({"passed": True})           # doctest: +SKIP
        >>> bundle.build()                                      # doctest: +SKIP
    """

    def __init__(self, run_dir: PathLike, run_id: str = "") -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id or self.run_dir.name
        self._evidence = models.EvidenceSet(run_id=self.run_id)
        self._screenshots: list[dict[str, Any]] = []
        self._qmp_log_lines: list[str] = []
        self._notes: list[str] = []

    # -- builders --------------------------------------------------------

    def add_qemu_args(self, args: list[str]) -> Path:
        """Write the actual QEMU argv to ``qemu-args.txt`` and record it.

        Args:
            args: The argv passed to QEMU, one argument per line, so the file
                is diffable against a previous run.

        Returns:
            The path written.

        Raises:
            UniPadError: ``artifact_manifest_invalid`` when ``args`` is empty:
                an empty command line is not evidence of a run.
        """
        if not args:
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "refusing to record an empty qemu argument list",
            )
        target = self.run_dir / "qemu-args.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(str(a) for a in args) + "\n", encoding="utf-8")
        self._evidence.qemu_args = str(target)
        return target

    def add_serial_log(self, path: PathLike, *, copy: bool = True) -> Path:
        """Record the guest serial log, copying it into the run directory.

        Args:
            path: Serial log produced by QEMU's ``-serial file:``.
            copy: Copy into ``<run_dir>/serial.log`` so the evidence set is
                self-contained even if the run directory is archived alone.

        Returns:
            The recorded path (the copy when ``copy`` is set).

        Raises:
            UniPadError: ``artifact_missing`` when the log does not exist.
        """
        source = Path(path)
        if not source.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "serial log does not exist",
                path=str(source),
            )
        if copy:
            target = self.run_dir / "serial.log"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            recorded = target
        else:
            recorded = source
        self._evidence.serial_log = str(recorded)
        return recorded

    def add_guest_log(self, path: PathLike, *, copy: bool = True) -> Path:
        """Record the guest kernel/launchd log when one is obtainable.

        PRD 7.4 notes that upstream kernel logs may not reach the serial port,
        so the collector must support whatever channel the pinned build offers.
        """
        source = Path(path)
        if not source.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "guest log does not exist",
                path=str(source),
            )
        if copy:
            target = self.run_dir / "guest.log"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            recorded = target
        else:
            recorded = source
        self._evidence.guest_log = str(recorded)
        return recorded

    def add_qmp_log(self, text: str) -> Path:
        """Append QMP control-channel text to ``qmp.log`` and record it.

        Args:
            text: Raw QMP/HMP transcript (greeting, commands, replies).  It
                must never contain credentials; the controller is responsible
                for what it forwards.

        Returns:
            The path written.
        """
        target = self.run_dir / "qmp.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)
            if text and not text.endswith("\n"):
                handle.write("\n")
        self._qmp_log_lines.append(text)
        self._evidence.qmp_log = str(target)
        return target

    def add_screenshot(self, path: PathLike) -> Path:
        """Copy a screenshot into ``<run_dir>/screenshots/`` and record it.

        Args:
            path: Existing image file (PPM/PNG).

        Returns:
            The recorded path.

        Raises:
            UniPadError: ``artifact_missing`` when the file does not exist,
                ``render_failed`` when it is empty.
        """
        source = Path(path)
        if not source.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "screenshot does not exist",
                path=str(source),
            )
        if source.stat().st_size <= 0:
            raise UniPadError(
                ErrorCode.RENDER_FAILED,
                "screenshot is empty",
                path=str(source),
            )
        target_dir = self.run_dir / "screenshots"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        if target.resolve() != source.resolve():
            shutil.copy2(source, target)
        record = {
            "path": str(target),
            "name": source.name,
            "bytes": target.stat().st_size,
            "sha256": sha256_file(target),
        }
        self._screenshots.append(record)
        self._evidence.screenshots = [item["path"] for item in self._screenshots]
        return target

    def set_experiment_json(self, path: PathLike) -> Path:
        """Record the experiment journal (written by the controller)."""
        source = Path(path)
        if not source.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "experiment.json does not exist",
                path=str(source),
            )
        self._evidence.experiment_json = str(source)
        return source

    def set_host_inventory(self, path: PathLike) -> Path:
        """Record the host inventory (written by ``unipad doctor``)."""
        source = Path(path)
        if not source.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "host-inventory.json does not exist",
                path=str(source),
            )
        self._evidence.host_inventory = str(source)
        return source

    def set_artifact_manifest(self, path: PathLike) -> Path:
        """Record the build artifact manifest."""
        source = Path(path)
        if not source.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "artifact-manifest.json does not exist",
                path=str(source),
            )
        self._evidence.artifact_manifest = str(source)
        return source

    def set_verification(self, verification: dict[str, Any]) -> Path:
        """Write ``verification.json`` and record it.

        Args:
            verification: The verifier's machine-readable verdict.  It is
                stored verbatim; the bundle never upgrades a verdict.

        Returns:
            The path written.

        Raises:
            UniPadError: ``artifact_manifest_invalid`` when ``verification`` is
                not a non-empty mapping.
        """
        if not isinstance(verification, dict) or not verification:
            raise UniPadError(
                ErrorCode.ARTIFACT_MANIFEST_INVALID,
                "verification payload must be a non-empty object",
                received_type=type(verification).__name__,
            )
        target = self.run_dir / "verification.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(verification, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self._evidence.verification_json = str(target)
        return target

    def set_gpu_bridge_log(self, path: PathLike) -> Path:
        """Record the host GPU bridge log (required only when GPU is enabled)."""
        source = Path(path)
        if not source.is_file():
            raise UniPadError(
                ErrorCode.ARTIFACT_MISSING,
                "gpu bridge log does not exist",
                path=str(source),
            )
        self._evidence.gpu_bridge_log = str(source)
        return source

    def note(self, text: str) -> None:
        """Record a free-form note that is stored next to the evidence."""
        self._notes.append(text)

    # -- output ----------------------------------------------------------

    @property
    def evidence(self) -> models.EvidenceSet:
        """The live :class:`unipad.models.EvidenceSet` being assembled."""
        return self._evidence

    @property
    def missing(self) -> list[str]:
        """The required evidence items that are still absent.

        Mirrors :attr:`unipad.models.EvidenceSet.missing`, which is a list
        attribute, so callers never have to remember which spelling applies.
        """
        return [name for name in models.EvidenceSet.REQUIRED if getattr(self._evidence, name) in (None, "")]

    def build(self, *, strict: bool = True) -> models.EvidenceSet:
        """Finalise the evidence set and write ``evidence.json``.

        Args:
            strict: When ``True`` (default) an incomplete set raises
                ``evidence_incomplete``; when ``False`` the incomplete set is
                returned so the caller can record *why* it is incomplete.

        Returns:
            The completed :class:`unipad.models.EvidenceSet` with ``missing``
            populated by :meth:`unipad.models.EvidenceSet.check_complete`.

        Raises:
            UniPadError: ``evidence_incomplete`` when ``strict`` is set and a
                required item is absent.  The missing names are in ``details``.
        """
        missing = self._evidence.check_complete()
        document = self._evidence.to_dict()
        document["run_dir"] = str(self.run_dir)
        document["screenshots_detail"] = list(self._screenshots)
        document["notes"] = list(self._notes)
        document["missing"] = list(missing)
        target = self.run_dir / "evidence.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if missing and strict:
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "runtime evidence set is incomplete; the gate cannot pass",
                missing=missing,
                evidence_json=str(target),
                run_id=self.run_id,
            )
        return self._evidence

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the bundle state."""
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "missing": list(self.missing),
            "screenshots": list(self._screenshots),
            "notes": list(self._notes),
        }


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    import datetime as _dt

    return (
        _dt.datetime.now(tz=_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
