"""Install-stage vocabulary and fault classification (PRD 9.1 / 10).

The IPA install chain is reported as a *stage*, never as a bare success
string.  ``InstallStage`` mirrors the ``stage`` field of
:class:`unipad.models.InstallResult` verbatim so the controller, the CLI and
the verifier share one vocabulary; ``FaultEntry`` is the machine-readable
fault unit used by the precheck and the installer.

Fault codes are the stable identifiers from
:class:`unipad.errors.ErrorCode` (PRD section 10): ``unsupported_platform``,
``unsupported_arch``, ``invalid_package``, ``transfer_failed``,
``signature_or_policy_rejected``, ``install_service_unavailable``,
``registration_failed``, ``missing_dependency``, ``dyld_failed``.  Renaming
one requires a decision record (``docs/decisions/``).
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from ..errors import ErrorCode, UniPadError

__all__ = [
    "InstallStage",
    "TERMINAL_STAGES",
    "SUCCESS_STAGES",
    "FaultEntry",
    "classify",
    "is_terminal",
    "is_success",
    "first_fault_code",
    "fault_codes",
]


class InstallStage(str, Enum):
    """Furthest stage actually completed by the install chain (PRD 9.1).

    A verified file transfer is ``transferred`` -- never ``installed``.
    ``installed`` and ``registered`` may only be reported by the guest
    install service, and ``failed`` always carries a stable error code.
    """

    RECEIVED = "received"
    CHECKED = "checked"
    TRANSFERRED = "transferred"
    INSTALLED = "installed"
    REGISTERED = "registered"
    FAILED = "failed"

    def __str__(self) -> str:  # keep JSON/log output equal to ``stage.value``
        """Return the stage value so JSON and logs share one spelling."""
        return self.value

    @classmethod
    def coerce(cls, value: "InstallStage | str") -> "InstallStage":
        """Return ``value`` as an :class:`InstallStage`, defaulting to FAILED.

        Unknown values collapse to ``failed``: an unclassifiable stage must
        never be reported as progress (PRD 7.4, no evidence -> no success).
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return cls.FAILED


#: Stages after which the install chain performs no further work.
TERMINAL_STAGES: tuple[str, ...] = ("installed", "registered", "failed")

#: Stages that report the install actually succeeded.  ``failed`` is terminal
#: but is not a success, so anything that asks "did the install complete" must
#: test against this, never against :data:`TERMINAL_STAGES`.
SUCCESS_STAGES: tuple[str, ...] = ("installed", "registered")

#: Reasonable stable reasons for precheck faults.  Not exhaustive: a caller
#: may add a reason, but existing spellings must not change meaning.
REASON_ENCRYPTED_BINARY = "encrypted_binary"
REASON_SIMULATOR_PLATFORM = "simulator_platform"
REASON_MACOS_PLATFORM = "macos_platform"
REASON_NO_ARM64_SLICE = "no_arm64_slice"
REASON_PLATFORM_UNKNOWN = "platform_unknown"
REASON_BROKEN_STRUCTURE = "broken_structure"
REASON_MISSING_FILE = "missing_file"
REASON_HASH_MISMATCH = "hash_mismatch"


@dataclass(frozen=True)
class FaultEntry:
    """One machine-readable fault.

    ``code`` is a stable :class:`unipad.errors.ErrorCode` value, ``reason``
    is a stable slug identifying *which* rule fired, and ``detail`` is a
    short human-readable note.  ``detail`` must never contain secrets
    (tokens, private keys, user documents) -- PRD 9.2.
    """

    code: str
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the fault as a JSON-serialisable mapping."""
        return {"code": self.code, "reason": self.reason, "detail": self.detail}

    def to_json(self) -> str:
        """Return the fault as a compact JSON object."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @property
    def label(self) -> str:
        """Compact ``code:reason`` label for logs and CLI output."""
        return f"{self.code}:{self.reason}"


def classify(exc: BaseException, default: str = ErrorCode.INVALID_PACKAGE) -> str:
    """Map an exception to a stable fault code.

    ``UniPadError`` keeps its own code (the most specific evidence
    available).  Known structural failures -- a corrupt zip, a bad plist, a
    decode error, a truncated file -- map to ``invalid_package``.  Anything
    unexpected also maps to ``default`` so a crash can never be reported as
    a different, more specific-sounding fault than it is.

    Args:
        exc: The exception to classify.
        default: Code returned for unexpected exceptions.  Callers that
            classify a *different* stage (e.g. the guest install call) may
            pass a stage-appropriate default.

    Returns:
        str: a stable :class:`unipad.errors.ErrorCode` value.
    """
    if isinstance(exc, UniPadError):
        return exc.code
    if isinstance(exc, (zipfile.BadZipFile,)):
        return ErrorCode.INVALID_PACKAGE
    if isinstance(exc, (ValueError, TypeError, KeyError, IndexError, ArithmeticError)):
        return ErrorCode.INVALID_PACKAGE
    if isinstance(exc, (UnicodeDecodeError, UnicodeEncodeError)):
        return ErrorCode.INVALID_PACKAGE
    if isinstance(exc, OSError):
        # A missing/unreadable file on the host is a package problem for the
        # install chain, not a host fault: the package could not be read.
        return ErrorCode.INVALID_PACKAGE
    return default


def is_terminal(stage: "InstallStage | str") -> bool:
    """Return True when ``stage`` ends the install chain (PRD 9.1)."""
    value = stage.value if isinstance(stage, InstallStage) else str(stage)
    return value in TERMINAL_STAGES


def is_success(stage: "InstallStage | str") -> bool:
    """Return True only when ``stage`` reports the install succeeded.

    ``failed`` is terminal, so :func:`is_terminal` answers "is this over?"
    while this answers "did it work?".  A gate that wants the second must call
    this one; using :func:`is_terminal` there lets a failed install pass.
    """
    value = stage.value if isinstance(stage, InstallStage) else str(stage)
    return value in SUCCESS_STAGES


def first_fault_code(faults: Iterable[FaultEntry]) -> str | None:
    """Return the code of the first fault, or ``None`` when there is none."""
    for fault in faults:
        return fault.code
    return None


def fault_codes(faults: Iterable[FaultEntry]) -> list[str]:
    """Return the distinct fault codes in first-seen order."""
    seen: list[str] = []
    for fault in faults:
        if fault.code not in seen:
            seen.append(fault.code)
    return seen
