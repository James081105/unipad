"""IPA (zip) package reader for the host-side install chain (PRD 5.1 / 9 / 10).

An IPA is a zip archive whose single ``Payload/<Name>.app`` directory holds
the Info.plist, the main Mach-O executable and any embedded frameworks.
This module turns that archive into a :class:`PackageInfo` fact sheet --
bundle identity, platform, minimum OS, permissions, linked dylibs and the
Mach-O slice facts from :mod:`unipad.ipa.macho`.

Rules enforced here (PRD 5.1: "只看后缀或 CPU 架构不足以通过检查"):

* the whole-file SHA-256 is computed in chunks and reported, never assumed;
* exactly one ``.app`` bundle may exist -- two is ``invalid_package``;
* the Info.plist must exist and parse (binary *or* XML plist);
* ``CFBundleIdentifier`` and the ``CFBundleExecutable`` file must exist;
* the main executable is parsed as Mach-O from the archive stream, so a
  corrupt executable is reported instead of being silently skipped.

The package is only ever opened for reading: nothing is extracted to disk
and nothing is mutated (PRD 10, "包预检" must not change the input).
"""

from __future__ import annotations

import hashlib
import plistlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ErrorCode, UniPadError
from .macho import MachOInfo, parse_macho_stream

__all__ = ["PackageInfo", "open_ipa", "sha256_file", "read_zip_member", "iter_ipa_members"]

_HASH_CHUNK = 1024 * 1024
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_APP_SUFFIX = ".app"
_PAYLOAD_PREFIX = "Payload/"


@dataclass
class PackageInfo:
    """Fact sheet for one IPA, extracted without executing or mutating it.

    ``sha256`` is the hash of the whole ``.ipa`` file (PRD 5.2 step 3 asks
    for the input package SHA-256 to be recorded before anything else
    happens).  ``macho`` is ``None`` only when the main executable could
    not be parsed at all; ``macho_error`` then carries the stable reason.
    """

    path: str
    sha256: str
    size_bytes: int
    app_dir: str                                  # 'Payload/T.app'
    app_bundle_name: str                          # 'T.app'
    bundle_id: str
    executable: str                               # 'Payload/T.app/T'
    bundle_name: str | None = None
    display_name: str | None = None
    bundle_version: str | None = None
    short_version: str | None = None
    min_os: str | None = None
    supported_platforms: list[str] = field(default_factory=list)
    device_family: list[int] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    privacy_keys: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    dylibs: list[str] = field(default_factory=list)
    macho: MachOInfo | None = None
    macho_error: str | None = None

    @property
    def app_name(self) -> str:
        """Best human-facing app name (display name, then bundle name, then dir)."""
        return self.display_name or self.bundle_name or Path(self.app_bundle_name).stem

    @property
    def has_arm64_slice(self) -> bool:
        """True when the main executable contains an ARM64 slice."""
        return bool(self.macho is not None and self.macho.has_arm64)

    @property
    def linked_dylibs(self) -> list[str]:
        """Dylib names the executable links against (dyld dependencies).

        Distinct from ``dylibs``, which lists ``.dylib`` *files* embedded in
        the bundle.
        """
        return list(self.macho.dylibs) if self.macho is not None else []

    @property
    def rpaths(self) -> list[str]:
        """``LC_RPATH`` entries of the main executable."""
        return list(self.macho.rpaths) if self.macho is not None else []

    @property
    def encrypted(self) -> bool:
        """True when any slice reports ``cryptid != 0``."""
        return bool(self.macho is not None and self.macho.encrypted)

    @property
    def slice_cputypes(self) -> list[str]:
        """Distinct slice architecture names, in file order."""
        return list(self.macho.slice_cputypes) if self.macho is not None else []

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view.

        The raw Info.plist is deliberately *not* included: it may carry
        keys this project has no business copying into logs or evidence
        (PRD 9.2 forbids user data and secrets in artifacts).
        """
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "app_dir": self.app_dir,
            "app_bundle_name": self.app_bundle_name,
            "app_name": self.app_name,
            "bundle_id": self.bundle_id,
            "executable": self.executable,
            "bundle_name": self.bundle_name,
            "display_name": self.display_name,
            "bundle_version": self.bundle_version,
            "short_version": self.short_version,
            "min_os": self.min_os,
            "supported_platforms": list(self.supported_platforms),
            "device_family": list(self.device_family),
            "required_capabilities": list(self.required_capabilities),
            "privacy_keys": list(self.privacy_keys),
            "frameworks": list(self.frameworks),
            "dylibs": list(self.dylibs),
            "has_arm64_slice": self.has_arm64_slice,
            "slice_cputypes": self.slice_cputypes,
            "encrypted": self.encrypted,
            "linked_dylibs": self.linked_dylibs,
            "rpaths": self.rpaths,
            "macho": self.macho.to_dict() if self.macho is not None else None,
            "macho_error": self.macho_error,
        }


def sha256_file(path: str | Path, chunk_size: int = _HASH_CHUNK) -> str:
    """Return the hex SHA-256 of a file, read in ``chunk_size`` chunks.

    Raises:
        UniPadError: ``invalid_package`` when the file cannot be read.
    """
    p = Path(path)
    digester = hashlib.sha256()
    try:
        with p.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digester.update(chunk)
    except OSError as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"cannot read package: {p}",
            reason="unreadable_file",
            path=str(p),
        ) from exc
    return digester.hexdigest()


def read_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    """Read one zip member into memory (bounded by ``_MAX_MEMBER_BYTES``).

    Raises:
        UniPadError: ``invalid_package`` when the member is missing or too
            large to inspect safely.
    """
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"package member is missing: {name}",
            reason="missing_member",
            member=name,
        ) from exc
    if info.file_size > _MAX_MEMBER_BYTES:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"package member is too large to inspect: {name}",
            reason="member_too_large",
            member=name,
            size_bytes=info.file_size,
        )
    with archive.open(name, "r") as handle:
        return handle.read()


def open_ipa(path: str | Path) -> PackageInfo:
    """Open an IPA and build its :class:`PackageInfo` fact sheet.

    Args:
        path: ``.ipa`` file (a zip archive).  Read-only.

    Returns:
        PackageInfo: bundle identity, platform facts, embedded frameworks
            and the parsed main executable.

    Raises:
        UniPadError: ``invalid_package`` with a ``reason`` detail for every
            structural problem (missing file, not a zip, no or multiple
            ``.app`` bundles, unreadable Info.plist, missing executable).
    """
    p = Path(path)
    if not p.is_file():
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"IPA file does not exist: {p}",
            reason="missing_file",
            path=str(p),
        )
    digest = sha256_file(p)
    size_bytes = p.stat().st_size

    try:
        archive = zipfile.ZipFile(p, "r")
    except zipfile.BadZipFile as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "IPA is not a readable zip archive",
            reason="not_a_zip",
            path=str(p),
            sha256=digest,
        ) from exc
    except OSError as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"cannot open IPA: {p}",
            reason="unreadable_file",
            path=str(p),
        ) from exc

    with archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        app_dirs = _find_app_dirs(names)
        app_dir = app_dirs[0]
        plist = _read_info_plist(archive, app_dir)

        bundle_id = _plist_string(plist, "CFBundleIdentifier")
        if not bundle_id:
            raise UniPadError(
                ErrorCode.INVALID_PACKAGE,
                "Info.plist has no CFBundleIdentifier",
                reason="missing_bundle_id",
                app_dir=app_dir,
                sha256=digest,
            )
        executable_name = _plist_string(plist, "CFBundleExecutable")
        if not executable_name:
            raise UniPadError(
                ErrorCode.INVALID_PACKAGE,
                "Info.plist has no CFBundleExecutable",
                reason="missing_executable_name",
                app_dir=app_dir,
                bundle_id=bundle_id,
            )
        executable = f"{app_dir}/{executable_name}"
        if executable not in archive.namelist():
            raise UniPadError(
                ErrorCode.INVALID_PACKAGE,
                f"main executable is absent from the package: {executable}",
                reason="missing_executable",
                app_dir=app_dir,
                bundle_id=bundle_id,
                executable=executable,
            )

        frameworks, dylibs = _list_embedded_code(app_dir, names)

        info = PackageInfo(
            path=str(p),
            sha256=digest,
            size_bytes=size_bytes,
            app_dir=app_dir,
            app_bundle_name=Path(app_dir).name,
            bundle_id=bundle_id,
            executable=executable,
            bundle_name=_plist_string(plist, "CFBundleName"),
            display_name=_plist_string(plist, "CFBundleDisplayName"),
            bundle_version=_plist_string(plist, "CFBundleVersion"),
            short_version=_plist_string(plist, "CFBundleShortVersionString"),
            min_os=_plist_string(plist, "MinimumOSVersion"),
            supported_platforms=_plist_strings(plist, "CFBundleSupportedPlatforms"),
            device_family=_plist_ints(plist, "UIDeviceFamily"),
            required_capabilities=_plist_strings(plist, "UIRequiredDeviceCapabilities"),
            privacy_keys=_privacy_keys(plist),
            frameworks=frameworks,
            dylibs=dylibs,
        )

        info.macho, info.macho_error = _parse_executable(archive, executable, digest)
        return info


def iter_ipa_members(path: str | Path) -> list[str]:
    """Return the file member names of an IPA (diagnostic helper).

    Raises:
        UniPadError: ``invalid_package`` when the archive cannot be read.
    """
    p = Path(path)
    try:
        with zipfile.ZipFile(p, "r") as archive:
            return [n for n in archive.namelist() if not n.endswith("/")]
    except (zipfile.BadZipFile, OSError) as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "IPA is not a readable zip archive",
            reason="not_a_zip",
            path=str(p),
        ) from exc


# --------------------------------------------------------------------------
# internal helpers
# --------------------------------------------------------------------------


def _find_app_dirs(names: list[str]) -> list[str]:
    """Return the distinct ``Payload/<Name>.app`` directories in an archive."""
    found: list[str] = []
    for name in names:
        normalised = name[2:] if name.startswith("./") else name
        if not normalised.startswith(_PAYLOAD_PREFIX):
            continue
        remainder = normalised[len(_PAYLOAD_PREFIX) :]
        head = remainder.split("/", 1)[0]
        if head.endswith(_APP_SUFFIX) and head:
            app_dir = f"{_PAYLOAD_PREFIX}{head}"
            if app_dir not in found:
                found.append(app_dir)
    if not found:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "IPA contains no Payload/*.app bundle",
            reason="no_app_bundle",
        )
    if len(found) > 1:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "IPA contains more than one app bundle",
            reason="multiple_app_bundles",
            app_dirs=found,
        )
    return found


def _read_info_plist(archive: zipfile.ZipFile, app_dir: str) -> dict[str, Any]:
    """Read and decode ``<app>/Info.plist`` (binary or XML plist)."""
    member = f"{app_dir}/Info.plist"
    if member not in archive.namelist():
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "Info.plist is absent from the app bundle",
            reason="missing_info_plist",
            app_dir=app_dir,
            member=member,
        )
    raw = read_zip_member(archive, member)
    try:
        plist = plistlib.loads(raw)
    except Exception as exc:  # plistlib raises ValueError/subclasses
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "Info.plist could not be parsed",
            reason="unreadable_info_plist",
            app_dir=app_dir,
            detail=type(exc).__name__,
        ) from exc
    if not isinstance(plist, dict):
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            "Info.plist is not a dictionary",
            reason="invalid_info_plist",
            app_dir=app_dir,
            plist_type=type(plist).__name__,
        )
    return plist


def _parse_executable(
    archive: zipfile.ZipFile,
    executable: str,
    package_sha256: str,
) -> tuple[MachOInfo | None, str | None]:
    """Parse the main executable from the archive stream.

    A Mach-O parse failure is *returned* as ``(None, reason)`` rather than
    raised: the package structure is valid, so the precheck can report the
    executable problem as its own fault instead of masking the rest.
    """
    try:
        with archive.open(executable, "r") as handle:
            info = parse_macho_stream(handle, size=archive.getinfo(executable).file_size, source=executable)
    except UniPadError as exc:
        return None, str(exc.details.get("reason") or exc.code)
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        return None, f"unreadable_executable:{type(exc).__name__}"
    return info, None


def _list_embedded_code(app_dir: str, names: list[str]) -> tuple[list[str], list[str]]:
    """List embedded ``.framework`` bundles and ``.dylib`` files.

    Paths are reported relative to the ``.app`` directory.
    """
    prefix = f"{app_dir}/"
    frameworks: list[str] = []
    dylibs: list[str] = []
    for name in names:
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix) :]
        parts = relative.split("/")
        framework = _framework_dir(parts)
        if framework is not None and framework not in frameworks:
            frameworks.append(framework)
        if relative.endswith(".dylib") and relative not in dylibs:
            dylibs.append(relative)
    return sorted(frameworks), sorted(dylibs)


def _framework_dir(parts: list[str]) -> str | None:
    """Return ``Frameworks/A.framework`` for a path inside a framework bundle."""
    for index, part in enumerate(parts):
        if part.endswith(".framework"):
            return "/".join(parts[: index + 1])
    return None


def _plist_string(plist: dict[str, Any], key: str) -> str | None:
    """Read a string plist value, tolerating non-string types."""
    value = plist.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _plist_strings(plist: dict[str, Any], key: str) -> list[str]:
    """Read a plist value that may be a string or a list of strings."""
    value = plist.get(key)
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip() and item not in out:
                out.append(item.strip())
        return out
    return []


def _plist_ints(plist: dict[str, Any], key: str) -> list[int]:
    """Read a plist value that may be an int or a list of ints."""
    value = plist.get(key)
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, int) and not isinstance(item, bool)]
    return []


def _privacy_keys(plist: dict[str, Any]) -> list[str]:
    """Return the Info.plist privacy usage-description keys (names only).

    Only the *key names* are reported: the descriptive text belongs to the
    app author and must not be copied into logs or evidence (PRD 9.2).
    """
    return sorted(
        key
        for key in plist
        if isinstance(key, str) and key.endswith("UsageDescription")
    )
