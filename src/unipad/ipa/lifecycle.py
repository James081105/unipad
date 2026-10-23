"""Install lifecycle: history, uninstall, update and error classification.

PRD 3.2 P1 (3) asks for 应用卸载、更新、安装历史和清晰的错误分类.  Those four
things are one problem, not four: each is a question about *state* -- what is
installed, what was it before, what happened in between, and why did something
fail.  Answering them correctly is a host-side obligation, so it is implemented
and proved here without a guest.

Three rules shape the module:

**The ledger is append-only.**  A history that can be rewritten is not a
history.  Entries are never edited or removed; the current state is *derived*
by reading the tail.  That is why :meth:`InstallLedger.installed` is a function
and not a field.

**Uninstall and update are transitions, not commands.**  Each is a new entry
naming the entry it supersedes, so "what happened" survives "what is true now".
Overwriting the old version would make the update unreproducible and the
rollback undefinable.

**Error classification is a closed vocabulary.**  :func:`classify_failure`
maps a stable :class:`unipad.errors.ErrorCode` onto a small set of classes.  A
code it does not recognise becomes ``"unknown"`` -- never a guess at a
neighbouring class, which would hide the real fault behind a plausible one.

Nothing here touches the guest.  It records what the host was told and what the
host did, which is exactly the boundary PRD 9.2 draws: evidence may not be
invented, and an entry is not a claim about the device.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from unipad.models import InstallResult

__all__ = [
    "LifecycleAction",
    "LifecycleEntry",
    "InstallLedger",
    "classify_failure",
    "FAILURE_CLASSES",
]


class LifecycleAction(str, Enum):
    """What an entry did to the bundle's state.

    An install that is also an update is recorded as ``update``: the action
    names the *transition*, so a reader can tell a first install from a
    replacement without diffing hashes.
    """

    INSTALL = "install"
    UPDATE = "update"
    UNINSTALL = "uninstall"


#: Error classes, as a closed vocabulary.  A class is a *category* for an
#: operator, not a diagnosis: which rule fired is ``FaultEntry.reason``.
FAILURE_CLASSES: tuple[str, ...] = (
    "package_rejected",       # the package itself is unacceptable
    "integrity_failed",       # bytes did not match what was declared
    "transfer_failed",        # the move to the guest failed
    "service_unavailable",    # the guest install service could not be reached
    "guest_refused",          # the guest looked at it and said no
    "unknown",                # not classifiable -- reported, never guessed
)

#: ``ErrorCode`` -> class.  Grouped by *where the failure lives* rather than by
#: which command raised it, so an operator reading a class knows what to check.
_CLASS_BY_CODE: Mapping[str, str] = {
    # the package is unacceptable before anything is attempted
    "invalid_package": "package_rejected",
    "unsupported_platform": "package_rejected",
    "unsupported_arch": "package_rejected",
    "encrypted_binary": "package_rejected",
    # the bytes are not what they claimed to be
    "image_hash_mismatch": "integrity_failed",
    # moving it failed
    "transfer_failed": "transfer_failed",
    "guest_transport_error": "transfer_failed",
    # the guest could not be asked
    "install_service_unavailable": "service_unavailable",
    "guest_unavailable": "service_unavailable",
    # the guest answered and refused
    "guest_refused": "guest_refused",
    "trust_denied": "guest_refused",
    "challenge_failed": "guest_refused",
}


def classify_failure(error_code: Optional[str]) -> str:
    """Return the failure class for ``error_code``.

    An unrecognised or absent code is ``"unknown"``.  It is deliberately *not*
    mapped onto a neighbouring class: a plausible-looking guess would send an
    operator to check the wrong thing, and the whole value of a class is that
    it can be acted on.
    """
    if not error_code:
        return "unknown"
    return _CLASS_BY_CODE.get(str(error_code), "unknown")


def _now() -> str:
    """UTC timestamp, second resolution, always with an explicit offset."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class LifecycleEntry:
    """One thing that happened to one bundle.

    ``entry_id`` is assigned by the ledger, not the caller, so an entry cannot
    be forged to supersede a different one.  ``supersedes`` names the entry
    this transition replaced; it is ``None`` for a first install.
    """

    entry_id: str
    bundle_id: str
    action: LifecycleAction
    package_sha256: str = ""
    stage: str = ""
    recorded_at: str = field(default_factory=_now)
    supersedes: Optional[str] = None
    reason: str = ""
    error_code: Optional[str] = None
    error_class: Optional[str] = None
    trust_mode: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view.  ``error_message`` is deliberately absent.

        PRD 9.2 forbids secrets in any output, and a guest-supplied message is
        exactly where one would leak: the class and the code are what an
        operator needs, and the raw text is not.
        """
        return {
            "entry_id": self.entry_id,
            "bundle_id": self.bundle_id,
            "action": self.action.value,
            "package_sha256": self.package_sha256,
            "stage": self.stage,
            "recorded_at": self.recorded_at,
            "supersedes": self.supersedes,
            "reason": self.reason,
            "error_code": self.error_code,
            "error_class": self.error_class,
            "trust_mode": self.trust_mode,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LifecycleEntry":
        try:
            action = LifecycleAction(str(data["action"]))
        except ValueError:
            # An entry whose action is not in the vocabulary is not an entry we
            # can reason about; the ledger reports it as unreadable rather than
            # silently dropping the field.
            raise ValueError(f"unknown lifecycle action: {data.get('action')!r}") from None
        return cls(
            entry_id=str(data["entry_id"]),
            bundle_id=str(data["bundle_id"]),
            action=action,
            package_sha256=str(data.get("package_sha256", "")),
            stage=str(data.get("stage", "")),
            recorded_at=str(data.get("recorded_at", "")),
            supersedes=(str(data["supersedes"]) if data.get("supersedes") else None),
            reason=str(data.get("reason", "")),
            error_code=(str(data["error_code"]) if data.get("error_code") else None),
            error_class=(str(data["error_class"]) if data.get("error_class") else None),
            trust_mode=(str(data["trust_mode"]) if data.get("trust_mode") else None),
        )


class InstallLedger:
    """An append-only record of install, update and uninstall transitions.

    The file is JSON Lines: one entry per line, appended.  A line that cannot
    be parsed is counted in :attr:`unreadable_lines` and skipped, so a
    truncated tail costs the last entry rather than the whole history.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: list[LifecycleEntry] = []
        self.unreadable_lines = 0
        if self.path.is_file():
            self._load()

    # -- reading ---------------------------------------------------------

    def _load(self) -> None:
        self._entries = []
        self.unreadable_lines = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._entries.append(LifecycleEntry.from_dict(json.loads(line)))
                except (ValueError, TypeError, json.JSONDecodeError):
                    self.unreadable_lines += 1

    @property
    def entries(self) -> list[LifecycleEntry]:
        """Every entry, oldest first.  A copy: the ledger is not editable."""
        return list(self._entries)

    def history(self, bundle_id: str) -> tuple[LifecycleEntry, ...]:
        """Entries for one bundle, oldest first."""
        return tuple(e for e in self._entries if e.bundle_id == bundle_id)

    def installed(self, bundle_id: str) -> Optional[LifecycleEntry]:
        """The entry that describes ``bundle_id``'s current state, or ``None``.

        Derived, never stored: the last entry that *installed* it wins, and an
        uninstall after that removes it.  A failed install is not an install,
        so it never makes a bundle present -- and it does not evict a previous
        good install either, because it did not touch it.
        """
        current: Optional[LifecycleEntry] = None
        for entry in self.history(bundle_id):
            if entry.action is LifecycleAction.INSTALL:
                if entry.stage in ("installed", "registered"):
                    current = entry
            elif entry.action is LifecycleAction.UPDATE:
                if entry.stage in ("installed", "registered"):
                    current = entry
            elif entry.action is LifecycleAction.UNINSTALL:
                current = None
        return current

    def installed_bundle_ids(self) -> tuple[str, ...]:
        """Every bundle currently installed, in first-installed order."""
        seen: dict[str, None] = {}
        for entry in self._entries:
            if entry.action in (LifecycleAction.INSTALL, LifecycleAction.UPDATE):
                if entry.stage in ("installed", "registered"):
                    seen.setdefault(entry.bundle_id, None)
            elif entry.action is LifecycleAction.UNINSTALL:
                seen.pop(entry.bundle_id, None)
        return tuple(seen)

    # -- writing ---------------------------------------------------------

    def _append(self, entry: LifecycleEntry) -> LifecycleEntry:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        self._entries.append(entry)
        return entry

    def _next_id(self) -> str:
        # Monotonic within the file, so an entry cannot be forged to supersede
        # a different one: the id is derived from the position it will occupy.
        return f"le-{len(self._entries) + 1:08d}"

    def _last_entry_for(self, bundle_id: str) -> Optional[LifecycleEntry]:
        history = self.history(bundle_id)
        return history[-1] if history else None

    def record_install(
        self,
        result: InstallResult,
        *,
        action: LifecycleAction = LifecycleAction.INSTALL,
        reason: str = "",
    ) -> LifecycleEntry:
        """Record an install or an update.

        An ``update`` supersedes the entry it replaces; a first install
        supersedes nothing.  An update with no predecessor keeps its action but
        claims no predecessor rather than inventing one.
        """
        supersedes = None
        if action is LifecycleAction.UPDATE:
            previous = self._last_entry_for(result.bundle_id or "")
            if previous is not None and previous.action in (
                LifecycleAction.INSTALL,
                LifecycleAction.UPDATE,
            ):
                supersedes = previous.entry_id

        return self._append(
            LifecycleEntry(
                entry_id=self._next_id(),
                bundle_id=result.bundle_id or "",
                action=action,
                package_sha256=result.package_sha256,
                stage=result.stage,
                supersedes=supersedes,
                reason=reason,
                error_code=result.error_code,
                error_class=(
                    classify_failure(result.error_code) if result.error_code else None
                ),
                trust_mode=result.trust_mode,
            )
        )

    def record_uninstall(
        self, result: InstallResult, *, reason: str = ""
    ) -> LifecycleEntry:
        """Record that a bundle was removed.

        Raises:
            ValueError: when the bundle is not currently installed.  Removing
                something that is not there must not fabricate a removal --
                the caller's view of the device is wrong, and silently agreeing
                with it would hide that.
        """
        bundle_id = result.bundle_id or ""
        current = self.installed(bundle_id)
        if current is None:
            raise ValueError(f"{bundle_id} is not installed")

        return self._append(
            LifecycleEntry(
                entry_id=self._next_id(),
                bundle_id=bundle_id,
                action=LifecycleAction.UNINSTALL,
                package_sha256=current.package_sha256,
                stage="removed",
                supersedes=current.entry_id,
                reason=reason,
            )
        )
