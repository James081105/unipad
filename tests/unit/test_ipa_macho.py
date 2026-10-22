"""Mach-O / universal-binary parser tests (PRD 5.1, 10 "包预检").

PRD 5.1 requires the host-side package precheck to inspect the main
executable's **Mach-O CPU slices**, the **platform identifier**, the
**minimum OS version**, the **linked dylibs** and the **encryption
information** of an IPA -- and states explicitly that "只看后缀或 CPU 架构
不足以通过检查" (the file name or the CPU architecture alone is not enough).
``unipad.ipa.macho`` is where those facts come from, so these tests pin the
facts the precheck depends on, using the structurally valid images built by
``tests.fixtures.macho_builder`` (real load commands, not hand-edited blobs).

Contracts pinned here:

* a big-endian FAT container's ``fat_arch`` entries select the right slice and
  an arm64 slice becomes the ``primary`` the precheck judges;
* an x86_64-only image is *not* arm64, whatever its platform says;
* ``LC_BUILD_VERSION`` platform numbers follow ``loader.h``/``ld.hpp``
  (1 == macOS, 2 == iOS, 7 == iPhoneSimulator) -- they are **not** 1-based, and
  swapping them would misclassify every real Device IPA;
* ``LC_ENCRYPTION_INFO(_64)`` with ``cryptid != 0`` marks a FairPlay/DRM
  binary, which UniPad must reject (PRD 5.1: samples must be DRM-free);
* ``LC_CODE_SIGNATURE`` presence is reported as an observable fact (never as a
  trust decision);
* anything that is not a readable Mach-O raises ``invalid_package``.

Everything runs from bytes in memory or files under ``tmp_path``: no process,
no network, no clock, no randomness (docs/testing-strategy.md D3).
"""

from __future__ import annotations

import json
import struct

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.ipa.macho import (
    CPU_TYPE_ARM,
    CPU_TYPE_ARM64,
    CPU_TYPE_X86_64,
    FAT_MAGIC,
    MH_MAGIC_64,
    PLATFORM_IOS,
    PLATFORM_IOSSIMULATOR,
    PLATFORM_MACOS,
    parse_macho,
    parse_macho_bytes,
    parse_macho_stream,
)
from tests.fixtures.macho_builder import (
    ARM32,
    ARM64,
    X86_64,
    align_up,
    arm32_ios_slice,
    arm64e_slice,
    build_fat,
    build_thin_macho,
    default_device_slice,
    device_fat,
    dylib_slice,
    mixed_platform_fat,
    simulator_slice,
    x86_64_ios_slice,
)


# --------------------------------------------------------------------------
# fat / universal containers (PRD 5.1: Mach-O CPU slice inspection)
# --------------------------------------------------------------------------


def test_fat_container_with_arm64_slice_selects_arm64_primary(tmp_path):
    """PRD 5.1: a universal image is judged on its arm64 Device slice."""
    # arrange -- arm64 + x86_64, both declaring the iOS platform
    container = device_fat()

    # act
    info = parse_macho_bytes(container, source="device.ipa/executable")

    # assert
    assert info.fat is True
    assert info.is_64_fat is False
    assert info.size_bytes == len(container)
    assert [s.cputype_name for s in info.slices] == ["arm64", "x86_64"]
    assert info.slice_cputypes == ["arm64", "x86_64"]
    assert info.primary is info.slices[0]
    assert info.primary.is_arm64 is True
    assert info.primary.cputype == CPU_TYPE_ARM64
    assert info.primary.cputype_name == "arm64"
    assert info.has_arm64 is True
    # The fat header is big-endian and its first entry is 4096-byte aligned
    # (``lipo`` behaviour), so a correct parse must land there.
    assert info.primary.offset == align_up(8 + 20 * 2, 4096)
    assert info.primary.size == len(default_device_slice())


def test_primary_is_the_arm64_slice_even_when_it_is_not_first(tmp_path):
    """PRD 5.1: the *arm64* slice is judged, not merely the first entry.

    ``lipo`` writes a universal binary in any order, and a container whose
    first slice is x86_64 must still be judged on its arm64 Device slice.
    Selecting ``slices[0]`` instead would let a Simulator-first image be
    accepted on the strength of an architecture it does not run.
    """
    # arrange -- x86_64 first, arm64 second (the reverse of ``device_fat``)
    container = build_fat([x86_64_ios_slice(), default_device_slice()])
    # the container really does declare x86_64 first (fat_arch is big-endian)
    declared = [struct.unpack_from(">II", container, 8 + 20 * i) for i in range(2)]
    assert declared[0][0] == X86_64
    assert declared[1][0] == ARM64
    image = tmp_path / "reversed.fat"
    image.write_bytes(container)

    # act
    info = parse_macho(image)

    # assert -- file order is preserved ...
    assert [s.cputype_name for s in info.slices] == ["x86_64", "arm64"]
    assert info.primary is info.slices[1]
    # ... but the primary slice is the arm64 one
    assert info.primary.is_arm64 is True
    assert info.primary.cputype_name == "arm64"
    assert info.arm64_slices == [info.primary]
    assert info.primary.offset != info.slices[0].offset


def test_fat_x86_64_only_has_no_arm64_slice():
    """PRD 10: no ARM64 slice is an architecture fault, not a platform one."""
    # arrange -- x86_64 simulator + x86_64 "iOS" slices
    container = build_fat([simulator_slice(), x86_64_ios_slice()])

    # act
    info = parse_macho_bytes(container)

    # assert
    assert info.fat is True
    assert info.has_arm64 is False
    assert info.arm64_slices == []
    assert info.primary is not None
    assert info.primary.is_arm64 is False
    assert info.primary.cputype == CPU_TYPE_X86_64
    assert info.primary.cputype_name == "x86_64"
    # A slice can be x86_64 and still not be a simulator build: the two facts
    # are independent and both are reported.
    assert info.platforms == [PLATFORM_IOSSIMULATOR, PLATFORM_IOS]


def test_arm64e_slice_counts_as_arm64():
    """arm64e shares cputype with arm64 and must not be dropped."""
    # arrange / act
    info = parse_macho_bytes(arm64e_slice())

    # assert
    assert info.primary.cputype == CPU_TYPE_ARM64
    assert info.primary.is_arm64 is True
    assert info.primary.is_arm64e is True
    assert info.primary.cputype_name == "arm64e"
    assert info.has_arm64 is True


def test_mixed_platform_fat_keeps_arm64_primary_and_lists_both_platforms():
    """An arm64 Device + x86_64 Simulator image still reports both facts."""
    # arrange / act
    info = parse_macho_bytes(mixed_platform_fat())

    # assert
    assert info.primary.is_arm64 is True
    assert info.primary.platform_name == "iOS"
    assert info.platforms == [PLATFORM_IOS, PLATFORM_IOSSIMULATOR]
    assert info.primary.is_simulator is False
    assert info.slices[1].is_simulator is True


def test_fat_slice_outside_the_image_is_rejected():
    """A fat_arch pointing past EOF is an invalid package, not a silent skip."""
    # arrange -- one fat entry whose offset is far beyond the buffer
    header = struct.pack(">II", 0xCAFEBABE, 1)
    entry = struct.pack(">IIIII", ARM64, 0, 4096, 128, 12)
    blob = header + entry + b"\x00" * 64

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        parse_macho_bytes(blob)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "slice_out_of_bounds"


# --------------------------------------------------------------------------
# LC_BUILD_VERSION: platform identifier and minimum OS (PRD 5.1)
# --------------------------------------------------------------------------


def test_thin_arm64_reports_platform_and_min_os():
    """PRD 5.1: the platform identifier and min OS come from LC_BUILD_VERSION."""
    # arrange
    image = build_thin_macho(
        platform=PLATFORM_IOS, minos=(17, 2), sdk=(17, 5)
    )

    # act
    info = parse_macho_bytes(image)

    # assert
    assert info.fat is False
    assert info.primary.platform == PLATFORM_IOS
    assert info.primary.platform_name == "iOS"
    assert info.primary.minos == (17, 2, 0)
    assert info.primary.sdk == (17, 5, 0)
    assert info.primary.minos_string == "17.2.0"
    assert info.primary.is_simulator is False


def test_platform_numbers_follow_loader_h_not_a_one_based_sequence():
    """1 == macOS, 2 == iOS, 7 == iPhoneSimulator (loader.h / ld.hpp).

    The numbering is *not* the order the platforms were introduced.  A build
    with ``platform=1`` is a macOS image the precheck must reject with
    ``unsupported_platform``; if these constants are ever "tidied" into a
    1-based sequence every real Device IPA is misclassified.
    """
    # arrange / act / assert -- constants
    assert PLATFORM_MACOS == 1
    assert PLATFORM_IOS == 2
    assert PLATFORM_IOSSIMULATOR == 7

    # ... and the same numbers decoded out of real images
    macos = parse_macho_bytes(build_thin_macho(platform=PLATFORM_MACOS))
    ios = parse_macho_bytes(build_thin_macho(platform=PLATFORM_IOS))
    sim = parse_macho_bytes(build_thin_macho(platform=PLATFORM_IOSSIMULATOR))
    assert macos.primary.platform_name == "macOS"
    assert ios.primary.platform_name == "iOS"
    assert sim.primary.platform_name == "iPhoneSimulator"
    assert sim.primary.is_simulator is True
    assert ios.primary.is_simulator is False


def test_version_min_iphoneos_is_accepted_as_ios():
    """LC_VERSION_MIN_IPHONEOS (pre-Xcode-10 images) still identifies iOS."""
    # arrange -- a hand-packed 32-bit-era command, no LC_BUILD_VERSION
    commands = [
        struct.pack("<II", 0x25, 8 + 8) + struct.pack("<II", 0x000F0000, 0x000F0000),
    ]
    sizeofcmds = sum(len(cmd) for cmd in commands)
    header = struct.pack(
        "<8I", MH_MAGIC_64, ARM64, 0, 2, len(commands), sizeofcmds, 0x85, 0
    )
    image = header + b"".join(commands) + b"\x00" * 64

    # act
    info = parse_macho_bytes(image)

    # assert
    assert info.primary.platform == PLATFORM_IOS
    assert info.primary.platform_name == "iOS"
    assert info.primary.minos == (15, 0, 0)


# --------------------------------------------------------------------------
# encryption information (PRD 5.1: samples must be DRM-free)
# --------------------------------------------------------------------------


def test_encrypted_slice_reports_nonzero_cryptid():
    """cryptid != 0 is FairPlay/DRM: the precheck must be able to see it."""
    # arrange
    image = build_thin_macho(encrypted=True)

    # act
    info = parse_macho_bytes(image)

    # assert
    assert info.encrypted is True
    assert info.cryptid == 1
    assert info.primary.encrypted is True
    assert info.primary.cryptid == 1


def test_explicit_cryptid_is_reported_verbatim():
    """A cryptid other than 1 is still a non-zero (DRM) value."""
    # arrange / act
    info = parse_macho_bytes(build_thin_macho(encrypted=True, cryptid=3))

    # assert
    assert info.primary.cryptid == 3
    assert info.primary.encrypted is True
    assert info.encrypted is True


def test_clear_slice_reports_zero_cryptid():
    """A DRM-free sample must not look encrypted."""
    # arrange / act
    info = parse_macho_bytes(default_device_slice())

    # assert
    assert info.encrypted is False
    assert info.cryptid == 0
    assert info.primary.encrypted is False


def test_thirty_two_bit_encryption_command_is_decoded():
    """32-bit images carry LC_ENCRYPTION_INFO (no ``pad`` field)."""
    # arrange / act
    info = parse_macho_bytes(build_thin_macho(cputype=ARM32, cpusubtype=9, is_64=False, encrypted=True))

    # assert
    assert info.primary.is_64 is False
    assert info.primary.encrypted is True
    assert info.primary.cryptid == 1


# --------------------------------------------------------------------------
# code signature, dylibs and rpaths (PRD 5.1)
# --------------------------------------------------------------------------


def test_code_signature_command_is_reported():
    """LC_CODE_SIGNATURE presence is an observable fact, never a trust claim."""
    # arrange
    image = build_thin_macho(has_code_signature=True)

    # act
    info = parse_macho_bytes(image)

    # assert
    assert info.primary.has_code_signature is True
    assert info.primary.code_signature_offset is not None
    assert info.primary.code_signature_offset > 0
    assert info.primary.code_signature_size == 32
    # The declared blob must lie inside the image the builder wrote.
    assert info.primary.code_signature_offset + info.primary.code_signature_size <= len(image)


def test_absent_code_signature_is_reported_as_absent():
    """An unsigned image must not inherit a signature fact."""
    # arrange / act
    info = parse_macho_bytes(build_thin_macho(has_code_signature=False))

    # assert
    assert info.primary.has_code_signature is False
    assert info.primary.code_signature_offset is None
    assert info.primary.code_signature_size is None


def test_linked_dylibs_and_rpaths_are_listed_in_order():
    """PRD 5.1: the linked dylibs (dyld dependencies) must be reported."""
    # arrange
    dylibs = (
        "/System/Library/Frameworks/UIKit.framework/UIKit",
        "/System/Library/Frameworks/Foundation.framework/Foundation",
        "/usr/lib/libobjc.A.dylib",
    )
    rpaths = ("@executable_path/Frameworks", "@loader_path/../Frameworks")
    image = build_thin_macho(dylibs=dylibs, rpaths=rpaths)

    # act
    info = parse_macho_bytes(image)

    # assert
    assert info.primary.dylibs == list(dylibs)
    assert info.primary.rpaths == list(rpaths)
    assert info.dylibs == list(dylibs)
    assert info.rpaths == list(rpaths)


def test_dylib_filetype_and_install_name_are_reported():
    """An embedded framework image is MH_DYLIB with an LC_ID_DYLIB name."""
    # arrange / act
    info = parse_macho_bytes(dylib_slice())

    # assert
    assert info.primary.filetype_name == "dylib"
    assert info.primary.install_name == "@rpath/F.framework/F"
    assert info.primary.dylibs == [
        "/System/Library/Frameworks/Foundation.framework/Foundation"
    ]
    # The framework image is deliberately unsigned in the fixture.
    assert info.primary.has_code_signature is False


def test_dylibs_are_deduplicated_across_slices():
    """Two slices linking the same dylib report it once, in file order."""
    # arrange
    shared = ("/usr/lib/libobjc.A.dylib",)
    container = build_fat(
        [
            build_thin_macho(dylibs=shared + ("/usr/lib/libSystem.B.dylib",)),
            build_thin_macho(cputype=X86_64, dylibs=shared),
        ]
    )

    # act
    info = parse_macho_bytes(container)

    # assert
    assert info.dylibs == [
        "/usr/lib/libobjc.A.dylib",
        "/usr/lib/libSystem.B.dylib",
    ]


# --------------------------------------------------------------------------
# 32-bit and byte-swapped spellings (module docstring contract)
# --------------------------------------------------------------------------


def test_thirty_two_bit_magic_is_handled():
    """A 32-bit MH_MAGIC image decodes instead of being called garbage."""
    # arrange
    image = arm32_ios_slice()

    # act
    info = parse_macho_bytes(image)

    # assert
    assert info.primary.is_64 is False
    # ``endian`` is the struct character; ``to_dict`` spells it out.
    assert info.primary.endian == "<"
    assert info.primary.to_dict()["endian"] == "little"
    assert info.primary.cputype == CPU_TYPE_ARM
    assert info.primary.cputype_name == "arm"
    assert info.primary.filetype_name == "executable"
    assert info.primary.platform_name == "iOS"
    assert info.primary.encrypted is False


def test_byte_swapped_sixty_four_bit_magic_is_decoded():
    """MH_CIGAM_64 decodes with swapped fields, not as an invalid package."""
    # arrange -- the same image written big-endian
    image = build_thin_macho(
        platform=PLATFORM_IOS,
        minos=(16, 1),
        dylibs=("/usr/lib/libobjc.A.dylib",),
        endian=">",
    )

    # act
    info = parse_macho_bytes(image)

    # assert
    assert info.primary.is_64 is True
    assert info.primary.endian == ">"
    assert info.primary.to_dict()["endian"] == "big"
    assert info.primary.platform == PLATFORM_IOS
    assert info.primary.minos == (16, 1, 0)
    assert info.primary.dylibs == ["/usr/lib/libobjc.A.dylib"]


# --------------------------------------------------------------------------
# rejection paths (PRD 10: ``invalid_package``)
# --------------------------------------------------------------------------


def test_garbage_bytes_are_rejected():
    """Bytes that are not a Mach-O at all are an invalid package."""
    # arrange
    blob = b"this is definitely not a Mach-O image, just text"

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        parse_macho_bytes(blob, source="garbage.bin")
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "bad_magic"
    assert excinfo.value.details.get("source") == "garbage.bin"


def test_empty_file_is_rejected(tmp_path):
    """PRD 10: an empty file is ``invalid_package``, not a zero-slice image."""
    # arrange
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        parse_macho(empty)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "truncated"


def test_truncated_in_memory_image_is_rejected():
    """Fewer than 4 bytes cannot even hold a magic."""
    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        parse_macho_bytes(b"\xfe\xed")
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "truncated"


def test_truncated_file_on_disk_is_rejected_with_reason_truncated(tmp_path):
    """A file that starts with a valid magic but ends mid-header is invalid.

    This is the stream path :mod:`unipad.ipa.package` uses for a zip member,
    so the reason must be the machine-readable ``truncated`` rather than a
    bare "not a Mach-O" (PRD 10).
    """
    # arrange -- a real arm64 image cut off inside its 32-byte header
    truncated = tmp_path / "truncated.executable"
    truncated.write_bytes(default_device_slice()[:20])

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        parse_macho(truncated)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "truncated"
    assert excinfo.value.details.get("source") == str(truncated)


def test_truncated_fat_header_is_rejected_with_a_stable_reason(tmp_path):
    """A fat container cut inside its arch table is invalid, not empty."""
    # arrange -- FAT_MAGIC + nfat_arch=2 but only one entry's worth of bytes
    blob = struct.pack(">II", FAT_MAGIC, 2) + struct.pack(
        ">IIIII", ARM64, 0, 4096, 128, 12
    )
    truncated = tmp_path / "truncated.fat"
    truncated.write_bytes(blob)

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        parse_macho(truncated)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "truncated_fat_header"


def test_missing_file_is_rejected(tmp_path):
    """A missing executable is an invalid package with a stable reason."""
    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        parse_macho(tmp_path / "does-not-exist")
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "unreadable_file"


def test_fat_header_with_no_slices_is_rejected():
    """FAT_MAGIC followed by nfat_arch == 0 declares nothing."""
    # arrange
    blob = struct.pack(">II", 0xCAFEBABE, 0) + b"\x00" * 16

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        parse_macho_bytes(blob)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") in {
        "empty_fat_container",
        "implausible_fat_arch_count",
    }


# --------------------------------------------------------------------------
# stream parsing (used by unipad.ipa.package for zip members)
# --------------------------------------------------------------------------


def test_parse_macho_stream_reads_from_a_zip_member(tmp_path):
    """The package reader parses the executable straight out of the archive."""
    import zipfile

    from tests.fixtures.ipa_builder import hello_ipa

    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    with zipfile.ZipFile(ipa, "r") as archive:
        member = "Payload/HelloUniPad.app/HelloUniPad"
        declared = archive.getinfo(member).file_size

        # act
        with archive.open(member, "r") as handle:
            info = parse_macho_stream(
                handle, size=declared, source=member
            )

    # assert
    assert info.primary.is_arm64 is True
    assert info.primary.platform_name == "iOS"
    assert info.primary.has_code_signature is True
    assert info.size_bytes == declared


def test_parse_macho_stream_restores_the_caller_position(tmp_path):
    """A shared stream must not be left moved by the inspection."""
    # arrange
    image = tmp_path / "thin.bin"
    image.write_bytes(default_device_slice())
    with image.open("rb") as handle:
        handle.seek(7)

        # act
        info = parse_macho_stream(handle, source=str(image))
        position_after = handle.tell()

    # assert
    assert position_after == 7
    assert info.primary.is_arm64 is True


def test_builder_images_are_deterministic():
    """testing-strategy D3: the same builder call yields identical bytes."""
    # act / assert
    assert build_thin_macho() == build_thin_macho()
    assert device_fat() == device_fat()
    assert build_thin_macho(endian=">") == build_thin_macho(endian=">")


# --------------------------------------------------------------------------
# evidence serialisation (PRD 9.2: the run's artifact manifest is JSON)
# --------------------------------------------------------------------------


def test_parsed_facts_round_trip_through_json():
    """PRD 9.2: the slice/image facts must survive as JSON evidence.

    The verifier reads the recorded facts, not the prose, so ``to_dict`` has
    to be JSON-serialisable for a fat image, a thin image and a byte-swapped
    one alike (tuples become lists, ``endian`` becomes a word).
    """
    # arrange
    fat = parse_macho_bytes(device_fat(), source="Payload/A.app/A")
    thin = parse_macho_bytes(build_thin_macho(minos=(17, 2), endian=">"))

    # act
    payload = json.loads(json.dumps([fat.to_dict(), thin.to_dict()], sort_keys=True))

    # assert -- the container level
    assert payload[0]["fat"] is True
    assert payload[0]["slice_count"] == 2
    assert payload[0]["slice_cputypes"] == ["arm64", "x86_64"]
    assert payload[0]["has_arm64"] is True
    assert payload[0]["source"] == "Payload/A.app/A"
    assert payload[0]["size_bytes"] == len(device_fat())
    assert payload[0]["primary"]["cputype_name"] == "arm64"
    # ... the slice level
    primary = payload[0]["slices"][0]
    assert primary["minos"] == [17, 0, 0]
    assert primary["minos_string"] == "17.0.0"
    assert primary["platform_name"] == "iOS"
    assert primary["is_simulator"] is False
    assert primary["encrypted"] is False
    assert primary["cryptid"] == 0
    assert primary["dylibs"][0].startswith("/System/Library/Frameworks/")
    assert primary["endian"] == "little"
    assert primary["parse_notes"] == []
    assert isinstance(primary["segments"], list)
    # ... and the byte-swapped thin image keeps a decodable endian word
    assert payload[1]["primary"]["endian"] == "big"
    assert payload[1]["primary"]["minos"] == [17, 2, 0]
