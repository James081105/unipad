"""The four-field acceptance record: two orthogonal axes, one check.

An acceptance check has to answer two different questions, and the existing
verifier (:mod:`unipad.controller.verifier`) only answers the first one:

* **axis A -- the condition axis.**  ``verdict`` says whether the registered
  fixed condition holds *for the evidence that was supplied*.  Four mutually
  exclusive values: ``PASS`` / ``FAIL`` / ``BLOCKED`` / ``UNVERIFIED``.
* **axis B -- the environment axis.**  ``fields`` says *in which environment*
  that evidence was produced: implementation state, Windows/simulation state,
  on-site state, and what blocks the on-site state.

They must stay separate.  ``Verifier.evaluate_detailed`` returns
``passed=True`` when the evidence is complete and the conditions hold; it has
no concept -- and must not acquire one -- of "this evidence came from a fake
guest".  Mapping ``passed=True`` straight onto ``PASS`` would launder "passes
against a fake guest" into "acceptance passed", which is exactly the
over-claiming the project keeps catching (PRD 6 "不足以通过的情况": a single
serial line, a static picture, ``mockfs`` PID 1, an icon on disk).

So a check may perfectly well be ``verdict = PASS`` while
``fields.onsite_status == "UNVERIFIED"``.  That is the structured form of
"SIMULATION PASS is not on-site PASS" (project CLAUDE.md, 四字段记录).

Three rules are load-bearing and are enforced by construction rather than by
convention:

1. **A PASS without evidence cannot be built.**  :class:`Verdict` has no
   default for any of its four fields and cross-validates them, so
   ``Verdict("PASS", (), (), False)`` raises instead of existing (PRD 9.2:
   证据缺失属于未验证，不能靠模型文字补全).
2. **A PASS must be re-verifiable.**  Every reference a ``PASS`` rests on has
   to carry a digest, so a later reader can re-hash the file and confirm the
   claim.  A reference without one is still constructible -- a ``FAIL`` or
   ``BLOCKED`` check has to be able to cite an artefact it could not read --
   but it cannot carry a ``PASS``.
3. **Scope is decided by the witness, not by the claimant.**  An
   ``ONSITE`` :class:`EvidenceRef` must carry a witness -- one concrete fact
   only the target object could have produced -- and a bundle that is loaded
   re-reads the file and re-checks it.
4. **The environment fields are derived, never assigned.**  ``FourFields``
   exposes ``implementation`` / ``windows_status`` / ``onsite_status`` as
   read-only properties computed from the verdict and the evidence scopes, so
   hanging a hundred simulation references off a check cannot lift its
   on-site field, and neither can a green test suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable

from ..errors import ErrorCode, UniPadError

__all__ = [
    "EvidenceScope",
    "EvidenceRef",
    "EnvironmentStamp",
    "FourFields",
    "Measurement",
    "Verdict",
    "VerdictKind",
    "VERDICT_KINDS",
    "SEVERITY",
    "CODE_STATES",
    "WINDOWS_STATUSES",
    "ONSITE_STATUSES",
    "WINDOWS_STATUS_ALIASES",
    "canonical_windows_status",
    "assert_payload_redacted",
    "assert_verdict_wellformed",
    "redact",
    "relativise",
    "scope_at_least",
    "worst_of",
]

# --- evidence scope -------------------------------------------------------
# Ordered by strength: SIMULATION < HOST_REAL < ONSITE.  The order is what
# makes "at least this scope" expressible without a second vocabulary.


class EvidenceScope(str):
    """Where a piece of evidence was produced (PRD 13/60, H05)."""

    SIMULATION = "simulation"    # driven by tests / fakes
    HOST_REAL = "host_real"      # real QEMU and real firmware on this host,
                                 # but the object is not the target iPadOS
    ONSITE = "onsite"            # this host, and the target iPadOS object was
                                 # observed (requires a witness)


_SCOPES: tuple[str, ...] = (
    EvidenceScope.SIMULATION,
    EvidenceScope.HOST_REAL,
    EvidenceScope.ONSITE,
)
_SCOPE_RANK: dict[str, int] = {scope: rank for rank, scope in enumerate(_SCOPES)}

#: Evidence kinds the bundle understands.  A closed set: an unknown kind is a
#: usage error, never something to carry silently into a report.
EVIDENCE_KINDS: tuple[str, ...] = (
    "serial_log",
    "guest_log",
    "qmp_log",
    "screenshot",
    "artifact",
    "suite_report",
    "verification_json",
    "manifest",
    "ledger",
    "probe_log",
)

#: A witness is ``"<file>:<lineno>:<literal>"`` -- one line of one file that
#: only the target object could have written -- or ``"witness:<id>"`` naming a
#: pattern registered by the probe registry.
_WITNESS_PATTERN = re.compile(r"^(?P<file>[A-Za-z0-9._\-/]+):(?P<lineno>\d+):(?P<literal>.+)$")
_WITNESS_REGISTRY_PREFIX = "witness:"


def is_witness(value: str) -> bool:
    """Return whether ``value`` is a well-formed witness reference."""
    if value.startswith(_WITNESS_REGISTRY_PREFIX):
        return len(value) > len(_WITNESS_REGISTRY_PREFIX)
    return _WITNESS_PATTERN.match(value) is not None


def scope_at_least(scope: str, minimum: str) -> bool:
    """Return whether ``scope`` is at least as strong as ``minimum``."""
    try:
        return _SCOPE_RANK[scope] >= _SCOPE_RANK[minimum]
    except KeyError as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown evidence scope",
            scope=scope,
            known_scopes=list(_SCOPES),
        ) from exc


# --- path and payload hygiene (PRD 9.2) -----------------------------------

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:[\\/]")
# A user-profile path, WITH the account component.  The account is the thing PRD
# 9.2 protects, so a pattern that stops at the literal ``/Users/`` redacts the
# directory and publishes the username -- which is what this one used to do:
#
#     redact("C:/Users/SomeBody/AppData/...")
#       -> "<redacted:user_path>SomeBody/AppData/..."
#
# The account survived its own redaction, and because this function builds the
# ``excerpt`` of assert_payload_redacted's refusal, the message that says "this
# payload carries an absolute path" carried the username along with it.
#
# ``[^\\/\s'\"<>|]*`` stops at a separator or at whitespace/quotes, so a path
# embedded in prose does not swallow the sentence around it.  The pattern is
# deliberately NOT anchored to the start: ``redact`` is applied to arbitrary
# message text, and a profile path can appear anywhere in it.  The cost is that a
# relative path with a ``users/`` segment (``myapp/users/config.json``) is also
# redacted -- over-redaction, in the same direction the previous pattern already
# erred, and the safe one: losing a segment is recoverable, publishing an account
# name is not.  See tests/unit/test_record_redaction.py.
_USER_PATH = re.compile(
    r"(?:[A-Za-z]:)?[\\/](?:Users|home|root)[\\/][^\\/\s'\"<>|]*",
    re.IGNORECASE,
)

#: Secret shapes that must never reach a report or a bundle (PRD 9.2: 日志不
#: 含账号令牌、签名私钥和用户文档).
_SECRET_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"ghp_[A-Za-z0-9]{16,}"), "github_token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_token"),
    (re.compile(r"(?:^|\s)Bearer\s+[A-Za-z0-9._\-]{8,}"), "bearer_token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private_key"),
)

#: 40-hex (git) and 64-hex (sha256) blobs are only allowed when the bundle
#: declares them as a digest of something; anything else is an unaccounted
#: secret that got pasted into a message.
_HEX_BLOB = re.compile(r"\b[a-f0-9]{40}\b|\b[a-f0-9]{64}\b")


def relativise(raw: str | Any, run_dir: str | Any | None = None) -> str:
    """Return ``raw`` as a clean run-relative POSIX path.

    Absolute paths, Windows separators, drive letters and ``..`` are refused
    rather than rewritten.  When ``run_dir`` is given, a path inside that
    directory is first expressed relative to it -- that is how an absolute
    ``verification.json`` path coming out of the verifier becomes a reference
    this bundle can re-check instead of a host path in a report (PRD 9.2).
    Anything outside the run directory is not evidence this run owns.

    Raises
    ------
    UniPadError
        ``CONFIG_INVALID`` with ``absolute_path_in_evidence`` or
        ``invalid_evidence_path``.
    """
    text = str(raw)
    if not text or text != text.strip():
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "evidence path must be a non-empty, unpadded string",
            path=repr(text),
        )
    if run_dir is not None:
        base = str(run_dir).replace("\\", "/").rstrip("/")
        candidate = text.replace("\\", "/")
        prefix = base + "/"
        if candidate.startswith(prefix):
            text = candidate[len(prefix) :]
        elif candidate == base:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "the run directory itself is not evidence",
                path=text,
            )
    if "\\" in text or _DRIVE_LETTER.match(text) or text.startswith("/"):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "absolute path is not admissible as evidence",
            absolute_path_in_evidence=text,
        )
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "evidence path is not a clean relative path",
            path=text,
        )
    return text


def redact(text: str) -> str:
    """Return ``text`` with tokens, keys and user paths scrubbed (PRD 9.2)."""
    scrubbed = str(text)
    for pattern, label in _SECRET_MARKERS:
        scrubbed = pattern.sub(f"<redacted:{label}>", scrubbed)
    scrubbed = _USER_PATH.sub("<redacted:user_path>", scrubbed)
    if _DRIVE_LETTER.match(scrubbed):
        scrubbed = "<redacted:absolute_path>" + scrubbed[2:]
    if scrubbed.startswith("/"):
        scrubbed = "<redacted:absolute_path>" + scrubbed[1:]
    return scrubbed


def assert_payload_redacted(
    payload: Any, *, declared_digests: frozenset[str] = frozenset()
) -> None:
    """Refuse a payload that carries a secret, a user path or an absolute path.

    The check walks every string in ``payload``.  A 40/64-hex blob is allowed
    only when it appears verbatim in ``declared_digests`` -- the digests this
    bundle accounts for -- so an undeclared one cannot ride along inside a
    message.  Refusing to write is the point: a redacted-after-the-fact bundle
    is a bundle that already leaked (PRD 9.2).

    Raises
    ------
    UniPadError
        ``CONFIG_INVALID`` with ``redaction_refused``, the offending kind and
        a scrubbed excerpt so the operator can find it without the secret
        being repeated in the error.
    """
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            _assert_string_redacted(current, declared_digests)
        elif isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)


def _assert_string_redacted(value: str, declared_digests: frozenset[str]) -> None:
    """Raise on one string that carries a secret or an absolute path."""
    excerpt = redact(value)[:120]
    for pattern, label in _SECRET_MARKERS:
        if pattern.search(value):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "payload carries a credential",
                redaction_refused=label,
                excerpt=excerpt,
            )
    if _USER_PATH.search(value) or _DRIVE_LETTER.match(value) or value.startswith("/"):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "payload carries an absolute or user path",
            redaction_refused="absolute_path",
            excerpt=excerpt,
        )
    for blob in _HEX_BLOB.findall(value):
        if blob not in declared_digests:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "payload carries an undeclared digest",
                redaction_refused="undeclared_digest",
                excerpt=excerpt,
            )


# --- evidence reference ---------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """One artefact a verdict rests on.

    ``path`` is run-relative (see :func:`relativise`); ``sha256`` is required
    unless ``digest_reason`` explains why there is none, so "no hash and no
    explanation" is not a representable state.  ``witness`` is required for
    ``ONSITE`` and must name one concrete fact only the target object could
    have produced -- a firmware banner on a serial log is ``HOST_REAL``
    however long it is (PRD 7.4: 串口文本用于定位，不单独证明阶段成功).

    A digestless reference is legitimate for a verdict that is *not* a pass: a
    ``FAIL`` or ``BLOCKED`` check still has to cite the artefact it could not
    read, and saying why it could not be hashed is more honest than omitting
    it.  It is **not** allowed to carry a ``PASS`` -- see :class:`Verdict`.

    ``ALLOWED_KINDS`` is a class attribute rather than a module constant so a
    module with a wider kind set can subclass instead of re-implementing the
    whole contract.  ``unipad.acceptance.e2e`` declares kinds the bundle
    vocabulary does not have (``qemu_args``, ``package_sha256``, ...); before
    this it kept a second, independent ``EvidenceRef`` whose path and digest
    rules had already drifted from these (F8).
    """

    #: The closed set of kinds this class accepts.  Override in a subclass.
    ALLOWED_KINDS: ClassVar[tuple[str, ...]] = EVIDENCE_KINDS

    path: str
    kind: str
    sha256: str | None = None
    digest_reason: str | None = None
    scope: str = EvidenceScope.SIMULATION
    witness: str | None = None
    observed_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", relativise(self.path))
        if self.kind not in self.ALLOWED_KINDS:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown evidence kind",
                kind=self.kind,
                known_kinds=list(self.ALLOWED_KINDS),
            )
        scope_at_least(self.scope, EvidenceScope.SIMULATION)
        # A whitespace-only "explanation" is the same non-explanation as none:
        # the reason has to be read by a human, so it has to say something.
        if self.sha256 is None and not (self.digest_reason or "").strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "evidence without a digest must say why",
                path=self.path,
                digest_reason=self.digest_reason,
            )
        if self.sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "evidence digest must be 64 lowercase hex characters",
                path=self.path,
            )
        if self.scope == EvidenceScope.ONSITE and not is_witness(self.witness or ""):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "on-site evidence requires a witness only the target could produce",
                onsite_evidence_without_witness=self.path,
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the reference."""
        return {
            "path": self.path,
            "kind": self.kind,
            "sha256": self.sha256,
            "digest_reason": self.digest_reason,
            "scope": self.scope,
            "witness": self.witness,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceRef":
        """Rebuild a reference from :meth:`to_dict` output."""
        return cls(
            path=str(data["path"]),
            kind=str(data["kind"]),
            sha256=data.get("sha256"),
            digest_reason=data.get("digest_reason"),
            scope=str(data.get("scope", EvidenceScope.SIMULATION)),
            witness=data.get("witness"),
            observed_at=str(data.get("observed_at", "")),
        )


@dataclass(frozen=True)
class EnvironmentStamp:
    """Where a run was produced (PRD 13, PRD 60/H05).

    ``accel`` must be ``tcg``: the host is an Intel Windows machine, so WHPX or
    HVF would mean the run did not happen here and must be refused before it
    reaches a verdict.  Every ``EvidenceRef`` of one run carries the same
    stamp, which is what makes "it built on a Mac, so it is fine" unstatable.
    """

    host_fingerprint: str
    host_label: str
    accel: str
    qemu_version: str
    os_build: str
    python_version: str
    collected_at: str
    host_inventory_sha256: str

    def __post_init__(self) -> None:
        if self.accel != "tcg":
            raise UniPadError(
                ErrorCode.ACCELERATOR_NOT_TCG,
                "acceptance evidence must be produced under TCG",
                accelerator_not_tcg=self.accel,
            )
        for name in ("host_fingerprint", "host_label", "qemu_version", "os_build"):
            if not str(getattr(self, name)).strip():
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "environment stamp is missing a required field",
                    field=name,
                )
        # The inventory digest is *declared* by the redaction gate, so a stamp
        # that is not a digest would turn arbitrary text into a string the gate
        # trusts.  It therefore has to look like one.
        if not re.fullmatch(r"[a-f0-9]{64}", self.host_inventory_sha256):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "host inventory digest must be 64 lowercase hex characters",
                field="host_inventory_sha256",
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the stamp."""
        return {
            "host_fingerprint": self.host_fingerprint,
            "host_label": self.host_label,
            "accel": self.accel,
            "qemu_version": self.qemu_version,
            "os_build": self.os_build,
            "python_version": self.python_version,
            "collected_at": self.collected_at,
            "host_inventory_sha256": self.host_inventory_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EnvironmentStamp":
        """Rebuild a stamp from :meth:`to_dict` output."""
        return cls(
            host_fingerprint=str(data["host_fingerprint"]),
            host_label=str(data["host_label"]),
            accel=str(data["accel"]),
            qemu_version=str(data["qemu_version"]),
            os_build=str(data["os_build"]),
            python_version=str(data["python_version"]),
            collected_at=str(data["collected_at"]),
            host_inventory_sha256=str(data["host_inventory_sha256"]),
        )


# --- axis A: the verdict --------------------------------------------------


class VerdictKind(str):
    """The four mutually exclusive condition-axis values (design section 4)."""

    PASS = "PASS"            # the condition holds for the supplied evidence
    FAIL = "FAIL"            # evidence complete, condition not met
    BLOCKED = "BLOCKED"      # material/credential/budget/environment missing
    UNVERIFIED = "UNVERIFIED"  # nobody established it


#: The four kinds, in declaration order.  Public because three other modules
#: used to keep a private copy each -- and one of them validated only ``FAIL``
#: against its own list, so a ``BLOCKED`` verdict with no reason was
#: constructible there (F8).
VERDICT_KINDS: tuple[str, ...] = (
    VerdictKind.PASS,
    VerdictKind.FAIL,
    VerdictKind.BLOCKED,
    VerdictKind.UNVERIFIED,
)

#: Badness order used by every roll-up: a definite failure carries more
#: information than an unknown blockage, so it dominates.
SEVERITY: dict[str, int] = {
    VerdictKind.PASS: 0,
    VerdictKind.UNVERIFIED: 1,
    VerdictKind.BLOCKED: 2,
    VerdictKind.FAIL: 3,
}

# Retained private aliases so the rest of this module reads unchanged.
_VERDICT_KINDS = VERDICT_KINDS
_SEVERITY = SEVERITY


def worst_of(kinds: Iterable[str]) -> str:
    """Return the worst verdict of ``kinds``; an empty set is ``UNVERIFIED``.

    Nothing proved is not a pass, so the empty case fails closed.  A non-empty
    set of ``PASS`` verdicts is ``PASS``: the empty default is the fallback,
    not a floor -- otherwise every all-passing case and milestone would roll up
    to ``UNVERIFIED`` and the roll-up would contradict its own checks.
    """
    worst: str | None = None
    for kind in kinds:
        if kind not in _SEVERITY:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown verdict kind",
                kind=kind,
                known_kinds=list(_VERDICT_KINDS),
            )
        if worst is None or _SEVERITY[kind] > _SEVERITY[worst]:
            worst = kind
    return worst if worst is not None else VerdictKind.UNVERIFIED


@dataclass(frozen=True)
class Verdict:
    """One condition-axis conclusion.

    All four fields are mandatory, so there is no "forgot to fill it in"
    construction, and ``__post_init__`` cross-validates them:

    * ``PASS`` needs at least one evidence reference **and** an observation;
    * ``FAIL`` / ``BLOCKED`` / ``UNVERIFIED`` need at least one reason slug;
    * every reference a ``PASS`` rests on must carry a digest, so the claim can
      be re-verified later by re-hashing the file.

    ``Verdict("PASS", (), (), False)``, ``Verdict("PASS", (), (), True)`` and
    ``Verdict("PASS", (ref,), (), True)`` therefore all raise: an unevidenced
    PASS is not constructible, not merely discouraged (PRD 9.2).

    The digest rule is the third one and it is the difference between "this
    evidence exists" and "this evidence can still be checked".  A reference may
    legitimately have no digest -- :class:`EvidenceRef` accepts one that says
    why -- because a ``FAIL`` or ``BLOCKED`` check still has to be able to cite
    an artefact it could not read.  But a ``PASS`` is a claim that the condition
    holds *now*, and the only way a later reader can confirm it is to re-hash
    the file and compare.  A digestless reference therefore cannot carry a
    ``PASS``: otherwise ``load_bundle`` -- which only re-hashes when ``sha256``
    is not ``None`` -- would accept a PASS on an artefact with no digest at all,
    which is an unverifiable pass wearing the word PASS.
    """

    kind: str
    reasons: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    observed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind))
        object.__setattr__(self, "reasons", tuple(str(reason) for reason in self.reasons))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "observed", bool(self.observed))
        if self.kind not in _VERDICT_KINDS:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown verdict kind",
                kind=self.kind,
                known_kinds=list(_VERDICT_KINDS),
            )
        if self.kind == VerdictKind.PASS:
            if not self.evidence:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "a PASS verdict requires at least one evidence reference",
                    reasons=list(self.reasons),
                )
            if not self.observed:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "a PASS verdict requires an observation",
                    reasons=list(self.reasons),
                )
            unverifiable = [ref.path for ref in self.evidence if ref.sha256 is None]
            if unverifiable:
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "a PASS verdict requires evidence that can be re-verified",
                    undigested_evidence=unverifiable,
                )
        elif not self.reasons:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a non-PASS verdict requires a reason slug",
                kind=self.kind,
            )

    # -- factories ---------------------------------------------------------
    # The only sanctioned way to build each kind.  They do not loosen the
    # cross-validation above; they just keep call sites readable.
    #
    # Every factory accepts evidence, including ``blocked`` and ``unverified``.
    # A check that could not run still has to cite the artefact it looked at
    # before giving up -- that is what makes a BLOCKED row arguable -- and a
    # factory that silently discarded the evidence would push every caller
    # into building the verdict by hand, which is how four modules ended up
    # with four contracts (F8).

    @staticmethod
    def pass_(evidence: Iterable[EvidenceRef], reasons: Iterable[str] = ()) -> "Verdict":
        """Build a ``PASS`` verdict -- the only entry point that can produce one."""
        return Verdict(VerdictKind.PASS, tuple(reasons), tuple(evidence), True)

    @staticmethod
    def fail(reasons: Iterable[str], evidence: Iterable[EvidenceRef] = ()) -> "Verdict":
        """Build a ``FAIL`` verdict: the probe ran and the condition did not hold."""
        return Verdict(VerdictKind.FAIL, tuple(reasons), tuple(evidence), True)

    @staticmethod
    def blocked(
        reason: str, evidence: Iterable[EvidenceRef] = ()
    ) -> "Verdict":
        """Build a ``BLOCKED`` verdict: the probe knows what it is missing.

        The evidence is what the probe *did* manage to look at; the reason is
        what it is missing.  Both belong on the row.
        """
        return Verdict(VerdictKind.BLOCKED, (str(reason),), tuple(evidence), False)

    @staticmethod
    def unverified(
        reason: str, evidence: Iterable[EvidenceRef] = ()
    ) -> "Verdict":
        """Build an ``UNVERIFIED`` verdict: nobody established this.

        The evidence is whatever the probe held before concluding that nobody
        had established the condition -- an UNVERIFIED verdict that cites
        nothing is indistinguishable from one that never ran.
        """
        return Verdict(VerdictKind.UNVERIFIED, (str(reason),), tuple(evidence), False)

    @property
    def is_pass(self) -> bool:
        """``True`` only for a ``PASS`` verdict."""
        return self.kind == VerdictKind.PASS

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the verdict."""
        return {
            "kind": self.kind,
            "reasons": list(self.reasons),
            "evidence": [ref.to_dict() for ref in self.evidence],
            "observed": self.observed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Verdict":
        """Rebuild a verdict from :meth:`to_dict` output."""
        return cls(
            kind=str(data["kind"]),
            reasons=tuple(str(reason) for reason in data.get("reasons", ())),
            evidence=tuple(EvidenceRef.from_dict(ref) for ref in data.get("evidence", ())),
            observed=bool(data.get("observed", False)),
        )


def assert_verdict_wellformed(verdict: Verdict) -> None:
    """Re-check a verdict against the axis-A invariants.

    Called both before a bundle is written and after one is loaded, so a
    verdict that reached disk through some other path is caught rather than
    trusted.  The invariants are the ones :class:`Verdict` enforces at
    construction; re-running them is the double check, not a second rule set.
    """
    if not isinstance(verdict, Verdict):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "verdict must be a Verdict instance",
            type=type(verdict).__name__,
        )
    Verdict(
        kind=verdict.kind,
        reasons=verdict.reasons,
        evidence=verdict.evidence,
        observed=verdict.observed,
    )

# --- axis B: the four fields ---------------------------------------------

#: The **one** four-field vocabulary the gate speaks.
#:
#: Four modules emit these columns and they used to disagree with each other:
#: ``runner.py`` and ``e2e.py`` said ``SIMULATION PASS``, ``milestones.py``
#: said ``PASS`` and added a ``not-applicable`` of its own, and ``p1.py``
#: documented only ``SIMULATION PASS`` / ``FAIL`` while it could also emit
#: ``NOT RUN``.  A reader diffing two rows could not tell whether ``PASS`` and
#: ``SIMULATION PASS`` were the same claim, and a script matching on the word
#: ``PASS`` silently matched the on-site column of one module and the Windows
#: column of another.
#:
#: These tuples are the canonical words.  :data:`WINDOWS_STATUS_ALIASES` maps
#: every local word a sibling module still emits onto one of them, and
#: :func:`canonical_windows_status` applies that mapping and refuses an unknown
#: word -- so the vocabulary is defined here, in one findable place, and every
#: other module imports it instead of re-declaring its own.
CODE_STATES: tuple[str, ...] = ("CODE COMPLETE", "PARTIAL", "NOT STARTED", "UNVERIFIED")

#: The Windows/simulation column.  ``SIMULATION PASS`` is deliberately not
#: ``PASS``: it is the claim "this held for evidence produced on this host",
#: which is a different claim from "this held on the target object".
WINDOWS_STATUSES: tuple[str, ...] = ("SIMULATION PASS", "FAIL", "NOT RUN")

#: The on-site column, ordered worst-last so a roll-up can only go down.
#: ``FAIL`` belongs here as much as anywhere else: a check that holds on-site
#: evidence and did not hold *failed on site*, and a vocabulary that cannot say
#: so would have to downgrade a real failure to ``UNVERIFIED``.  The order is
#: the verdict badness order (``UNVERIFIED`` < ``BLOCKED`` < ``FAIL``): an
#: unknown is better than a known blockage, and a known blockage is better than
#: a known failure.
ONSITE_STATUSES: tuple[str, ...] = ("PASS", "UNVERIFIED", "BLOCKED", "FAIL")

#: Local Windows-column words mapped onto :data:`WINDOWS_STATUSES`.
#:
#: ``milestones.py`` reports the host-provable half of a milestone and reaches
#: for three words of its own.  Their canonical meanings:
#:
#: * ``PASS`` -- every host-scoped pass requirement is satisfied, i.e. the
#:   host-provable half held: ``SIMULATION PASS``.
#: * ``UNVERIFIED`` -- host requirements exist and are not all satisfied, i.e.
#:   the host-provable half did *not* hold: ``FAIL``.  It is not "nothing was
#:   run"; the requirement list is what is unmet.
#: * ``not-applicable`` -- the milestone declares no host-scoped requirement at
#:   all, so nothing was run on this host for this column: ``NOT RUN``.
#:
#: The mapping is total over these keys and refuses everything else, so a new
#: local word cannot slip into a bundle unnormalised.
WINDOWS_STATUS_ALIASES: dict[str, str] = {
    "PASS": "SIMULATION PASS",
    "UNVERIFIED": "FAIL",
    "not-applicable": "NOT RUN",
}

#: The on-site column needs no alias table: every module already emits
#: ``PASS`` / ``BLOCKED`` / ``UNVERIFIED``, all three of which are in
#: :data:`ONSITE_STATUSES`.  Only ``FAIL`` was missing, and it is added above.


def canonical_windows_status(value: str) -> str:
    """Return ``value`` as one of :data:`WINDOWS_STATUSES`.

    A word that is already canonical is returned unchanged.  A documented alias
    is translated.  Anything else raises: an unmapped word reaching a bundle is
    a vocabulary defect, and silently passing it through is how the four
    vocabularies drifted apart in the first place.
    """
    if value in WINDOWS_STATUSES:
        return value
    if value in WINDOWS_STATUS_ALIASES:
        return WINDOWS_STATUS_ALIASES[value]
    raise UniPadError(
        ErrorCode.CONFIG_INVALID,
        "unknown windows_status",
        windows_status=value,
        canonical=list(WINDOWS_STATUSES),
        aliases=sorted(WINDOWS_STATUS_ALIASES),
    )


@dataclass(frozen=True)
class FourFields:
    """The four-field record of one check.

    ``verdict`` and ``evidence`` are the *inputs*; ``implementation``,
    ``windows_status`` and ``onsite_status`` are **derived read-only
    properties**.  There is no setter, so no caller -- however careless -- can
    copy one field into another:

    * ``onsite_status`` reads only ``scope == ONSITE`` references, so a hundred
      simulation references cannot lift it, and neither can a green suite;
    * ``implementation`` reads ``implementation_evidence``, which is a separate
      tuple from ``evidence``: the credentials that the code exists do not
      double as credentials that the object ran;
    * ``onsite_status == "BLOCKED"`` requires a non-empty ``blocker``, and a
      ``BLOCKED`` verdict without one is refused at construction.

    Both status columns return a value from :data:`WINDOWS_STATUSES` /
    :data:`ONSITE_STATUSES`; those are the only words a bundle row may carry,
    and :func:`canonical_windows_status` maps a sibling module's local word
    onto them.
    """

    check_id: str
    verdict: Verdict
    evidence: tuple[EvidenceRef, ...] = ()
    blocker: str | None = None
    implementation_evidence: tuple[EvidenceRef, ...] = ()
    code_state: str = "UNVERIFIED"

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "implementation_evidence", tuple(self.implementation_evidence))
        if self.code_state not in CODE_STATES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown implementation state",
                code_state=self.code_state,
                known_states=list(CODE_STATES),
            )
        if self.verdict.kind == VerdictKind.BLOCKED and not (self.blocker or "").strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a BLOCKED verdict must name what blocks it",
                check_id=self.check_id,
                reasons=list(self.verdict.reasons),
            )

    @property
    def implementation(self) -> str:
        """Implementation state; ``CODE COMPLETE`` without evidence is ``UNVERIFIED``."""
        if self.code_state == "CODE COMPLETE" and not self.implementation_evidence:
            return "UNVERIFIED"
        return self.code_state

    @property
    def windows_status(self) -> str:
        """Windows/simulation state, read off the simulation-scoped evidence only.

        Always one of :data:`WINDOWS_STATUSES`.  ``SIMULATION PASS`` rather than
        ``PASS`` because the claim is about evidence this host produced.
        """
        refs = [
            ref
            for ref in self.evidence
            if ref.scope in (EvidenceScope.SIMULATION, EvidenceScope.HOST_REAL)
        ]
        if not refs:
            return "NOT RUN"
        return "SIMULATION PASS" if self.verdict.kind == VerdictKind.PASS else "FAIL"

    @property
    def onsite_status(self) -> str:
        """On-site state, read off the ``ONSITE`` evidence only (never assigned).

        Always one of :data:`ONSITE_STATUSES`.  ``FAIL`` is reachable and
        correct: holding on-site evidence that did not hold is a failure that
        happened on site, and calling it ``UNVERIFIED`` would lose it.
        """
        refs = [ref for ref in self.evidence if ref.scope == EvidenceScope.ONSITE]
        if refs:
            return "PASS" if self.verdict.kind == VerdictKind.PASS else "FAIL"
        if (self.blocker or "").strip():
            return "BLOCKED"
        return "UNVERIFIED"

    def to_dict(self) -> dict[str, Any]:
        """Return the four-field row as it appears in the bundle."""
        return {
            "check_id": self.check_id,
            "implementation": self.implementation,
            "windows_status": self.windows_status,
            "onsite_status": self.onsite_status,
            "blocker": self.blocker or "-",
        }


# --- section 5.3 measurements --------------------------------------------


class Comparator(str):
    """Closed comparison vocabulary for a measured target."""

    AT_MOST = "<="
    AT_LEAST = ">="
    EQUAL = "=="


_COMPARATORS: tuple[str, ...] = (Comparator.AT_MOST, Comparator.AT_LEAST, Comparator.EQUAL)


@dataclass(frozen=True)
class Measurement:
    """One PRD 5.3 metric.

    PRD 187 states these numbers are **initial product targets, not measured
    capability**, so ``measured`` is derived from the data and must agree with
    what the caller declares: a row that claims to be measured with no value
    and no samples is refused.  An unmeasured metric renders as ``not
    measured`` with ``value: null`` -- never as a met target.
    """

    metric_id: str
    prd_citation: str
    value: float | None
    unit: str
    target: float | None
    comparator: str
    samples: int
    measured: bool
    scope: str = EvidenceScope.SIMULATION
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "measured", bool(self.measured))
        object.__setattr__(self, "samples", int(self.samples))
        if self.comparator not in _COMPARATORS:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown comparator",
                comparator=self.comparator,
                known_comparators=list(_COMPARATORS),
            )
        scope_at_least(self.scope, EvidenceScope.SIMULATION)
        if self.measured != (self.value is not None and self.samples >= 1):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "measured must equal (value is not None and samples >= 1)",
                metric_id=self.metric_id,
                value=self.value,
                samples=self.samples,
                measured=self.measured,
            )

    def outcome(self) -> str:
        """Return ``"met"``, ``"missed"`` or ``"not_measured"`` (PRD 5.3)."""
        if not self.measured:
            return "not_measured"
        if self.target is None:
            return "not_measured"
        value = float(self.value)  # type: ignore[arg-type]  # measured implies a value
        target = float(self.target)
        if self.comparator == Comparator.AT_MOST:
            return "met" if value <= target else "missed"
        if self.comparator == Comparator.AT_LEAST:
            return "met" if value >= target else "missed"
        return "met" if value == target else "missed"

    def reason(self) -> str:
        """Return the machine-readable reason slug, or ``""`` when the target holds."""
        outcome = self.outcome()
        if outcome == "not_measured":
            return f"metric_not_measured:{self.metric_id}"
        if outcome == "missed":
            return f"metric_target_missed:{self.metric_id}"
        return ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view; an unmeasured value stays ``null``."""
        return {
            "metric_id": self.metric_id,
            "prd_citation": self.prd_citation,
            "value": self.value,
            "unit": self.unit,
            "initial_target": self.target,
            "comparator": self.comparator,
            "samples": self.samples,
            "measured": self.measured,
            "scope": self.scope,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Measurement":
        """Rebuild a measurement from :meth:`to_dict` output."""
        value = data.get("value")
        return cls(
            metric_id=str(data["metric_id"]),
            prd_citation=str(data["prd_citation"]),
            value=None if value is None else float(value),
            unit=str(data["unit"]),
            target=data.get("initial_target"),
            comparator=str(data["comparator"]),
            samples=int(data.get("samples", 0)),
            measured=bool(data.get("measured", False)),
            scope=str(data.get("scope", EvidenceScope.SIMULATION)),
            notes=str(data.get("notes", "")),
        )
