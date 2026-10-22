"""Install-stage vocabulary and fault classification tests (PRD 9.1 / 10).

``unipad.ipa.faults`` is the shared vocabulary of the IPA install chain:
:class:`InstallStage` is what ``unipad.models.InstallResult.stage`` may say,
:class:`FaultEntry` is the machine-readable fault unit the precheck and the
installer emit, and :func:`classify` is the only place an exception becomes a
stable :class:`unipad.errors.ErrorCode`.

These tests pin the two contracts that make the rest of the chain auditable:

* the stage vocabulary is **exactly** the PRD 9.1 stage list and it matches
  ``models.InstallResult`` -- a stage the model cannot express, or a stage the
  installer invents, would desynchronise the CLI, the controller and the
  verifier (PRD 7.4: the verifier judges fixed conditions);
* an exception can only ever be classified as ``invalid_package`` (the package
  could not be read) or as the *caller-supplied* stage default.  A crash must
  never be relabelled as a more specific-sounding fault such as
  ``signature_or_policy_rejected``: that would turn a host bug into a verdict
  about the package (PRD 7.4, no evidence -> no success).

Pure in-memory checks: no processes, no network, no clock, no randomness
(docs/testing-strategy.md D3).
"""

from __future__ import annotations

import json
import zipfile

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.ipa.faults import (
    REASON_BROKEN_STRUCTURE,
    REASON_ENCRYPTED_BINARY,
    REASON_HASH_MISMATCH,
    REASON_MACOS_PLATFORM,
    REASON_MISSING_FILE,
    REASON_NO_ARM64_SLICE,
    REASON_PLATFORM_UNKNOWN,
    REASON_SIMULATOR_PLATFORM,
    SUCCESS_STAGES,
    TERMINAL_STAGES,
    FaultEntry,
    InstallStage,
    classify,
    fault_codes,
    first_fault_code,
    is_success,
    is_terminal,
)
from unipad.ipa.precheck import run_precheck
from unipad.models import InstallResult
from tests.fixtures.ipa_builder import CORRUPT_VARIANTS, build_ipa

#: The stage list PRD 9.1 documents for the install interface, in order.
PRD_STAGE_ORDER = (
    "received",
    "checked",
    "transferred",
    "installed",
    "registered",
    "failed",
)

#: Fault codes that describe a *guest-side* judgement about the package.  A
#: host-side crash must never be classified into one of these.
GUEST_JUDGEMENT_CODES = (
    ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
    ErrorCode.REGISTRATION_FAILED,
    ErrorCode.MISSING_DEPENDENCY,
    ErrorCode.DYLD_FAILED,
    ErrorCode.UNSUPPORTED_PLATFORM,
    ErrorCode.UNSUPPORTED_ARCH,
    ErrorCode.TRANSFER_FAILED,
)


def _error_code_values() -> set[str]:
    """Every stable identifier declared by :class:`unipad.errors.ErrorCode`."""
    return {
        value
        for name, value in vars(ErrorCode).items()
        if isinstance(value, str) and not name.startswith("_")
    }


# --------------------------------------------------------------------------
# InstallStage <-> models.InstallResult (PRD 9.1)
# --------------------------------------------------------------------------


def test_stage_vocabulary_matches_the_install_result_contract():
    """PRD 9.1: one stage list for the enum, the model, the CLI and the verifier."""
    # arrange / act
    stage_values = [stage.value for stage in InstallStage]

    # assert -- exactly the documented stages, in order
    assert stage_values == list(PRD_STAGE_ORDER)
    # ... and the model's default stage is a member of that vocabulary
    assert InstallResult(package_sha256="a" * 64).stage in stage_values
    # ... and no stage exists that the model cannot express
    assert set(stage_values) <= set(PRD_STAGE_ORDER)


def test_terminal_stages_agree_with_the_model():
    """The installer and the model must agree on where the chain stops."""
    # arrange / act / assert
    assert TERMINAL_STAGES == InstallResult.TERMINAL_STAGES
    assert set(TERMINAL_STAGES) == {"installed", "registered", "failed"}
    # ``transferred`` is *not* terminal: PRD 9.1 leaves the guest to finish.
    assert "transferred" not in TERMINAL_STAGES
    assert "checked" not in TERMINAL_STAGES


def test_success_stages_exclude_the_failure_that_is_also_terminal():
    """``failed`` ends the chain but is not a success.

    The two vocabularies exist because "is this over?" and "did it work?" are
    different questions, and a gate that wants the second must not be handed
    the answer to the first.
    """
    # arrange / act / assert
    assert SUCCESS_STAGES == InstallResult.SUCCESS_STAGES
    assert set(SUCCESS_STAGES) == {"installed", "registered"}
    assert set(SUCCESS_STAGES) < set(TERMINAL_STAGES)
    assert "failed" not in SUCCESS_STAGES
    assert "failed" in TERMINAL_STAGES


@pytest.mark.parametrize("stage", list(InstallStage), ids=lambda s: s.value)
def test_is_success_is_true_only_for_a_completed_install(stage):
    """``is_success`` accepts the enum and the raw string alike."""
    # arrange / act / assert
    expected = stage.value in SUCCESS_STAGES
    assert is_success(stage) is expected
    assert is_success(stage.value) is expected


def test_is_success_rejects_a_failure_that_is_terminal():
    """The distinction the verifier depends on (PRD 9.1 / 7.4)."""
    # arrange / act / assert
    assert is_terminal("failed") is True
    assert is_success("failed") is False


def test_stage_str_and_json_share_one_spelling():
    """A stage in a log, in JSON and in a comparison must be the same string."""
    # arrange / act
    payload = json.dumps({"stage": InstallStage.TRANSFERRED})

    # assert
    assert str(InstallStage.TRANSFERRED) == "transferred"
    assert InstallStage.TRANSFERRED == "transferred"      # str enum
    assert json.loads(payload) == {"stage": "transferred"}
    assert payload == '{"stage": "transferred"}'


def test_coerce_collapses_unknown_stages_to_failed():
    """PRD 7.4: an unclassifiable stage must never be reported as progress."""
    # arrange / act / assert
    assert InstallStage.coerce("installed") is InstallStage.INSTALLED
    assert InstallStage.coerce(InstallStage.REGISTERED) is InstallStage.REGISTERED
    for bogus in ("", "unknown", "Installed", "success", "done", None, 17):
        assert InstallStage.coerce(bogus) is InstallStage.FAILED


@pytest.mark.parametrize("stage", list(InstallStage), ids=lambda s: s.value)
def test_is_terminal_marks_only_the_end_of_the_chain(stage):
    """``is_terminal`` accepts the enum and the raw string alike."""
    # arrange / act / assert
    expected = stage.value in TERMINAL_STAGES
    assert is_terminal(stage) is expected
    assert is_terminal(stage.value) is expected


def test_is_terminal_does_not_treat_a_partial_stage_as_the_end():
    """A verified transfer is progress, not a verdict (PRD 9.1)."""
    # arrange / act / assert
    assert is_terminal("transferred") is False
    assert is_terminal("checked") is False
    assert is_terminal("received") is False


# --------------------------------------------------------------------------
# classify (PRD 10 fault taxonomy)
# --------------------------------------------------------------------------


def test_classify_keeps_the_most_specific_code_available():
    """A UniPadError already carries a stable code; it must survive."""
    # arrange
    exc = UniPadError(
        ErrorCode.TRANSFER_FAILED,
        "receiver hash does not match the host hash",
        reason="hash_mismatch",
    )

    # act / assert
    assert classify(exc) == ErrorCode.TRANSFER_FAILED
    # ... even when the caller passes a different stage default
    assert classify(exc, default=ErrorCode.INSTALL_SERVICE_UNAVAILABLE) == (
        ErrorCode.TRANSFER_FAILED
    )


@pytest.mark.parametrize(
    "exc",
    [
        zipfile.BadZipFile("not a zip"),
        ValueError("bad value"),
        TypeError("bad type"),
        KeyError("CFBundleIdentifier"),
        IndexError("past the end"),
        ZeroDivisionError("arithmetic"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        OSError("cannot read"),
        FileNotFoundError("missing"),
        PermissionError("denied"),
    ],
)
def test_classify_maps_structural_failures_to_invalid_package(exc):
    """PRD 10: an unreadable package is ``invalid_package``, whatever the shape."""
    # act / assert
    assert classify(exc) == ErrorCode.INVALID_PACKAGE


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("guest exploded"),
        AttributeError("no such attribute"),
        NotImplementedError("not implemented"),
        AssertionError("impossible state"),
        MemoryError("out of memory"),
    ],
)
def test_classify_maps_unexpected_exceptions_to_the_default(exc):
    """An unrecognised exception falls back to the caller's default code."""
    # act / assert
    assert classify(exc) == ErrorCode.INVALID_PACKAGE


def test_classify_uses_the_stage_appropriate_default():
    """The guest-install stage passes its own default (see IpaInstaller)."""
    # arrange / act / assert
    assert classify(RuntimeError("boom")) == ErrorCode.INVALID_PACKAGE
    assert classify(
        RuntimeError("boom"), default=ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    ) == ErrorCode.INSTALL_SERVICE_UNAVAILABLE


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("boom"), AttributeError("gone"), NotImplementedError("todo")],
)
def test_an_unexpected_exception_is_never_a_guest_judgement(exc):
    """A host bug must not be relabelled as a verdict about the package.

    If :func:`classify` ever learned to "recognise" an unknown exception as a
    specific install-chain fault, a crash would be reported to the user as
    evidence about their IPA (PRD 7.4).  The only code it may return is the
    one the caller supplied as the default.
    """
    # act / assert -- the package-stage default is the whole answer
    assert classify(exc) == ErrorCode.INVALID_PACKAGE
    assert classify(exc) not in GUEST_JUDGEMENT_CODES
    # ... and an explicit default is honoured verbatim, never "improved"
    for default in (ErrorCode.INSTALL_SERVICE_UNAVAILABLE, ErrorCode.BLOCKED):
        assert classify(exc, default=default) == default


def test_a_custom_exception_subclass_is_not_guessed():
    """An application-specific bug is not a package fault either."""
    # arrange
    class GuestBug(Exception):
        pass

    # act / assert
    assert classify(GuestBug("internal")) == ErrorCode.INVALID_PACKAGE
    assert classify(GuestBug("internal"), default=ErrorCode.BLOCKED) == ErrorCode.BLOCKED


# --------------------------------------------------------------------------
# FaultEntry
# --------------------------------------------------------------------------


def test_fault_entry_to_dict_is_json_serialisable():
    """PRD 9.1: the fault is a machine-readable unit, not prose."""
    # arrange
    fault = FaultEntry(
        code=ErrorCode.UNSUPPORTED_PLATFORM,
        reason=REASON_ENCRYPTED_BINARY,
        detail="main executable is encrypted (cryptid=1)",
    )

    # act
    payload = json.loads(json.dumps(fault.to_dict(), sort_keys=True))

    # assert
    assert payload == {
        "code": ErrorCode.UNSUPPORTED_PLATFORM,
        "reason": "encrypted_binary",
        "detail": "main executable is encrypted (cryptid=1)",
    }
    assert fault.to_json() == json.dumps(
        fault.to_dict(), ensure_ascii=False, sort_keys=True
    )


def test_fault_entry_label_is_code_and_reason():
    """Logs and CLI output use one compact spelling."""
    # arrange / act
    fault = FaultEntry(code=ErrorCode.TRANSFER_FAILED, reason=REASON_HASH_MISMATCH)

    # assert
    assert fault.label == "transfer_failed:hash_mismatch"
    assert fault.detail == ""                # optional, defaults to empty


def test_the_prd10_fault_taxonomy_is_fully_declared_and_distinct():
    """PRD 10's fault classification must exist in ``unipad.errors``.

    If a code is dropped or two codes collapse onto one string, the precheck,
    the installer and the CLI stop agreeing on what happened, so the taxonomy
    is pinned here rather than discovered at the field site.
    """
    # arrange
    taxonomy = (
        ErrorCode.UNSUPPORTED_PLATFORM,
        ErrorCode.UNSUPPORTED_ARCH,
        ErrorCode.INVALID_PACKAGE,
        ErrorCode.TRANSFER_FAILED,
        ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
        ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
        ErrorCode.REGISTRATION_FAILED,
        ErrorCode.MISSING_DEPENDENCY,
        ErrorCode.DYLD_FAILED,
        ErrorCode.RENDER_FAILED,
        ErrorCode.INPUT_FAILED,
        ErrorCode.PERSISTENCE_FAILED,
    )
    declared = _error_code_values()

    # act / assert
    for code in taxonomy:
        assert code in declared
    assert len(set(taxonomy)) == len(taxonomy)      # no aliased codes


@pytest.mark.parametrize(
    "reason",
    [
        REASON_ENCRYPTED_BINARY,
        REASON_SIMULATOR_PLATFORM,
        REASON_MACOS_PLATFORM,
        REASON_NO_ARM64_SLICE,
        REASON_PLATFORM_UNKNOWN,
        REASON_BROKEN_STRUCTURE,
        REASON_MISSING_FILE,
        REASON_HASH_MISMATCH,
    ],
)
def test_reason_slugs_are_stable_identifiers(reason):
    """Renaming a reason slug requires a decision record (module docstring)."""
    # act / assert -- lowercase snake_case, no whitespace, never empty
    assert isinstance(reason, str)
    assert reason
    assert reason == reason.strip().lower()
    assert " " not in reason


# --------------------------------------------------------------------------
# fault collection helpers
# --------------------------------------------------------------------------


def test_first_fault_code_and_fault_codes_on_an_empty_collection():
    """No fault means no code -- not a default one."""
    # arrange / act / assert
    assert first_fault_code([]) is None
    assert fault_codes([]) == []


def test_fault_codes_preserve_order_and_deduplicate():
    """The first code is the one the installer reports (most specific first)."""
    # arrange
    faults = [
        FaultEntry(code=ErrorCode.UNSUPPORTED_ARCH, reason=REASON_SIMULATOR_PLATFORM),
        FaultEntry(code=ErrorCode.UNSUPPORTED_ARCH, reason=REASON_NO_ARM64_SLICE),
        FaultEntry(code=ErrorCode.UNSUPPORTED_PLATFORM, reason=REASON_ENCRYPTED_BINARY),
        FaultEntry(code=ErrorCode.UNSUPPORTED_ARCH, reason=REASON_SIMULATOR_PLATFORM),
    ]

    # act
    codes = fault_codes(faults)

    # assert
    assert codes == [
        ErrorCode.UNSUPPORTED_ARCH,
        ErrorCode.UNSUPPORTED_PLATFORM,
    ]
    assert first_fault_code(faults) == ErrorCode.UNSUPPORTED_ARCH


def test_helpers_agree_with_the_report_accessor(tmp_path):
    """``PrecheckReport.fault_codes`` is the same helper, not a second spelling."""
    # arrange -- a Simulator package records two faults sharing one code
    ipa = build_ipa(tmp_path / "sim.ipa", corrupt="simulator")
    report = run_precheck(ipa)

    # act / assert
    assert report.faults
    assert report.fault_codes == fault_codes(report.faults)
    assert report.fault_codes[0] == first_fault_code(report.faults)


@pytest.mark.parametrize("variant", sorted(CORRUPT_VARIANTS))
def test_every_precheck_fault_code_is_a_declared_error_code(tmp_path, variant):
    """PRD 10: the fault taxonomy and ``unipad.errors`` share one code list."""
    # arrange
    ipa = build_ipa(tmp_path / f"{variant}.ipa", corrupt=variant)

    # act
    report = run_precheck(ipa)

    # assert -- the precheck rejected, and every code it used is a declared one
    assert report.verdict == "reject"
    assert report.fault_codes
    for code in report.fault_codes:
        assert code in _error_code_values()
    for fault in report.faults:
        assert fault.reason
