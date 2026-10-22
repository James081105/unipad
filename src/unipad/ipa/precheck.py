"""Package precheck for the IPA install chain (PRD 5.1 / 10, "包预检").

``run_precheck`` answers one question with evidence: *may this IPA be handed
to the guest install chain at all?*  It inspects the package structure, the
Info.plist platform identifier, the Mach-O CPU slices, the minimum OS
version, the linked dylibs and the encryption info -- never the file name or
the ``.ipa`` suffix alone (PRD 5.1).

Decision table (PRD 10):

===========================  =========================================
Observation                  Fault
===========================  =========================================
Simulator-only platform      ``unsupported_arch`` / ``simulator_platform``
No ARM64 slice (x86_64 ...)  ``unsupported_arch`` / ``no_arm64_slice``
macOS / Catalyst platform    ``unsupported_platform`` / ``macos_platform``
``cryptid != 0`` (FairPlay)  ``unsupported_platform`` / ``encrypted_binary``
Broken structure/plist       ``invalid_package`` / ``broken_structure``
Platform not determinable    ``invalid_package`` / ``platform_unknown``
===========================  =========================================

The verdict is ``reject`` exactly when at least one fault was recorded, so
``accept`` is a positive statement backed by the facts in the report.  The
package is only ever read: nothing is extracted, rewritten or moved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ErrorCode, UniPadError
from . import faults as _faults
from .faults import FaultEntry
from .macho import (
    PLATFORM_MACOS,
    PLATFORM_UNKNOWN,
    SIMULATOR_PLATFORMS,
    MachOInfo,
)
from .package import PackageInfo, open_ipa, sha256_file

__all__ = ["PrecheckReport", "run_precheck", "run_precheck_package"]

VERDICT_ACCEPT = "accept"
VERDICT_REJECT = "reject"


@dataclass
class PrecheckReport:
    """Machine-readable precheck outcome (PRD 10, "包预检").

    ``verdict`` is ``accept`` only when ``faults`` is empty.  ``slice_cputypes``
    and ``platform`` facts are reported even for a rejected package so the
    rejection itself is auditable.
    """

    package_sha256: str
    bundle_id: str | None = None
    app_name: str | None = None
    min_os: str | None = None
    supported_platforms: list[str] = field(default_factory=list)
    has_arm64_slice: bool = False
    slice_cputypes: list[str] = field(default_factory=list)
    encrypted: bool = False
    cryptid: int = 0
    dylibs: list[str] = field(default_factory=list)
    rpaths: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    faults: list[FaultEntry] = field(default_factory=list)
    verdict: str = VERDICT_ACCEPT
    capabilities: list[str] = field(default_factory=list)
    macho_platforms: list[str] = field(default_factory=list)
    executable: str | None = None
    size_bytes: int = 0

    @property
    def accepted(self) -> bool:
        """True when the package may enter the install chain."""
        return self.verdict == VERDICT_ACCEPT

    @property
    def fault_codes(self) -> list[str]:
        """Distinct fault codes in first-seen order."""
        return _faults.fault_codes(self.faults)

    def to_dict(self) -> dict[str, Any]:
        """Return the report as a JSON-serialisable mapping."""
        return {
            "package_sha256": self.package_sha256,
            "bundle_id": self.bundle_id,
            "app_name": self.app_name,
            "min_os": self.min_os,
            "supported_platforms": list(self.supported_platforms),
            "macho_platforms": list(self.macho_platforms),
            "has_arm64_slice": self.has_arm64_slice,
            "slice_cputypes": list(self.slice_cputypes),
            "encrypted": self.encrypted,
            "cryptid": self.cryptid,
            "dylibs": list(self.dylibs),
            "rpaths": list(self.rpaths),
            "permissions": list(self.permissions),
            "capabilities": list(self.capabilities),
            "executable": self.executable,
            "size_bytes": self.size_bytes,
            "faults": [f.to_dict() for f in self.faults],
            "fault_codes": self.fault_codes,
            "verdict": self.verdict,
        }

    def to_json(self) -> str:
        """Return the report as an indented JSON document."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)


def run_precheck(path: str | Path) -> PrecheckReport:
    """Precheck an IPA on the host without touching the guest.

    Args:
        path: ``.ipa`` file to inspect (read-only).

    Returns:
        PrecheckReport: facts plus faults; ``verdict`` is ``reject`` when any
            fault was recorded.  Never raises for a *rejectable* package --
            a structurally broken package is reported as an
            ``invalid_package`` fault so the caller gets one uniform result.
    """
    p = Path(path)
    digest = _safe_sha256(p)
    report = PrecheckReport(package_sha256=digest)

    try:
        package = open_ipa(p)
    except UniPadError as exc:
        report.faults.append(
            FaultEntry(
                code=exc.code,
                reason=str(exc.details.get("reason") or _faults.REASON_BROKEN_STRUCTURE),
                detail=exc.message,
            )
        )
        return _finalize(report)
    except Exception as exc:  # never let an unexpected bug look like a verdict
        report.faults.append(
            FaultEntry(
                code=_faults.classify(exc),
                reason=_faults.REASON_BROKEN_STRUCTURE,
                detail=f"{type(exc).__name__} while reading the package",
            )
        )
        return _finalize(report)

    return run_precheck_package(package)


def run_precheck_package(package: PackageInfo) -> PrecheckReport:
    """Precheck an already-opened :class:`PackageInfo`.

    Split out so a caller that already read the package (the installer) does
    not hash and unzip it twice.
    """
    report = PrecheckReport(
        package_sha256=package.sha256,
        bundle_id=package.bundle_id,
        app_name=package.app_name,
        min_os=package.min_os,
        supported_platforms=list(package.supported_platforms),
        permissions=list(package.privacy_keys),
        capabilities=list(package.required_capabilities),
        executable=package.executable,
        size_bytes=package.size_bytes,
    )

    macho = package.macho
    if macho is None:
        report.faults.append(
            FaultEntry(
                code=ErrorCode.INVALID_PACKAGE,
                reason=_faults.REASON_BROKEN_STRUCTURE,
                detail=(
                    f"main executable could not be parsed: {package.macho_error}"
                    if package.macho_error
                    else "main executable could not be parsed"
                ),
            )
        )
        return _finalize(report)

    report.has_arm64_slice = macho.has_arm64
    report.slice_cputypes = macho.slice_cputypes
    report.encrypted = macho.encrypted
    report.cryptid = macho.cryptid
    report.dylibs = macho.dylibs
    report.rpaths = macho.rpaths
    report.macho_platforms = _macho_platform_names(macho)

    simulator, macos, known = _classify_platform(package, macho)
    if simulator:
        report.faults.append(
            FaultEntry(
                code=ErrorCode.UNSUPPORTED_ARCH,
                reason=_faults.REASON_SIMULATOR_PLATFORM,
                detail=(
                    "package targets a Simulator platform: "
                    f"{_describe_platforms(package, macho)}"
                ),
            )
        )
    elif macos:
        report.faults.append(
            FaultEntry(
                code=ErrorCode.UNSUPPORTED_PLATFORM,
                reason=_faults.REASON_MACOS_PLATFORM,
                detail=(
                    "package targets macOS/Catalyst, not an arm64 Device build: "
                    f"{_describe_platforms(package, macho)}"
                ),
            )
        )
    elif not known:
        report.faults.append(
            FaultEntry(
                code=ErrorCode.INVALID_PACKAGE,
                reason=_faults.REASON_PLATFORM_UNKNOWN,
                detail="neither Info.plist nor the Mach-O slices identify a platform",
            )
        )

    if not macho.has_arm64:
        report.faults.append(
            FaultEntry(
                code=ErrorCode.UNSUPPORTED_ARCH,
                reason=_faults.REASON_NO_ARM64_SLICE,
                detail=(
                    "no ARM64 slice in the main executable"
                    + (f" (found: {', '.join(macho.slice_cputypes)})" if macho.slices else "")
                ),
            )
        )

    if macho.encrypted:
        report.faults.append(
            FaultEntry(
                code=ErrorCode.UNSUPPORTED_PLATFORM,
                reason=_faults.REASON_ENCRYPTED_BINARY,
                detail=(
                    f"main executable is encrypted (cryptid={macho.cryptid}); "
                    "FairPlay/DRM binaries are out of scope (PRD 5.1)"
                ),
            )
        )

    return _finalize(report)


# --------------------------------------------------------------------------
# internal helpers
# --------------------------------------------------------------------------


def _finalize(report: PrecheckReport) -> PrecheckReport:
    """Set the verdict from the recorded faults."""
    report.verdict = VERDICT_REJECT if report.faults else VERDICT_ACCEPT
    return report


def _safe_sha256(path: Path) -> str:
    """Best-effort whole-file digest; empty when the file cannot be read."""
    try:
        return sha256_file(path)
    except UniPadError:
        return ""


def _macho_platform_names(macho: MachOInfo) -> list[str]:
    """Distinct slice platform names, in file order."""
    names: list[str] = []
    for slice_info in macho.slices:
        if slice_info.platform_name not in names:
            names.append(slice_info.platform_name)
    return names


def _classify_platform(package: PackageInfo, macho: MachOInfo) -> tuple[bool, bool, bool]:
    """Return ``(simulator, macos, platform_known)`` for a package.

    Both the Info.plist ``CFBundleSupportedPlatforms`` and the Mach-O
    ``LC_BUILD_VERSION`` platform are consulted: either one is enough
    evidence, and disagreeing values are both reported rather than averaged.
    """
    plist_platforms = {p.strip() for p in package.supported_platforms if p.strip()}
    slice_platforms = {s.platform for s in macho.slices}

    simulator = any(p.endswith("Simulator") for p in plist_platforms) or any(
        platform in SIMULATOR_PLATFORMS for platform in slice_platforms
    )
    macos = ("MacOSX" in plist_platforms) or any(
        platform == PLATFORM_MACOS for platform in slice_platforms
    )
    known = bool(plist_platforms) or any(
        platform != PLATFORM_UNKNOWN for platform in slice_platforms
    )
    return simulator, macos, known


def _describe_platforms(package: PackageInfo, macho: MachOInfo) -> str:
    """Short platform description for a fault detail (no app data)."""
    parts: list[str] = []
    if package.supported_platforms:
        parts.append("Info.plist=" + ",".join(package.supported_platforms))
    if macho.slices:
        parts.append(
            "Mach-O="
            + ",".join(
                f"{s.platform_name}({s.cputype_name})" for s in macho.slices
            )
        )
    return "; ".join(parts) if parts else "unknown"
