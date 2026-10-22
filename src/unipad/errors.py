"""Stable error codes for UniPad (PRD sections 9.1 and 10).

Every failure surfaced to the CLI, the experiment journal, or the verifier
must carry one of these codes.  Codes are part of the product contract:
renaming or repurposing a code requires a decision record (docs/decisions/).
The CLI maps them to fixed process exit codes so scripts can branch on
failures without parsing text.
"""

from __future__ import annotations

import json
from enum import IntEnum
from typing import Any


class ErrorCode(str):
    """String identifiers of the fault taxonomy (PRD section 10) and host/controller faults."""

    # --- IPA package precheck (PRD section 10, "package precheck") ---
    UNSUPPORTED_PLATFORM = "unsupported_platform"      # not a Device (arm64) build
    UNSUPPORTED_ARCH = "unsupported_arch"              # no ARM64 slice / simulator slice only
    INVALID_PACKAGE = "invalid_package"                # structure/Info.plist/unreadable

    # --- IPA install chain (PRD section 10) ---
    TRANSFER_FAILED = "transfer_failed"                # hash mismatch / transport error
    SIGNATURE_OR_POLICY_REJECTED = "signature_or_policy_rejected"
    INSTALL_SERVICE_UNAVAILABLE = "install_service_unavailable"
    REGISTRATION_FAILED = "registration_failed"        # container/icon not established
    MISSING_DEPENDENCY = "missing_dependency"          # dyld cannot find a dependency
    DYLD_FAILED = "dyld_failed"                        # entry did not execute
    RENDER_FAILED = "render_failed"
    INPUT_FAILED = "input_failed"
    PERSISTENCE_FAILED = "persistence_failed"          # data lost across normal reboot

    # --- Host / QEMU / QMP ---
    QEMU_LAUNCH_FAILED = "qemu_launch_failed"
    QEMU_TIMEOUT = "qemu_timeout"                      # no progress within budget
    QMP_UNAVAILABLE = "qmp_unavailable"                # control socket never became ready
    QMP_PROTOCOL_ERROR = "qmp_protocol_error"
    ACCELERATOR_NOT_TCG = "accelerator_not_tcg"        # guard: never silently use WHPX/HVF

    # --- Images / artifacts ---
    IMAGE_NOT_FOUND = "image_not_found"
    IMAGE_HASH_MISMATCH = "image_hash_mismatch"
    ARTIFACT_MANIFEST_INVALID = "artifact_manifest_invalid"
    ARTIFACT_MISSING = "artifact_missing"
    RESTORE_FAILED = "restore_failed"

    # --- Controller / verifier / budget ---
    CONFIG_INVALID = "config_invalid"
    EXPERIMENT_STATE_INVALID = "experiment_state_invalid"
    EVIDENCE_INCOMPLETE = "evidence_incomplete"        # verifier refuses to pass
    VERIFICATION_FAILED = "verification_failed"        # fixed conditions not met
    BUDGET_EXCEEDED = "budget_exceeded"
    UNSUPPORTED_COMMAND = "unsupported_command"

    # --- Generic blocked marker: always carries a concrete reason in details ---
    BLOCKED = "blocked"


class ExitCode(IntEnum):
    """Fixed process exit codes for the ``unipad`` CLI."""

    OK = 0
    FAILURE = 1                     # generic failure; see machine-readable error.code
    USAGE = 2                       # bad command line / config
    BLOCKED = 3                     # missing material, credential, or budget (PRD 7.5)


_CODE_TO_EXIT: dict[str, ExitCode] = {
    ErrorCode.BLOCKED: ExitCode.BLOCKED,
    ErrorCode.BUDGET_EXCEEDED: ExitCode.BLOCKED,
    ErrorCode.CONFIG_INVALID: ExitCode.USAGE,
    ErrorCode.UNSUPPORTED_COMMAND: ExitCode.USAGE,
    ErrorCode.EXPERIMENT_STATE_INVALID: ExitCode.USAGE,
}


class UniPadError(Exception):
    """Base error carrying a stable code and structured details.

    ``details`` must never contain secrets (tokens, private keys, user
    documents) -- PRD section 9.2.  The CLI serializes errors as JSON so
    callers branch on ``code``, not on prose.
    """

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def exit_code(self) -> ExitCode:
        return _CODE_TO_EXIT.get(self.code, ExitCode.FAILURE)

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def blocked(reason: str, **details: Any) -> UniPadError:
    """Construct a BLOCKED error with a concrete reason (PRD appendix A)."""
    return UniPadError(ErrorCode.BLOCKED, reason, **details)
