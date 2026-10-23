"""IPA application support matrix (PRD 3.2 P1 bullet 2; PRD 5.1 / 2.2 / 10 / 12).

PRD 3.2 P1 asks for 更多自有或获授权的 IPA 兼容性测试 -- *more* compatibility testing of
own-or-authorised IPAs.  More testing only pays off if the result is recorded in a
shape that cannot overstate what was proved, so this module owns the *record*,
and every rule in it is a refusal:

**The key is exactly three parts (PRD 10).**
应用支持矩阵必须按"某个包版本 + 某个系统构建 + 某个运行配置"记录。发现一款 App 可用，只能证明该样本，不外推成
整个 UIKit 或 App Store 兼容.
:class:`MatrixKey` carries those three and nothing coarser is admissible.  There is
no row for an app, no row for UIKit, and no row for "the App Store".

**Trust categories cannot be collapsed (PRD 5.1).**
签名、嵌入框架、权限和所采用的客体信任方式必须被记录。不能把未签名包、普通开发签名包、客体所有者信任包混为同一支持类别。
:class:`TrustCategory` is a closed vocabulary, and the matrix *refuses* to merge two
entries that differ only in trust category.  Collapsing them is the specific
mistake this vocabulary exists to prevent.

**Authorisation is declared, never inferred (PRD 12).**
公开可得不等于已获得再分发许可.
:class:`Authorisation` is a required declaration.  ``NOT_AUTHORISED`` is refused, and
an entry that declares nothing is not an entry at all -- so the gate cannot be
bypassed by omission, neither in memory nor in a hand-edited file.

**A verdict names a condition, never "compatible" (PRD 2.2 item 3).**
无 DRM、包含 ARM64、签名合规于某一安装方式，是不同条件.
:class:`Verdict` therefore contains no compatibility word.  A positive verdict must
name which of :class:`EstablishedCondition` was established, and must name the
install stage that was reached -- which on a Windows host with no iPadOS guest
means no positive verdict can be recorded here at all.

**The default is untested.**  A package that has never been run is
``untested``, never ``works``.  Absence of evidence never reads as presence of
support.

Storage mirrors :class:`unipad.ipa.lifecycle.InstallLedger`: one JSON object per
line, appended, never rewritten.  A line that cannot be parsed -- or that fails
the authorisation gate -- is counted in :attr:`CompatibilityMatrix.unreadable_lines`
and skipped, so a corrupt line costs one entry rather than the matrix.  The
append is newline-safe, so a torn tail costs exactly that one line and cannot
swallow the observation written after it.

The two causes a line can be skipped for are counted *separately*, because they
are not the same fact and the difference is load-bearing:
:attr:`CompatibilityMatrix.corrupt_lines` is damage -- a torn write, a disk
error, a half-migrated schema, a value outside a closed vocabulary -- and is a
repair problem.  :attr:`CompatibilityMatrix.refused_lines` is a line that
parsed as a well-formed entry and was then refused by one of this module's own
gates: the authorisation gate, the trust-category gate, or a verdict that claims
more than the entry can back.  The authorisation case is the security signal --
somebody wrote an unauthorised entry into this file -- and it must never be able
to hide inside a disk error.  The two sum to
:attr:`CompatibilityMatrix.unreadable_lines`, so a caller that only reads the
total keeps the answer it had, and a caller that branches can now tell "this
file is broken" from "this file holds an entry the matrix refuses".

Several observations may share one key; they are the key's history.  The
*current* entry for a key is the newest one that is allowed to be, and two
kinds of observation are not: one that names no install stage never displaces
one that does, and one whose verdict is :attr:`Verdict.UNTESTED` -- this
module's own definition of *no observation* -- never displaces a recorded
outcome for the same sample.  Both rules exist for the same reason: an
observation that carries no evidence must not be able to retract one that does,
or the operator reads ``counts()`` reporting no install while a line on disk
records one.  The refused observation is still appended and still reachable
through :meth:`CompatibilityMatrix.observations_for`; it is simply not the
current answer.

Every append is flushed and ``fsync``-ed before it returns, exactly as
:class:`~unipad.ipa.lifecycle.InstallLedger` does.  The two are two records of
one install: an observation the matrix holds but the disk does not is a matrix
that disagrees with the ledger about what is installed, and there is nothing
later that reconciles the two.

The read paths are indexed where the index is free.  ``_load`` already walks
every line to build the observation list and the current entries, so the
key -> observations grouping it used to throw away is kept and
:meth:`CompatibilityMatrix.observations_for` is a lookup instead of a scan of
the whole file -- a per-key report over k keys was O(n*k), measured at 839 ms
for 40 keys over 50 000 observations.  ``entries_for_bundle`` and ``counts``
stay reductions over the current entries, which is the size of their own
answers; the bounds are stated on each method so neither is mistaken for an
oversight.

What this module is not: it is not a downloader, a registry, or an allow-list, and
it never opens a package.  It records facts a caller already read off
:class:`~unipad.ipa.package.PackageInfo` and
:class:`~unipad.models.InstallResult`; it does not re-derive them.

Stage record (CLAUDE.md, 代码阶段与现场阶段分离):

* 实现状态 -- CODE COMPLETE.  The matrix, its closed vocabularies, the
  authorisation gate, the merge refusal and the JSONL store are implemented.
* Windows/模拟测试状态 -- SIMULATION PASS.  Every entry the suite can build is
  a host-side record about a synthesized image, and 109 unit tests in
  ``tests/unit/test_ipa_matrix.py`` cover the rules above.
* 现场验证状态 -- UNVERIFIED.  There is no iPadOS guest on this machine, so no
  entry produced here may be read as "an IPA runs".  In particular
  ``installs-and-runs`` and ``installs-only`` are unreachable without a guest
  install stage, by construction.
* 阻断原因 -- no iPadOS / SpringBoard / install service on the development host;
  only two own-source samples (HelloUniPad, UniPadNotes) are authorised, and
  both are *built* fixtures rather than stored binaries.

Runtime dependencies: Python standard library only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from ..errors import ErrorCode, UniPadError
from ..models import InstallResult
from .package import PackageInfo
from .precheck import PrecheckReport

__all__ = [
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
]


class _Vocabulary(str, Enum):
    """Base for the closed vocabularies.

    ``str`` so a value serialises as its spelling, and ``__str__`` is pinned to
    the value so a log line and a JSON file never disagree.
    """

    def __str__(self) -> str:
        return self.value

    @classmethod
    def coerce(cls, value: "_Vocabulary | str") -> "_Vocabulary":
        """Return ``value`` as a member of this vocabulary.

        Raises:
            ValueError: for a value outside the vocabulary.  A near-miss is not
                rounded to a neighbour: ``"compatible"`` must not become
                ``"installs-and-runs"``, and ``"works"`` must not become
                ``"untested"``.
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            allowed = ", ".join(member.value for member in cls)
            raise ValueError(
                f"unknown {cls.__name__} value: {value!r}; expected one of {allowed}"
            ) from None


class Authorisation(_Vocabulary):
    """Whether this package may be recorded at all (PRD 5.1 / 12).

    ``OWN_SOURCE`` and ``LICENSED_WITH_EVIDENCE`` are the only two ways in.
    ``NOT_AUTHORISED`` is the explicit refusal, and *no value at all* is refused
    the same way -- a package nobody has cleared is not a package the matrix may
    describe.
    """

    OWN_SOURCE = "own-source"
    LICENSED_WITH_EVIDENCE = "licensed-with-evidence"
    NOT_AUTHORISED = "not-authorised"


class TrustCategory(_Vocabulary):
    """How the package was trusted when it was observed (PRD 5.1).

    This is the *support category*.  An unsigned package, a development-signed
    package and an owner-trusted package are three different support categories
    even when every other fact about them is identical, so the matrix refuses to
    merge entries that differ only here.

    ``GUEST_REPORTED`` is the honest fallback: the host did not classify the
    trust, and the guest's own label is recorded verbatim in
    :attr:`MatrixEntry.guest_trust_mode`.  It is a category in its own right, not
    a synonym for the other three.
    """

    UNSIGNED = "unsigned"
    DEVELOPMENT_SIGNED = "development-signed"
    OWNER_TRUSTED = "owner-trusted"
    GUEST_REPORTED = "guest-reported"


class Verdict(_Vocabulary):
    """What was established about one package under one key (PRD 2.2 / 10).

    None of these is a compatibility word.  Each names an *outcome*, and the
    outcome is only meaningful together with
    :attr:`MatrixEntry.established_conditions` and
    :attr:`MatrixEntry.install_stage`.
    """

    #: The guest installed it *and* it ran.  Requires a guest observation, so it
    #: is unreachable on a host with no iPadOS guest.
    INSTALLS_AND_RUNS = "installs-and-runs"
    #: The guest installed it; nothing is claimed about running it.
    INSTALLS_ONLY = "installs-only"
    #: It was refused.  Carries the stable error code and the reason.
    REJECTED = "rejected"
    #: Never run.  The default, and the only honest answer for a package with no
    #: observation behind it.
    UNTESTED = "untested"
    #: The test does not apply to this package (e.g. a host-side-only check).
    NOT_APPLICABLE = "not-applicable"


class EstablishedCondition(_Vocabulary):
    """Which condition a positive verdict actually established (PRD 2.2 item 3).

    无 DRM、包含 ARM64、签名合规于某一安装方式，是不同条件.  Three members, three different
    conditions, no way to record one and imply another.
    """

    DRM_FREE = "drm-free"
    ARM64_PRESENT = "arm64-present"
    #: Compliance with *one named install method* -- not with signing in general.
    SIGNATURE_COMPLIANT_FOR_INSTALL_METHOD = "signature-compliant-for-install-method"


#: The closed vocabularies, as tuples, so a caller can iterate without
#: reflection and a test can pin the membership.
AUTHORISATIONS: tuple[Authorisation, ...] = tuple(Authorisation)
TRUST_CATEGORIES: tuple[TrustCategory, ...] = tuple(TrustCategory)
VERDICTS: tuple[Verdict, ...] = tuple(Verdict)
ESTABLISHED_CONDITIONS: tuple[EstablishedCondition, ...] = tuple(EstablishedCondition)

#: Verdicts that assert something worked.  Each of these must name the condition
#: it established and the install stage it reached; :data:`Verdict.REJECTED` and
#: :data:`Verdict.UNTESTED` are not success claims and are deliberately absent.
POSITIVE_VERDICTS: tuple[Verdict, ...] = (Verdict.INSTALLS_AND_RUNS, Verdict.INSTALLS_ONLY)

#: Verdicts that assert nothing was tested, so they may not carry a condition.
_NO_CONDITION_VERDICTS: frozenset[Verdict] = frozenset({Verdict.NOT_APPLICABLE})

#: Conditions that cannot be established without an install attempt under a
#: named method.  ``drm-free`` and ``arm64-present`` are readable off the
#: package on the host; this one is not, so it is gated on an install stage.
_INSTALL_ONLY_CONDITIONS: frozenset[EstablishedCondition] = frozenset(
    {EstablishedCondition.SIGNATURE_COMPLIANT_FOR_INSTALL_METHOD}
)

#: The PRD 5.1 record that describes *one sample*.  These are read off the
#: package's own bytes, so under one key, one trust category and one
#: ``package_sha256`` there is exactly one right answer for each of them -- see
#: :meth:`CompatibilityMatrix._refuse_merge`.
_SUPPORT_RECORD_FIELDS: tuple[str, ...] = (
    "signing_mode",
    "embedded_frameworks",
    "requested_permissions",
)


def _now() -> str:
    """UTC timestamp, second resolution, always with an explicit offset."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _same_sample(existing: MatrixEntry, incoming: MatrixEntry) -> bool:
    """True when ``incoming`` describes the same package bytes as ``existing``.

    An entry that names no ``package_sha256`` is treated as the same sample as
    the one it is being compared with.  That is the omission-safe direction, and
    it is the same direction the authorisation gate takes: an entry that
    declares nothing is not an entry that may be read as "probably fine", so an
    absent hash is not evidence that the bytes differ.  A hash that *is* named
    and differs is a rebuild, i.e. a different sample -- the carve-out
    :meth:`CompatibilityMatrix._refuse_merge` already uses for the PRD 5.1
    record, scoped the same way here so the two rules cannot drift apart.
    """
    recorded = str(existing.package_sha256).strip()
    incoming_sha = str(incoming.package_sha256).strip()
    if not recorded or not incoming_sha:
        return True
    return recorded == incoming_sha


def declared_signature_mode(package: PackageInfo) -> str:
    """Report what the package's Mach-O *shows* about signing.

    This is the same declaration the installer hands the guest
    (``LC_CODE_SIGNATURE`` present or absent), read off the already-parsed
    :class:`~unipad.ipa.package.PackageInfo` rather than by re-opening the
    archive.  It is a statement about the artifact, never a trust claim: how the
    guest trusts the package comes back separately as
    :attr:`MatrixEntry.guest_trust_mode`.

    One deliberate divergence from the installer: when the main executable could
    not be parsed at all there is no slice to look at, so this reports
    ``"unparsed-executable"`` instead of guessing ``"unsigned"``.
    """
    if package.macho is None:
        return "unparsed-executable"
    signed = any(slice_info.has_code_signature for slice_info in package.macho.slices)
    return "code-signature-present" if signed else "unsigned"


@dataclass(frozen=True)
class MatrixKey:
    """Where in the matrix an observation belongs (PRD 10).

    Exactly three parts -- a package version, a system build and a run
    configuration -- and all three are required.  A blank part is not a coarser
    key: it is a different, unnamed key, and an unnamed key is how "this app
    works" gets recorded as if it were a sample.
    """

    package_version: str
    system_build: str
    run_config: str

    def __post_init__(self) -> None:
        for name in ("package_version", "system_build", "run_config"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"matrix key part {name!r} must be a non-empty string")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_version": self.package_version,
            "system_build": self.system_build,
            "run_config": self.run_config,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MatrixKey":
        return cls(
            package_version=str(data["package_version"]),
            system_build=str(data["system_build"]),
            run_config=str(data["run_config"]),
        )


@dataclass
class MatrixEntry:
    """One observation about one package under one :class:`MatrixKey`.

    The §5.1 record lives in :attr:`signing_mode`, :attr:`embedded_frameworks`,
    :attr:`requested_permissions` and :attr:`guest_trust_mode`.  They are read
    off a :class:`~unipad.ipa.package.PackageInfo` and an
    :class:`~unipad.models.InstallResult` by :meth:`from_package`; this module
    never re-derives them and never opens the package itself.

    ``authorisation`` and ``trust_category`` have no defaults *and* no silent
    fallback: an entry that declares neither is refused at construction, at
    :meth:`from_dict` and again at
    :meth:`CompatibilityMatrix.record`.  The dataclass is deliberately mutable so
    that last re-check is reachable -- :meth:`CompatibilityMatrix.record` does
    not trust an object it did not build.
    """

    key: MatrixKey
    #: Required.  ``None`` is refused rather than treated as "probably fine".
    authorisation: Optional[Authorisation] = None
    #: Required.  The support category this observation was made under.
    trust_category: Optional[TrustCategory] = None

    # --- package identity (read off PackageInfo) ---
    bundle_id: str = ""
    package_sha256: str = ""
    package_path: str = ""
    app_name: str = ""

    # --- the PRD 5.1 record ---
    signing_mode: str = ""
    embedded_frameworks: tuple[str, ...] = ()
    requested_permissions: tuple[str, ...] = ()
    guest_trust_mode: Optional[str] = None

    # --- what was established ---
    verdict: Verdict = Verdict.UNTESTED
    established_conditions: tuple[EstablishedCondition, ...] = ()
    install_stage: str = ""

    # --- why it did not work ---
    error_code: Optional[str] = None
    reason: str = ""

    note: str = ""
    observed_at: str = field(default_factory=_now)
    schema_version: int = 1

    def __post_init__(self) -> None:
        self.key = MatrixKey.from_dict(self.key.to_dict()) if isinstance(self.key, Mapping) else self.key
        self.authorisation = self._authorisation()
        self.trust_category = self._trust_category()
        self.verdict = Verdict.coerce(self.verdict)
        self.established_conditions = self._conditions()
        self._check_verdict_is_backed()

    # -- construction -----------------------------------------------------

    @classmethod
    def from_package(
        cls,
        package: PackageInfo,
        *,
        key: MatrixKey,
        authorisation: Optional[Authorisation] = None,
        trust_category: Optional[TrustCategory] = None,
        verdict: Verdict | str = Verdict.UNTESTED,
        established_conditions: Iterable[EstablishedCondition | str] = (),
        install_result: Optional[InstallResult] = None,
        signing_mode: Optional[str] = None,
        error_code: Optional[str] = None,
        reason: str = "",
        note: str = "",
    ) -> "MatrixEntry":
        """Build an entry from facts a caller already read.

        Args:
            package: An already-opened :class:`~unipad.ipa.package.PackageInfo`.
                Read-only; nothing is extracted and nothing is re-parsed.
            key: The PRD 10 three-part key.
            authorisation: Required; see :class:`Authorisation`.
            trust_category: Required; see :class:`TrustCategory`.
            verdict: Defaults to :attr:`Verdict.UNTESTED`.
            established_conditions: Which conditions were established.  Required
                (and non-empty) for a positive verdict; forbidden for
                ``untested`` / ``not-applicable``.
            install_result: Optional guest outcome.  Only ``stage`` and
                ``trust_mode`` are read, and ``trust_mode`` is recorded exactly
                as reported -- never assumed, never defaulted.
            signing_mode: Override for the host's declaration.  Defaults to
                :func:`declared_signature_mode`.
            error_code: Stable :class:`~unipad.errors.ErrorCode` for a rejection.
            reason: Stable slug naming *which* rule fired.
            note: Free text.  Must not contain secrets or user document content
                (PRD 9.2).

        Raises:
            UniPadError: ``blocked`` for an undeclared or ``NOT_AUTHORISED``
                authorisation, or an undeclared trust category;
                ``evidence_incomplete`` for a verdict that claims more than it
                can back.
            ValueError: for a value outside a closed vocabulary, or for a
                positive verdict that names no condition.
        """
        return cls(
            key=key,
            authorisation=authorisation,
            trust_category=trust_category,
            bundle_id=package.bundle_id,
            package_sha256=package.sha256,
            package_path=package.path,
            app_name=package.app_name,
            signing_mode=(
                signing_mode if signing_mode is not None else declared_signature_mode(package)
            ),
            embedded_frameworks=tuple(package.frameworks),
            requested_permissions=tuple(package.privacy_keys),
            guest_trust_mode=(install_result.trust_mode if install_result is not None else None),
            verdict=verdict,
            established_conditions=tuple(established_conditions),
            install_stage=(install_result.stage if install_result is not None else ""),
            error_code=error_code,
            reason=reason,
            note=note,
        )

    @classmethod
    def from_precheck(
        cls,
        package: PackageInfo,
        report: PrecheckReport,
        *,
        key: MatrixKey,
        authorisation: Optional[Authorisation] = None,
        trust_category: Optional[TrustCategory] = None,
        install_result: Optional[InstallResult] = None,
        signing_mode: Optional[str] = None,
        verdict: Optional[Verdict | str] = None,
        note: str = "",
    ) -> "MatrixEntry":
        """Record a *precheck* outcome -- the only compatibility evidence a
        Windows host can produce today.

        The verdict is derived, not chosen: a report with faults is ``rejected``
        and carries the first fault's stable code and reason; a clean report is
        ``untested``.  A clean precheck is never ``installs-only``, because the
        host has not installed anything.

        The conditions are derived too, and only the two a host can actually
        read off the package: ``drm-free`` when the image is not encrypted and
        ``arm64-present`` when it carries an ARM64 slice (PRD 2.2 item 3 -- these
        are separate conditions, so they are recorded separately).
        ``signature-compliant-for-install-method`` is never derived here: it
        needs an install attempt under a named method, which needs a guest.

        Args:
            package: The already-opened package the report describes.
            report: A :class:`~unipad.ipa.precheck.PrecheckReport`.
            key: The PRD 10 three-part key.
            authorisation: Required; see :class:`Authorisation`.
            trust_category: Required; see :class:`TrustCategory`.
            install_result: Optional guest outcome, if one exists.
            signing_mode: Override for the host's declaration.
            verdict: Override the derived verdict.  Defaults to deriving it.
            note: Free text, no secrets (PRD 9.2).

        Raises:
            UniPadError: as :meth:`from_package`.
            ValueError: as :meth:`from_package`.
        """
        conditions: list[EstablishedCondition] = []
        if not report.encrypted:
            conditions.append(EstablishedCondition.DRM_FREE)
        if report.has_arm64_slice:
            conditions.append(EstablishedCondition.ARM64_PRESENT)

        derived: Verdict
        error_code: Optional[str] = None
        reason = ""
        if report.faults:
            first = report.faults[0]
            derived = Verdict.REJECTED
            error_code = first.code
            reason = first.reason
        else:
            derived = Verdict.UNTESTED

        return cls.from_package(
            package,
            key=key,
            authorisation=authorisation,
            trust_category=trust_category,
            verdict=derived if verdict is None else verdict,
            established_conditions=conditions,
            install_result=install_result,
            signing_mode=signing_mode,
            error_code=error_code,
            reason=reason,
            note=note,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view.

        The enum fields are written defensively (``None`` when unset) so that a
        caller who stripped a required field gets the *gate* error from
        :meth:`CompatibilityMatrix.record` rather than an ``AttributeError``
        from here.

        ``requested_permissions`` holds Info.plist *key names* only: the
        usage-description prose belongs to the app author and must not be copied
        into evidence (PRD 9.2).  ``package_path`` is recorded; the package's
        contents are not.
        """
        return {
            "schema_version": self.schema_version,
            "key": self.key.to_dict(),
            "authorisation": self.authorisation.value if self.authorisation is not None else None,
            "trust_category": self.trust_category.value if self.trust_category is not None else None,
            "bundle_id": self.bundle_id,
            "package_sha256": self.package_sha256,
            "package_path": self.package_path,
            "app_name": self.app_name,
            "signing_mode": self.signing_mode,
            "embedded_frameworks": list(self.embedded_frameworks),
            "requested_permissions": list(self.requested_permissions),
            "guest_trust_mode": self.guest_trust_mode,
            "verdict": self.verdict.value if self.verdict is not None else None,
            "established_conditions": [c.value for c in self.established_conditions],
            "install_stage": self.install_stage,
            "error_code": self.error_code,
            "reason": self.reason,
            "note": self.note,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MatrixEntry":
        """Rebuild an entry from its stored form.

        Every gate in :meth:`__post_init__` runs again, so a line that declares
        no authorisation is not an entry -- it is an unreadable line, and the
        matrix counts it as one.
        """
        return cls(
            key=MatrixKey.from_dict(data["key"]),
            authorisation=data.get("authorisation"),
            trust_category=data.get("trust_category"),
            bundle_id=str(data.get("bundle_id", "")),
            package_sha256=str(data.get("package_sha256", "")),
            package_path=str(data.get("package_path", "")),
            app_name=str(data.get("app_name", "")),
            signing_mode=str(data.get("signing_mode", "")),
            embedded_frameworks=tuple(data.get("embedded_frameworks") or ()),
            requested_permissions=tuple(data.get("requested_permissions") or ()),
            guest_trust_mode=(str(data["guest_trust_mode"]) if data.get("guest_trust_mode") else None),
            verdict=data.get("verdict", Verdict.UNTESTED.value),
            established_conditions=tuple(data.get("established_conditions") or ()),
            install_stage=str(data.get("install_stage", "")),
            error_code=(str(data["error_code"]) if data.get("error_code") else None),
            reason=str(data.get("reason", "")),
            note=str(data.get("note", "")),
            observed_at=str(data.get("observed_at", "")),
            schema_version=int(data.get("schema_version", 1)),
        )

    # -- validation -------------------------------------------------------

    def _authorisation(self) -> Authorisation:
        """Return the authorisation, or refuse the entry (PRD 12)."""
        if self.authorisation is None:
            raise UniPadError(
                ErrorCode.BLOCKED,
                "a compatibility-matrix entry must declare its authorisation",
                reason="authorisation_undeclared",
            )
        authorisation = Authorisation.coerce(self.authorisation)
        if authorisation is Authorisation.NOT_AUTHORISED:
            raise UniPadError(
                ErrorCode.BLOCKED,
                "this package is not authorised for the compatibility matrix",
                reason="not_authorised",
                bundle_id=self.bundle_id,
                package_sha256=self.package_sha256,
            )
        return authorisation

    def _trust_category(self) -> TrustCategory:
        """Return the trust category, or refuse the entry (PRD 5.1)."""
        if self.trust_category is None:
            raise UniPadError(
                ErrorCode.BLOCKED,
                "a compatibility-matrix entry must declare its trust category",
                reason="trust_category_undeclared",
            )
        return TrustCategory.coerce(self.trust_category)

    def _conditions(self) -> tuple[EstablishedCondition, ...]:
        conditions = tuple(EstablishedCondition.coerce(c) for c in self.established_conditions)
        if self.verdict in _NO_CONDITION_VERDICTS and conditions:
            raise ValueError(
                f"verdict {self.verdict.value!r} establishes nothing, so it cannot "
                f"record conditions: {[c.value for c in conditions]}"
            )
        return conditions

    def _check_verdict_is_backed(self) -> None:
        """Refuse a verdict that claims more than the entry can back.

        A positive verdict must name both *which* condition it established and
        the install stage it reached.  The second requirement is what keeps this
        matrix honest on a Windows host: with no iPadOS guest there is no stage
        to name, so ``installs-and-runs`` is unreachable here -- by construction,
        not by good intentions.

        The same gate applies per condition: signature compliance is only ever
        established by an install attempt under one named method, so it too
        needs a stage.  ``drm-free`` and ``arm64-present`` are readable off the
        package on the host and need no guest, which is why an ``untested`` entry
        may legitimately carry them.
        """
        if self.verdict in POSITIVE_VERDICTS and not self.established_conditions:
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                f"verdict {self.verdict.value!r} must name the condition it established",
                reason="no_established_condition",
                verdict=self.verdict.value,
                allowed=[c.value for c in ESTABLISHED_CONDITIONS],
            )
        if self.verdict in POSITIVE_VERDICTS and not str(self.install_stage).strip():
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                f"verdict {self.verdict.value!r} must name the install stage reached",
                reason="no_install_stage",
                verdict=self.verdict.value,
            )
        unbacked = sorted(
            c.value
            for c in self.established_conditions
            if c in _INSTALL_ONLY_CONDITIONS and not str(self.install_stage).strip()
        )
        if unbacked:
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "these conditions can only be established by an install attempt",
                reason="condition_requires_install_stage",
                conditions=unbacked,
            )
        if self.verdict is Verdict.REJECTED and not (
            str(self.error_code or "").strip() or self.reason.strip()
        ):
            raise UniPadError(
                ErrorCode.EVIDENCE_INCOMPLETE,
                "verdict 'rejected' must carry an error code or a reason",
                reason="no_rejection_reason",
            )


class CompatibilityMatrix:
    """An append-only JSON Lines record of IPA compatibility observations.

    One line per observation, appended, never rewritten -- the same contract as
    :class:`unipad.ipa.lifecycle.InstallLedger`, and for the same reason: a
    compatibility claim that can be edited is not evidence.  A line that cannot
    be parsed, or that fails the authorisation gate, is counted in
    :attr:`unreadable_lines` and skipped.

    The merge rule (PRD 5.1): recording an observation whose key is already
    present is refused when the existing entry was observed under a *different*
    trust category, under a different authorisation, or when the *same sample*
    (one ``package_sha256``) is described by a different PRD 5.1 record --
    ``signing_mode``, ``embedded_frameworks``, ``requested_permissions``.  Those
    are the facts that decide the support category, and merging them would
    record one category's evidence under another's name, or a wrong record in
    place of a right one.  A differing ``package_sha256`` under the same key is
    *not* a conflict: a rebuild of the same version is a new sample, it is
    appended rather than merged, and :meth:`observations_for` exposes both.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.unreadable_lines = 0
        #: Lines the loader could not turn into an entry at all: damage.
        self.corrupt_lines = 0
        #: Lines that parsed as a well-formed entry and were then refused by one
        #: of this module's own gates -- the authorisation gate, the
        #: trust-category gate, or a verdict that claims more than it can back.
        #: A security signal, not a repair problem, and never the same number as
        #: :attr:`corrupt_lines` unless both kinds of line are present.
        self.refused_lines = 0
        self._latest: dict[MatrixKey, MatrixEntry] = {}
        self._observations: list[MatrixEntry] = []
        #: Every observation, grouped by key, oldest first.  Maintained while
        #: loading and appending, so :meth:`observations_for` is a lookup
        #: rather than a scan of the whole file.
        self._by_key: dict[MatrixKey, list[MatrixEntry]] = {}
        if self.path.is_file():
            self._load()

    # -- reading ---------------------------------------------------------

    def _load(self) -> None:
        self._latest = {}
        self._observations = []
        self._by_key = {}
        self.unreadable_lines = 0
        self.corrupt_lines = 0
        self.refused_lines = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    # json.JSONDecodeError is a ValueError, and so is a line
                    # that is valid JSON but not an object at all.
                    self._skip_corrupt()
                    continue
                try:
                    entry = MatrixEntry.from_dict(data)
                except UniPadError:
                    # The authorisation or trust gate fired on a stored line.
                    # That is the outcome wanted, and it is counted apart from
                    # damage so a tampered line cannot hide inside a disk error.
                    self._skip_refused()
                    continue
                except (ValueError, TypeError, KeyError):
                    # Outside a closed vocabulary, or not an entry's shape.
                    self._skip_corrupt()
                    continue
                self._observations.append(entry)
                self._by_key.setdefault(entry.key, []).append(entry)
                self._promote(entry)

    def _skip_corrupt(self) -> None:
        self.corrupt_lines += 1
        self.unreadable_lines += 1

    def _skip_refused(self) -> None:
        self.refused_lines += 1
        self.unreadable_lines += 1

    def _promote(self, entry: MatrixEntry) -> None:
        """Make ``entry`` the current entry for its key, if it may be.

        Two kinds of observation are refused the current slot, both for the
        same reason -- an observation that carries no evidence must not retract
        one that does:

        * **No install stage.**  ``install_stage == ""`` means *no guest
          observation was made*, not "the install got nowhere", so such an
          observation never displaces one that names a stage.  Doing so would
          retract an install the guest really did, and the operator would read
          it as ``counts()`` reporting no successful install while a line on
          disk records one.
        * **``untested``.**  :attr:`Verdict.UNTESTED` is this module's own
          definition of *no observation* -- "the only honest answer for a
          package with no observation behind it".  An entry that establishes
          nothing therefore cannot become the current answer for a key that has
          an observation behind it, which is the same mistake on the axis this
          module *does* own a closed, ranked vocabulary for.  Scoped to one
          ``package_sha256``, mirroring :meth:`_refuse_merge`: a rebuild is a
          new sample, and the newest sample is honestly ``untested``.

        In both cases the newer observation is still appended and still
        reachable through :meth:`observations_for`; it is simply not the current
        answer.

        The guard is deliberately scoped that narrowly in every other respect.
        An observation that *does* name a stage and *does* record an outcome --
        including a later run that got less far -- is a real outcome and becomes
        the current entry, because suppressing it would overstate support,
        which is the same mistake in the other direction.  This module owns no
        vocabulary of stages, so it cannot honestly rank two named stages
        against each other and does not try.
        """
        existing = self._latest.get(entry.key)
        if existing is not None and self._must_not_retract(existing, entry):
            return
        self._latest[entry.key] = entry

    @staticmethod
    def _must_not_retract(existing: MatrixEntry, entry: MatrixEntry) -> bool:
        """True when ``entry`` may not take the current slot from ``existing``."""
        if not str(entry.install_stage).strip() and str(existing.install_stage).strip():
            return True
        if (
            entry.verdict is Verdict.UNTESTED
            and existing.verdict is not Verdict.UNTESTED
            and _same_sample(existing, entry)
        ):
            return True
        return False

    @property
    def entries(self) -> tuple[MatrixEntry, ...]:
        """The current entry per key, in first-seen order."""
        return tuple(self._latest.values())

    @property
    def observations(self) -> tuple[MatrixEntry, ...]:
        """Every observation, oldest first.  A copy: the matrix is not editable."""
        return tuple(self._observations)

    def entry_for(self, key: MatrixKey) -> Optional[MatrixEntry]:
        """The current entry for ``key``, or ``None`` when never observed."""
        return self._latest.get(key)

    def observations_for(self, key: MatrixKey) -> tuple[MatrixEntry, ...]:
        """Every observation recorded under ``key``, oldest first."""
        return tuple(self._by_key.get(key, ()))

    def entries_for_bundle(self, bundle_id: str) -> tuple[MatrixEntry, ...]:
        """Every current entry whose package is ``bundle_id``.

        A reduction over the current entries, in ``entries`` order.  That is
        O(k) where k is the number of current entries -- the size of the answer,
        the same class as :attr:`entries` -- and it is left that way on purpose:
        a bundle id is not part of the key, so one key's current entry can move
        between bundles, and an index grouped by bundle would have to be
        re-ordered on every such move to keep answering in ``entries`` order.
        """
        return tuple(e for e in self._latest.values() if e.bundle_id == bundle_id)

    def counts(self) -> dict[Verdict, int]:
        """Verdict -> number of current entries.  Every verdict is present.

        A missing verdict reports ``0`` rather than being absent, so a reader
        cannot mistake "no row" for "not counted".

        A reduction over the current entries, so O(k) where k is the number of
        current entries -- the size of the answer, the same class as
        :attr:`entries`.  Left that way deliberately: an incrementally
        maintained tally would be O(1), and there is no deterministic way to
        prove it does less work, which makes it exactly the kind of change this
        module refuses to ship untested.
        """
        tally = {verdict: 0 for verdict in VERDICTS}
        for entry in self._latest.values():
            tally[entry.verdict] += 1
        return tally

    def __contains__(self, key: object) -> bool:
        return key in self._latest

    def __len__(self) -> int:
        return len(self._latest)

    def __iter__(self) -> Iterator[MatrixEntry]:
        return iter(self.entries)

    # -- writing ---------------------------------------------------------

    def record(self, entry: MatrixEntry) -> MatrixEntry:
        """Append one observation and return it as stored.

        The returned entry is a validated *copy*: mutating the object the caller
        passed afterwards cannot change what was written, which is the same
        property :class:`~unipad.ipa.lifecycle.InstallLedger` gets from being
        append-only.

        Raises:
            UniPadError: ``blocked`` when the entry is not authorised -- the gate
                is re-run here, because an entry the matrix did not build is not
                an entry it may trust.  ``experiment_state_invalid`` when the
                entry's key is already present under a different trust category,
                a different authorisation, or a different PRD 5.1 record for the
                same sample (PRD 5.1).
        """
        if not isinstance(entry, MatrixEntry):
            raise TypeError(f"expected a MatrixEntry, got {type(entry).__name__}")
        # Re-run every gate: a caller may have built or mutated the entry.
        validated = MatrixEntry.from_dict(entry.to_dict())

        existing = self._latest.get(validated.key)
        if existing is not None:
            self._refuse_merge(existing, validated)

        self._append(validated)
        return validated

    def _refuse_merge(self, existing: MatrixEntry, incoming: MatrixEntry) -> None:
        """Refuse to record two support categories under one key (PRD 5.1)."""
        if existing.trust_category is not incoming.trust_category:
            raise UniPadError(
                ErrorCode.EXPERIMENT_STATE_INVALID,
                "this key is already recorded under a different trust category",
                reason="trust_category_conflict",
                key=existing.key.to_dict(),
                recorded_trust_category=existing.trust_category.value,
                incoming_trust_category=incoming.trust_category.value,
                recorded_observed_at=existing.observed_at,
            )
        if existing.authorisation is not incoming.authorisation:
            raise UniPadError(
                ErrorCode.EXPERIMENT_STATE_INVALID,
                "this key is already recorded under a different authorisation",
                reason="authorisation_conflict",
                key=existing.key.to_dict(),
                recorded_authorisation=existing.authorisation.value,
                incoming_authorisation=incoming.authorisation.value,
            )
        # One sample has exactly one PRD 5.1 record, so a second description of
        # the *same* bytes is not a second observation -- it is a wrong one, and
        # letting it in would make the wrong record the current entry and the
        # right one unreachable.  Scoped to one package_sha256 on purpose: a
        # differing hash is a rebuild, i.e. a different sample, which the class
        # docstring documents as appended rather than refused.
        if existing.package_sha256 == incoming.package_sha256:
            for name in _SUPPORT_RECORD_FIELDS:
                recorded = getattr(existing, name)
                incoming_value = getattr(incoming, name)
                if recorded != incoming_value:
                    raise UniPadError(
                        ErrorCode.EXPERIMENT_STATE_INVALID,
                        "the same sample is recorded under this key with a "
                        f"different {name.replace('_', ' ')}",
                        reason="support_record_conflict",
                        key=existing.key.to_dict(),
                        field=name,
                        recorded=list(recorded) if isinstance(recorded, tuple) else recorded,
                        incoming=(
                            list(incoming_value)
                            if isinstance(incoming_value, tuple)
                            else incoming_value
                        ),
                    )

    def _append(self, entry: MatrixEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A torn tail has no trailing newline, and append mode would concatenate
        # the next observation onto it -- losing *two* observations while
        # unreadable_lines reports one.  Reading the last byte is O(1), so this
        # stays cheap on a large matrix.
        separator = ""
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("rb") as probe:
                probe.seek(-1, os.SEEK_END)
                if probe.read(1) != b"\n":
                    separator = "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                separator + json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            )
            # The append is the only record that the observation happened, so it
            # is not allowed to live in a page cache: flush to the OS and force
            # it to the device before the call returns.  An OSError here is left
            # to propagate -- evidence that could not be written is a host
            # fault, not something to report as a recorded observation.  This is
            # the same discipline `InstallLedger._append` uses, because the two
            # are two records of one install and must not be able to disagree
            # about it after a crash.
            handle.flush()
            os.fsync(handle.fileno())
        self._observations.append(entry)
        self._by_key.setdefault(entry.key, []).append(entry)
        self._promote(entry)
