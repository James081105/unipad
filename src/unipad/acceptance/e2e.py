"""PRD 5.2 end-to-end acceptance cases, run as one ordered stateful sequence.

This module owns the *gate*, not the work.  The ten cases of ``docs/PRD.md``
lines 172-181 are not a checklist that can be scored in any order: case 4
cannot be evaluated before case 3 imported the package it installs, case 5
cannot be evaluated before case 4 registered the icon it clicks, case 7
cannot be evaluated before case 6 proved the app is interactive, and case 8
cannot be evaluated before case 7 saved the string it must find again after
the reboot.  :class:`E2ESequence` therefore walks the cases **in PRD order**,
carries one mutable session across them, and propagates dependency
explicitly: a case whose predecessor did not reach ``PASS`` is itself
``BLOCKED`` with a ``predecessor not satisfied`` reason -- never silently
skipped, and never ``PASS``.

Two rules make the gate trustworthy rather than decorative:

1. **Every case declares the evidence it needs** (``CaseSpec.required_evidence``)
   and the runner refuses to emit ``PASS`` when a declared kind is absent,
   downgrading to ``UNVERIFIED`` with ``missing_evidence:<kind>``.  A ``PASS``
   :class:`CaseVerdict` additionally cannot be *constructed* without at least
   one :class:`EvidenceRef` and ``observed=True`` (PRD 9.2: "证据缺失属于未验证,
   不能靠模型文字补全").

2. **Two independent axes.**  ``verdict`` answers "did the fixed condition hold
   for the evidence submitted?".  :class:`FourFields` answers "in which
   environment was that evidence produced?".  ``onsite_status`` is a read-only
   property computed *only* from ``scope == "onsite"`` references, so a hundred
   simulation references cannot raise it, and ``CODE COMPLETE`` cannot raise it
   either.  ``release_verdict`` reads the field axis: with zero ``onsite``
   references in the sequence the release verdict is ``BLOCKED`` even when every
   single case reads ``PASS``.  That is the mechanical form of "SIMULATION PASS
   is not 现场 PASS" (CLAUDE.md four-field rule; PRD 1 line 13).

What is *not* here
------------------
There is no iPadOS on this Windows host, no built UniXNU kernel image and no
guest install service (``docs/implementation-plan.md`` "Tier-1 启动关键路径现状":
``kernel.macho`` is the single remaining blocker; ``config/machine.json`` carries
``disks.system_image: null``).  Case 1 -- "start a brand-new QEMU process on the
user's Windows machine" -- is the case that gates everything, and on this host it
is ``BLOCKED`` naming exactly what is missing.  Nothing in this file may be read
as "an IPA runs on Windows"; the host-side code that performs each action lives
in :mod:`unipad.host.qemu`, :mod:`unipad.guest.client` and :mod:`unipad.ipa`,
and is injected through the :class:`E2ERuntime` seam so the gate can be exercised
without a real QEMU.

Evidence hygiene (PRD 9.2, appendix A lines 476-480)
----------------------------------------------------
``EvidenceRef`` rejects absolute paths, drive letters, ``..`` and backslashes at
construction time, requires either a SHA-256 or an explicit ``digest_reason``,
and requires a ``witness`` for ``onsite`` scope.  ``verify_witness`` re-reads the
referenced file and re-finds the literal, so a hand-edited scope cannot be
loaded back.  ``assert_publishable`` scans every reason and the serialized
report for absolute user paths and token-shaped strings and refuses rather than
emitting a leaking bundle.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Mapping, Sequence

from ..errors import ErrorCode, UniPadError
from . import record
from .record import CODE_STATES, ONSITE_STATUSES, WINDOWS_STATUSES

__all__ = [
    # verdict vocabulary
    "PASS",
    "FAIL",
    "BLOCKED",
    "UNVERIFIED",
    "INCOMPLETE",
    "VERDICT_KINDS",
    "worst_of",
    # evidence
    "SIMULATION",
    "HOST_REAL",
    "ONSITE",
    "EVIDENCE_SCOPES",
    "EVIDENCE_KINDS",
    "EvidenceRef",
    "verify_witness",
    # verdicts and the four fields
    "CaseVerdict",
    "verdict_pass",
    "verdict_fail",
    "verdict_blocked",
    "verdict_unverified",
    "FourFields",
    "FIELD_VOCABULARY",
    # host / sample observation
    "SampleSpec",
    "SAMPLE_SPECS",
    "SampleFacts",
    "HostFacts",
    "observe_host",
    "observe_samples",
    # the sequence
    "CaseSpec",
    "CASES",
    "case_spec",
    "coverage_gaps",
    "StepObservation",
    "E2ERuntime",
    "E2EEnvironment",
    "CaseSession",
    "CaseOutcome",
    "SequenceReport",
    "E2ESequence",
    "release_verdict",
    "assert_publishable",
]

# ---------------------------------------------------------------------------
# PRD citations used throughout (docs/PRD.md is the authority)
# ---------------------------------------------------------------------------

PRD = "docs/PRD.md"
CITE_5_1_TABLE = f"{PRD}:161-165"
CITE_5_1_RECORD = f"{PRD}:166"
CITE_5_1_INSPECT = f"{PRD}:168"
CITE_5_2_CASES = f"{PRD}:172-181"
CITE_5_2_NOTE = f"{PRD}:183"
CITE_9_2_EVIDENCE = f"{PRD}:395"
CITE_9_2_NO_SECRETS = f"{PRD}:397"
CITE_7_4_SERIAL = f"{PRD}:294"
CITE_APPENDIX_A = f"{PRD}:476-480"

# ---------------------------------------------------------------------------
# verdict vocabulary
# ---------------------------------------------------------------------------

#: The condition-axis vocabulary, the badness order and the roll-up all come
#: from :mod:`unipad.acceptance.record`.  This module used to keep its own copy
#: of all three; the copy agreed with the canonical one by luck rather than by
#: construction, and nothing would have noticed if it had stopped agreeing (F8).
PASS = record.VerdictKind.PASS
FAIL = record.VerdictKind.FAIL
BLOCKED = record.VerdictKind.BLOCKED
UNVERIFIED = record.VerdictKind.UNVERIFIED

#: An ``INCOMPLETE`` case is not a fifth verdict kind: it is a case whose
#: predecessor never reached ``PASS``, reported by :func:`coverage_gaps` and by
#: the runner as ``INCOMPLETE``.  Keeping it out of ``VERDICT_KINDS`` is what
#: stops it being aggregated as if it were a condition-axis conclusion.
INCOMPLETE = "INCOMPLETE"

VERDICT_KINDS: tuple[str, ...] = record.VERDICT_KINDS
_SEVERITY = record.SEVERITY

#: The roll-up.  Re-exported from :mod:`unipad.acceptance.record` so an e2e
#: stage and a milestone can never aggregate by two different badness orders.
#: Note the empty-set answer differs from this module's old copy, which seeded
#: ``worst = PASS``: nothing proved is not a pass, so the canonical one fails
#: closed to ``UNVERIFIED``.
worst_of = record.worst_of


# ---------------------------------------------------------------------------
# evidence scopes and kinds
# ---------------------------------------------------------------------------

#: The three scopes, from :mod:`unipad.acceptance.record` (F8).  ``simulation``
#: is driven by tests/fakes; ``host_real`` is real host QEMU/firmware but the
#: object is not the target guest; ``onsite`` means this host *and* the target
#: iPadOS guest was observed.
SIMULATION = record.EvidenceScope.SIMULATION
HOST_REAL = record.EvidenceScope.HOST_REAL
ONSITE = record.EvidenceScope.ONSITE

EVIDENCE_SCOPES: tuple[str, ...] = (
    record.EvidenceScope.SIMULATION,
    record.EvidenceScope.HOST_REAL,
    record.EvidenceScope.ONSITE,
)

#: Every evidence kind a case may declare.  A declared kind outside this set is
#: a coverage gap computed at import time, not a silently ignored string.
EVIDENCE_KINDS: frozenset[str] = frozenset(
    {
        "host_inventory",       # PRD 9.2 minimum evidence set
        "experiment_json",      # PRD 9.2
        "artifact_manifest",    # PRD 9.2
        "qemu_args",            # PRD 5.2 case 1 "实际配置"
        "qemu_version",         # PRD 5.2 case 1 "版本证据"
        "serial_log",           # PRD 9.2
        "qmp_log",              # PRD 9.2
        "screenshot",           # PRD 9.2 "带时间戳截图"
        "screendump",           # framebuffer dump used as a before/after pair
        "guest_log",            # PRD 9.2 "可获取的客体内核/进程日志"
        "process_evidence",     # PRD 5.2 case 5 "对应进程或等效可信运行证据"
        "package_sha256",       # PRD 5.2 case 3 "记录输入包 SHA-256"
        "precheck_report",      # PRD 5.2 case 4 "完成包检查"
        "preinstall_status",    # PRD 5.2 case 3 "先证明它此前未安装"
        "transfer_receipt",     # PRD 10 "主客体传输"
        "install_record",       # PRD 9.1 install result
        "icon_evidence",        # PRD 5.2 case 4 "SpringBoard 出现真实图标"
        "bundle_id",            # PRD 5.2 case 4 "获得安装结果与 bundle ID"
        "challenge_value",      # PRD 5.2 case 5 "显示本次挑战值"
        "input_journal",        # PRD 5.2 case 6
        "counter_trace",        # PRD 5.2 case 6 "画面计数正确变化"
        "relaunch_evidence",    # PRD 5.2 case 6 "退出回主屏幕后能再次打开"
        "saved_text",           # PRD 5.2 case 7
        "read_back_text",       # PRD 5.2 case 7 "关闭重开 App 后可读"
        "shutdown_record",      # PRD 5.2 case 8 "正常关机"
        "new_process_record",   # PRD 5.2 case 8 "结束 QEMU 进程，再从磁盘启动新进程"
        "post_reboot_registry", # PRD 5.2 case 8 "两款 App ... 仍存在"
        "post_reboot_marker",   # PRD 5.2 case 8 "... 和保存的字符串仍存在"
        "boot_timings",         # PRD 5.2 case 9 "保存耗时"
        "cycle_results",        # PRD 5.2 case 9 "和结果"
        "failure_preserved",    # PRD 5.2 case 9 "测试失败时保留证据，不覆盖上次失败"
        "rejection_reason",     # PRD 5.2 case 10 "返回明确原因"
        "post_rejection_status",# PRD 5.2 case 10 "已有可用系统和应用保持可恢复"
        "manifest",             # this module's own bookkeeping
    }
)

#: Directory (relative to the run directory) this module writes its evidence to.
EVIDENCE_DIRNAME = "acceptance-e2e"

#: ``<file>:<lineno>:<literal>`` -- the only witness shape accepted for
#: ``onsite`` evidence, because it names one concrete line of one concrete file.
_WITNESS_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,120}:\d{1,9}:\S.{0,200}$")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_PATH_SEGMENT_BAD = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")


def _is_absolute_like(path: str) -> bool:
    """True for drive-letter, UNC or POSIX-absolute spellings."""
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", path)
        or path.startswith("\\\\")
        or path.startswith("/")
        or path.startswith("\\")
    )


@dataclass(frozen=True)
class EvidenceRef(record.EvidenceRef):
    """One piece of evidence, addressed relative to the run directory.

    This is :class:`unipad.acceptance.record.EvidenceRef`, not a second
    implementation of it.  The local copy it replaces re-spelled the path,
    digest and witness rules with its own error messages and its own regular
    expressions; the rules themselves agreed, so the duplication bought nothing
    and cost the guarantee that they would keep agreeing.

    What is e2e-specific is the *kind* set: a case may declare evidence the
    bundle vocabulary has no word for (``qemu_args``, ``package_sha256``,
    ``process_evidence``, ...), because PRD 5.2 asks for things a bundle does
    not store.  :attr:`ALLOWED_KINDS` is the extension point for exactly that,
    so the wider set no longer requires a second class.

    ``is_onsite`` is kept as a property because the sequence reads it in a
    dozen places.
    """

    #: Every evidence kind a case may declare.  A declared kind outside this
    #: set is a coverage gap computed at import time, not a silently ignored
    #: string.  The bundle kinds are included so a reference a bundle would
    #: accept is also acceptable here.
    ALLOWED_KINDS: ClassVar[tuple[str, ...]] = tuple(
        sorted(EVIDENCE_KINDS | set(record.EVIDENCE_KINDS))
    )

    @property
    def is_onsite(self) -> bool:
        """``True`` when this reference claims the target guest was observed."""
        return self.scope == ONSITE


def verify_witness(ref: EvidenceRef, base_dir: Path) -> bool:
    """Re-find the witness literal in the referenced file, on this host.

    A bundle that claims ``onsite`` scope is only trusted when the line it
    names still contains the literal it names.  Anything else -- missing file,
    short file, changed bytes -- is a failed verification, never a warning.
    """
    if ref.scope != ONSITE or not ref.witness:
        return False
    target = base_dir / ref.path
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    try:
        file_part, lineno_part, literal = ref.witness.rsplit(":", 2)
        lineno = int(lineno_part)
    except ValueError:
        return False
    if file_part not in (ref.path, Path(ref.path).name):
        return False
    if not 1 <= lineno <= len(lines):
        return False
    return literal in lines[lineno - 1]


# ---------------------------------------------------------------------------
# publication guard (PRD 9.2 line 397)
# ---------------------------------------------------------------------------

_ABSOLUTE_PATH_RES = (
    re.compile(r"[A-Za-z]:[\\/]{1,2}[^\s\"']*"),
    re.compile(r"\\\\[^\s\"']+"),
    re.compile(r"(?:^|[\s\"'(=])(?:/home/|/Users/|/root/)[^\s\"')]*"),
)
_SECRET_RES = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}"),
)

#: A bare 64-hex run is deliberately *not* a secret signal here: every digest in
#: this module is a declared ``sha256`` field, so the pattern would refuse its
#: own evidence index.  Token-shaped prefixes are unambiguous and are what the
#: guard looks for.


def assert_publishable(text: str, *, where: str = "report") -> None:
    """Raise when ``text`` carries an absolute user path or a secret shape.

    Applied to every reason and to the serialized report, so a leak becomes a
    refusal to publish rather than a leaked bundle (PRD 9.2 line 397).
    """
    for pattern in _ABSOLUTE_PATH_RES:
        match = pattern.search(text)
        if match:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "absolute user path in publishable output",
                where=where,
                span=match.group(0)[:24],
            )
    for pattern in _SECRET_RES:
        match = pattern.search(text)
        if match:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "secret-shaped material in publishable output",
                where=where,
            )


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseVerdict(record.Verdict):
    """Axis A: did the fixed condition hold for the evidence submitted?

    This is :class:`unipad.acceptance.record.Verdict`, not a second
    implementation of it.  The local copy it replaces spelled out the same four
    invariants a second time and had already drifted from the canonical one in
    one respect: it did not require a ``PASS`` to rest on digestable evidence,
    so an e2e case could pass on a reference nobody could re-hash (C2).  Every
    invariant now lives in one place.

    Two properties are e2e-specific and are the whole reason this is a subclass
    rather than a plain alias:

    * :attr:`blocker` -- the first reason, when the verdict is not a pass.  The
      four-field row needs a single string for its ``blocker`` column, and the
      canonical class deliberately does not pick one.
    * :attr:`evidence_kinds` -- the set of kinds this verdict cites, which is
      what ``coverage_gaps`` compares against a case's declared requirements.
    """

    @property
    def blocker(self) -> str | None:
        """The first reason, when the verdict is not a pass."""
        if self.kind == record.VerdictKind.PASS:
            return None
        return self.reasons[0] if self.reasons else None

    @property
    def evidence_kinds(self) -> frozenset[str]:
        """The kinds this verdict cites; ``coverage_gaps`` diffs it against a spec."""
        return frozenset(ref.kind for ref in self.evidence)


def verdict_pass(
    evidence: Sequence[EvidenceRef], reasons: Sequence[str] = ()
) -> CaseVerdict:
    """The only entry point that can produce ``PASS``."""
    refs = tuple(evidence)
    if not refs:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "verdict_pass requires at least one evidence reference",
        )
    return CaseVerdict(PASS, tuple(reasons), refs, True)


def verdict_fail(
    reasons: Sequence[str], evidence: Sequence[EvidenceRef] = ()
) -> CaseVerdict:
    clean = tuple(str(r) for r in reasons if str(r).strip())
    if not clean:
        raise UniPadError(ErrorCode.CONFIG_INVALID, "verdict_fail requires a reason")
    return CaseVerdict(FAIL, clean, tuple(evidence), True)


def verdict_blocked(reason: str, evidence: Sequence[EvidenceRef] = ()) -> CaseVerdict:
    """A case that knows exactly what it is missing and therefore did not run."""
    clean = str(reason).strip()
    if not clean:
        raise UniPadError(ErrorCode.CONFIG_INVALID, "verdict_blocked requires a reason")
    return CaseVerdict(BLOCKED, (clean,), tuple(evidence), False)


def verdict_unverified(reason: str, evidence: Sequence[EvidenceRef] = ()) -> CaseVerdict:
    """A case nobody established, as distinct from one that failed."""
    clean = str(reason).strip()
    if not clean:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID, "verdict_unverified requires a reason"
        )
    return CaseVerdict(UNVERIFIED, (clean,), tuple(evidence), False)


# ---------------------------------------------------------------------------
# the four fields
# ---------------------------------------------------------------------------

#: The four-field vocabulary (docs/testing-strategy.md D1 -- no new words).
#: The words are the canonical ones from :mod:`unipad.acceptance.record`; this
#: module used to re-declare its own copy, which is how the gate came to have
#: four vocabularies where ``PASS`` meant one thing here and another thing in
#: ``milestones.py`` (F9).
FIELD_VOCABULARY: dict[str, tuple[str, ...]] = {
    "implementation": CODE_STATES,
    "windows_status": WINDOWS_STATUSES,
    "onsite_status": ONSITE_STATUSES,
    "blocker": ("-",),
}


@dataclass(frozen=True)
class FourFields:
    """Axis B: in which environment was the evidence produced?

    ``windows_status`` and ``onsite_status`` are **derived properties**, not
    writable fields.  ``onsite_status`` reads only ``scope == onsite``
    references, so no amount of simulation evidence and no ``CODE COMPLETE``
    declaration can raise it.  ``blocker`` is filled from the verdict's first
    reason when the verdict is ``BLOCKED``, so a blocked row can never carry an
    empty blocker.
    """

    check_id: str
    verdict: CaseVerdict
    evidence: tuple[EvidenceRef, ...] = ()
    blocker: str | None = None
    implementation_evidence: tuple[EvidenceRef, ...] = ()
    code_state: str = "UNVERIFIED"

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(
            self, "implementation_evidence", tuple(self.implementation_evidence)
        )
        if not (self.check_id or "").strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID, "four fields need a check id"
            )
        if self.code_state not in FIELD_VOCABULARY["implementation"]:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown implementation state",
                code_state=self.code_state,
                allowed=FIELD_VOCABULARY["implementation"],
            )
        if self.verdict.kind == BLOCKED and not (self.blocker or "").strip():
            object.__setattr__(self, "blocker", self.verdict.reasons[0])

    @property
    def implementation(self) -> str:
        """``CODE COMPLETE`` only survives when implementation evidence exists."""
        if self.code_state == "CODE COMPLETE" and not self.implementation_evidence:
            return "UNVERIFIED"
        return self.code_state

    @property
    def windows_status(self) -> str:
        refs = [r for r in self.evidence if r.scope in (SIMULATION, HOST_REAL)]
        if not refs:
            return "NOT RUN"
        return "SIMULATION PASS" if self.verdict.kind == PASS else "FAIL"

    @property
    def onsite_status(self) -> str:
        refs = [r for r in self.evidence if r.scope == ONSITE]
        if refs:
            return "PASS" if self.verdict.kind == PASS else "FAIL"
        if (self.blocker or "").strip():
            return "BLOCKED"
        return "UNVERIFIED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "verdict": self.verdict.kind,
            "implementation": self.implementation,
            "windows_status": self.windows_status,
            "onsite_status": self.onsite_status,
            "blocker": self.blocker or "-",
        }


# ---------------------------------------------------------------------------
# PRD 5.1 sample prerequisites
# ---------------------------------------------------------------------------

#: Permissions a sample may declare and still count as "最少权限".  Empty by
#: default: a privacy usage description is a claim about user data, and PRD 5.1
#: line 163 asks for the minimum.  A sample that needs more must record them in
#: its source manifest, which is then checked against this set.
MINIMAL_PERMISSIONS: frozenset[str] = frozenset()

#: Keys a sample's source manifest must record (PRD 5.1 line 166: "签名、嵌入
#: 框架、权限和所采用的客体信任方式必须被记录").
REQUIRED_SOURCE_MANIFEST_KEYS: tuple[str, ...] = (
    "own_source",
    "signature_mode",
    "embedded_frameworks",
    "permissions",
    "trust_approach",
)

#: Where an *archived* sample IPA would live on this host.  Scratch copies under
#: ``local-private/tmp/`` are deliberately not searched: a build fixture is not
#: an archived sample binary (docs/implementation-plan.md line 41).
DEFAULT_SAMPLE_ROOTS: tuple[str, ...] = ("artifacts", "samples", "local-private/ipa")


@dataclass(frozen=True)
class SampleSpec:
    """One PRD 5.1 sample and the constraints it must meet."""

    name: str
    prd_citation: str
    behaviour: str
    extra_evidence: tuple[str, ...] = ()


SAMPLE_SPECS: tuple[SampleSpec, ...] = (
    SampleSpec(
        name="HelloUniPad.ipa",
        prd_citation=f"{PRD}:163",
        behaviour="显示项目名、当次随机挑战值、按钮和点击计数",
        extra_evidence=("challenge_value", "counter_trace"),
    ),
    SampleSpec(
        name="UniPadNotes.ipa",
        prd_citation=f"{PRD}:164",
        behaviour="输入测试文本，保存到应用容器，重启后恢复",
        extra_evidence=("saved_text", "read_back_text"),
    ),
)


@dataclass(frozen=True)
class SampleFacts:
    """What this host can actually show about one PRD 5.1 sample."""

    name: str
    prd_citation: str
    exists: bool = False
    size_bytes: int = 0
    sha256: str = ""
    precheck: dict[str, Any] = field(default_factory=dict)
    source_recorded: bool = False
    verdict: str = UNVERIFIED
    reasons: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def accepted(self) -> bool:
        return self.verdict == PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "prd_citation": self.prd_citation,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "precheck": dict(self.precheck),
            "source_recorded": self.source_recorded,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "evidence": [ref.to_dict() for ref in self.evidence],
        }


# ---------------------------------------------------------------------------
# host observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostFacts:
    """What this host can actually show, observed rather than assumed.

    ``missing`` is the load-bearing field: it is the concrete list of slugs for
    everything a real guest boot needs and this host does not have.  Case 1
    reads it and refuses to claim a QEMU process was started.
    """

    host_inventory: dict[str, Any] = field(default_factory=dict)
    host_inventory_sha256: str = ""
    host_label: str = ""
    accel: str = ""
    qemu_binary: str = ""
    qemu_binary_present: bool = False
    qemu_version: str = ""
    qemu_sha256: str = ""
    firmware_path: str = ""
    kernel_path: str = ""
    kernel_sha256: str = ""
    system_image: str = ""
    data_image: str = ""
    guest_service_reachable: bool = False
    springboard_pid: int | None = None
    system_version: tuple[int, ...] = ()
    samples: dict[str, SampleFacts] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing", tuple(self.missing))
        object.__setattr__(self, "notes", tuple(self.notes))

    @property
    def guest_available(self) -> bool:
        return not self.missing


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_tuple(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(text))
    return tuple(int(n) for n in numbers[:4])


def _host_label(inventory: Mapping[str, Any]) -> str:
    """A non-identifying host caption: no username, no path, no serial."""
    cpu = inventory.get("cpu") or {}
    memory = inventory.get("memory") or {}
    host = inventory.get("host") or {}
    cores = cpu.get("cores_physical")
    threads = cpu.get("threads_logical")
    total = memory.get("total_gb")
    parts: list[str] = []
    if cores and threads:
        parts.append(f"{cores}C/{threads}T")
    if isinstance(total, (int, float)):
        parts.append(f"{float(total):.1f}GB")
    caption = str(host.get("os") or platform.system() or "unknown-host")
    parts.append(caption)
    return " ".join(parts)


def _sanitize_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    """Drop values that are host paths rather than host facts (PRD 9.2)."""
    cleaned = json.loads(json.dumps(inventory, ensure_ascii=False, default=str))
    toolchain = cleaned.get("toolchain")
    if isinstance(toolchain, dict):
        python_block = toolchain.get("python")
        if isinstance(python_block, dict) and "command" in python_block:
            python_block.pop("command", None)
            python_block["redacted"] = "toolchain.python.command removed (PRD 9.2)"
    return cleaned


def _read_qemu_version(binary: Path, timeout: float = 20.0) -> str:
    """Measure the QEMU version instead of quoting the pin for it."""
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **(
                {"creationflags": 0x08000000}
                if sys.platform == "win32"
                else {}
            ),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    text = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    return text.splitlines()[0].strip() if text else ""


def _locate_sample(name: str, roots: Sequence[Path]) -> Path | None:
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _root_labels(env: "E2EEnvironment") -> list[str]:
    """Repo-relative labels for the configured sample roots (PRD 9.2).

    A reason string is publishable output, so it names the configured root, not
    the absolute path it resolved to.
    """
    labels: list[str] = []
    repo = Path(env.repo_root)
    for root in env.sample_roots:
        try:
            labels.append(Path(root).resolve().relative_to(repo.resolve()).as_posix())
        except (ValueError, OSError):
            labels.append(Path(root).name or "sample-root")
    return labels


def _precheck_package(path: Path) -> dict[str, Any]:
    """Run the real package precheck (PRD 5.1 line 168, PRD 10 "包预检")."""
    try:
        from unipad.ipa.precheck import run_precheck
    except Exception as exc:  # pragma: no cover - concurrent fix in flight
        raise UniPadError(
            ErrorCode.BLOCKED,
            "package precheck unavailable on this host",
            reason=f"{type(exc).__name__} importing unipad.ipa.precheck",
        ) from exc
    return run_precheck(path).to_dict()


def observe_samples(
    env: "E2EEnvironment", evidence_dir: Path, observed_at: str
) -> dict[str, SampleFacts]:
    """Apply the PRD 5.1 build constraints to both samples on this host.

    A sample that does not exist is ``BLOCKED`` with a reason naming it -- never
    a pass, and never a silent omission (PRD 5.1, appendix A line 480).
    """
    # The caller may point at a run directory that does not exist yet (the
    # sequence creates it, but this helper is also usable on its own); the
    # evidence it writes must land somewhere, so make the directory now.
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    facts: dict[str, SampleFacts] = {}
    for spec in SAMPLE_SPECS:
        facts[spec.name] = _observe_one_sample(spec, env, evidence_dir, observed_at)
    return facts


def _observe_one_sample(
    spec: SampleSpec,
    env: "E2EEnvironment",
    evidence_dir: Path,
    observed_at: str,
) -> SampleFacts:
    roots = [Path(root) for root in env.sample_roots]
    found = _locate_sample(spec.name, roots)
    if found is None:
        return SampleFacts(
            name=spec.name,
            prd_citation=spec.prd_citation,
            verdict=BLOCKED,
            reasons=(
                f"sample_ipa_not_found:{spec.name}",
                "no archived "
                + spec.name
                + " under any sample root ("
                + (", ".join(_root_labels(env)) or "none configured")
                + ")",
            ),
        )

    digest = _sha256_file(found)
    digest_name = f"{spec.name}.sha256"
    (evidence_dir / digest_name).write_text(digest + "\n", encoding="utf-8")
    evidence: list[EvidenceRef] = [
        EvidenceRef(
            path=f"{EVIDENCE_DIRNAME}/{digest_name}",
            kind="manifest",
            sha256=_sha256_bytes((digest + "\n").encode("utf-8")),
            scope=HOST_REAL,
            observed_at=observed_at,
        )
    ]
    try:
        report = _precheck_package(found)
    except UniPadError as exc:
        return SampleFacts(
            name=spec.name,
            prd_citation=spec.prd_citation,
            exists=True,
            size_bytes=found.stat().st_size,
            sha256=digest,
            verdict=BLOCKED,
            reasons=(f"sample_precheck_unavailable:{exc.code}",),
            evidence=tuple(evidence),
        )

    report_bytes = json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8")
    report_name = f"sample-{spec.name}.precheck.json"
    (evidence_dir / report_name).write_bytes(report_bytes)
    evidence.append(
        EvidenceRef(
            path=f"{EVIDENCE_DIRNAME}/{report_name}",
            kind="precheck_report",
            sha256=_sha256_bytes(report_bytes),
            scope=HOST_REAL,
            observed_at=observed_at,
        )
    )

    if report.get("verdict") != "accept":
        codes = report.get("fault_codes") or []
        return SampleFacts(
            name=spec.name,
            prd_citation=spec.prd_citation,
            exists=True,
            size_bytes=found.stat().st_size,
            sha256=digest,
            precheck=report,
            verdict=FAIL,
            reasons=(
                f"sample_precheck_rejected:{spec.name}:"
                + ",".join(str(c) for c in codes),
            ),
            evidence=tuple(evidence),
        )

    reasons: list[str] = []

    min_os = str(report.get("min_os") or "")
    if not min_os:
        reasons.append(f"sample_min_os_missing:{spec.name}")
    elif env.system_version and _version_tuple(min_os) > env.system_version:
        reasons.append(
            f"sample_min_os_incompatible:{spec.name}:{min_os}"
            f">{'.'.join(str(n) for n in env.system_version)}"
        )

    privacy_keys = [str(k) for k in (report.get("permissions") or [])]
    manifest = env.sample_source_manifests.get(spec.name)
    allowed = set(MINIMAL_PERMISSIONS)
    source_recorded = False
    if manifest is not None and Path(manifest).is_file():
        data = _load_json(Path(manifest))
        recorded = [str(p) for p in (data.get("permissions") or [])]
        allowed |= set(recorded)
        if data.get("own_source") is True:
            source_recorded = True
        missing_keys = [
            key for key in REQUIRED_SOURCE_MANIFEST_KEYS if key not in data
        ]
        if missing_keys:
            reasons.append(
                f"sample_source_incomplete:{spec.name}:{','.join(missing_keys)}"
            )
    else:
        reasons.append(f"sample_source_not_recorded:{spec.name}")

    excessive = sorted(set(privacy_keys) - allowed)
    if excessive:
        reasons.append(f"sample_excessive_permissions:{spec.name}:{','.join(excessive)}")

    if reasons:
        return SampleFacts(
            name=spec.name,
            prd_citation=spec.prd_citation,
            exists=True,
            size_bytes=found.stat().st_size,
            sha256=digest,
            precheck=report,
            source_recorded=source_recorded,
            verdict=FAIL if any(r.startswith(("sample_precheck", "sample_min_os", "sample_excessive")) for r in reasons) else UNVERIFIED,
            reasons=tuple(reasons),
            evidence=tuple(evidence),
        )

    return SampleFacts(
        name=spec.name,
        prd_citation=spec.prd_citation,
        exists=True,
        size_bytes=found.stat().st_size,
        sha256=digest,
        precheck=report,
        source_recorded=True,
        verdict=PASS,
        reasons=(f"sample_meets_prd_5_1_constraints:{spec.name}",),
        evidence=tuple(evidence),
    )


def observe_host(env: "E2EEnvironment") -> HostFacts:
    """Observe this host: inventory, QEMU, kernel, images and both samples.

    Nothing here is assumed.  A missing pin, a missing binary or a missing
    kernel image becomes a slug in ``missing`` and a note, never a default.
    """
    notes: list[str] = []
    repo = Path(env.repo_root)
    machine = _load_json(repo / "config" / "machine.json")
    lock = _load_json(repo / "config" / "sources.lock.json")

    accel = str(machine.get("accel") or "")
    if accel and accel != "tcg":
        notes.append(f"machine config requests accel={accel}; PRD 2.2 requires tcg")

    disks = machine.get("disks") or {}
    system_image = str(disks.get("system_image") or "")
    data_image = str(disks.get("data_image") or "")

    tools = (lock.get("tools") or {}).get("qemu") or {}
    qemu_rel = str(tools.get("binary") or "")
    qemu_version = str(tools.get("full_version") or tools.get("version") or "")
    qemu_sha256 = str(tools.get("sha256") or "")
    firmware_rel = str((tools.get("firmware") or {}).get("path") or "")
    qemu_present = bool(qemu_rel) and (repo / qemu_rel).is_file()
    if qemu_rel and not qemu_present:
        notes.append(f"pinned qemu binary not present at {qemu_rel}")
    if env.verify_qemu_version and qemu_present:
        measured = _read_qemu_version(repo / qemu_rel)
        if measured:
            qemu_version = measured
        else:
            notes.append("qemu --version could not be measured; pin quoted instead")

    firmware_path = firmware_rel if firmware_rel and (repo / firmware_rel).is_file() else ""

    known_good = _load_json(repo / "artifacts" / "known-good.json")
    kernel_path = ""
    kernel_sha256 = ""
    if known_good:
        kernel_path = str(known_good.get("kernel") or known_good.get("kernel_macho") or "")
        kernel_sha256 = str(known_good.get("kernel_sha256") or "")
    if not kernel_path:
        kernel_path = system_image
    if kernel_path and (repo / kernel_path).is_file():
        if not kernel_sha256:
            kernel_sha256 = _sha256_file(repo / kernel_path)
    else:
        if kernel_path:
            notes.append(f"pinned kernel image not present at {kernel_path}")
        kernel_path = ""

    inventory: dict[str, Any] = {}
    try:
        from unipad.host.inventory import collect_host_inventory
    except Exception as exc:  # pragma: no cover - concurrent fix in flight
        notes.append(f"host inventory unavailable: {type(exc).__name__}")
    else:
        inventory = collect_host_inventory(probe_cim=env.probe_cim)

    inventory_bytes = json.dumps(
        _sanitize_inventory(inventory), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")

    system_version = _version_tuple(
        (lock.get("system_identity") or {}).get("system") or ""
    )

    missing: list[str] = []
    if not qemu_present:
        missing.append("qemu_binary")
    if accel != "tcg":
        missing.append("accelerator_not_tcg")
    if not kernel_path:
        missing.append("kernel_image")
    if not system_image:
        missing.append("system_image")
    if not data_image:
        missing.append("data_image")
    missing.append("guest_install_service")
    missing.append("springboard_process")

    evidence_dir = Path(env.run_dir) / EVIDENCE_DIRNAME
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "host-inventory.json").write_bytes(inventory_bytes)
    samples = observe_samples(env, evidence_dir, _utc_now_iso())

    return HostFacts(
        host_inventory=inventory,
        host_inventory_sha256=_sha256_bytes(inventory_bytes),
        host_label=_host_label(inventory),
        accel=accel,
        qemu_binary=qemu_rel,
        qemu_binary_present=qemu_present,
        qemu_version=qemu_version,
        qemu_sha256=qemu_sha256,
        firmware_path=firmware_path,
        kernel_path=kernel_path,
        kernel_sha256=kernel_sha256,
        system_image=system_image,
        data_image=data_image,
        guest_service_reachable=False,
        springboard_pid=None,
        system_version=system_version,
        samples=samples,
        missing=tuple(missing),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# the runtime seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepObservation:
    """What one PRD 5.2 action produced when it was actually performed.

    ``ok`` is the only thing that can turn a case into ``PASS``; ``reason`` is
    mandatory when ``ok`` is false so a non-pass is always explicable.  Any
    ``onsite`` reference must carry a witness -- that is enforced by
    :class:`EvidenceRef`, not by this class.
    """

    ok: bool
    reason: str = ""
    evidence: tuple[EvidenceRef, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.ok and not str(self.reason).strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a failed step observation requires a reason",
            )


class E2ERuntime:
    """The collaborator that performs the PRD 5.2 actions against a real guest.

    The default is *no runtime*: this host has no kernel image and no guest, so
    there is nothing that could perform these actions.  A runtime is injected by
    whoever has a bootable guest -- in production that is
    :class:`unipad.host.qemu.QemuLauncher` plus :class:`unipad.guest.client`
    plus :class:`unipad.ipa.installer`; in tests it is a stub.  Every method
    returns :class:`StepObservation`, and returning ``ok=False`` with a reason is
    a *failed* step (``FAIL``), never a pass.
    """

    def start_new_qemu_process(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")

    def reach_springboard(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")

    def import_hello_ipa(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")

    def install_hello_ipa(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")

    def launch_hello_ipa(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")

    def tap_button_ten_times(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")

    def import_notes_ipa_and_save_text(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")

    def shutdown_and_reboot_from_disk(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")

    def repeat_cold_boot_three_times(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")

    def import_unsupported_platform_package(self) -> StepObservation:
        return StepObservation(False, reason="no_qemu_runtime")


# ---------------------------------------------------------------------------
# case probes
# ---------------------------------------------------------------------------


def _observation_verdict(
    case_id: str, observation: StepObservation, extra: Sequence[EvidenceRef] = ()
) -> CaseVerdict:
    """Map a performed step onto the verdict vocabulary."""
    if observation.ok:
        return verdict_pass(tuple(observation.evidence) + tuple(extra))
    return verdict_fail(
        (f"{case_id}_step_failed:{observation.reason}",),
        tuple(observation.evidence) + tuple(extra),
    )


def _case1_start_new_qemu_process(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 1 (docs/PRD.md:172)."""
    blocked = session.require_guest("case1")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.start_new_qemu_process()
    document = {
        "case_id": "case1",
        "prd_citation": f"{PRD}:172",
        "qemu_binary": session.facts.qemu_binary,
        "qemu_version": session.facts.qemu_version,
        "qemu_sha256": session.facts.qemu_sha256,
        "accel": session.facts.accel,
        "kernel": session.facts.kernel_path,
        "kernel_sha256": session.facts.kernel_sha256,
        "system_image": session.facts.system_image,
        "data_image": session.facts.data_image,
        "argv": observation.facts.get("argv", []),
        "pid": observation.facts.get("pid"),
    }
    ref = session.record_json("case1-qemu-process.json", document, kind="qemu_args")
    return _observation_verdict("case1", observation, (ref,))


def _case2_reach_springboard(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 2 (docs/PRD.md:173)."""
    blocked = session.require_guest("case2")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.reach_springboard()
    facts = observation.facts
    pid = facts.get("springboard_pid")
    before = facts.get("framebuffer_before")
    after = facts.get("framebuffer_after")
    if observation.ok and (not isinstance(pid, int) or before == after):
        # PRD 7.4 line 294: a serial line alone never proves a stage, and a
        # screenshot that does not change proves nothing about input.
        return verdict_fail(
            (
                "case2_springboard_not_operable",
                "mouse input did not change the guest framebuffer",
            ),
            tuple(observation.evidence),
        )
    return _observation_verdict("case2", observation)


def _case3_import_hello_ipa(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 3 (docs/PRD.md:174)."""
    blocked = session.require_sample("case3", "HelloUniPad.ipa")
    if blocked is not None:
        return blocked
    blocked = session.require_guest("case3")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.import_hello_ipa()
    facts = observation.facts
    if observation.ok:
        digest = str(facts.get("package_sha256") or "")
        installed_before = facts.get("installed_before")
        problems: list[str] = []
        if not _SHA256_RE.match(digest):
            problems.append("case3_package_sha256_missing")
        if installed_before:
            problems.append("case3_package_was_already_installed")
        if problems:
            return verdict_fail(problems, tuple(observation.evidence))
    return _observation_verdict("case3", observation)


def _case4_install_hello_ipa(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 4 (docs/PRD.md:175)."""
    blocked = session.require_guest("case4")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.install_hello_ipa()
    facts = observation.facts
    if observation.ok:
        problems: list[str] = []
        if str(facts.get("stage") or "") != "registered":
            problems.append("case4_install_stage_not_registered")
        if not str(facts.get("bundle_id") or ""):
            problems.append("case4_bundle_id_missing")
        if facts.get("icon") is not True:
            problems.append("case4_springboard_icon_missing")
        imported = session.facts_of("case3").get("package_sha256")
        if imported and facts.get("package_sha256") != imported:
            problems.append("case4_package_digest_differs_from_import")
        if problems:
            return verdict_fail(problems, tuple(observation.evidence))
    return _observation_verdict("case4", observation)


def _case5_launch_hello_ipa(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 5 (docs/PRD.md:176)."""
    blocked = session.require_guest("case5")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.launch_hello_ipa()
    facts = observation.facts
    if observation.ok:
        problems: list[str] = []
        challenge = str(facts.get("challenge_value") or "")
        if not _SHA256_RE.match(challenge):
            problems.append("case5_challenge_value_missing")
        expected = str(facts.get("expected_challenge") or "")
        if expected and challenge != expected:
            problems.append("case5_challenge_value_mismatch")
        if not (facts.get("pid") or facts.get("process_evidence")):
            problems.append("case5_no_process_evidence")
        if problems:
            return verdict_fail(problems, tuple(observation.evidence))
    return _observation_verdict("case5", observation)


def _case6_tap_button_ten_times(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 6 (docs/PRD.md:177)."""
    blocked = session.require_guest("case6")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.tap_button_ten_times()
    facts = observation.facts
    if observation.ok:
        problems: list[str] = []
        if facts.get("tap_count") != 10:
            problems.append("case6_tap_count_not_ten")
        counter = facts.get("counter_sequence")
        if list(counter or []) != list(range(1, 11)):
            problems.append("case6_counter_sequence_wrong")
        if facts.get("relaunched") is not True:
            problems.append("case6_app_not_reopened_after_home")
        if problems:
            return verdict_fail(problems, tuple(observation.evidence))
    return _observation_verdict("case6", observation)


def _case7_notes_app_save_and_reopen(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 7 (docs/PRD.md:178)."""
    blocked = session.require_sample("case7", "UniPadNotes.ipa")
    if blocked is not None:
        return blocked
    blocked = session.require_guest("case7")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.import_notes_ipa_and_save_text()
    facts = observation.facts
    if observation.ok:
        saved = str(facts.get("saved_text") or "")
        read_back = str(facts.get("read_back_text") or "")
        problems: list[str] = []
        if not saved:
            problems.append("case7_no_text_saved")
        if saved != read_back:
            problems.append("case7_text_not_read_back")
        if problems:
            return verdict_fail(problems, tuple(observation.evidence))
    return _observation_verdict("case7", observation)


def _case8_shutdown_and_reboot_from_disk(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 8 (docs/PRD.md:179)."""
    blocked = session.require_guest("case8")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.shutdown_and_reboot_from_disk()
    facts = observation.facts
    if observation.ok:
        problems: list[str] = []
        if facts.get("old_pid") is None or facts.get("new_pid") is None:
            problems.append("case8_process_ids_not_recorded")
        elif facts.get("old_pid") == facts.get("new_pid"):
            problems.append("case8_not_a_new_process")
        if facts.get("graceful_shutdown") is not True:
            problems.append("case8_shutdown_not_normal")
        installed = {str(b) for b in (facts.get("installed_bundle_ids") or [])}
        if len(installed) < 2:
            problems.append("case8_apps_missing_after_reboot")
        if not str(facts.get("marker_read_back") or ""):
            problems.append("case8_saved_string_missing_after_reboot")
        if problems:
            return verdict_fail(problems, tuple(observation.evidence))
    return _observation_verdict("case8", observation)


def _case9_three_cold_boots(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 9 (docs/PRD.md:180)."""
    blocked = session.require_guest("case9")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.repeat_cold_boot_three_times()
    facts = observation.facts
    if observation.ok:
        cycles = list(facts.get("cycles") or [])
        problems: list[str] = []
        if len(cycles) != 3:
            problems.append("case9_cycle_count_not_three")
        for index, cycle in enumerate(cycles, start=1):
            if not isinstance(cycle.get("duration_s"), (int, float)):
                problems.append(f"case9_cycle_{index}_duration_missing")
            if not str(cycle.get("result") or ""):
                problems.append(f"case9_cycle_{index}_result_missing")
        paths = [str(p) for p in (facts.get("evidence_paths") or [])]
        if len(paths) != len(set(paths)):
            problems.append("case9_evidence_overwritten")
        if problems:
            return verdict_fail(problems, tuple(observation.evidence))
    return _observation_verdict("case9", observation)


def _case10_unsupported_platform_package(session: CaseSession) -> CaseVerdict:
    """PRD 5.2 case 10 (docs/PRD.md:181)."""
    blocked = session.require_guest("case10")
    if blocked is not None:
        return blocked
    assert session.runtime is not None
    observation = session.runtime.import_unsupported_platform_package()
    facts = observation.facts
    if observation.ok:
        problems: list[str] = []
        code = str(facts.get("error_code") or "")
        if code not in (
            ErrorCode.UNSUPPORTED_PLATFORM,
            ErrorCode.UNSUPPORTED_ARCH,
            ErrorCode.INVALID_PACKAGE,
        ):
            problems.append("case10_rejection_code_not_a_package_precheck_code")
        if not str(facts.get("reason") or ""):
            problems.append("case10_rejection_reason_missing")
        if facts.get("previously_installed_recoverable") is not True:
            problems.append("case10_existing_apps_not_recoverable")
        if problems:
            return verdict_fail(problems, tuple(observation.evidence))
    return _observation_verdict("case10", observation)


CASE_PROBES: dict[str, Callable[[CaseSession], CaseVerdict]] = {
    "case1_start_new_qemu_process": _case1_start_new_qemu_process,
    "case2_reach_springboard": _case2_reach_springboard,
    "case3_import_hello_ipa": _case3_import_hello_ipa,
    "case4_install_hello_ipa": _case4_install_hello_ipa,
    "case5_launch_hello_ipa": _case5_launch_hello_ipa,
    "case6_tap_button_ten_times": _case6_tap_button_ten_times,
    "case7_notes_app_save_and_reopen": _case7_notes_app_save_and_reopen,
    "case8_shutdown_and_reboot_from_disk": _case8_shutdown_and_reboot_from_disk,
    "case9_three_cold_boots": _case9_three_cold_boots,
    "case10_unsupported_platform_package": _case10_unsupported_platform_package,
}
# ---------------------------------------------------------------------------
# the case registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseSpec:
    """One PRD 5.2 case: identity, dependency, declared evidence, probe."""

    case_id: str
    prd_citation: str
    title: str
    depends_on: tuple[str, ...]
    required_evidence: tuple[str, ...]
    probe: str
    code_state: str = "CODE COMPLETE"
    deadline_ms: int = 600_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "required_evidence", tuple(self.required_evidence))
        if not self.case_id or not self.title:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID, "a case needs an id and a title"
            )
        if self.probe not in CASE_PROBES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "case probe is not registered",
                case_id=self.case_id,
                probe=self.probe,
            )
        unknown = sorted(set(self.required_evidence) - EVIDENCE_KINDS)
        if unknown:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "case declares an unknown evidence kind",
                case_id=self.case_id,
                unknown=unknown,
            )
        if not self.required_evidence:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a case must declare the evidence it needs",
                case_id=self.case_id,
            )
        if self.code_state not in FIELD_VOCABULARY["implementation"]:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown implementation state",
                case_id=self.case_id,
                code_state=self.code_state,
            )


CASES: tuple[CaseSpec, ...] = (
    CaseSpec(
        case_id="case1",
        prd_citation=f"{PRD}:172",
        title="在用户 Windows 本机启动一个全新的 QEMU 进程；保留实际配置与版本证据",
        depends_on=(),
        required_evidence=(
            "host_inventory",
            "experiment_json",
            "qemu_args",
            "qemu_version",
            "serial_log",
            "qmp_log",
        ),
        probe="case1_start_new_qemu_process",
    ),
    CaseSpec(
        case_id="case2",
        prd_citation=f"{PRD}:173",
        title="进入真实 SpringBoard，通过鼠标操作改变客体界面",
        depends_on=("case1",),
        required_evidence=("guest_log", "screendump", "input_journal", "process_evidence"),
        probe="case2_reach_springboard",
    ),
    CaseSpec(
        case_id="case3",
        prd_citation=f"{PRD}:174",
        title="在桌面启动后导入 HelloUniPad.ipa，记录输入包 SHA-256；先证明它此前未安装",
        depends_on=("case2",),
        required_evidence=("package_sha256", "precheck_report", "preinstall_status"),
        probe="case3_import_hello_ipa",
    ),
    CaseSpec(
        case_id="case4",
        prd_citation=f"{PRD}:175",
        title="完成包检查、传输、安装、注册；SpringBoard 出现真实图标，同时获得安装结果与 bundle ID",
        depends_on=("case3",),
        required_evidence=(
            "precheck_report",
            "transfer_receipt",
            "install_record",
            "icon_evidence",
            "bundle_id",
        ),
        probe="case4_install_hello_ipa",
    ),
    CaseSpec(
        case_id="case5",
        prd_citation=f"{PRD}:176",
        title="点击图标启动；获得对应进程或等效可信运行证据，并显示本次挑战值",
        depends_on=("case4",),
        required_evidence=("process_evidence", "challenge_value", "screenshot"),
        probe="case5_launch_hello_ipa",
    ),
    CaseSpec(
        case_id="case6",
        prd_citation=f"{PRD}:177",
        title="连续点击按钮 10 次，画面计数正确变化；退出回主屏幕后能再次打开",
        depends_on=("case5",),
        required_evidence=("input_journal", "counter_trace", "relaunch_evidence"),
        probe="case6_tap_button_ten_times",
    ),
    CaseSpec(
        case_id="case7",
        prd_citation=f"{PRD}:178",
        title="导入 UniPadNotes.ipa，输入本次生成的字符串并保存；关闭重开 App 后可读",
        depends_on=("case6",),
        required_evidence=(
            "install_record",
            "package_sha256",
            "saved_text",
            "read_back_text",
        ),
        probe="case7_notes_app_save_and_reopen",
    ),
    CaseSpec(
        case_id="case8",
        prd_citation=f"{PRD}:179",
        title="正常关机，结束 QEMU 进程，再从磁盘启动新进程；两款 App 和保存的字符串仍存在",
        depends_on=("case7",),
        required_evidence=(
            "shutdown_record",
            "new_process_record",
            "post_reboot_registry",
            "post_reboot_marker",
        ),
        probe="case8_shutdown_and_reboot_from_disk",
    ),
    CaseSpec(
        case_id="case9",
        prd_citation=f"{PRD}:180",
        title="重复冷启动流程三次，保存耗时和结果；测试失败时保留证据，不覆盖上次失败",
        depends_on=("case8",),
        required_evidence=("boot_timings", "cycle_results", "failure_preserved"),
        probe="case9_three_cold_boots",
    ),
    CaseSpec(
        case_id="case10",
        prd_citation=f"{PRD}:181",
        title="导入一个不支持的平台包，返回明确原因；已有可用系统和应用保持可恢复",
        depends_on=("case9",),
        required_evidence=("precheck_report", "rejection_reason", "post_rejection_status"),
        probe="case10_unsupported_platform_package",
    ),
)

_CASE_INDEX: dict[str, CaseSpec] = {spec.case_id: spec for spec in CASES}


def case_spec(case_id: str) -> CaseSpec:
    """Return the registered spec for ``case_id`` (import-time validated)."""
    try:
        return _CASE_INDEX[case_id]
    except KeyError:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown case id",
            case_id=case_id,
            known_cases=sorted(_CASE_INDEX),
        ) from None


def _check_dependency_graph() -> tuple[str, ...]:
    """Return the dependency defects of :data:`CASES` (empty when sound)."""
    defects: list[str] = []
    known = set(_CASE_INDEX)
    for spec in CASES:
        for dep in spec.depends_on:
            if dep not in known:
                defects.append(f"unknown_dependency:{spec.case_id}->{dep}")
            if dep == spec.case_id:
                defects.append(f"self_dependency:{spec.case_id}")
        if spec.case_id not in spec.depends_on and spec.depends_on:
            position = list(_CASE_INDEX).index(spec.case_id)
            for dep in spec.depends_on:
                if list(_CASE_INDEX).index(dep) >= position:
                    defects.append(f"forward_dependency:{spec.case_id}->{dep}")
    if len(_CASE_INDEX) != len(CASES):
        defects.append("duplicate_case_id")
    return tuple(defects)


def coverage_gaps() -> tuple[str, ...]:
    """Every PRD 5.2 case that is not fully mapped to a registered probe.

    A gap is a failure reason for the whole sequence, not an omission: the
    release verdict cannot be ``PASS`` while this is non-empty.
    """
    gaps: list[str] = list(_check_dependency_graph())
    for spec in CASES:
        if spec.probe not in CASE_PROBES:
            gaps.append(f"case_without_probe:{spec.case_id}")
        if not spec.required_evidence:
            gaps.append(f"case_without_required_evidence:{spec.case_id}")
    expected = [f"case{index}" for index in range(1, 11)]
    if sorted(_CASE_INDEX) != sorted(expected):
        gaps.append("case_registry_mismatch")
    return tuple(gaps)


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------


class CaseSession:
    """The state carried across the ten cases, in PRD order.

    Everything a later case reads from an earlier one goes through
    :meth:`remember` / :meth:`facts_of`, so the dependency is explicit data
    rather than an accident of evaluation order.
    """

    def __init__(
        self,
        env: "E2EEnvironment",
        facts: HostFacts,
        runtime: E2ERuntime | None,
        evidence_dir: Path,
    ) -> None:
        self.env = env
        self.facts = facts
        self.runtime = runtime
        self.evidence_dir = evidence_dir
        self.case_facts: dict[str, dict[str, Any]] = {}
        self.case_evidence: dict[str, tuple[EvidenceRef, ...]] = {}
        self.written: list[str] = []

    # -- evidence ---------------------------------------------------------

    def record(
        self,
        filename: str,
        payload: str | bytes,
        *,
        kind: str,
        scope: str = HOST_REAL,
        witness: str | None = None,
    ) -> EvidenceRef:
        """Write one evidence file under the run directory and reference it."""
        data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        relative = f"{EVIDENCE_DIRNAME}/{filename}"
        if "\\" in filename or _is_absolute_like(filename):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "evidence filename must be a plain relative name",
                filename=filename,
            )
        target = self.evidence_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.written.append(relative)
        return EvidenceRef(
            path=relative,
            kind=kind,
            sha256=_sha256_bytes(data),
            scope=scope,
            witness=witness,
            observed_at=_utc_now_iso(),
        )

    def record_json(
        self, filename: str, document: Mapping[str, Any], *, kind: str = "manifest",
        scope: str = HOST_REAL, witness: str | None = None,
    ) -> EvidenceRef:
        text = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
        return self.record(
            filename, text, kind=kind, scope=scope, witness=witness
        )

    # -- cross-case state -------------------------------------------------

    def remember(
        self, case_id: str, facts: Mapping[str, Any], evidence: Sequence[EvidenceRef]
    ) -> None:
        self.case_facts[case_id] = dict(facts)
        self.case_evidence[case_id] = tuple(evidence)

    def facts_of(self, case_id: str) -> dict[str, Any]:
        return dict(self.case_facts.get(case_id, {}))

    def evidence_of(self, case_id: str) -> tuple[EvidenceRef, ...]:
        return tuple(self.case_evidence.get(case_id, ()))

    # -- prerequisites ----------------------------------------------------

    def sample(self, name: str) -> SampleFacts:
        found = self.facts.samples.get(name)
        if found is None:
            return SampleFacts(
                name=name,
                prd_citation=f"{PRD}:163-164",
                verdict=BLOCKED,
                reasons=(f"sample_not_registered:{name}",),
            )
        return found

    def missing_prerequisites(self) -> tuple[str, ...]:
        """Concrete slugs for what a real guest boot needs and this host lacks."""
        return tuple(self.facts.missing)

    def require_guest(self, case_id: str) -> CaseVerdict | None:
        """A ``BLOCKED`` verdict naming every missing guest prerequisite."""
        missing = self.missing_prerequisites()
        if missing:
            detail = "; ".join(
                {
                    "qemu_binary": "no pinned qemu-system-aarch64 binary is present",
                    "accelerator_not_tcg": "machine config does not request tcg",
                    "kernel_image": "no built UniXNU kernel image (kernel.macho)",
                    "system_image": "no system image is configured (disks.system_image is null)",
                    "data_image": "no persistent Data image is configured",
                    "guest_install_service": "no guest install service is reachable",
                    "springboard_process": "no SpringBoard process has been observed",
                }.get(item, item)
                for item in missing
            )
            return verdict_blocked(f"host_cannot_boot_a_guest: {detail}")
        if self.runtime is None:
            return verdict_blocked(
                "no_e2e_runtime: no runtime is bound to perform the PRD 5.2 actions"
            )
        return None

    def require_sample(self, case_id: str, name: str) -> CaseVerdict | None:
        """A ``BLOCKED``/``UNVERIFIED`` verdict when a PRD 5.1 sample is unusable."""
        found = self.sample(name)
        if found.verdict == PASS:
            return None
        return CaseVerdict(
            found.verdict,
            found.reasons,
            found.evidence,
            found.verdict == FAIL,
        )




def _enforce_required_evidence(
    spec: CaseSpec, verdict: CaseVerdict
) -> CaseVerdict:
    """Refuse ``PASS`` when a declared evidence kind is absent.

    Only ever downgrades: a verdict that is already ``FAIL``/``BLOCKED`` keeps
    its own reasons, and a ``PASS`` becomes ``UNVERIFIED`` with
    ``missing_evidence:<kind>`` rather than being waved through.
    """
    missing = sorted(set(spec.required_evidence) - verdict.evidence_kinds)
    if not missing:
        return verdict
    if verdict.kind == PASS:
        return verdict_unverified(
            f"missing_evidence:{spec.case_id}:{','.join(missing)}",
            verdict.evidence,
        )
    return CaseVerdict(
        verdict.kind,
        verdict.reasons + tuple(f"missing_evidence:{kind}" for kind in missing),
        verdict.evidence,
        verdict.observed,
    )


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class E2EEnvironment:
    """Everything the sequence needs that is not the host itself."""

    repo_root: Path
    run_dir: Path
    sample_roots: tuple[Path, ...] = ()
    sample_source_manifests: Mapping[str, Path] = field(default_factory=dict)
    runtime: E2ERuntime | None = None
    host_facts: HostFacts | None = None
    probe_cim: bool = False
    verify_qemu_version: bool = True
    clock: Callable[[], float] = time.monotonic
    iso_now: Callable[[], str] = _utc_now_iso

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_roots", tuple(self.sample_roots))
        object.__setattr__(
            self, "sample_source_manifests", dict(self.sample_source_manifests)
        )

    @classmethod
    def create(
        cls,
        run_dir: str | Path,
        *,
        repo_root: str | Path | None = None,
        **kwargs: Any,
    ) -> "E2EEnvironment":
        """Build an environment rooted at the repository, with default roots."""
        root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[3]
        return cls(
            repo_root=root,
            run_dir=Path(run_dir),
            sample_roots=tuple(root / name for name in DEFAULT_SAMPLE_ROOTS),
            **kwargs,
        )

    @property
    def system_version(self) -> tuple[int, ...]:
        lock = _load_json(Path(self.repo_root) / "config" / "sources.lock.json")
        return _version_tuple((lock.get("system_identity") or {}).get("system") or "")


# ---------------------------------------------------------------------------
# outcomes and the report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseOutcome:
    """One evaluated case: verdict, four fields, and whether it was attempted."""

    case_id: str
    prd_citation: str
    title: str
    depends_on: tuple[str, ...]
    verdict: CaseVerdict
    fields: FourFields
    probe: str
    attempted: bool
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "prd_citation": self.prd_citation,
            "title": self.title,
            "depends_on": list(self.depends_on),
            "probe": self.probe,
            "attempted": self.attempted,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "verdict": self.verdict.to_dict(),
            "four_fields": self.fields.to_dict(),
        }


@dataclass(frozen=True)
class SequenceReport:
    """The whole PRD 5.2 sequence as one machine-readable document."""

    schema_version: int
    run_id: str
    started_at: str
    finished_at: str
    run_complete: bool
    environment: dict[str, Any]
    cases: tuple[CaseOutcome, ...]
    samples: tuple[SampleFacts, ...]
    four_fields: tuple[FourFields, ...]
    field_unverified: tuple[dict[str, Any], ...]
    aggregate: dict[str, Any]
    evidence_index: tuple[dict[str, Any], ...]
    coverage_gaps: tuple[str, ...]

    @property
    def verdict(self) -> str:
        return str(self.aggregate.get("verdict", INCOMPLETE))

    def case(self, case_id: str) -> CaseOutcome:
        for outcome in self.cases:
            if outcome.case_id == case_id:
                return outcome
        raise UniPadError(
            ErrorCode.CONFIG_INVALID, "unknown case id in report", case_id=case_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "run_complete": self.run_complete,
            "environment": dict(self.environment),
            "coverage_gaps": list(self.coverage_gaps),
            "samples": [sample.to_dict() for sample in self.samples],
            "cases": [outcome.to_dict() for outcome in self.cases],
            "four_fields": [fields.to_dict() for fields in self.four_fields],
            "field_unverified": [dict(row) for row in self.field_unverified],
            "aggregate": dict(self.aggregate),
            "evidence_index": [dict(row) for row in self.evidence_index],
        }

    def to_json(self) -> str:
        text = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        assert_publishable(text, where="sequence-report")
        return text


def release_verdict(report: SequenceReport) -> tuple[str, tuple[str, ...]]:
    """The release judgement, computed from the *field* axis, not the verdicts.

    Even when every case reads ``PASS``, zero ``onsite`` references anywhere in
    the sequence keep the release ``BLOCKED`` -- the mechanical form of
    "SIMULATION PASS is not 现场 PASS" (CLAUDE.md four-field rule).
    """
    if not report.run_complete:
        return (INCOMPLETE, ("run_incomplete",))
    if report.coverage_gaps:
        return (FAIL, tuple(f"coverage_gap:{gap}" for gap in report.coverage_gaps))
    kinds = [outcome.verdict.kind for outcome in report.cases]
    if FAIL in kinds:
        return (
            FAIL,
            tuple(
                f"case_failed:{outcome.case_id}:{outcome.verdict.reasons[0]}"
                for outcome in report.cases
                if outcome.verdict.kind == FAIL
            ),
        )
    if BLOCKED in kinds:
        return (
            BLOCKED,
            tuple(
                f"case_blocked:{outcome.case_id}:{outcome.verdict.reasons[0]}"
                for outcome in report.cases
                if outcome.verdict.kind == BLOCKED
            ),
        )
    if UNVERIFIED in kinds:
        return (
            UNVERIFIED,
            tuple(
                f"case_unverified:{outcome.case_id}:{outcome.verdict.reasons[0]}"
                for outcome in report.cases
                if outcome.verdict.kind == UNVERIFIED
            ),
        )
    onsite = [
        ref
        for outcome in report.cases
        for ref in outcome.verdict.evidence
        if ref.scope == ONSITE
    ]
    if not onsite:
        return (BLOCKED, ("no_onsite_evidence",))
    return (PASS, ())


# ---------------------------------------------------------------------------
# the sequence
# ---------------------------------------------------------------------------


class E2ESequence:
    """Run the ten PRD 5.2 cases in order, propagating dependency explicitly."""

    SCHEMA_VERSION = 1

    def __init__(self, env: E2EEnvironment) -> None:
        gaps = coverage_gaps()
        if gaps:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the PRD 5.2 case registry has coverage gaps",
                gaps=list(gaps),
            )
        self.env = env
        self.run_dir = Path(env.run_dir)
        self.evidence_dir = self.run_dir / EVIDENCE_DIRNAME
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.facts = env.host_facts or observe_host(env)

    # -- public API -------------------------------------------------------

    def run(self, run_id: str = "e2e-prd-5-2") -> SequenceReport:
        started_at = self.env.iso_now()
        started = self.env.clock()
        session = CaseSession(
            self.env, self.facts, self.env.runtime, self.evidence_dir
        )
        outcomes: list[CaseOutcome] = []
        run_complete = True
        for spec in CASES:
            try:
                outcome = self._run_case(spec, outcomes, session)
            except UniPadError:
                raise
            except Exception as exc:  # a crashing probe is a failed case
                run_complete = False
                outcome = self._blocked_outcome(
                    spec,
                    verdict_blocked(f"probe_error:{spec.case_id}:{type(exc).__name__}"),
                    attempted=True,
                )
            outcomes.append(outcome)
        finished_at = self.env.iso_now()

        implementation_ref = session.record_json(
            "implementation.json",
            {
                "schema_version": 1,
                "note": (
                    "Which host-side modules implement each PRD 5.2 case.  This "
                    "records that the code exists; it says nothing about a guest."
                ),
                "cases": {
                    spec.case_id: {
                        "probe": spec.probe,
                        "modules": [
                            "src/unipad/acceptance/e2e.py",
                            "src/unipad/host/qemu.py",
                            "src/unipad/host/qmp.py",
                            "src/unipad/host/input.py",
                            "src/unipad/ipa/precheck.py",
                            "src/unipad/ipa/installer.py",
                            "src/unipad/guest/client.py",
                        ],
                    }
                    for spec in CASES
                },
            },
            kind="manifest",
        )

        four_fields: list[FourFields] = []
        for outcome in outcomes:
            four_fields.append(
                FourFields(
                    check_id=outcome.case_id,
                    verdict=outcome.verdict,
                    evidence=outcome.verdict.evidence,
                    blocker=outcome.verdict.blocker,
                    implementation_evidence=(implementation_ref,),
                    code_state=case_spec(outcome.case_id).code_state,
                )
            )

        field_unverified: list[dict[str, Any]] = []
        for fields in four_fields:
            if fields.onsite_status != "PASS":
                field_unverified.append(
                    {
                        "case_id": fields.check_id,
                        "prd_citation": case_spec(fields.check_id).prd_citation,
                        "verdict": fields.verdict.kind,
                        "blocker": fields.blocker or "-",
                    }
                )

        evidence_index = tuple(
            {
                "path": ref.path,
                "sha256": ref.sha256,
                "scope": ref.scope,
                "witness": ref.witness,
                "observed_at": ref.observed_at,
            }
            for outcome in outcomes
            for ref in outcome.verdict.evidence
        )

        report = SequenceReport(
            schema_version=self.SCHEMA_VERSION,
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            run_complete=run_complete,
            environment=self._environment_stamp(),
            cases=tuple(outcomes),
            samples=tuple(self.facts.samples[name] for name in sorted(self.facts.samples)),
            four_fields=tuple(four_fields),
            field_unverified=tuple(field_unverified),
            aggregate={},
            evidence_index=evidence_index,
            coverage_gaps=coverage_gaps(),
        )
        verdict, reasons = release_verdict(report)
        object.__setattr__(
            report, "aggregate", {"verdict": verdict, "reasons": list(reasons)}
        )
        _ = started
        return report

    # -- internals --------------------------------------------------------

    def _run_case(
        self,
        spec: CaseSpec,
        prior: Sequence[CaseOutcome],
        session: CaseSession,
    ) -> CaseOutcome:
        case_started = self.env.iso_now()
        unsatisfied = [
            (outcome.case_id, outcome.verdict.kind)
            for outcome in prior
            if outcome.case_id in spec.depends_on and outcome.verdict.kind != PASS
        ]
        if unsatisfied:
            # Two spellings of the same fact, as two *discrete* reasons: the
            # sentence a human reads, and the token a machine greps for.  A
            # blocked case must name which predecessor and what it was, and
            # neither spelling may be folded into the other.
            reasons: list[str] = []
            for dep, kind in unsatisfied:
                reasons.append(f"predecessor not satisfied: {dep} is {kind}")
                reasons.append(f"predecessor_not_satisfied:{dep}:{kind}")
            verdict = CaseVerdict(BLOCKED, tuple(reasons), (), False)
            return self._blocked_outcome(spec, verdict, attempted=False)

        probe = CASE_PROBES[spec.probe]
        verdict = probe(session)
        verdict = _enforce_required_evidence(spec, verdict)
        facts = session.case_facts.get(spec.case_id, {})
        session.remember(spec.case_id, facts, verdict.evidence)
        return CaseOutcome(
            case_id=spec.case_id,
            prd_citation=spec.prd_citation,
            title=spec.title,
            depends_on=spec.depends_on,
            verdict=verdict,
            fields=FourFields(
                check_id=spec.case_id,
                verdict=verdict,
                evidence=verdict.evidence,
                blocker=verdict.blocker,
                code_state=spec.code_state,
            ),
            probe=spec.probe,
            attempted=True,
            started_at=case_started,
            finished_at=self.env.iso_now(),
        )

    def _blocked_outcome(
        self, spec: CaseSpec, verdict: CaseVerdict, *, attempted: bool
    ) -> CaseOutcome:
        return CaseOutcome(
            case_id=spec.case_id,
            prd_citation=spec.prd_citation,
            title=spec.title,
            depends_on=spec.depends_on,
            verdict=verdict,
            fields=FourFields(
                check_id=spec.case_id,
                verdict=verdict,
                evidence=verdict.evidence,
                blocker=verdict.blocker,
                code_state=spec.code_state,
            ),
            probe=spec.probe,
            attempted=attempted,
            started_at="",
            finished_at=self.env.iso_now(),
        )

    def _environment_stamp(self) -> dict[str, Any]:
        """A stamp with no username, no path and no token in it (PRD 9.2)."""
        inventory = self.facts.host_inventory
        host = inventory.get("host") or {}
        return {
            "host_fingerprint": self.facts.host_inventory_sha256,
            "host_label": self.facts.host_label,
            "accel": self.facts.accel,
            "qemu_version": self.facts.qemu_version,
            "os_build": str(host.get("kernel") or ""),
            "python_version": platform.python_version(),
            "collected_at": _utc_now_iso(),
            "host_inventory_sha256": self.facts.host_inventory_sha256,
        }
