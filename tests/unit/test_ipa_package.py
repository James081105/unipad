"""IPA (zip) package reader tests (PRD 5.1, 9.2, 10 "包预检").

``unipad.ipa.package`` turns a ``.ipa`` archive into a fact sheet.  PRD 5.1
requires the precheck to inspect ``Payload/*.app``, the ``Info.plist``, the
main executable, the Mach-O slices, the platform, the minimum OS, the dylibs
and the encryption info -- and PRD 5.2 step 3 requires the *input package
SHA-256* to be recorded before anything else happens.  These tests pin that
behaviour against the real archives built by ``tests.fixtures.ipa_builder``.

Contracts pinned here:

* ``open_ipa`` hashes the whole file in chunks and the digest equals
  ``hashlib.sha256`` of the same bytes (never a length- or name-derived value);
* the Info.plist fields the install chain needs are extracted (bundle id,
  executable name, minimum OS, supported platforms, ...);
* every corrupt variant the builder documents is rejected with
  ``invalid_package`` **and** a machine-readable ``reason`` detail, so the
  caller can tell *why* without parsing prose;
* a Simulator platform is detected from the plist *and* from the Mach-O
  ``LC_BUILD_VERSION`` platform;
* embedded frameworks/dylibs and privacy usage-description **key names** are
  listed -- the descriptive *text* belongs to the app author and must never
  reach logs or evidence (PRD 9.2).

The package is only ever opened for reading: nothing is extracted to disk and
nothing is mutated.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import zipfile

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.ipa.package import (
    PackageInfo,
    iter_ipa_members,
    open_ipa,
    read_zip_member,
    sha256_file,
)
from tests.fixtures.ipa_builder import (
    CORRUPT_VARIANTS,
    DEFAULT_BUNDLE_ID,
    DEFAULT_EXECUTABLE,
    FRAMEWORK_INSTALL_NAME,
    FRAMEWORK_MEMBER,
    NOTES_CONTAINER_MARKER,
    NOTES_CONTAINER_MARKER_BYTES,
    PKGINFO_MEMBER,
    build_app_members,
    build_ipa,
    hello_ipa,
    notes_ipa,
    valid_device_ipa,
)
from tests.fixtures.macho_builder import (
    arm64e_slice,
    device_fat,
    mixed_platform_fat,
)

#: ``corrupt=`` variant -> the ``reason`` detail the reader must report.
#: Straight from the builder's documented table (ipa_builder module docstring).
CORRUPT_REASONS = {
    "no_payload": "no_app_bundle",
    "no_executable": "missing_executable",
    "bad_plist": "unreadable_info_plist",
    "two_apps": "multiple_app_bundles",
}


# --------------------------------------------------------------------------
# whole-file digest (PRD 5.2 step 3)
# --------------------------------------------------------------------------


def test_open_ipa_records_the_chunked_sha256(tmp_path):
    """PRD 5.2 step 3: the input package hash is recorded before anything else."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    raw = ipa.read_bytes()

    # act
    info = open_ipa(ipa)

    # assert
    assert isinstance(info, PackageInfo)
    assert info.sha256 == hashlib.sha256(raw).hexdigest()
    assert len(info.sha256) == 64
    assert info.size_bytes == len(raw)
    assert info.path == str(ipa)


def test_sha256_file_is_chunked_without_changing_the_digest(tmp_path):
    """A tiny chunk size must produce the same digest as one big read."""
    # arrange
    ipa = valid_device_ipa(tmp_path / "chunked.ipa")
    expected = hashlib.sha256(ipa.read_bytes()).hexdigest()

    # act / assert -- 1, 7 and 1 MiB chunk sizes all agree
    assert sha256_file(ipa, chunk_size=1) == expected
    assert sha256_file(ipa, chunk_size=7) == expected
    assert sha256_file(ipa, chunk_size=1024 * 1024) == expected


def test_sha256_file_reports_an_unreadable_file(tmp_path):
    """A missing package is ``invalid_package`` with a stable reason."""
    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        sha256_file(tmp_path / "nope.ipa")
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "unreadable_file"


# --------------------------------------------------------------------------
# Info.plist extraction (PRD 5.1)
# --------------------------------------------------------------------------


def test_info_plist_fields_are_extracted(tmp_path):
    """The install chain needs bundle identity, executable and minimum OS."""
    # arrange
    ipa = build_ipa(
        tmp_path / "fields.ipa",
        app_name="HelloUniPad",
        bundle_id="com.unipad.hello",
        executable="HelloUniPad",
        platform="iPhoneOS",
        min_os="17.0",
    )

    # act
    info = open_ipa(ipa)

    # assert
    assert info.bundle_id == "com.unipad.hello"
    assert info.executable == "Payload/HelloUniPad.app/HelloUniPad"
    assert info.app_dir == "Payload/HelloUniPad.app"
    assert info.app_bundle_name == "HelloUniPad.app"
    assert info.app_name == "HelloUniPad"
    assert info.min_os == "17.0"
    assert info.supported_platforms == ["iPhoneOS"]
    assert info.bundle_version == "1"
    assert info.short_version == "1.0"
    assert info.device_family == [2]  # iPad
    assert info.required_capabilities == []


def test_min_os_is_read_as_the_plist_string(tmp_path):
    """MinimumOSVersion is a *string* in the plist and is reported verbatim."""
    # arrange / act
    dotted = open_ipa(build_ipa(tmp_path / "dotted.ipa", min_os="16.3"))
    rendered = open_ipa(build_ipa(tmp_path / "rendered.ipa", min_os=15.1))

    # assert
    assert dotted.min_os == "16.3"
    assert rendered.min_os == "15.1"


def test_bundle_executable_name_comes_from_the_plist(tmp_path):
    """CFBundleExecutable (not the directory name) names the Mach-O member."""
    # arrange -- the app directory and the executable have different names
    ipa = build_ipa(
        tmp_path / "renamed.ipa",
        app_name="Container",
        executable="RenamedBinary",
    )

    # act
    info = open_ipa(ipa)

    # assert
    assert info.executable == "Payload/Container.app/RenamedBinary"
    assert info.bundle_id == DEFAULT_BUNDLE_ID
    assert info.macho is not None
    assert info.macho.primary.is_arm64 is True


def test_executable_facts_are_taken_from_the_archive_member(tmp_path):
    """The Mach-O facts come from the archive stream, not from the file name."""
    # arrange
    ipa = hello_ipa(tmp_path / "facts.ipa")

    # act
    info = open_ipa(ipa)

    # assert
    assert info.macho is not None
    assert info.slice_cputypes == ["arm64"]
    assert info.has_arm64_slice is True
    assert info.encrypted is False
    assert info.linked_dylibs == [
        "/System/Library/Frameworks/UIKit.framework/UIKit",
        "/System/Library/Frameworks/Foundation.framework/Foundation",
        "/usr/lib/libobjc.A.dylib",
    ]


def test_fat_executable_is_parsed_from_the_archive(tmp_path):
    """A universal main executable keeps every slice's facts."""
    # arrange
    ipa = build_ipa(tmp_path / "fat.ipa", macho_bytes=device_fat())

    # act
    info = open_ipa(ipa)

    # assert
    assert info.macho is not None
    assert info.macho.fat is True
    assert info.slice_cputypes == ["arm64", "x86_64"]
    assert info.has_arm64_slice is True


def test_corrupt_executable_is_reported_not_raised(tmp_path):
    """A broken Mach-O keeps the package open but records the parse failure."""
    # arrange -- the structure is fine, the executable is garbage
    ipa = build_ipa(tmp_path / "badmacho.ipa", macho_bytes=b"not a mach-o at all")

    # act
    info = open_ipa(ipa)

    # assert
    assert info.bundle_id == DEFAULT_BUNDLE_ID
    assert info.macho is None
    assert info.macho_error
    assert info.has_arm64_slice is False
    assert info.linked_dylibs == []


# --------------------------------------------------------------------------
# corrupt variants (PRD 10: ``invalid_package`` + reason)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("variant", sorted(CORRUPT_REASONS))
def test_corrupt_variants_are_rejected_with_a_reason(tmp_path, variant):
    """PRD 10: every structural problem is ``invalid_package`` + a reason."""
    # arrange
    ipa = build_ipa(tmp_path / f"{variant}.ipa", corrupt=variant)

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        open_ipa(ipa)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == CORRUPT_REASONS[variant]


def test_two_apps_reason_lists_every_app_dir(tmp_path):
    """The rejection detail must name both bundles so it is auditable."""
    # arrange
    ipa = build_ipa(tmp_path / "two.ipa", corrupt="two_apps")

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        open_ipa(ipa)
    assert excinfo.value.details["app_dirs"] == [
        "Payload/HelloUniPad.app",
        "Payload/SecondUniPad.app",
    ]


def test_builder_documents_every_corrupt_variant(tmp_path):
    """The builder's variant set is what these tests parametrise over."""
    # assert -- the four structural variants are all documented variants
    assert set(CORRUPT_REASONS) <= set(CORRUPT_VARIANTS)
    # ... and a typo fails loudly instead of silently building a valid IPA
    with pytest.raises(ValueError):
        build_ipa(tmp_path / "typo.ipa", corrupt="not_a_variant")


def test_missing_file_is_rejected(tmp_path):
    """A package that does not exist never reaches the zip reader."""
    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        open_ipa(tmp_path / "absent.ipa")
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "missing_file"


def test_non_zip_file_is_rejected(tmp_path):
    """An ``.ipa`` suffix is not evidence of a package (PRD 5.1)."""
    # arrange
    bogus = tmp_path / "not-really.ipa"
    bogus.write_bytes(b"this is not a zip archive at all")

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        open_ipa(bogus)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "not_a_zip"


def test_missing_info_plist_is_rejected(tmp_path):
    """A renamed/absent Info.plist is a structural fault."""
    # arrange -- an .app whose plist member was dropped
    import zipfile

    members = build_app_members()
    ipa = tmp_path / "noplist.ipa"
    with zipfile.ZipFile(ipa, "w") as archive:
        for name, data in members:
            if name.endswith("/Info.plist"):
                continue
            archive.writestr(name, data)

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        open_ipa(ipa)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "missing_info_plist"


def test_missing_bundle_id_is_rejected(tmp_path):
    """CFBundleIdentifier is mandatory: without it there is no install target."""
    # arrange
    ipa = build_ipa(
        tmp_path / "noid.ipa",
        include_plist_extra={"CFBundleIdentifier": ""},
    )

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        open_ipa(ipa)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "missing_bundle_id"


def test_read_zip_member_reports_a_missing_member(tmp_path):
    """The member reader has its own stable reason."""
    # arrange
    ipa = hello_ipa(tmp_path / "members.ipa")

    # act / assert
    with zipfile.ZipFile(ipa, "r") as archive:
        with pytest.raises(UniPadError) as excinfo:
            read_zip_member(archive, "Payload/HelloUniPad.app/Absent")
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "missing_member"


# --------------------------------------------------------------------------
# platform detection (PRD 5.1 / 10)
# --------------------------------------------------------------------------


def test_simulator_platform_is_detected(tmp_path):
    """A Simulator build is recognised from the plist *and* the Mach-O."""
    # arrange
    ipa = build_ipa(tmp_path / "sim.ipa", corrupt="simulator")

    # act
    info = open_ipa(ipa)

    # assert
    assert info.supported_platforms == ["iPhoneSimulator"]
    assert info.slice_cputypes == ["x86_64"]
    assert info.has_arm64_slice is False
    assert info.macho is not None
    assert info.macho.primary.platform_name == "iPhoneSimulator"
    assert info.macho.primary.is_simulator is True


def test_simulator_slice_inside_an_arm64_fat_is_visible(tmp_path):
    """One simulator slice is enough to reject the package (PRD 5.1)."""
    # arrange -- arm64 Device + x86_64 Simulator in one executable
    ipa = build_ipa(tmp_path / "mixed.ipa", macho_bytes=mixed_platform_fat())

    # act
    info = open_ipa(ipa)

    # assert
    assert info.has_arm64_slice is True
    assert info.macho is not None
    assert info.macho.platforms == [2, 7]
    assert [s.is_simulator for s in info.macho.slices] == [False, True]


def test_arm64e_is_reported_as_arm64(tmp_path):
    """arm64e is a device architecture and must be listed as such."""
    # arrange
    ipa = build_ipa(tmp_path / "arm64e.ipa", macho_bytes=arm64e_slice())

    # act
    info = open_ipa(ipa)

    # assert
    assert info.slice_cputypes == ["arm64e"]
    assert info.has_arm64_slice is True


# --------------------------------------------------------------------------
# embedded code and privacy keys (PRD 5.1 / 9.2)
# --------------------------------------------------------------------------


def test_embedded_framework_and_rpath_are_listed(tmp_path):
    """Embedded frameworks and the rpath that finds them are reported."""
    # arrange
    ipa = build_ipa(tmp_path / "fw.ipa", include_framework=True)

    # act
    info = open_ipa(ipa)

    # assert
    assert info.frameworks == ["Frameworks/F.framework"]
    assert info.rpaths == ["@executable_path/Frameworks"]
    assert FRAMEWORK_INSTALL_NAME in info.linked_dylibs
    assert FRAMEWORK_MEMBER.endswith("/F")


def test_embedded_dylib_files_are_listed(tmp_path):
    """``.dylib`` members inside the bundle are listed, not just frameworks."""
    # arrange
    ipa = build_ipa(
        tmp_path / "dylibs.ipa",
        extra_files={
            "PlugIns/libExtra.dylib": b"\x00" * 16,
            "Frameworks/libBundled.dylib": b"\x00" * 16,
            "Assets/data.json": b"{}",
        },
    )

    # act
    info = open_ipa(ipa)

    # assert
    assert info.dylibs == [
        "Frameworks/libBundled.dylib",
        "PlugIns/libExtra.dylib",
    ]
    assert info.frameworks == []


def test_privacy_usage_description_keys_are_listed_without_their_text(tmp_path):
    """PRD 9.2: key names only -- the author's prose never enters evidence."""
    # arrange
    ipa = build_ipa(
        tmp_path / "privacy.ipa",
        include_plist_extra={
            "NSCameraUsageDescription": "the app opens the camera to scan codes",
            "NSLocationWhenInUseUsageDescription": "the app uses your location",
            "UIRequiredDeviceCapabilities": ["arm64"],
        },
    )

    # act
    info = open_ipa(ipa)

    # assert
    assert info.privacy_keys == [
        "NSCameraUsageDescription",
        "NSLocationWhenInUseUsageDescription",
    ]
    assert info.required_capabilities == ["arm64"]
    # The serialised fact sheet must not carry the descriptive text either.
    serialised = json.dumps(info.to_dict(), ensure_ascii=False)
    assert "scan codes" not in serialised
    assert "uses your location" not in serialised


def test_pkginfo_member_is_present_and_ignored(tmp_path):
    """A real IPA carries PkgInfo; the precheck must not care about it."""
    # arrange
    ipa = hello_ipa(tmp_path / "pkginfo.ipa")

    # act
    info = open_ipa(ipa)

    # assert
    assert f"Payload/HelloUniPad.app/{PKGINFO_MEMBER}" in iter_ipa_members(ipa)
    assert info.frameworks == []
    assert info.dylibs == []


def test_notes_ipa_carries_its_container_marker(tmp_path):
    """PRD 5.1: the UniPadNotes sample ships its container data file."""
    # arrange
    ipa = notes_ipa(tmp_path / "UniPadNotes.ipa")

    # act
    info = open_ipa(ipa)
    with zipfile.ZipFile(ipa, "r") as archive:
        member = f"{info.app_dir}/{NOTES_CONTAINER_MARKER}"
        assert member in archive.namelist()
        payload = archive.read(member)

    # assert
    assert info.bundle_id == "com.unipad.notes"
    assert info.executable == "Payload/UniPadNotes.app/UniPadNotes"
    assert payload == NOTES_CONTAINER_MARKER_BYTES


def test_iter_ipa_members_lists_files_only(tmp_path):
    """The diagnostic helper reports members, never directory entries."""
    # arrange / act
    ipa = hello_ipa(tmp_path / "list.ipa")
    names = iter_ipa_members(ipa)

    # assert
    assert "Payload/HelloUniPad.app/Info.plist" in names
    assert "Payload/HelloUniPad.app/HelloUniPad" in names
    assert all(not name.endswith("/") for name in names)


def test_iter_ipa_members_rejects_a_non_zip(tmp_path):
    """Same stable code as ``open_ipa`` for an unreadable archive."""
    # arrange
    bogus = tmp_path / "junk.ipa"
    bogus.write_bytes(b"\x00\x01\x02")

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        iter_ipa_members(bogus)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "not_a_zip"


def test_fact_sheet_to_dict_is_json_serialisable(tmp_path):
    """PRD 9.1: the install interface returns machine-readable state."""
    # arrange
    ipa = hello_ipa(tmp_path / "serial.ipa")

    # act
    payload = json.loads(json.dumps(open_ipa(ipa).to_dict(), sort_keys=True))

    # assert
    assert payload["bundle_id"] == DEFAULT_BUNDLE_ID
    assert payload["executable"] == f"Payload/HelloUniPad.app/{DEFAULT_EXECUTABLE}"
    assert payload["has_arm64_slice"] is True
    assert payload["encrypted"] is False
    assert payload["macho"]["primary"]["platform_name"] == "iOS"


def test_open_ipa_is_read_only_and_repeatable(tmp_path):
    """Opening a package twice yields the same facts and changes nothing."""
    # arrange
    ipa = hello_ipa(tmp_path / "repeat.ipa")
    before = ipa.read_bytes()

    # act
    first = open_ipa(ipa)
    second = open_ipa(ipa)

    # assert
    assert ipa.read_bytes() == before
    assert first.sha256 == second.sha256
    assert first.bundle_id == second.bundle_id
    assert first.macho is not None and second.macho is not None
    assert first.macho.primary.minos == second.macho.primary.minos


def test_info_plist_bytes_round_trip_through_plistlib(tmp_path):
    """The builder really writes a *binary* plist (PRD 5.1)."""
    # arrange
    ipa = hello_ipa(tmp_path / "binary-plist.ipa")

    # act
    with zipfile.ZipFile(ipa, "r") as archive:
        raw = archive.read("Payload/HelloUniPad.app/Info.plist")
        plist = plistlib.loads(raw)

    # assert
    assert raw[:8] == b"bplist00"
    assert plist["CFBundleIdentifier"] == DEFAULT_BUNDLE_ID
    assert plist["CFBundleExecutable"] == DEFAULT_EXECUTABLE
    assert plist["MinimumOSVersion"] == "17.0"
    assert plist["CFBundleSupportedPlatforms"] == ["iPhoneOS"]
