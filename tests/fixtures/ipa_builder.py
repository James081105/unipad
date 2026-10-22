"""IPA (zip) fixture builders for the UniPad install chain (PRD 5.1 / 10).

The package precheck, the installer and the transfer layer all need *real*
``.ipa`` archives: a zip with a single ``Payload/<Name>.app`` bundle, a
binary ``Info.plist``, a Mach-O main executable and the optional embedded
framework.  Hand-editing those by hand in every test is how a test suite
starts lying about what it covers, so the shapes live here once.

Every builder writes to the path it is given (tests should pass a
``tmp_path`` member) and returns that :class:`~pathlib.Path`.  Nothing is
cached, nothing is written into the repository working tree, and no fixture
contains a secret (PRD 9.2).

Corrupt variants are built by *changing the archive*, never by post-editing
a good one, so a variant cannot accidentally inherit the good structure:

===============  =====================================================
``corrupt=``     What the precheck must report
===============  =====================================================
``no_payload``   ``invalid_package`` / ``no_app_bundle``
``no_executable``  ``invalid_package`` / ``missing_executable``
``bad_plist``    ``invalid_package`` / ``unreadable_info_plist``
``two_apps``     ``invalid_package`` / ``multiple_app_bundles``
``simulator``    ``unsupported_arch`` / ``simulator_platform``
``encrypted``    ``unsupported_platform`` / ``encrypted_binary``
===============  =====================================================

Determinism (testing-strategy D3): zip entries use a fixed timestamp and a
fixed compression setting, so two runs produce byte-identical archives and
the recorded package SHA-256 is reproducible.
"""

from __future__ import annotations

import plistlib
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from unipad.ipa.macho import PLATFORM_IOSSIMULATOR

from .macho_builder import (
    DEFAULT_DEVICE_DYLIBS,
    FRAMEWORK_INSTALL_NAME,
    X86_64,
    build_thin_macho,
    default_device_slice,
    dylib_slice,
)

__all__ = [
    "CORRUPT_VARIANTS",
    "DEFAULT_APP_NAME",
    "DEFAULT_BUNDLE_ID",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_DEVICE_DYLIBS",
    "FRAMEWORK_INSTALL_NAME",
    "FRAMEWORK_MEMBER",
    "PKGINFO_MEMBER",
    "NOTES_CONTAINER_MARKER",
    "NOTES_CONTAINER_MARKER_BYTES",
    "build_ipa",
    "build_app_members",
    "valid_device_ipa",
    "hello_ipa",
    "notes_ipa",
]

#: Sample app identity used by the presets (PRD 5.1).
DEFAULT_APP_NAME = "HelloUniPad"
DEFAULT_BUNDLE_ID = "com.unipad.hello"
DEFAULT_EXECUTABLE = "HelloUniPad"

#: Embedded framework member (inside the ``.app`` bundle).
FRAMEWORK_MEMBER = "Frameworks/F.framework/F"

#: ``PkgInfo`` member -- present in real IPAs, ignored by the precheck.
PKGINFO_MEMBER = "PkgInfo"

#: Marker file the UniPadNotes preset carries in its container (PRD 5.1:
#: "输入测试文本，保存到应用容器").  Content is deterministic; a test that
#: needs a per-run value should rebuild the IPA with its own bytes.
NOTES_CONTAINER_MARKER = "Documents/container-data.txt"
NOTES_CONTAINER_MARKER_BYTES = b"unipad-notes-container-marker\n"

#: Every accepted ``corrupt=`` value.  Unknown values raise ``ValueError``
#: instead of silently building a *valid* IPA (a typo must not look like a
#: fixture that happens to pass).
CORRUPT_VARIANTS = frozenset(
    {
        "no_payload",
        "no_executable",
        "bad_plist",
        "two_apps",
        "simulator",
        "encrypted",
    }
)

#: Fixed zip metadata: no clock, no host locale, no random generator.
_ZIP_DATE = (2026, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o644 << 16


def build_ipa(
    path: str | Path,
    app_name: str = DEFAULT_APP_NAME,
    bundle_id: str = DEFAULT_BUNDLE_ID,
    executable: str = DEFAULT_EXECUTABLE,
    platform: str = "iPhoneOS",
    macho_bytes: bytes | None = None,
    extra_files: Mapping[str, bytes | str] | None = None,
    min_os: float | str = 17.0,
    include_plist_extra: Mapping[str, Any] | None = None,
    corrupt: str | Iterable[str] | None = None,
    *,
    include_framework: bool = False,
    include_pkginfo: bool = True,
) -> Path:
    """Write an ``.ipa`` (zip) fixture and return its path.

    Args:
        path: Destination file; parent directories are created.
        app_name: ``.app`` directory name (``Payload/<app_name>.app``).
        bundle_id: ``CFBundleIdentifier``.
        executable: ``CFBundleExecutable`` (also the member file name).
        platform: ``CFBundleSupportedPlatforms`` value (``iPhoneOS`` or
            ``iPhoneSimulator``).
        macho_bytes: Main executable image.  Defaults to a valid iOS arm64
            Device slice.  An explicit value wins over ``corrupt="simulator"``
            / ``corrupt="encrypted"`` so the caller stays in control.
        extra_files: Extra ``.app`` members as ``{relative path: bytes}``.
        min_os: ``MinimumOSVersion`` (numbers are rendered as ``"17.0"`` --
            the precheck reads a *string*).
        include_plist_extra: Keys merged into the Info.plist.
        corrupt: One or more of :data:`CORRUPT_VARIANTS`.
        include_framework: Embed ``Frameworks/F.framework/F`` (a dylib
            Mach-O).  When ``macho_bytes`` is ``None`` the generated
            executable also links it and carries the matching ``LC_RPATH``.
        include_pkginfo: Write a ``PkgInfo`` member.

    Returns:
        The path written.

    Raises:
        ValueError: unknown ``corrupt`` variant.
    """
    variants = _normalise_corrupt(corrupt)
    destination = Path(path)
    if destination.parent and not destination.parent.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)

    is_simulator = "simulator" in variants

    entries: list[tuple[str, bytes]] = []
    if "no_payload" in variants:
        # A zip that is not an app package at all: no ``Payload/`` prefix.
        entries.append(("iTunesArtwork", b"\x00" * 16))
        entries.append(("META-INF/notes.txt", b"not an ipa\n"))
    else:
        if macho_bytes is None:
            macho_bytes = _default_executable(
                simulator=is_simulator,
                encrypted="encrypted" in variants,
                include_framework=include_framework,
            )
        entries.extend(build_app_members(
            app_name=app_name,
            bundle_id=bundle_id,
            executable=executable,
            platform="iPhoneSimulator" if is_simulator else platform,
            macho_bytes=macho_bytes,
            extra_files=extra_files,
            min_os=min_os,
            include_plist_extra=include_plist_extra,
            include_framework=include_framework,
            include_pkginfo=include_pkginfo,
        ))
        if "no_executable" in variants:
            entries = [
                item for item in entries
                if item[0] != f"Payload/{app_name}.app/{executable}"
            ]
        if "bad_plist" in variants:
            entries = [
                (name, _garbage_plist() if name.endswith("/Info.plist") else data)
                for name, data in entries
            ]
        if "two_apps" in variants:
            entries.extend(
                _second_app_members(
                    platform="iPhoneSimulator" if is_simulator else platform
                )
            )

    _write_zip(destination, entries)
    return destination


def build_app_members(
    *,
    app_name: str = DEFAULT_APP_NAME,
    bundle_id: str = DEFAULT_BUNDLE_ID,
    executable: str = DEFAULT_EXECUTABLE,
    platform: str = "iPhoneOS",
    macho_bytes: bytes | None = None,
    extra_files: Mapping[str, bytes | str] | None = None,
    min_os: float | str = 17.0,
    include_plist_extra: Mapping[str, Any] | None = None,
    include_framework: bool = False,
    include_pkginfo: bool = True,
) -> list[tuple[str, bytes]]:
    """Build the ``(member name, bytes)`` pairs of one ``.app`` bundle.

    Split out from :func:`build_ipa` so a test that needs to assemble an
    unusual archive (extra bundles, missing members, renamed plists) can
    reuse the plist/executable logic without going through ``corrupt=``.

    When ``macho_bytes`` is supplied it is written verbatim: the framework
    bundle is still embedded by ``include_framework``, but linking it is the
    caller's job (that combination is a legitimate "framework present but
    not linked" fixture).  With ``macho_bytes=None`` the generated image links
    :data:`DEFAULT_DEVICE_DYLIBS` plus the embedded framework.
    """
    app_dir = f"Payload/{app_name}.app"

    if macho_bytes is None:
        macho_bytes = _default_executable(
            simulator=False,
            encrypted=False,
            include_framework=include_framework,
        )

    entries: list[tuple[str, bytes]] = [
        (f"{app_dir}/Info.plist", _info_plist_bytes(
            app_name=app_name,
            bundle_id=bundle_id,
            executable=executable,
            platform=platform,
            min_os=min_os,
            include_plist_extra=include_plist_extra,
        )),
        (f"{app_dir}/{executable}", macho_bytes),
    ]
    if include_framework:
        entries.append((f"{app_dir}/{FRAMEWORK_MEMBER}", dylib_slice()))
    if include_pkginfo:
        entries.append((f"{app_dir}/{PKGINFO_MEMBER}", b"APPLUniP"))
    for relative, data in (extra_files or {}).items():
        entries.append((f"{app_dir}/{_normalise_member(relative)}", _as_bytes(data)))
    return entries


def valid_device_ipa(path: str | Path) -> Path:
    """A complete, acceptable iOS Device IPA (arm64 slice, clear binary)."""
    return build_ipa(path)


def hello_ipa(path: str | Path) -> Path:
    """The PRD 5.1 ``HelloUniPad.ipa`` sample."""
    return build_ipa(
        path,
        app_name=DEFAULT_APP_NAME,
        bundle_id=DEFAULT_BUNDLE_ID,
        executable=DEFAULT_EXECUTABLE,
    )


def notes_ipa(
    path: str | Path,
    *,
    marker_bytes: bytes = NOTES_CONTAINER_MARKER_BYTES,
    **overrides: Any,
) -> Path:
    """The PRD 5.1 ``UniPadNotes.ipa`` sample.

    Carries the container-data marker file that the persistence acceptance
    test rewrites and reads back.  Extra keyword arguments are forwarded to
    :func:`build_ipa` (e.g. ``include_framework=True``).
    """
    params: dict[str, Any] = dict(
        app_name="UniPadNotes",
        bundle_id="com.unipad.notes",
        executable="UniPadNotes",
        extra_files={NOTES_CONTAINER_MARKER: marker_bytes},
    )
    params.update(overrides)
    return build_ipa(path, **params)


# --------------------------------------------------------------------------
# internal helpers
# --------------------------------------------------------------------------


def _default_executable(
    *,
    simulator: bool,
    encrypted: bool,
    include_framework: bool,
) -> bytes:
    """Build the default main executable for a generated ``.app`` bundle.

    Single source of truth for the generated image, so ``build_ipa`` and
    ``build_app_members`` cannot drift apart on which dylibs an app links.
    """
    dylibs = list(DEFAULT_DEVICE_DYLIBS)
    rpaths: list[str] = []
    if include_framework:
        dylibs.append(FRAMEWORK_INSTALL_NAME)
        rpaths.append("@executable_path/Frameworks")
    if simulator:
        return build_thin_macho(
            cputype=X86_64,
            platform=PLATFORM_IOSSIMULATOR,
            dylibs=dylibs,
            rpaths=rpaths,
        )
    if encrypted:
        return build_thin_macho(encrypted=True, dylibs=dylibs, rpaths=rpaths)
    return build_thin_macho(dylibs=dylibs, rpaths=rpaths)


def _normalise_corrupt(corrupt: str | Iterable[str] | None) -> frozenset[str]:
    """Validate and normalise the ``corrupt`` argument."""
    if corrupt is None:
        return frozenset()
    values = {corrupt} if isinstance(corrupt, str) else set(corrupt)
    unknown = sorted(values - CORRUPT_VARIANTS)
    if unknown:
        raise ValueError(
            f"unknown corrupt variant(s): {', '.join(unknown)}; "
            f"expected one of {', '.join(sorted(CORRUPT_VARIANTS))}"
        )
    return frozenset(values)


def _normalise_member(relative: str) -> str:
    """Normalise a caller-supplied member path to a clean relative form."""
    cleaned = relative.replace("\\", "/").lstrip("/")
    if not cleaned:
        raise ValueError("extra_files keys must be non-empty relative paths")
    return cleaned


def _as_bytes(data: bytes | str) -> bytes:
    """Encode a str member payload; bytes pass through."""
    return data if isinstance(data, bytes) else str(data).encode("utf-8")


def _min_os_string(min_os: float | str) -> str:
    """Render ``MinimumOSVersion`` as the string the precheck expects."""
    if isinstance(min_os, str):
        return min_os
    return f"{float(min_os):.1f}"


def _info_plist_bytes(
    *,
    app_name: str,
    bundle_id: str,
    executable: str,
    platform: str,
    min_os: float | str,
    include_plist_extra: Mapping[str, Any] | None,
) -> bytes:
    """Serialise the ``Info.plist`` as a *binary* plist (PRD 5.1)."""
    plist: dict[str, Any] = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": executable,
        "CFBundleName": app_name,
        "CFBundleDisplayName": app_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "MinimumOSVersion": _min_os_string(min_os),
        "CFBundleSupportedPlatforms": [platform],
        "UIDeviceFamily": [2],  # iPad
    }
    if include_plist_extra:
        plist.update(include_plist_extra)
    return plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)


def _garbage_plist() -> bytes:
    """Bytes that are not a plist in any format (binary, XML or JSON-ish)."""
    return b"this is not a plist\x00\x01\x02 neither binary nor xml"


def _second_app_members(platform: str) -> list[tuple[str, bytes]]:
    """A second ``Payload/*.app`` bundle (``two_apps`` variant)."""
    return build_app_members(
        app_name="SecondUniPad",
        bundle_id="com.unipad.second",
        executable="SecondUniPad",
        platform=platform,
        macho_bytes=default_device_slice(),
        include_pkginfo=False,
    )


def _write_zip(path: Path, entries: Iterable[tuple[str, bytes]]) -> None:
    """Write ``entries`` with fixed metadata for byte-stable output."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = _ZIP_FILE_MODE
            archive.writestr(info, data)
