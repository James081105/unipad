"""Unit tests for the shared error contract (PRD 9.1 and 10).

Mapping
-------
* PRD 9.1 "命令返回机器可读状态与稳定错误码" -- every fault of the PRD 10
  taxonomy plus the host/controller faults must be a distinct, stable,
  snake_case identifier.
* PRD 9.1 / CLI contract -- the fixed process exit codes scripts branch on:
  ``blocked`` and ``budget_exceeded`` -> 3, the usage-class errors -> 2,
  everything else -> 1.
* PRD 9.2 "日志不含账号令牌、签名私钥和用户文档" -- the serialized error shape
  carries only the code, the message and structured details.
* PRD appendix A -- ``blocked()`` always carries a concrete reason.

All assertions here are on observable contracts: attribute values, the exit
code of a raised error, and the parsed JSON document.
"""

from __future__ import annotations

import json
import re

import pytest

from unipad import errors
from unipad.errors import ErrorCode, ExitCode, UniPadError, blocked

#: snake_case, no leading/trailing underscore, digits allowed after a letter.
_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")

#: PRD 9.1 / CLI contract: which codes mean "blocked" (exit 3) and which mean
#: "usage error" (exit 2).  Everything else is a generic failure (exit 1).
_BLOCKED_CODES = (ErrorCode.BLOCKED, ErrorCode.BUDGET_EXCEEDED)
_USAGE_CODES = (
    ErrorCode.CONFIG_INVALID,
    ErrorCode.UNSUPPORTED_COMMAND,
    ErrorCode.EXPERIMENT_STATE_INVALID,
)


def _declared_codes() -> dict[str, str]:
    """Return every code declared on :class:`ErrorCode` as ``{name: value}``.

    Only public, non-callable attributes are contract surface: dunders and
    imported helpers must not widen the taxonomy by accident.
    """
    return {
        name: value
        for name, value in vars(ErrorCode).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def test_every_declared_error_code_is_present() -> None:
    # Arrange / act: the contract surface of ErrorCode.
    codes = _declared_codes()

    # Assert: the PRD 10 fault taxonomy and the controller codes are all there.
    assert codes, "ErrorCode must declare at least one code"
    for expected in (
        "UNSUPPORTED_PLATFORM",
        "UNSUPPORTED_ARCH",
        "INVALID_PACKAGE",
        "TRANSFER_FAILED",
        "SIGNATURE_OR_POLICY_REJECTED",
        "INSTALL_SERVICE_UNAVAILABLE",
        "REGISTRATION_FAILED",
        "MISSING_DEPENDENCY",
        "DYLD_FAILED",
        "RENDER_FAILED",
        "INPUT_FAILED",
        "PERSISTENCE_FAILED",
        "QEMU_LAUNCH_FAILED",
        "QEMU_TIMEOUT",
        "QMP_UNAVAILABLE",
        "QMP_PROTOCOL_ERROR",
        "ACCELERATOR_NOT_TCG",
        "IMAGE_NOT_FOUND",
        "IMAGE_HASH_MISMATCH",
        "ARTIFACT_MANIFEST_INVALID",
        "ARTIFACT_MISSING",
        "RESTORE_FAILED",
        "CONFIG_INVALID",
        "EXPERIMENT_STATE_INVALID",
        "EVIDENCE_INCOMPLETE",
        "VERIFICATION_FAILED",
        "BUDGET_EXCEEDED",
        "UNSUPPORTED_COMMAND",
        "BLOCKED",
    ):
        assert expected in codes, f"ErrorCode.{expected} is part of the PRD 10 / 9.1 taxonomy"


def test_error_code_values_are_unique() -> None:
    # Arrange.
    codes = _declared_codes()

    # Act / assert: renaming a code onto an existing value would make the
    # machine-readable contract ambiguous, so duplicates are a defect.
    values = list(codes.values())
    duplicates = sorted({value for value in values if values.count(value) > 1})
    assert duplicates == [], f"duplicate ErrorCode values: {duplicates}"


def test_error_code_values_are_snake_case() -> None:
    # Arrange.
    codes = _declared_codes()

    # Assert: codes travel through JSON, file names and shell branches.
    for name, value in sorted(codes.items()):
        assert _SNAKE_CASE.match(value), f"ErrorCode.{name}={value!r} is not snake_case"
        assert value == value.strip(), f"ErrorCode.{name} has surrounding whitespace"


def test_exit_code_mapping_is_fixed() -> None:
    # PRD 9.1: scripts branch on the exit code, so the mapping is contract.
    for code in _BLOCKED_CODES:
        assert UniPadError(code, "x").exit_code is ExitCode.BLOCKED, code
        assert int(UniPadError(code, "x").exit_code) == 3, code
    for code in _USAGE_CODES:
        assert UniPadError(code, "x").exit_code is ExitCode.USAGE, code
        assert int(UniPadError(code, "x").exit_code) == 2, code


def test_unmapped_error_codes_map_to_generic_failure() -> None:
    # Arrange: every code that is neither blocked nor a usage error.
    special = set(_BLOCKED_CODES) | set(_USAGE_CODES)

    for name, value in sorted(_declared_codes().items()):
        if value in special:
            continue
        # Act / assert: PRD 9.1 -- "others -> 1" with the code in the payload.
        assert UniPadError(value, "x").exit_code is ExitCode.FAILURE, name
        assert int(UniPadError(value, "x").exit_code) == 1, name


def test_unknown_error_code_fails_closed_to_generic_failure() -> None:
    # Arrange: a code that is not registered at all.
    error = UniPadError("not_a_registered_code", "something happened")

    # Act / assert: an unrecognised code must never look like blocked (exit 3)
    # or like a usage error (exit 2); it is a generic failure.
    assert error.exit_code is ExitCode.FAILURE
    assert int(error.exit_code) == 1


def test_error_to_dict_shape() -> None:
    # Arrange.
    error = UniPadError(
        ErrorCode.TRANSFER_FAILED,
        "hash mismatch",
        package_sha256="a" * 64,
        expected="b" * 64,
    )

    # Act.
    payload = error.to_dict()

    # Assert: exactly the three documented keys, nested under "error".
    assert set(payload) == {"error"}
    body = payload["error"]
    assert set(body) == {"code", "message", "details"}
    assert body["code"] == ErrorCode.TRANSFER_FAILED
    assert body["message"] == "hash mismatch"
    assert body["details"] == {"package_sha256": "a" * 64, "expected": "b" * 64}


def test_error_to_json_is_one_parseable_object() -> None:
    # Arrange.
    error = UniPadError(ErrorCode.EVIDENCE_INCOMPLETE, "missing evidence", missing=["serial_log"])

    # Act.
    document = json.loads(error.to_json())

    # Assert: the CLI prints exactly one JSON object per command (PRD 9.1), so
    # to_json must not emit a document stream.
    assert document["error"]["code"] == ErrorCode.EVIDENCE_INCOMPLETE
    assert document["error"]["details"]["missing"] == ["serial_log"]
    assert error.to_json().count("\n") == 0


def test_error_details_default_to_empty_mapping() -> None:
    # Arrange / act.
    error = UniPadError(ErrorCode.BLOCKED, "no guest channel")

    # Assert.
    assert error.details == {}
    assert error.to_dict()["error"]["details"] == {}


def test_error_is_an_exception_carrying_code_and_message() -> None:
    # Arrange / act.
    error = UniPadError(ErrorCode.INSTALL_SERVICE_UNAVAILABLE, "installer unreachable")

    # Assert: raised form keeps the code reachable without string parsing.
    assert isinstance(error, Exception)
    assert str(error) == "installer unreachable"
    assert error.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    with pytest.raises(UniPadError) as caught:
        raise error
    assert caught.value.code == ErrorCode.INSTALL_SERVICE_UNAVAILABLE


def test_blocked_helper_produces_blocked_code_and_reason() -> None:
    # Act.
    error = blocked("no QEMU binary on PATH", expected="qemu-system-aarch64")

    # Assert: PRD appendix A -- a blocked state always names a concrete reason.
    assert isinstance(error, UniPadError)
    assert error.code == ErrorCode.BLOCKED
    assert error.message == "no QEMU binary on PATH"
    assert error.exit_code is ExitCode.BLOCKED
    assert error.details == {"expected": "qemu-system-aarch64"}


def test_exit_code_enum_values() -> None:
    # PRD 9.1: 0 success, 1 generic failure, 2 usage, 3 blocked.
    assert int(ExitCode.OK) == 0
    assert int(ExitCode.FAILURE) == 1
    assert int(ExitCode.USAGE) == 2
    assert int(ExitCode.BLOCKED) == 3


def test_internal_mapping_table_only_references_declared_codes() -> None:
    # Arrange / act: the exit-code table is private but load-bearing.
    table = errors._CODE_TO_EXIT

    # Assert: a typo in the table would silently fall back to exit 1.
    declared = set(_declared_codes().values())
    assert set(table) <= declared, f"unknown codes in the exit table: {sorted(set(table) - declared)}"
    assert set(table) == set(_BLOCKED_CODES) | set(_USAGE_CODES)
