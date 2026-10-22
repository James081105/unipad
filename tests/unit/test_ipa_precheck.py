"""Package precheck tests (PRD 5.1, 10 "包预检", 5.2 step 10).

``unipad.ipa.precheck`` decides whether an IPA may be handed to the guest
install chain at all.  PRD 5.1 is explicit that the file name and the CPU
architecture alone are **not** enough: the platform identifier, the minimum
OS, the linked dylibs and the encryption info must all be inspected, and PRD
5.2 step 10 requires an unsupported-platform package to come back with a
clear reason while the rest of the system stays usable.

Decision table under test (PRD 10) -- identical to the table documented in
``tests.fixtures.ipa_builder``:

======================  ==========================================
Observation             Fault
======================  ==========================================
Simulator platform      ``unsupported_arch`` / ``simulator_platform``
``cryptid != 0``        ``unsupported_platform`` / ``encrypted_binary``
Broken structure/plist  ``invalid_package`` / (builder reason)
No ARM64 slice          ``unsupported_arch`` / ``no_arm64_slice``
======================  ==========================================

Contracts pinned here:

* a real Device IPA is **accepted**, and the report carries the facts that
  justify it (bundle id, arm64 slice, no encryption);
* the verdict is ``reject`` exactly when at least one fault was recorded --
  never "accept with a warning";
* a rejected package still reports its facts, so the rejection is auditable;
* the package file is **never modified**: the SHA-256, the size and the
  mtime are identical before and after the precheck, and nothing is extracted
  to disk.
"""

from __future__ import annotations

import json
import os

import pytest

from unipad.errors import ErrorCode
from unipad.ipa import (
    VERDICT_ACCEPT,
    VERDICT_REJECT,
    run_precheck,
    run_precheck_package,
)
from unipad.ipa.package import open_ipa
from tests.fixtures.ipa_builder import (
    DEFAULT_BUNDLE_ID,
    build_ipa,
    hello_ipa,
)
from tests.fixtures.macho_builder import (
    arm64e_slice,
    build_thin_macho,
    device_fat,
    mixed_platform_fat,
    simulator_slice,
)


def _fingerprint(path) -> tuple[str, int, int]:
    """Return ``(sha256, size, mtime_ns)`` so any mutation is detectable."""
    import hashlib

    stat = os.stat(path)
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        stat.st_size,
        stat.st_mtime_ns,
    )


# --------------------------------------------------------------------------
# accept path (PRD 5.1 sample)
# --------------------------------------------------------------------------


def test_good_device_ipa_is_accepted(tmp_path):
    """PRD 5.1: the HelloUniPad sample is a Device, arm64, DRM-free build."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_ACCEPT
    assert report.accepted is True
    assert report.faults == []
    assert report.fault_codes == []
    assert report.bundle_id == DEFAULT_BUNDLE_ID
    assert report.app_name == "HelloUniPad"
    assert report.has_arm64_slice is True
    assert report.slice_cputypes == ["arm64"]
    assert report.encrypted is False
    assert report.cryptid == 0
    assert report.min_os == "17.0"
    assert report.supported_platforms == ["iPhoneOS"]
    assert report.macho_platforms == ["iOS"]
    assert report.size_bytes > 0
    assert report.package_sha256 == open_ipa(ipa).sha256


def test_arm64e_device_ipa_is_accepted(tmp_path):
    """arm64e is still a device architecture (PRD 5.1: ARM64 slice)."""
    # arrange
    ipa = build_ipa(tmp_path / "arm64e.ipa", macho_bytes=arm64e_slice())

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_ACCEPT
    assert report.has_arm64_slice is True
    assert report.slice_cputypes == ["arm64e"]


def test_fat_device_ipa_is_accepted_on_its_arm64_slice(tmp_path):
    """A universal image is judged on the arm64 slice, not the first one."""
    # arrange -- arm64 Device + x86_64 "iOS"
    ipa = build_ipa(tmp_path / "fat.ipa", macho_bytes=device_fat())

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_ACCEPT
    assert report.has_arm64_slice is True
    assert report.slice_cputypes == ["arm64", "x86_64"]


# --------------------------------------------------------------------------
# reject: platform / architecture (PRD 10)
# --------------------------------------------------------------------------


def test_simulator_variant_is_unsupported_arch(tmp_path):
    """Builder table: ``simulator`` -> ``unsupported_arch``/``simulator_platform``."""
    # arrange
    ipa = build_ipa(tmp_path / "sim.ipa", corrupt="simulator")

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_REJECT
    assert report.accepted is False
    assert "unsupported_arch" in report.fault_codes
    simulator_faults = [f for f in report.faults if f.reason == "simulator_platform"]
    assert len(simulator_faults) == 1
    assert simulator_faults[0].code == ErrorCode.UNSUPPORTED_ARCH
    # ... and the facts that justify the rejection are still reported.
    assert report.has_arm64_slice is False
    assert report.slice_cputypes == ["x86_64"]
    assert report.macho_platforms == ["iPhoneSimulator"]


def test_encrypted_variant_is_unsupported_platform(tmp_path):
    """Builder table: ``encrypted`` -> ``unsupported_platform``/``encrypted_binary``."""
    # arrange
    ipa = build_ipa(tmp_path / "enc.ipa", corrupt="encrypted")

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_REJECT
    assert "unsupported_platform" in report.fault_codes
    encrypted_faults = [f for f in report.faults if f.reason == "encrypted_binary"]
    assert len(encrypted_faults) == 1
    assert encrypted_faults[0].code == ErrorCode.UNSUPPORTED_PLATFORM
    assert "cryptid=1" in encrypted_faults[0].detail
    # PRD 5.1: DRM-free is a build constraint, so the fact is reported too.
    assert report.encrypted is True
    assert report.cryptid == 1


def test_macos_platform_is_unsupported_platform(tmp_path):
    """A macOS/Catalyst build is not an arm64 Device build (PRD 10)."""
    # arrange -- plist says MacOSX *and* LC_BUILD_VERSION says macOS
    ipa = build_ipa(
        tmp_path / "macos.ipa",
        platform="MacOSX",
        macho_bytes=build_thin_macho(platform=1),
    )

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_REJECT
    assert "unsupported_platform" in report.fault_codes
    assert [f.reason for f in report.faults] == ["macos_platform"]


def test_simulator_slice_in_a_universal_image_is_rejected(tmp_path):
    """PRD 5.1: an arm64 slice alone does not make a package installable."""
    # arrange -- arm64 Device + x86_64 Simulator in one executable
    ipa = build_ipa(tmp_path / "mixed.ipa", macho_bytes=mixed_platform_fat())

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_REJECT
    assert report.has_arm64_slice is True          # the arch fact is still true
    assert "unsupported_arch" in report.fault_codes
    assert [f.reason for f in report.faults if f.code == "unsupported_arch"] == [
        "simulator_platform"
    ]


def test_x86_64_only_image_is_unsupported_arch(tmp_path):
    """An x86_64-only executable has no ARM64 slice (PRD 10)."""
    # arrange
    ipa = build_ipa(tmp_path / "x86.ipa", macho_bytes=simulator_slice())

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_REJECT
    assert report.has_arm64_slice is False
    reasons = [f.reason for f in report.faults if f.code == ErrorCode.UNSUPPORTED_ARCH]
    assert "no_arm64_slice" in reasons


# --------------------------------------------------------------------------
# reject: structure (PRD 10 ``invalid_package``)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant,reason",
    [
        ("no_payload", "no_app_bundle"),
        ("no_executable", "missing_executable"),
        ("bad_plist", "unreadable_info_plist"),
        ("two_apps", "multiple_app_bundles"),
    ],
)
def test_corrupt_variants_give_invalid_package(tmp_path, variant, reason):
    """A structurally broken package is one uniform ``invalid_package`` fault."""
    # arrange
    ipa = build_ipa(tmp_path / f"{variant}.ipa", corrupt=variant)

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_REJECT
    assert report.fault_codes == [ErrorCode.INVALID_PACKAGE]
    assert report.faults[0].reason == reason
    assert report.faults[0].detail           # a human-readable note exists
    # No bundle identity can be claimed from a broken package.
    assert report.bundle_id is None


def test_corrupt_executable_is_invalid_package(tmp_path):
    """A garbage Mach-O keeps the package structurally open but unusable."""
    # arrange
    ipa = build_ipa(tmp_path / "badmacho.ipa", macho_bytes=b"not a mach-o")

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_REJECT
    assert report.fault_codes == [ErrorCode.INVALID_PACKAGE]
    assert report.faults[0].reason == "broken_structure"
    assert report.has_arm64_slice is False


def test_missing_file_gives_invalid_package_not_an_exception(tmp_path):
    """``run_precheck`` never raises for a rejectable package (PRD 10)."""
    # act
    report = run_precheck(tmp_path / "absent.ipa")

    # assert
    assert report.verdict == VERDICT_REJECT
    assert report.fault_codes == [ErrorCode.INVALID_PACKAGE]
    assert report.faults[0].reason == "missing_file"
    # An unreadable file has no digest to report.
    assert report.package_sha256 == ""


def test_non_zip_file_gives_invalid_package(tmp_path):
    """The ``.ipa`` suffix is not evidence (PRD 5.1)."""
    # arrange
    bogus = tmp_path / "fake.ipa"
    bogus.write_bytes(b"definitely not a zip")

    # act
    report = run_precheck(bogus)

    # assert
    assert report.verdict == VERDICT_REJECT
    assert report.faults[0].code == ErrorCode.INVALID_PACKAGE
    assert report.faults[0].reason == "not_a_zip"


def test_platform_that_cannot_be_determined_is_invalid_package(tmp_path):
    """Neither plist nor Mach-O identifies a platform -> invalid, not accepted."""
    # arrange -- an arm64 slice whose platform is ``unknown`` and a plist with
    # no CFBundleSupportedPlatforms at all.
    ipa = build_ipa(
        tmp_path / "unknown.ipa",
        platform="",
        macho_bytes=build_thin_macho(platform=0),
    )

    # act
    report = run_precheck(ipa)

    # assert
    assert report.verdict == VERDICT_REJECT
    assert report.fault_codes == [ErrorCode.INVALID_PACKAGE]
    assert report.faults[0].reason == "platform_unknown"
    assert report.macho_platforms == ["unknown"]


# --------------------------------------------------------------------------
# verdict semantics
# --------------------------------------------------------------------------


def test_verdict_is_reject_exactly_when_a_fault_was_recorded(tmp_path):
    """``accept`` is a positive statement; a warning is not a verdict."""
    # arrange
    good = run_precheck(hello_ipa(tmp_path / "good.ipa"))
    simulator = run_precheck(build_ipa(tmp_path / "sim.ipa", corrupt="simulator"))
    encrypted = run_precheck(build_ipa(tmp_path / "enc.ipa", corrupt="encrypted"))
    broken = run_precheck(build_ipa(tmp_path / "bad.ipa", corrupt="bad_plist"))

    # act / assert
    for report in (good, simulator, encrypted, broken):
        assert report.verdict == (VERDICT_ACCEPT if not report.faults else VERDICT_REJECT)
        assert report.accepted is (report.verdict == VERDICT_ACCEPT)
        assert (report.verdict == VERDICT_ACCEPT) is (report.fault_codes == [])


def test_report_is_json_serialisable_for_the_controller(tmp_path):
    """PRD 9.1: the install interface returns machine-readable state."""
    # arrange
    ipa = build_ipa(tmp_path / "enc.ipa", corrupt="encrypted")

    # act
    payload = json.loads(run_precheck(ipa).to_json())

    # assert
    assert payload["verdict"] == VERDICT_REJECT
    assert payload["fault_codes"] == [ErrorCode.UNSUPPORTED_PLATFORM]
    assert payload["faults"][0]["reason"] == "encrypted_binary"
    assert payload["encrypted"] is True
    assert payload["cryptid"] == 1
    assert payload["has_arm64_slice"] is True
    assert payload["package_sha256"] == open_ipa(ipa).sha256


def test_run_precheck_package_matches_run_precheck(tmp_path):
    """The two entry points must agree (the installer reuses the second)."""
    # arrange
    ipa = build_ipa(tmp_path / "both.ipa", corrupt="encrypted")
    package = open_ipa(ipa)

    # act
    from_path = run_precheck(ipa)
    from_package = run_precheck_package(package)

    # assert
    assert from_path.verdict == from_package.verdict
    assert from_path.faults == from_package.faults
    assert from_path.package_sha256 == from_package.package_sha256


def test_documented_variant_table_matches_the_reported_faults(tmp_path):
    """The builder's documented table is what the precheck actually reports.

    ==============  ==========================================
    ``corrupt=``    Fault (code / reason)
    ==============  ==========================================
    ``simulator``   ``unsupported_arch`` / ``simulator_platform``
    ``encrypted``   ``unsupported_platform`` / ``encrypted_binary``
    ==============  ==========================================
    """
    # arrange
    simulator = run_precheck(build_ipa(tmp_path / "sim.ipa", corrupt="simulator"))
    encrypted = run_precheck(build_ipa(tmp_path / "enc.ipa", corrupt="encrypted"))

    # act / assert -- first fault of each variant, code and reason
    assert simulator.fault_codes[0] == ErrorCode.UNSUPPORTED_ARCH
    assert simulator.faults[0].reason == "simulator_platform"
    assert encrypted.fault_codes[0] == ErrorCode.UNSUPPORTED_PLATFORM
    assert encrypted.faults[0].reason == "encrypted_binary"


# --------------------------------------------------------------------------
# read-only guarantee (PRD 10, "包预检" must not change the input)
# --------------------------------------------------------------------------


def test_precheck_never_modifies_the_package_file(tmp_path):
    """PRD 10: the input package is read-only for the whole precheck."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    before = _fingerprint(ipa)

    # act
    report = run_precheck(ipa)
    run_precheck_package(open_ipa(ipa))

    # assert -- bytes, size and mtime are all unchanged
    after = _fingerprint(ipa)
    assert after == before
    assert report.package_sha256 == before[0]


def test_precheck_extracts_nothing_to_disk(tmp_path):
    """Nothing is unpacked next to the package while prechecking."""
    # arrange
    work = tmp_path / "work"
    work.mkdir()
    ipa = hello_ipa(work / "HelloUniPad.ipa")
    before = sorted(entry.name for entry in work.iterdir())

    # act
    run_precheck(ipa)

    # assert
    assert sorted(entry.name for entry in work.iterdir()) == before == [
        "HelloUniPad.ipa"
    ]
