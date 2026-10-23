"""IPA application support matrix (PRD 3.2 P1 bullet 2; PRD 5.1 / 2.2 / 10 / 12).

PRD 3.2 P1 asks for 更多自有或获授权的 IPA 兼容性测试 -- *more* compatibility
testing of own-or-authorised IPAs.  "More testing" is only worth anything if the
results are recorded in a shape that cannot overstate what was proved, so this
file pins the record rather than the testing.

Four PRD rules decide the whole design, and each one is a refusal:

**PRD 10**: 应用支持矩阵必须按"某个包版本 + 某个系统构建 + 某个运行配置"记录。发现一款
App 可用，只能证明该样本，不外推成整个 UIKit 或 App Store 兼容.
An entry is keyed on exactly those three parts.  Anything coarser -- a bundle id
alone, an app name, a "UIKit works" row -- is inadmissible.

**PRD 5.1**: 签名、嵌入框架、权限和所采用的客体信任方式必须被记录。不能把未签名包、普通开发签名包、
客体所有者信任包混为同一支持类别.
``TrustCategory`` is a closed vocabulary and two entries that differ *only* in
trust category are two different support categories, so the matrix refuses to
merge them.  That specific mistake is the one this vocabulary exists to prevent.

**PRD 12**: 公开可得不等于已获得再分发许可.
``Authorisation`` is declared, never inferred.  A package that is not authorised
is refused with a stable error code, and an entry that declares nothing is not
an entry -- so the gate cannot be bypassed by omission.

**PRD 2.2 item 3**: 无 DRM、包含 ARM64、签名合规于某一安装方式，是不同条件.
``Verdict`` is therefore not a compatibility word.  It names an outcome, and the
outcome must name *which* of those three conditions was established.  There is
no ``compatible`` verdict to hide behind.

Scope, stated once: every entry this suite can build today is a host-side record
about a synthesized image.  There is no iPadOS guest on this machine, so no test
here may be read as "an IPA runs".
"""

from __future__ import annotations

import json
import os

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.ipa.lifecycle import InstallLedger
from unipad.ipa.matrix import (
    AUTHORISATIONS,
    ESTABLISHED_CONDITIONS,
    POSITIVE_VERDICTS,
    TRUST_CATEGORIES,
    VERDICTS,
    Authorisation,
    CompatibilityMatrix,
    EstablishedCondition,
    MatrixEntry,
    MatrixKey,
    TrustCategory,
    Verdict,
    declared_signature_mode,
)
from unipad.ipa.package import open_ipa
from unipad.ipa.precheck import run_precheck
from unipad.models import InstallResult

from fixtures.ipa_builder import build_ipa, hello_ipa, notes_ipa
from fixtures.macho_builder import build_thin_macho


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

#: One fixed run configuration.  Spelled out in full so an entry names the
#: machine it was observed on, not "the default".
RUN_CONFIG = "windows-tcg/cortex-a710/1c/6144mb/ramfb"


def _key(**overrides) -> MatrixKey:
    params = {
        "package_version": "1.0",
        "system_build": "21H461",
        "run_config": RUN_CONFIG,
    }
    params.update(overrides)
    return MatrixKey(**params)


def _entry(package, **overrides) -> MatrixEntry:
    params = {
        "key": _key(),
        "authorisation": Authorisation.OWN_SOURCE,
        "trust_category": TrustCategory.UNSIGNED,
    }
    params.update(overrides)
    return MatrixEntry.from_package(package, **params)


@pytest.fixture
def matrix(tmp_path) -> CompatibilityMatrix:
    return CompatibilityMatrix(tmp_path / "compat-matrix.jsonl")


@pytest.fixture
def hello(tmp_path):
    return open_ipa(hello_ipa(tmp_path / "HelloUniPad.ipa"))


@pytest.fixture
def notes(tmp_path):
    return open_ipa(notes_ipa(tmp_path / "UniPadNotes.ipa", include_framework=True))


# ---------------------------------------------------------------------------
# PRD 10: the key is (package version, system build, run configuration)
# ---------------------------------------------------------------------------


def test_the_key_has_exactly_the_three_prd_10_parts():
    assert MatrixKey(
        package_version="1.0", system_build="21H461", run_config=RUN_CONFIG
    ) == _key()


@pytest.mark.parametrize("part", ["package_version", "system_build", "run_config"])
def test_a_key_field_may_not_be_empty(part):
    """A blank part is a coarser key pretending to be the PRD 10 key."""
    params = {"package_version": "1.0", "system_build": "21H461", "run_config": RUN_CONFIG}
    params[part] = "   "
    with pytest.raises(ValueError):
        MatrixKey(**params)


def test_entries_differing_in_any_one_key_part_are_different_entries(hello):
    """One working sample proves that sample -- not the app, not UIKit."""
    base = _entry(hello)
    variants = [
        _entry(hello, key=_key(package_version="1.1")),
        _entry(hello, key=_key(system_build="21H462")),
        _entry(hello, key=_key(run_config="windows-tcg/cortex-a710/2c/6144mb/ramfb")),
    ]
    assert all(other.key != base.key for other in variants)


def test_the_key_round_trips_through_json():
    assert MatrixKey.from_dict(_key().to_dict()) == _key()


def test_the_key_is_hashable_so_it_can_index_the_matrix(hello):
    assert {_key(): "one"}[_entry(hello).key] == "one"


# ---------------------------------------------------------------------------
# PRD 5.1: the trust categories cannot be collapsed
# ---------------------------------------------------------------------------


def test_the_trust_vocabulary_is_the_closed_set_prd_51_names():
    assert TRUST_CATEGORIES == (
        TrustCategory.UNSIGNED,
        TrustCategory.DEVELOPMENT_SIGNED,
        TrustCategory.OWNER_TRUSTED,
        TrustCategory.GUEST_REPORTED,
    )


def test_an_unknown_trust_category_is_refused(hello):
    with pytest.raises(ValueError):
        _entry(hello, trust_category="enterprise-supervised")


def test_two_entries_differing_only_in_trust_category_are_not_merged(matrix, hello):
    """The specific mistake PRD 5.1 forbids: mixing support categories."""
    matrix.record(_entry(hello, trust_category=TrustCategory.UNSIGNED))

    with pytest.raises(UniPadError) as exc:
        matrix.record(
            _entry(hello, trust_category=TrustCategory.DEVELOPMENT_SIGNED)
        )
    assert exc.value.code == ErrorCode.EXPERIMENT_STATE_INVALID
    assert exc.value.details["reason"] == "trust_category_conflict"


def test_two_entries_differing_only_in_authorisation_are_not_merged(matrix, hello):
    """The symmetric twin of the trust-category refusal (refute2 M06).

    Authorisation decides whether the package may be recorded at all, so two
    entries under one key that were cleared on different grounds are two
    different permissions to describe the same sample.  Recording the second
    would put one clearance's evidence under the other's name.
    """
    matrix.record(_entry(hello, authorisation=Authorisation.OWN_SOURCE))

    with pytest.raises(UniPadError) as exc:
        matrix.record(_entry(hello, authorisation=Authorisation.LICENSED_WITH_EVIDENCE))
    assert exc.value.code == ErrorCode.EXPERIMENT_STATE_INVALID
    assert exc.value.details["reason"] == "authorisation_conflict"

    assert len(matrix.entries) == 1
    assert len(matrix.observations) == 1
    assert matrix.entries[0].authorisation is Authorisation.OWN_SOURCE


@pytest.mark.parametrize(
    ("field", "contradicts_with"),
    [
        ("signing_mode", "unsigned"),
        ("embedded_frameworks", ("Frameworks/Evil.framework",)),
        ("requested_permissions", ("NSCameraUsageDescription",)),
    ],
    ids=lambda value: value if isinstance(value, str) else "tuple",
)
def test_the_same_sample_may_not_be_recorded_with_a_contradictory_support_record(
    matrix, hello, field, contradicts_with
):
    """PRD 5.1: the support record must be recorded, so it must not contradict itself.

    ``signing_mode``, ``embedded_frameworks`` and ``requested_permissions`` are
    read off *the bytes*.  Under one key, one ``package_sha256`` and one trust
    category there is exactly one right answer for each of them, so a second
    observation that answers differently is not a second observation -- it is a
    wrong one.  Accepting it lets the wrong record become the current entry and
    the correct one unreachable through ``entries``.
    """
    matrix.record(_entry(hello))

    second = _entry(hello, reason="re-observed")
    setattr(second, field, contradicts_with)
    with pytest.raises(UniPadError) as exc:
        matrix.record(second)
    assert exc.value.code == ErrorCode.EXPERIMENT_STATE_INVALID
    assert exc.value.details["reason"] == "support_record_conflict"
    assert exc.value.details["field"] == field

    assert len(matrix.entries) == 1
    assert len(matrix.observations) == 1
    assert getattr(matrix.entries[0], field) == getattr(_entry(hello), field)


def test_a_rebuild_under_one_key_is_a_new_sample_not_a_contradiction(matrix, hello, notes):
    """The carve-out that keeps the refusal from over-reaching.

    A differing ``package_sha256`` is a different set of bytes, and a rebuild of
    the same version may legitimately change how it is signed and what it
    embeds.  That is the documented "a rebuild is a new sample, appended rather
    than merged" case -- so the support-record refusal is scoped to one sample,
    and both observations stay reachable.
    """
    matrix.record(_entry(hello))
    rebuilt = _entry(notes, reason="rebuilt 1.0, new sample")
    matrix.record(rebuilt)

    assert len(matrix.entries) == 1
    assert len(matrix.observations) == 2
    assert matrix.entry_for(_key()).package_sha256 == notes.sha256
    assert len(matrix.observations_for(_key())) == 2


def test_a_different_package_sha256_under_one_key_is_appended_not_refused(matrix, hello):
    """The documented append-on-rebuild rule, proved rather than assumed (refute2 M25).

    The class docstring promises that a differing ``package_sha256`` is appended
    rather than merged and that ``observations_for`` exposes both.  Until now no
    test recorded two observations under one key with different shas, so turning
    that into a conflict would have gone unnoticed.
    """
    first = matrix.record(_entry(hello, reason="first build"))
    rebuilt = _entry(hello, reason="second build")
    rebuilt.package_sha256 = "b" * 64
    matrix.record(rebuilt)

    assert len(matrix.entries) == 1
    assert len(matrix.observations) == 2
    assert [e.package_sha256 for e in matrix.observations_for(_key())] == [
        first.package_sha256,
        "b" * 64,
    ]
    assert [e.reason for e in matrix.observations_for(_key())] == ["first build", "second build"]


def test_a_contradictory_support_record_is_refused_after_a_reload_too(tmp_path, hello):
    """A refusal the matrix made once is made again after a reload."""
    path = tmp_path / "compat-matrix.jsonl"
    matrix = CompatibilityMatrix(path)
    matrix.record(_entry(hello))

    reloaded = CompatibilityMatrix(path)
    second = _entry(hello, reason="re-observed")
    second.signing_mode = "unsigned"
    with pytest.raises(UniPadError) as exc:
        reloaded.record(second)
    assert exc.value.code == ErrorCode.EXPERIMENT_STATE_INVALID
    assert exc.value.details["reason"] == "support_record_conflict"
    assert len(reloaded.entries) == 1


@pytest.mark.parametrize("first", TRUST_CATEGORIES, ids=lambda c: c.value)
@pytest.mark.parametrize("second", TRUST_CATEGORIES, ids=lambda c: c.value)
def test_no_two_trust_categories_share_a_key(matrix, hello, first, second):
    """Every category is its own support category, for every pair.

    The same category appends (a re-observation is a new record); a different
    one is refused, because merging them would record one category's evidence
    under another's name.
    """
    matrix.record(_entry(hello, trust_category=first))
    if first is second:
        matrix.record(_entry(hello, trust_category=first))
        assert len(matrix.entries) == 1
        assert len(matrix.observations) == 2
        return

    with pytest.raises(UniPadError) as exc:
        matrix.record(_entry(hello, trust_category=second))
    assert exc.value.code == ErrorCode.EXPERIMENT_STATE_INVALID
    assert len(matrix.entries) == 1
    assert len(matrix.observations) == 1


def test_the_same_key_and_category_appends_rather_than_replacing(matrix, hello):
    """A re-observation is a new record; the old one is still in the history."""
    matrix.record(_entry(hello))
    matrix.record(_entry(hello, reason="re-observed after rebuild"))

    assert len(matrix.entries) == 1
    assert len(matrix.observations) == 2


def test_an_observation_without_an_install_stage_does_not_erase_the_recorded_one(matrix, hello):
    """A stage must not move backwards when the later observation names none.

    ``install_stage == ""`` means *no guest observation was made*, not "the
    install went nowhere".  Letting such an observation become the current entry
    for a key retracts an install the guest really did, and the operator feels
    it as ``counts()`` reporting ``installs-only: 0`` while a line on disk
    records ``installs-only`` -- absence of evidence reading as presence of
    failure.  The later observation is still appended and still reachable
    through :meth:`CompatibilityMatrix.observations_for`; it simply does not
    become the current answer.
    """
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            install_result=_installed_result(hello),
            established_conditions=(EstablishedCondition.DRM_FREE,),
            reason="guest installed it",
        )
    )
    assert matrix.entry_for(_key()).install_stage == "installed"
    assert matrix.counts()[Verdict.INSTALLS_ONLY] == 1

    matrix.record(_entry(hello, reason="re-checked on the host, no guest"))

    assert matrix.entry_for(_key()).install_stage == "installed"
    assert matrix.counts()[Verdict.INSTALLS_ONLY] == 1
    assert len(matrix.entries) == 1
    assert len(matrix.observations) == 2
    assert [e.reason for e in matrix.observations_for(_key())] == [
        "guest installed it",
        "re-checked on the host, no guest",
    ]


def test_the_recorded_install_stage_survives_a_reload(matrix, hello):
    """The regression must not come back through the door the file came in by."""
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            install_result=_installed_result(hello),
            established_conditions=(EstablishedCondition.DRM_FREE,),
            reason="guest installed it",
        )
    )
    matrix.record(_entry(hello, reason="re-checked on the host, no guest"))

    reloaded = CompatibilityMatrix(matrix.path)
    assert reloaded.entry_for(_key()).install_stage == "installed"
    assert reloaded.counts()[Verdict.INSTALLS_ONLY] == 1
    assert len(reloaded.observations) == 2


def test_a_later_observation_that_names_a_stage_replaces_the_current_one(matrix, hello):
    """The guard is scoped to observations that carry no install evidence.

    A second run that genuinely got *less* far is a real outcome and must be
    the current entry -- hiding it would overstate support, which is the other
    direction of the same mistake.
    """
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            install_result=_installed_result(hello),
            established_conditions=(EstablishedCondition.DRM_FREE,),
            reason="guest installed it",
        )
    )
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.REJECTED,
            install_result=InstallResult(
                package_sha256=hello.sha256, stage="transferred", error_code="transfer_failed"
            ),
            error_code="transfer_failed",
            reason="second run never installed",
        )
    )

    assert matrix.entry_for(_key()).install_stage == "transferred"
    assert matrix.entry_for(_key()).verdict is Verdict.REJECTED
    assert len(matrix.observations) == 2


# ---------------------------------------------------------------------------
# PRD 12: the authorisation gate
# ---------------------------------------------------------------------------


def test_the_authorisation_vocabulary_is_closed():
    assert AUTHORISATIONS == (
        Authorisation.OWN_SOURCE,
        Authorisation.LICENSED_WITH_EVIDENCE,
        Authorisation.NOT_AUTHORISED,
    )


def test_an_unknown_authorisation_is_refused(hello):
    with pytest.raises(ValueError):
        _entry(hello, authorisation="publicly-available")


@pytest.mark.parametrize("authorisation", [Authorisation.OWN_SOURCE, Authorisation.LICENSED_WITH_EVIDENCE])
def test_an_authorised_package_is_admitted(matrix, hello, authorisation):
    matrix.record(_entry(hello, authorisation=authorisation))
    assert matrix.entries[0].authorisation is authorisation


def test_a_not_authorised_package_is_refused_with_a_stable_code(hello):
    """PRD 12: 公开可得不等于已获得再分发许可."""
    with pytest.raises(UniPadError) as exc:
        _entry(hello, authorisation=Authorisation.NOT_AUTHORISED)
    assert exc.value.code == ErrorCode.BLOCKED
    assert exc.value.details["reason"] == "not_authorised"


def test_an_undeclared_authorisation_is_not_an_entry(hello):
    """The gate cannot be bypassed by omission."""
    with pytest.raises(UniPadError) as exc:
        _entry(hello, authorisation=None)
    assert exc.value.code == ErrorCode.BLOCKED
    assert exc.value.details["reason"] == "authorisation_undeclared"


def test_an_undeclared_trust_category_is_not_an_entry(hello):
    with pytest.raises(UniPadError) as exc:
        _entry(hello, trust_category=None)
    assert exc.value.code == ErrorCode.BLOCKED
    assert exc.value.details["reason"] == "trust_category_undeclared"


def test_the_gate_is_rechecked_when_recording(matrix, hello):
    """A hand-built entry must pass the same gate as a parsed one."""
    entry = _entry(hello)
    entry.authorisation = Authorisation.NOT_AUTHORISED
    with pytest.raises(UniPadError) as exc:
        matrix.record(entry)
    assert exc.value.code == ErrorCode.BLOCKED


def test_a_stored_line_without_authorisation_is_unreadable_not_admitted(tmp_path, hello):
    """A hostile or truncated file must not smuggle an entry past the gate."""
    path = tmp_path / "compat-matrix.jsonl"
    record = _entry(hello).to_dict()
    record.pop("authorisation")
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    reloaded = CompatibilityMatrix(path)
    assert reloaded.entries == ()
    assert reloaded.unreadable_lines == 1


# ---------------------------------------------------------------------------
# PRD 2.2 item 3: the verdict names a condition, never "compatible"
# ---------------------------------------------------------------------------


def test_the_verdict_vocabulary_is_the_closed_set():
    assert VERDICTS == (
        Verdict.INSTALLS_AND_RUNS,
        Verdict.INSTALLS_ONLY,
        Verdict.REJECTED,
        Verdict.UNTESTED,
        Verdict.NOT_APPLICABLE,
    )


def test_the_verdict_vocabulary_cannot_say_compatible():
    """No token in the vocabulary is a bare compatibility claim."""
    banned = {"compatible", "incompatible", "works", "supported", "unsupported", "pass", "ok"}
    assert not banned & {v.value for v in Verdict}


def test_the_three_prd_22_conditions_are_three_distinct_conditions():
    """无 DRM、包含 ARM64、签名合规于某一安装方式，是不同条件."""
    assert ESTABLISHED_CONDITIONS == (
        EstablishedCondition.DRM_FREE,
        EstablishedCondition.ARM64_PRESENT,
        EstablishedCondition.SIGNATURE_COMPLIANT_FOR_INSTALL_METHOD,
    )
    assert len(set(ESTABLISHED_CONDITIONS)) == 3


def test_an_unknown_condition_is_refused(hello):
    with pytest.raises(ValueError):
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            install_result=InstallResult(package_sha256=hello.sha256, stage="installed"),
            established_conditions=["no-drm"],
        )


def test_a_positive_verdict_must_name_the_condition_it_established(hello):
    """An "installs" claim that names no condition is an unbacked claim."""
    with pytest.raises(UniPadError) as exc:
        _entry(
            hello,
            verdict=Verdict.INSTALLS_AND_RUNS,
            install_result=InstallResult(package_sha256=hello.sha256, stage="registered"),
            established_conditions=(),
        )
    assert exc.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert exc.value.details["reason"] == "no_established_condition"


def test_a_positive_verdict_must_name_an_install_stage(hello):
    """A host-side record may not claim an app runs; there is no guest here."""
    with pytest.raises(UniPadError) as exc:
        _entry(
            hello,
            verdict=Verdict.INSTALLS_AND_RUNS,
            established_conditions=(EstablishedCondition.DRM_FREE,),
        )
    assert exc.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert exc.value.details["reason"] == "no_install_stage"


def _installed_result(package, **overrides) -> InstallResult:
    params = {"package_sha256": package.sha256, "stage": "installed"}
    params.update(overrides)
    return InstallResult(**params)


def test_a_positive_verdict_with_a_condition_and_a_stage_is_a_record(hello):
    entry = _entry(
        hello,
        verdict=Verdict.INSTALLS_ONLY,
        install_result=_installed_result(hello),
        established_conditions=(
            EstablishedCondition.DRM_FREE,
            EstablishedCondition.ARM64_PRESENT,
        ),
    )
    assert entry.verdict is Verdict.INSTALLS_ONLY
    assert EstablishedCondition.ARM64_PRESENT in entry.established_conditions


def test_the_conditions_of_a_positive_verdict_are_a_subset_of_the_vocabulary(hello):
    entry = _entry(
        hello,
        verdict=Verdict.INSTALLS_ONLY,
        install_result=_installed_result(hello),
        established_conditions=[EstablishedCondition.DRM_FREE],
    )
    assert set(entry.established_conditions) <= set(ESTABLISHED_CONDITIONS)


def test_not_applicable_claims_no_condition(hello):
    """The test does not apply, so nothing was established."""
    with pytest.raises(ValueError):
        _entry(hello, verdict=Verdict.NOT_APPLICABLE, established_conditions=[EstablishedCondition.DRM_FREE])


def test_untested_may_carry_the_conditions_the_host_can_read(hello):
    """A static check can establish DRM-freedom and ARM64 without a guest.

    That is not a support claim: the verdict is still ``untested``, and
    ``counts()`` still reports it under ``untested``.
    """
    entry = _entry(
        hello,
        established_conditions=(
            EstablishedCondition.DRM_FREE,
            EstablishedCondition.ARM64_PRESENT,
        ),
    )
    assert entry.verdict is Verdict.UNTESTED
    assert len(entry.established_conditions) == 2


def test_signature_compliance_cannot_be_claimed_without_an_install(hello):
    """PRD 2.2: 签名合规于某一安装方式 needs an install under a named method."""
    with pytest.raises(UniPadError) as exc:
        _entry(
            hello,
            established_conditions=(EstablishedCondition.SIGNATURE_COMPLIANT_FOR_INSTALL_METHOD,),
        )
    assert exc.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert exc.value.details["reason"] == "condition_requires_install_stage"


def test_a_rejected_verdict_must_carry_a_reason(hello):
    with pytest.raises(UniPadError) as exc:
        _entry(hello, verdict=Verdict.REJECTED)
    assert exc.value.code == ErrorCode.EVIDENCE_INCOMPLETE
    assert exc.value.details["reason"] == "no_rejection_reason"


def test_a_rejected_verdict_may_still_name_what_was_established(hello):
    """A package can be DRM-free and ARM64-carrying and still be rejected."""
    entry = _entry(
        hello,
        verdict=Verdict.REJECTED,
        error_code=ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
        reason="guest_refused",
        established_conditions=(
            EstablishedCondition.DRM_FREE,
            EstablishedCondition.ARM64_PRESENT,
        ),
    )
    assert entry.verdict is Verdict.REJECTED
    assert len(entry.established_conditions) == 2


def test_an_unknown_verdict_is_refused(hello):
    with pytest.raises(ValueError):
        _entry(hello, verdict="compatible")


def test_the_default_verdict_is_untested(hello):
    """Absence of evidence must never read as presence of support."""
    assert _entry(hello).verdict is Verdict.UNTESTED
    assert _entry(hello).established_conditions == ()


def test_the_positive_verdicts_are_the_two_install_outcomes():
    assert POSITIVE_VERDICTS == (Verdict.INSTALLS_AND_RUNS, Verdict.INSTALLS_ONLY)


# ---------------------------------------------------------------------------
# PRD 5.1: signing / embedding / permissions / guest trust are recorded
# ---------------------------------------------------------------------------


def test_the_signing_mode_is_read_off_the_package(hello):
    """The fixture image carries an LC_CODE_SIGNATURE, so that is what is recorded."""
    assert _entry(hello).signing_mode == "code-signature-present"
    assert declared_signature_mode(hello) == "code-signature-present"


def test_an_unsigned_image_is_recorded_as_unsigned(tmp_path):
    package = open_ipa(
        build_ipa(
            tmp_path / "unsigned.ipa",
            macho_bytes=build_thin_macho(has_code_signature=False),
        )
    )
    entry = _entry(package)
    assert entry.signing_mode == "unsigned"
    assert entry.trust_category is TrustCategory.UNSIGNED


def test_a_code_signature_in_the_image_changes_the_declared_mode(tmp_path):
    package = open_ipa(
        build_ipa(
            tmp_path / "signed.ipa",
            macho_bytes=build_thin_macho(has_code_signature=True),
        )
    )
    assert declared_signature_mode(package) == "code-signature-present"
    assert MatrixEntry.from_package(
        package,
        key=_key(),
        authorisation=Authorisation.OWN_SOURCE,
        trust_category=TrustCategory.DEVELOPMENT_SIGNED,
    ).signing_mode == "code-signature-present"


def test_the_declared_signing_mode_is_not_a_trust_claim(tmp_path):
    """A signed image under the unsigned category stays its own category."""
    package = open_ipa(
        build_ipa(
            tmp_path / "signed2.ipa",
            macho_bytes=build_thin_macho(has_code_signature=True),
        )
    )
    entry = _entry(package, trust_category=TrustCategory.UNSIGNED)
    assert entry.signing_mode == "code-signature-present"
    assert entry.trust_category is TrustCategory.UNSIGNED


def test_an_unparsed_executable_is_declared_not_guessed_unsigned(tmp_path):
    """No slice to look at is not the same as an unsigned slice (refute2 M13).

    ``declared_signature_mode`` is the one place the module could quietly
    invent a fact, and the honest answer -- ``unparsed-executable`` -- was never
    executed by the suite, because nothing fed it an unparseable image.
    """
    package = open_ipa(build_ipa(tmp_path / "garbage.ipa", macho_bytes=b"not a mach-o at all"))
    assert package.macho is None

    assert declared_signature_mode(package) == "unparsed-executable"
    assert _entry(package).signing_mode == "unparsed-executable"


def test_the_embedded_frameworks_are_recorded(notes):
    assert list(_entry(notes).embedded_frameworks) == ["Frameworks/F.framework"]


def test_the_requested_permissions_are_recorded_by_key_name_only(tmp_path):
    """PRD 9.2: the usage-description *text* belongs to the app author."""
    package = open_ipa(
        build_ipa(
            tmp_path / "perm.ipa",
            include_plist_extra={"NSCameraUsageDescription": "secret user-facing prose"},
        )
    )
    entry = _entry(package)
    assert list(entry.requested_permissions) == ["NSCameraUsageDescription"]
    assert "secret user-facing prose" not in json.dumps(entry.to_dict())


def test_the_guest_trust_mode_is_recorded_verbatim_and_never_invented(hello):
    entry = _entry(hello, install_result=InstallResult(package_sha256=hello.sha256, stage="installed"))
    assert entry.guest_trust_mode is None

    reported = _entry(
        hello,
        install_result=InstallResult(
            package_sha256=hello.sha256,
            stage="installed",
            trust_mode="self-signed-dev-trust",
        ),
    )
    assert reported.guest_trust_mode == "self-signed-dev-trust"
    assert reported.trust_category is TrustCategory.UNSIGNED


def test_the_install_stage_is_taken_from_the_result_not_from_the_verdict(hello):
    entry = _entry(
        hello,
        install_result=InstallResult(package_sha256=hello.sha256, stage="transferred"),
    )
    assert entry.install_stage == "transferred"
    assert entry.verdict is Verdict.UNTESTED


def test_the_package_identity_is_recorded(hello):
    entry = _entry(hello)
    assert entry.bundle_id == hello.bundle_id
    assert entry.package_sha256 == hello.sha256
    assert entry.package_path == hello.path


# ---------------------------------------------------------------------------
# storage: JSON, append-only, one corrupt line costs one entry
# ---------------------------------------------------------------------------


def test_a_new_matrix_has_no_file(matrix):
    assert matrix.entries == ()
    assert not matrix.path.exists()


def test_recording_appends_one_json_line_per_observation(matrix, hello, notes):
    matrix.record(_entry(hello))
    matrix.record(_entry(notes, key=_key(package_version="2.0")))

    lines = matrix.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["bundle_id"] for line in lines] == [
        hello.bundle_id,
        notes.bundle_id,
    ]


def test_a_corrupt_line_costs_one_entry_not_the_matrix(tmp_path, hello, notes):
    """A truncated tail is the realistic failure; the rest still loads."""
    path = tmp_path / "compat-matrix.jsonl"
    matrix = CompatibilityMatrix(path)
    matrix.record(_entry(hello))
    matrix.record(_entry(notes, key=_key(package_version="2.0")))
    good = path.read_text(encoding="utf-8").splitlines()

    path.write_text(
        good[0] + "\n" + '{"schema_version": 1, "key": {"package_vers' + "\n" + good[1] + "\n",
        encoding="utf-8",
    )

    reloaded = CompatibilityMatrix(path)
    assert reloaded.unreadable_lines == 1
    assert len(reloaded.entries) == 2


def test_an_empty_line_is_not_an_unreadable_line(tmp_path, hello):
    path = tmp_path / "compat-matrix.jsonl"
    matrix = CompatibilityMatrix(path)
    matrix.record(_entry(hello))
    path.write_text("\n" + matrix.path.read_text(encoding="utf-8"), encoding="utf-8")

    reloaded = CompatibilityMatrix(path)
    assert reloaded.unreadable_lines == 0
    assert len(reloaded.entries) == 1


def test_a_truncated_tail_costs_one_line_and_does_not_swallow_the_next_observation(
    tmp_path, hello, notes
):
    """A torn write must not eat its successor, and the count must say so.

    ``_append`` opens in append mode.  If the file's last line was torn off
    mid-write there is no trailing newline, so the next observation is
    concatenated onto the corrupt line: *two* observations are lost while
    ``unreadable_lines`` reports one.  The count is then wrong in the only
    direction that matters -- it under-reports what was lost, so nothing
    signals that a second entry vanished.
    """
    path = tmp_path / "compat-matrix.jsonl"
    matrix = CompatibilityMatrix(path)
    matrix.record(_entry(hello))
    whole = path.read_text(encoding="utf-8").splitlines()
    path.write_text(whole[0][:-1], encoding="utf-8")  # torn tail: half a JSON object, no newline

    torn = CompatibilityMatrix(path)
    assert torn.unreadable_lines == 1
    assert torn.entries == ()

    torn.record(_entry(notes, key=_key(package_version="2.0")))

    physical = path.read_text(encoding="utf-8").splitlines()
    assert len(physical) == 2, "the next observation was concatenated onto the torn line"

    reloaded = CompatibilityMatrix(path)
    assert reloaded.unreadable_lines == 1
    assert len(reloaded.entries) == 1
    assert reloaded.entry_for(_key(package_version="2.0")).bundle_id == notes.bundle_id
    # Every physical line is accounted for: readable observations plus unreadable
    # lines, no line silently dropped and none counted twice.
    assert len(reloaded.observations) + reloaded.unreadable_lines == len(physical)


def test_the_matrix_survives_a_reload(matrix, hello):
    matrix.record(_entry(hello, reason="first observation"))

    reloaded = CompatibilityMatrix(matrix.path)
    assert reloaded.entries[0].key == _key()
    assert reloaded.entries[0].reason == "first observation"


def test_the_matrix_round_trips_through_json(matrix, hello, notes):
    """Every field of every entry survives the file."""
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            established_conditions=(EstablishedCondition.DRM_FREE,),
            install_result=InstallResult(
                package_sha256=hello.sha256, stage="installed", trust_mode="owner-trusted"
            ),
            note="host-side record only",
        )
    )
    matrix.record(
        _entry(
            notes,
            key=_key(package_version="2.0"),
            verdict=Verdict.REJECTED,
            error_code="unsupported_arch",
            reason="no_arm64_slice",
        )
    )

    reloaded = CompatibilityMatrix(matrix.path)
    assert [e.to_dict() for e in reloaded.entries] == [e.to_dict() for e in matrix.entries]
    assert reloaded.entries[0].established_conditions == (EstablishedCondition.DRM_FREE,)
    assert reloaded.entries[0].guest_trust_mode == "owner-trusted"
    assert reloaded.entries[1].verdict is Verdict.REJECTED
    assert reloaded.entries[1].error_code == "unsupported_arch"


def test_the_conflict_survives_a_reload(tmp_path, hello):
    """A merge the matrix refused once is refused again after a reload."""
    path = tmp_path / "compat-matrix.jsonl"
    matrix = CompatibilityMatrix(path)
    matrix.record(_entry(hello, trust_category=TrustCategory.UNSIGNED))

    reloaded = CompatibilityMatrix(path)
    with pytest.raises(UniPadError) as exc:
        reloaded.record(_entry(hello, trust_category=TrustCategory.OWNER_TRUSTED))
    assert exc.value.code == ErrorCode.EXPERIMENT_STATE_INVALID


def test_lookup_by_key_and_by_bundle(matrix, hello, notes):
    matrix.record(_entry(hello))
    matrix.record(_entry(notes, key=_key(package_version="2.0")))

    assert matrix.entry_for(_key()).bundle_id == hello.bundle_id
    assert len(matrix.entries_for_bundle(hello.bundle_id)) == 1
    assert len(matrix.entries_for_bundle(notes.bundle_id)) == 1
    assert matrix.entry_for(_key(package_version="9.9")) is None


def test_counts_report_every_verdict_in_the_matrix(matrix, hello, notes):
    matrix.record(_entry(hello))
    matrix.record(_entry(notes, key=_key(package_version="2.0"), verdict=Verdict.REJECTED, reason="x"))

    counts = matrix.counts()
    assert counts[Verdict.UNTESTED] == 1
    assert counts[Verdict.REJECTED] == 1
    assert counts[Verdict.INSTALLS_AND_RUNS] == 0


def test_the_matrix_is_a_sized_collection(matrix, hello):
    matrix.record(_entry(hello))
    assert len(matrix) == 1
    assert _key() in matrix


def test_observations_for_one_key_expose_the_whole_history(matrix, hello):
    matrix.record(_entry(hello, reason="first"))
    matrix.record(_entry(hello, reason="second"))

    assert [e.reason for e in matrix.observations_for(_key())] == ["first", "second"]


# ---------------------------------------------------------------------------
# the PRD 5.1 samples are own-source, so they can be admitted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", ["hello_ipa", "notes_ipa"])
def test_the_prd_51_samples_can_be_admitted(matrix, tmp_path, preset):
    """Both PRD 5.1 samples are own-source; the gate must let them in."""
    builder = {"hello_ipa": hello_ipa, "notes_ipa": notes_ipa}[preset]
    package = open_ipa(builder(tmp_path / f"{preset}.ipa"))

    entry = matrix.record(
        _entry(
            package,
            authorisation=Authorisation.OWN_SOURCE,
            trust_category=TrustCategory.UNSIGNED,
        )
    )
    assert entry.bundle_id == package.bundle_id
    assert matrix.entries[0].verdict is Verdict.UNTESTED


def test_an_unauthorised_third_party_ipa_is_refused_forever(matrix, tmp_path):
    """The rule that keeps an unlicensed third-party IPA out of the matrix."""
    package = open_ipa(
        build_ipa(tmp_path / "ThirdParty.ipa", bundle_id="com.third.party", app_name="ThirdParty")
    )
    with pytest.raises(UniPadError) as exc:
        _entry(package, authorisation=Authorisation.NOT_AUTHORISED)
    assert exc.value.code == ErrorCode.BLOCKED
    assert matrix.entries == ()
    assert not matrix.path.exists()


def test_no_entry_today_may_claim_an_app_runs(matrix, hello):
    """There is no iPadOS guest here, so nothing recorded today is a run claim."""
    matrix.record(_entry(hello))
    assert all(e.verdict is Verdict.UNTESTED for e in matrix.entries)
    assert all(e.install_stage == "" for e in matrix.entries)


# ---------------------------------------------------------------------------
# the precheck bridge: the only compatibility evidence a Windows host has
# ---------------------------------------------------------------------------


def test_a_clean_precheck_is_recorded_as_untested_with_the_host_conditions(matrix, hello):
    """A package the host can read is not a package that runs."""
    report = run_precheck(hello.path)
    assert report.verdict == "accept"

    entry = matrix.record(
        MatrixEntry.from_precheck(
            hello,
            report,
            key=_key(),
            authorisation=Authorisation.OWN_SOURCE,
            trust_category=TrustCategory.UNSIGNED,
        )
    )
    assert entry.verdict is Verdict.UNTESTED
    assert set(entry.established_conditions) == {
        EstablishedCondition.DRM_FREE,
        EstablishedCondition.ARM64_PRESENT,
    }
    assert matrix.counts()[Verdict.INSTALLS_AND_RUNS] == 0


def test_a_faulted_precheck_is_recorded_as_rejected_with_its_code(matrix, tmp_path):
    package = open_ipa(
        build_ipa(
            tmp_path / "sim.ipa",
            corrupt="simulator",
            bundle_id="com.unipad.sim",
            app_name="SimUniPad",
            executable="SimUniPad",
        )
    )
    report = run_precheck(package.path)
    assert report.verdict == "reject"

    entry = matrix.record(
        MatrixEntry.from_precheck(
            package,
            report,
            key=_key(),
            authorisation=Authorisation.OWN_SOURCE,
            trust_category=TrustCategory.UNSIGNED,
        )
    )
    assert entry.verdict is Verdict.REJECTED
    assert entry.error_code == ErrorCode.UNSUPPORTED_ARCH
    assert entry.reason == "simulator_platform"
    # The rejection is recorded even though the package *is* DRM-free.
    assert EstablishedCondition.DRM_FREE in entry.established_conditions
    assert EstablishedCondition.ARM64_PRESENT not in entry.established_conditions


def test_a_clean_precheck_never_becomes_an_install_verdict(matrix, hello):
    report = run_precheck(hello.path)
    entry = MatrixEntry.from_precheck(
        hello,
        report,
        key=_key(),
        authorisation=Authorisation.OWN_SOURCE,
        trust_category=TrustCategory.UNSIGNED,
    )
    assert entry.verdict not in POSITIVE_VERDICTS
    assert entry.install_stage == ""


def test_the_precheck_bridge_round_trips(matrix, hello):
    report = run_precheck(hello.path)
    matrix.record(
        MatrixEntry.from_precheck(
            hello,
            report,
            key=_key(),
            authorisation=Authorisation.OWN_SOURCE,
            trust_category=TrustCategory.UNSIGNED,
        )
    )

    reloaded = CompatibilityMatrix(matrix.path)
    assert [e.to_dict() for e in reloaded.entries] == [e.to_dict() for e in matrix.entries]


# ---------------------------------------------------------------------------
# reverse review m8 (matrix half): unreadable_lines conflates two causes
# ---------------------------------------------------------------------------


def _write_lines(path, lines) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_gate_refused_line_is_counted_separately_from_a_corrupt_one(tmp_path, hello, notes):
    """The two causes are not the same fact, so they are not one number.

    ``unreadable_lines`` counted a line the authorisation gate refused in the
    same bucket as a line the JSON parser could not read.  An operator (or a
    CLI command) reading ``2`` cannot tell whether the file is *damaged* -- a
    torn write, a disk error, a half-migrated schema -- or whether somebody
    hand-wrote an unauthorised entry into it.  Those are opposite situations:
    the first is a repair problem, the second is a security signal, and the
    second must never be able to hide inside the first.
    """
    matrix = CompatibilityMatrix(tmp_path / "compat-matrix.jsonl")
    matrix.record(_entry(hello))
    matrix.record(_entry(notes, key=_key(package_version="2.0")))
    good = matrix.path.read_text(encoding="utf-8").splitlines()

    # A line that is valid JSON and a well-formed entry, but declares no
    # authorisation: the gate fires, which is the outcome wanted.
    gated = json.loads(good[0])
    gated.pop("authorisation")
    # A line that is not JSON at all.
    corrupt = '{"schema_version": 1, "key": {"package_vers'

    _write_lines(matrix.path, [good[0], corrupt, json.dumps(gated), good[1]])

    reloaded = CompatibilityMatrix(matrix.path)
    assert reloaded.corrupt_lines == 1, "the unparseable line is damage"
    assert reloaded.refused_lines == 1, "the gated line is a refusal"
    assert reloaded.unreadable_lines == 2


def test_the_unreadable_breakdown_accounts_for_every_physical_line(tmp_path, hello):
    """``corrupt + gate_refused == unreadable``, and no line is dropped twice.

    The total is what the old single counter was, so a caller that only reads
    ``unreadable_lines`` keeps the answer it had; the breakdown is what makes
    the total mean something.  Both halves together must still account for
    every physical line in the file, which is the property that keeps a
    swallow from being silent.
    """
    matrix = CompatibilityMatrix(tmp_path / "compat-matrix.jsonl")
    matrix.record(_entry(hello))
    good = matrix.path.read_text(encoding="utf-8").splitlines()

    not_an_entry = json.dumps({"schema_version": 1, "key": {"package_version": "1.0"}})
    unknown_verdict = json.loads(good[0])
    unknown_verdict["verdict"] = "compatible"
    _write_lines(
        matrix.path, [good[0], "{oops", not_an_entry, json.dumps(unknown_verdict), "null"]
    )

    reloaded = CompatibilityMatrix(matrix.path)
    assert reloaded.corrupt_lines + reloaded.refused_lines == reloaded.unreadable_lines
    assert reloaded.unreadable_lines == 4
    physical = [line for line in matrix.path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(reloaded.observations) + reloaded.unreadable_lines == len(physical)


def test_a_schema_drifted_line_is_damage_not_a_refusal(tmp_path, hello):
    """A line outside the closed vocabularies is corrupt, not gate-refused.

    The gate is a *security* boundary with its own name; a verdict the module
    does not know is a line the module cannot read.  Filing it under the gate
    would make an ordinary schema migration look like a trust incident.
    """
    matrix = CompatibilityMatrix(tmp_path / "compat-matrix.jsonl")
    matrix.record(_entry(hello))
    good = matrix.path.read_text(encoding="utf-8").splitlines()
    drifted = json.loads(good[0])
    drifted["verdict"] = "works"
    _write_lines(matrix.path, [good[0], json.dumps(drifted)])

    reloaded = CompatibilityMatrix(matrix.path)
    assert reloaded.corrupt_lines == 1
    assert reloaded.refused_lines == 0


def test_a_clean_matrix_reports_zero_of_each(matrix, hello):
    """No line skipped, so no cause to report."""
    matrix.record(_entry(hello))
    reloaded = CompatibilityMatrix(matrix.path)
    assert reloaded.unreadable_lines == 0
    assert reloaded.corrupt_lines == 0
    assert reloaded.refused_lines == 0


def test_a_line_the_gates_refuse_is_a_refusal_whatever_the_gate(tmp_path, hello):
    """Every gate refusal is a refusal; only damage is damage.

    ``refused_lines`` covers the authorisation gate, the trust-category gate and
    a verdict that claims more than the entry can back, because all three are
    "this line is an entry the matrix will not admit" rather than "this line
    cannot be read".  Pinned over all of them so the classification cannot
    quietly become "whatever the first except clause happens to catch".
    """
    matrix = CompatibilityMatrix(tmp_path / "compat-matrix.jsonl")
    matrix.record(_entry(hello))
    good = json.loads(matrix.path.read_text(encoding="utf-8").splitlines()[0])

    variants = {
        "no authorisation": {**good, "authorisation": None},
        "not authorised": {**good, "authorisation": Authorisation.NOT_AUTHORISED.value},
        "no trust category": {**good, "trust_category": None},
        "positive verdict with no stage": {**good, "verdict": Verdict.INSTALLS_ONLY.value},
        "rejected with no reason": {**good, "verdict": Verdict.REJECTED.value},
    }
    for label, line in variants.items():
        path = tmp_path / f"{label.replace(' ', '-')}.jsonl"
        _write_lines(path, [json.dumps(line)])
        reloaded = CompatibilityMatrix(path)
        assert reloaded.refused_lines == 1, f"{label} is a refusal, not damage"
        assert reloaded.corrupt_lines == 0, f"{label} must not be counted as damage"
        assert reloaded.unreadable_lines == 1


# ---------------------------------------------------------------------------
# callchain 6.2 / finding #3 (ledger half): the two stores of one install must
# be durable by the same rule, or they disagree about what is installed
# ---------------------------------------------------------------------------


def test_an_appended_observation_reaches_the_device_before_record_returns(
    matrix, hello, monkeypatch
):
    """Durability: the observation is on the disk when ``record`` returns.

    The matrix and :class:`~unipad.ipa.lifecycle.InstallLedger` are two records
    of the same install, and the module docstring says the matrix's storage
    "mirrors" the ledger's.  The ledger forces every append to the device before
    it returns, with the reasoning that a transition the host performed but the
    ledger does not know about is a host that disagrees with the guest about
    what is installed -- and there is nothing later that reconciles the two.

    The matrix had no equivalent: the bytes sat in the process's page cache, so
    a crash immediately after a *successful* install left the ledger holding the
    install and the matrix holding nothing.  Two stores, one event, two
    different answers.
    """
    observed: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        # Whatever the process has handed to the kernel must already be there.
        # A size smaller than the file on disk means flush() did not run before
        # fsync(), so the bytes were never really pushed.
        observed.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)

    matrix.record(_entry(hello))
    assert len(observed) == 1, "every append must be forced to the device"
    assert observed[0] == matrix.path.stat().st_size, "flush must precede fsync"

    matrix.record(_entry(hello, reason="second observation"))
    assert len(observed) == 2
    assert observed[1] > observed[0], "the second observation grew the file on disk"


def test_the_matrix_and_the_install_ledger_force_every_append_with_one_discipline(
    tmp_path, hello, monkeypatch
):
    """The symmetry the docstring claims, proved rather than asserted.

    One install writes two records.  Count the forced writes for each: if the
    matrix does fewer than the ledger, the matrix is the half that can still
    lose an observation the ledger keeps, and ``unipad history`` and the
    compatibility matrix then answer "is this package installed" differently
    with nothing to reconcile them.
    """
    observed: list[int] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        observed.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)

    ledger = InstallLedger(tmp_path / "install-ledger.jsonl")
    ledger.record_install(
        InstallResult(package_sha256=hello.sha256, stage="installed", trust_mode="unsigned")
    )
    ledger_forced = len(observed)

    observed.clear()
    matrix = CompatibilityMatrix(tmp_path / "compat-matrix.jsonl")
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            established_conditions=(EstablishedCondition.DRM_FREE,),
            install_result=_installed_result(hello),
            reason="guest installed it",
        )
    )
    matrix_forced = len(observed)

    assert ledger_forced == 1
    assert matrix_forced == ledger_forced, (
        "the ledger and the matrix are two records of one install; a store that "
        "is not forced to the device can still lose the half the other keeps"
    )


# ---------------------------------------------------------------------------
# callchain finding #7: a verdict must not move backwards
# ---------------------------------------------------------------------------


def test_an_untested_observation_does_not_erase_a_positive_verdict(matrix, hello):
    """``untested`` records no observation, so it cannot retract one.

    ``Verdict.UNTESTED`` is this module's own definition of *no observation*:
    "Never run. The default, and the only honest answer for a package with no
    observation behind it."  Letting such an entry become the current entry for
    a key that has an observation behind it makes ``counts()`` report
    ``untested: 1`` for a package the matrix's own history says installed -- the
    operator reads "never run" while a line on disk records ``installs-only``.

    That is the same absence-of-evidence-reading-as-presence-of-failure mistake
    the install-stage guard already prevents, on the axis this module *does* own
    a closed, ranked vocabulary for.  The observation is still appended and still
    reachable through :meth:`CompatibilityMatrix.observations_for`; it simply is
    not the current answer.
    """
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            established_conditions=(EstablishedCondition.DRM_FREE,),
            install_result=_installed_result(hello),
            reason="guest installed it",
        )
    )
    assert matrix.counts()[Verdict.INSTALLS_ONLY] == 1

    matrix.record(
        _entry(
            hello,
            verdict=Verdict.UNTESTED,
            install_result=_installed_result(hello),
            reason="re-observed, no verdict recorded",
        )
    )

    assert matrix.entry_for(_key()).verdict is Verdict.INSTALLS_ONLY
    assert matrix.counts()[Verdict.INSTALLS_ONLY] == 1
    assert matrix.counts()[Verdict.UNTESTED] == 0
    assert len(matrix.entries) == 1
    assert len(matrix.observations) == 2


def test_an_untested_observation_does_not_erase_a_rejection(matrix, hello):
    """The same guard on the other outcome a host can actually record.

    A rejection carries a stable error code and a reason -- it is the only
    compatibility evidence a Windows host can produce today.  An entry that
    establishes nothing must not be able to make ``counts()`` forget it.
    """
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.REJECTED,
            error_code=ErrorCode.UNSUPPORTED_ARCH,
            reason="no_arm64_slice",
        )
    )
    assert matrix.counts()[Verdict.REJECTED] == 1

    matrix.record(_entry(hello, reason="re-checked on the host"))

    assert matrix.entry_for(_key()).verdict is Verdict.REJECTED
    assert matrix.counts()[Verdict.REJECTED] == 1
    assert matrix.counts()[Verdict.UNTESTED] == 0
    assert len(matrix.observations) == 2


def test_the_verdict_regression_does_not_come_back_through_the_file(matrix, hello):
    """The regression must not return through the door the file came in by."""
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            established_conditions=(EstablishedCondition.DRM_FREE,),
            install_result=_installed_result(hello),
            reason="guest installed it",
        )
    )
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.UNTESTED,
            install_result=_installed_result(hello),
            reason="re-observed, no verdict recorded",
        )
    )

    reloaded = CompatibilityMatrix(matrix.path)
    assert reloaded.entry_for(_key()).verdict is Verdict.INSTALLS_ONLY
    assert reloaded.counts()[Verdict.INSTALLS_ONLY] == 1
    assert reloaded.counts()[Verdict.UNTESTED] == 0
    assert len(reloaded.observations) == 2


def test_a_rebuild_that_was_never_run_is_the_current_entry(matrix, hello, notes):
    """The carve-out that keeps the guard from overstating support.

    A differing ``package_sha256`` is a different set of bytes, so a rebuild of
    the same version is a new sample -- the same scoping :meth:`_refuse_merge`
    uses for the PRD 5.1 record.  The newest sample names the same stage but no
    verdict, so ``untested`` is the honest current answer for the key, and
    suppressing it would report the *previous* build's outcome as if it were
    this one's.
    """
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            established_conditions=(EstablishedCondition.DRM_FREE,),
            install_result=_installed_result(hello),
            reason="guest installed the first build",
        )
    )
    matrix.record(
        _entry(
            notes,
            verdict=Verdict.UNTESTED,
            install_result=_installed_result(notes),
            reason="rebuilt 1.0, installed but no verdict recorded",
        )
    )

    assert matrix.entry_for(_key()).verdict is Verdict.UNTESTED
    assert matrix.entry_for(_key()).package_sha256 == notes.sha256
    assert len(matrix.observations) == 2


def test_an_observation_that_names_no_sample_cannot_erase_one_that_does(matrix, hello):
    """The guard may not be bypassed by omitting the hash.

    ``record`` re-validates through ``to_dict`` / ``from_dict``, which coerces
    ``package_sha256`` to ``""``.  An entry that names no sample is treated as
    the same sample as the one it would retract, for the same reason the
    authorisation gate refuses an entry that declares nothing: absence is not
    evidence of difference.  It names the same install stage, so only the
    missing hash could let it through.
    """
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            established_conditions=(EstablishedCondition.DRM_FREE,),
            install_result=_installed_result(hello),
            reason="guest installed it",
        )
    )
    blank = _entry(
        hello,
        verdict=Verdict.UNTESTED,
        install_result=_installed_result(hello),
        reason="re-observed, no hash recorded",
    )
    blank.package_sha256 = ""
    matrix.record(blank)

    assert matrix.entry_for(_key()).verdict is Verdict.INSTALLS_ONLY
    assert matrix.counts()[Verdict.INSTALLS_ONLY] == 1


def test_a_later_outcome_still_replaces_an_earlier_one(matrix, hello):
    """The guard is scoped to ``untested``; every other verdict is an outcome.

    A second run that genuinely got less far is a real outcome and must be the
    current entry -- hiding it would overstate support, which is the other
    direction of the same mistake.  Pinned here so the new guard cannot quietly
    grow into "the first positive verdict wins forever".
    """
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            established_conditions=(EstablishedCondition.DRM_FREE,),
            install_result=_installed_result(hello),
            reason="guest installed it",
        )
    )
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.REJECTED,
            install_result=InstallResult(
                package_sha256=hello.sha256, stage="transferred", error_code="transfer_failed"
            ),
            error_code="transfer_failed",
            reason="second run never installed",
        )
    )

    assert matrix.entry_for(_key()).verdict is Verdict.REJECTED
    assert matrix.counts()[Verdict.INSTALLS_ONLY] == 0


# ---------------------------------------------------------------------------
# perf review F4: the read paths are indexed, not scanned
# ---------------------------------------------------------------------------


class _CountingKey(MatrixKey):
    """A :class:`MatrixKey` that records every equality test it takes part in.

    The only way to measure a read path's *work* without a wall clock: an
    indexed lookup needs at most one comparison, a scan needs one per
    observation in the file.  Equality is by value rather than by exact class,
    so the probe still matches the plain keys the matrix stores.
    """

    comparisons: list = []

    def __eq__(self, other):
        type(self).comparisons.append(other)
        if not isinstance(other, MatrixKey):
            return NotImplemented
        return (self.package_version, self.system_build, self.run_config) == (
            other.package_version,
            other.system_build,
            other.run_config,
        )

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return MatrixKey.__hash__(self)


class _CountingBundle(str):
    """A bundle id that records every equality test it takes part in."""

    comparisons: list = []

    def __eq__(self, other):
        type(self).comparisons.append(other)
        return str.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return str.__hash__(self)


def test_observations_for_reads_the_index_instead_of_scanning_the_matrix(matrix, hello, notes):
    """A per-key report must not cost a scan of the whole matrix.

    ``observations_for`` filtered ``self._observations``, so a report over k
    keys cost O(n*k): measured at 839 ms for 40 keys over 50 000 observations,
    which is the shape of any per-key report a ``unipad matrix`` surface would
    produce.  ``_load`` already walks every line to build ``_observations`` and
    ``_latest``, so the grouping it threw away cost nothing to keep.
    """
    for version in ("1.0", "2.0", "3.0"):
        matrix.record(_entry(hello, key=_key(package_version=version), reason=version))
    matrix.record(_entry(notes, key=_key(package_version="4.0"), reason="notes"))

    _CountingKey.comparisons = []
    probe = _CountingKey(**_key(package_version="3.0").to_dict())
    found = matrix.observations_for(probe)

    assert [e.reason for e in found] == ["3.0"], "the index must answer the right question"
    assert len(_CountingKey.comparisons) <= 1, (
        "an indexed lookup must not compare against every observation in the file"
    )


def test_the_observations_index_survives_a_reload_and_a_displacement(matrix, hello, notes):
    """The index is rebuilt from the file, and a refused promotion keeps the
    observation in the history it belongs to.

    ``_promote`` may decline to make an observation current; the observation
    itself is still appended and still belongs to its key's history, so the
    grouping has to hold every line the loader admitted -- not only the ones
    that became current.
    """
    matrix.record(
        _entry(
            hello,
            verdict=Verdict.INSTALLS_ONLY,
            established_conditions=(EstablishedCondition.DRM_FREE,),
            install_result=_installed_result(hello),
            reason="guest installed it",
        )
    )
    matrix.record(_entry(hello, reason="re-checked on the host, no guest"))
    matrix.record(_entry(notes, key=_key(package_version="2.0"), reason="notes"))

    reloaded = CompatibilityMatrix(matrix.path)
    assert [e.reason for e in reloaded.observations_for(_key())] == [
        "guest installed it",
        "re-checked on the host, no guest",
    ]
    assert [e.reason for e in reloaded.observations_for(_key(package_version="2.0"))] == ["notes"]
    assert reloaded.observations_for(_key(package_version="9.9")) == ()


def test_entries_for_bundle_still_answers_what_a_scan_of_the_current_entries_would(
    matrix, hello, notes
):
    """``entries_for_bundle`` is a reduction over the current entries, in
    ``entries`` order -- and it is pinned against exactly that.

    A bundle id is not part of the key, so one key's current entry can move
    between bundles (a rebuild under one key is a new sample).  An index grouped
    by bundle would have to be re-ordered on every such move to keep answering
    in ``entries`` order, so the reduction is left as it is and pinned here:
    the method's cost is the size of its own answer, and its answer is not
    allowed to change shape while that is true.
    """
    matrix.record(_entry(hello, reason="first"))
    matrix.record(_entry(notes, key=_key(package_version="2.0"), reason="second"))
    # A rebuild under the same key: appended, current, and a different bundle.
    matrix.record(_entry(notes, key=_key(), reason="rebuilt under the first key"))

    for bundle_id in (hello.bundle_id, notes.bundle_id, "com.unipad.nobody"):
        assert matrix.entries_for_bundle(bundle_id) == tuple(
            e for e in matrix.entries if e.bundle_id == bundle_id
        )

    # And the cost is the size of the answer, not a scan of the file: one
    # comparison per *current* entry, never one per observation ever recorded.
    _CountingBundle.comparisons = []
    probe = _CountingBundle(notes.bundle_id)
    assert len(matrix.entries_for_bundle(probe)) == 2
    assert len(_CountingBundle.comparisons) == len(matrix.entries)
    assert len(_CountingBundle.comparisons) < len(matrix.observations)


def test_counts_still_agrees_with_the_current_entries(matrix, hello, notes):
    """``counts`` is a reduction over the current entries, so it stays one.

    It is O(k) where k is the number of current entries -- the size of the
    answer, the same class as ``entries`` -- and it is pinned here against a
    brute-force recomputation so that any future index cannot make it quietly
    wrong.
    """
    matrix.record(_entry(hello, reason="first"))
    matrix.record(
        _entry(notes, key=_key(package_version="2.0"), verdict=Verdict.REJECTED, reason="x")
    )
    matrix.record(_entry(hello, reason="third"))

    tally = {verdict: 0 for verdict in VERDICTS}
    for entry in matrix.entries:
        tally[entry.verdict] += 1
    assert matrix.counts() == tally
    assert matrix.counts()[Verdict.UNTESTED] == 1
    assert matrix.counts()[Verdict.REJECTED] == 1
