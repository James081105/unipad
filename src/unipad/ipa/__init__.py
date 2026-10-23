"""IPA install chain: package precheck, transfer, and install orchestration.

This package owns the *host* side of PRD sections 5, 9.1 and 10:

* :mod:`unipad.ipa.macho`     pure-Python Mach-O / universal binary parser
* :mod:`unipad.ipa.package`   IPA (zip) reader -> :class:`PackageInfo`
* :mod:`unipad.ipa.precheck`  package precheck -> :class:`PrecheckReport`
* :mod:`unipad.ipa.faults`    install stages and fault classification
* :mod:`unipad.ipa.transfer`  host -> guest transfer with hash verification
* :mod:`unipad.ipa.installer` stage orchestration -> ``models.InstallResult``

Nothing here talks to a real guest: the channel is a Protocol implemented by
``unipad.guest`` (real) or :class:`LoopbackTransport` (tests), so every stage
can be exercised on Windows without an iPad.

Usage::

    from unipad.ipa import LoopbackTransport, IpaInstaller

    installer = IpaInstaller(LoopbackTransport(), run_id="exp-local")
    result = installer.install("HelloUniPad.ipa")
    print(result.stage, result.error_code)   # 'failed' + code, or installed/registered

Runtime dependencies: Python standard library only.
"""

from __future__ import annotations

from .lifecycle import (
    FAILURE_CLASSES,
    InstallLedger,
    LifecycleAction,
    LifecycleEntry,
    classify_failure,
)
from .faults import (
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
from .macho import (
    CPU_TYPE_ARM64,
    CPU_TYPE_X86_64,
    FAT_CIGAM,
    FAT_CIGAM_64,
    FAT_MAGIC,
    FAT_MAGIC_64,
    MH_CIGAM,
    MH_CIGAM_64,
    MH_MAGIC,
    MH_MAGIC_64,
    PLATFORM_IOS,
    PLATFORM_IOSSIMULATOR,
    PLATFORM_MACOS,
    PLATFORM_NAMES,
    SIMULATOR_PLATFORMS,
    MachOInfo,
    SegmentInfo,
    SliceInfo,
    cputype_name,
    parse_macho,
    parse_macho_bytes,
    parse_macho_stream,
    platform_name,
)
from .package import PackageInfo, iter_ipa_members, open_ipa, read_zip_member, sha256_file
from .precheck import (
    VERDICT_ACCEPT,
    VERDICT_REJECT,
    PrecheckReport,
    run_precheck,
    run_precheck_package,
)
from .matrix import (
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
from .transfer import (
    DEFAULT_CHUNK_SIZE,
    GuestTransport,
    HostSideTransfer,
    LoopbackTransport,
    TransferProgress,
    TransferReceipt,
    iter_chunks,
)
from .installer import (
    EVENTS_NAME,
    RECORD_NAME,
    GuestClientChannel,
    GuestReply,
    InstallChannel,
    InstallStore,
    IpaInstaller,
    load_guest_module,
)

__all__ = [
    # macho
    "MachOInfo",
    "SliceInfo",
    "SegmentInfo",
    "parse_macho",
    "parse_macho_bytes",
    "parse_macho_stream",
    "cputype_name",
    "platform_name",
    "FAT_MAGIC",
    "FAT_MAGIC_64",
    "FAT_CIGAM",
    "FAT_CIGAM_64",
    "MH_MAGIC",
    "MH_MAGIC_64",
    "MH_CIGAM",
    "MH_CIGAM_64",
    "CPU_TYPE_ARM64",
    "CPU_TYPE_X86_64",
    "PLATFORM_MACOS",
    "PLATFORM_IOS",
    "PLATFORM_IOSSIMULATOR",
    "PLATFORM_NAMES",
    "SIMULATOR_PLATFORMS",
    # package
    "PackageInfo",
    "open_ipa",
    "sha256_file",
    "read_zip_member",
    "iter_ipa_members",
    # precheck
    "PrecheckReport",
    "run_precheck",
    "run_precheck_package",
    "VERDICT_ACCEPT",
    "VERDICT_REJECT",
    # matrix
    "Authorisation",
    "TrustCategory",
    "Verdict",
    "EstablishedCondition",
    "AUTHORISATIONS",
    "TRUST_CATEGORIES",
    "VERDICTS",
    "ESTABLISHED_CONDITIONS",
    "POSITIVE_VERDICTS",
    "MatrixKey",
    "MatrixEntry",
    "CompatibilityMatrix",
    "declared_signature_mode",
    # lifecycle
    "LifecycleAction",
    "LifecycleEntry",
    "InstallLedger",
    "classify_failure",
    "FAILURE_CLASSES",
    # faults
    "InstallStage",
    "FaultEntry",
    "classify",
    "is_terminal",
    "is_success",
    "fault_codes",
    "first_fault_code",
    "TERMINAL_STAGES",
    "SUCCESS_STAGES",
    # transfer
    "GuestTransport",
    "HostSideTransfer",
    "LoopbackTransport",
    "TransferReceipt",
    "TransferProgress",
    "iter_chunks",
    "DEFAULT_CHUNK_SIZE",
    # installer
    "IpaInstaller",
    "InstallChannel",
    "InstallStore",
    "GuestClientChannel",
    "GuestReply",
    "load_guest_module",
    "RECORD_NAME",
    "EVENTS_NAME",
]
