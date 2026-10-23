"""PRD 3.2 P1 acceptance checks -- one check per bullet, verbatim.

Authority
---------
``docs/PRD.md`` section 3.2 lists six P1 bullets at lines 86-91.  This module
implements **one check per bullet**, using the PRD's own words as the
requirement text, and never the implementation plan's conclusions about them.
That separation is the point: ``docs/implementation-plan.md`` already asserts
that all six bullets are CODE COMPLETE, and a gate that transcribed that table
could not notice when the table is wrong.  Every check here opens the real tree,
runs whatever can run on this host, and reports what it actually found.

The six bullets (``docs/PRD.md:86-91``)::

    86  - 通过 VirtIO Net 等路线联网，优先出站网络和自控 HTTPS 服务。
    87  - 更多自有或获授权的 IPA 兼容性测试。
    88  - 应用卸载、更新、安装历史和清晰的错误分类。
    89  - 旋转、可选分辨率、音频和安装包体验。
    90  - 真正经过验证的快照/恢复；必须说明保存的是磁盘还是整机状态。
    91  - 简洁的 Windows 启动器：开始、停止、导入 IPA、查看日志、恢复。

Two axes, never merged (project CLAUDE.md, "代码阶段与现场阶段分离")
--------------------------------------------------------------------
Each check answers two independent questions and keeps them apart:

* **verdict** -- do the fixed conditions hold for the evidence this check
  actually produced?  ``PASS`` / ``FAIL`` / ``BLOCKED`` / ``UNVERIFIED``.
* **four fields** -- where was that evidence produced?
  ``implementation`` / ``windows_status`` / ``onsite_status`` / ``blocker``.

A check may therefore be ``PASS`` on the condition axis while its
``onsite_status`` is ``BLOCKED``: that is the structured form of
"SIMULATION PASS is not 现场 PASS", not a disclaimer in prose.

Why ``onsite`` evidence is unconstructible here
-----------------------------------------------
:class:`Evidence` accepts only ``simulation`` and ``host_real`` and refuses
``onsite`` at construction.  The reason is not squeamishness: on this host there
is no iPadOS guest, so nothing any of these six checks can do could produce an
observation only the target guest could produce.  Refusing the scope in the
constructor means no caller can hand-write it, and ``onsite_status`` therefore
can never read ``PASS`` from this module -- the same "PASS is not constructible
without evidence" rule the acceptance-gate design applies to verdicts.

``PASS`` is not constructible without evidence either
-----------------------------------------------------
:func:`_make_verdict` refuses a ``PASS`` that carries no evidence or no
observation, and a ``FAIL`` that carries no reason.  A check that could not run
has to say ``BLOCKED`` or ``UNVERIFIED`` and name what is missing.

Redaction (PRD 9.2, ``docs/PRD.md:397``)
----------------------------------------
Every string that reaches a reason, a finding or an observation detail goes
through :func:`redact`, which strips Windows drive paths, UNC paths and POSIX
home paths.  Evidence paths are repo-relative POSIX by construction.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from unipad.errors import ErrorCode, UniPadError

from . import record

__all__ = [
    "PASS",
    "FAIL",
    "BLOCKED",
    "UNVERIFIED",
    "VERDICT_KINDS",
    "SCOPES",
    "Bullet",
    "P1_BULLETS",
    "Evidence",
    "Observation",
    "P1Check",
    "redact",
    "run_checks",
    "summarise",
    "to_dict",
    "render_text",
    "LAUNCHER_INVOCATIONS",
    "check_p1_1_networking",
    "check_p1_2_compatibility",
    "check_p1_3_lifecycle",
    "check_p1_4_display_audio_package",
    "check_p1_5_snapshot_restore",
    "check_p1_6_launcher",
]


# ---------------------------------------------------------------------------
# Verdict vocabulary (DESIGN.md section 4; the same four words, no new ones)
# ---------------------------------------------------------------------------

#: The four condition-axis values are :class:`unipad.acceptance.record.
#: VerdictKind`'s, and the badness order and the roll-up are record's too (F8).
#: This module used to spell all three out again -- and its private badness
#: order happened to agree, which is not the same as being guaranteed to.
PASS = record.VerdictKind.PASS
FAIL = record.VerdictKind.FAIL
BLOCKED = record.VerdictKind.BLOCKED
UNVERIFIED = record.VerdictKind.UNVERIFIED

#: Badness order, used for aggregation: ``FAIL`` dominates because a known
#: failure is more information than an unknown blockage.
_BADNESS = record.SEVERITY

VERDICT_KINDS: tuple[str, ...] = record.VERDICT_KINDS

#: Evidence scopes this module may construct.  ``onsite`` is deliberately
#: absent; see the module docstring.
SCOPES: tuple[str, ...] = (
    record.EvidenceScope.SIMULATION,
    record.EvidenceScope.HOST_REAL,
)


# ---------------------------------------------------------------------------
# Repository root
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return the repository root, located from this file rather than cwd."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "docs" / "PRD.md").is_file():
            return candidate
    # Fall back to the conventional layout: src/unipad/acceptance/p1.py.
    return here.parents[3]


def _rel(path: Path | str, root: Path) -> str:
    """Return ``path`` relative to ``root`` as POSIX, never absolute."""
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        # A path outside the tree (a temporary directory) is reported by name
        # only: an absolute path is exactly what PRD 9.2 forbids.
        return candidate.name


# ---------------------------------------------------------------------------
# Redaction (PRD 9.2: no tokens, no private keys, no absolute user paths)
# ---------------------------------------------------------------------------

#: A drive-letter absolute path.  The lookbehind is what keeps ``https://`` and
#: ``10.0.2.2:8443`` from being read as a drive letter: the letter must not be
#: part of a longer word.
_DRIVE_PATH_RE = re.compile(r"(?<![\w.])[A-Za-z]:[\\/][^\s'\"<>|]*")
_UNC_PATH_RE = re.compile(r"\\\\[^\s'\"<>|]*")
_POSIX_HOME_RE = re.compile(r"/(?:home|Users|root)/[^\s'\"<>|]*")
_TOKENISH_RE = re.compile(r"\b(?:ghp_[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._\-]{8,})\b")
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]+-----")


def redact(text: Any) -> str:
    """Return ``text`` with absolute user paths and credential-shaped runs removed.

    Applied to every reason, finding and observation detail this module
    produces, so a leaked absolute path cannot reach a report by accident.
    """
    rendered = str(text)
    rendered = _PEM_RE.sub("<redacted:key>", rendered)
    rendered = _TOKENISH_RE.sub("<redacted:credential>", rendered)
    rendered = _DRIVE_PATH_RE.sub("<path>", rendered)
    rendered = _UNC_PATH_RE.sub("<path>", rendered)
    rendered = _POSIX_HOME_RE.sub("<path>", rendered)
    return rendered


# ---------------------------------------------------------------------------
# Verdict construction guard (DESIGN.md section 3.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Verdict:
    """A verdict kind together with the machine-readable slugs behind it.

    ``_make_verdict`` used to return the bare kind string and drop the reasons
    it had just validated, so a FAIL could only ever be read back as prose.
    Keeping the two together is what makes F7 checkable: :attr:`P1Check.
    verdict_reasons` is derived from this, and a non-PASS check cannot be
    built without one.
    """

    kind: str
    reasons: tuple[str, ...]


def _make_verdict(
    kind: str,
    *,
    reasons: Sequence[str] = (),
    evidence: Sequence["Evidence"] = (),
    observed: bool = False,
) -> _Verdict:
    """Return the verdict after refusing one that cannot be justified.

    The returned slugs are the caller's ``reasons`` verbatim: a FAIL's slugs are
    what a reader or a machine will match on, so they must survive to
    :attr:`P1Check.verdict_reasons` rather than being paraphrased into the
    operator-facing ``reasons``.

    Raises:
        UniPadError: ``config_invalid`` for an unknown kind, a ``PASS`` with no
            evidence or no observation, or a non-PASS verdict with no reason.
            These are usage errors in the check itself, never a silent
            downgrade.
    """
    slugs = tuple(reasons)
    if kind not in VERDICT_KINDS:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown verdict kind",
            kind=kind,
            known_kinds=list(VERDICT_KINDS),
        )
    if kind == PASS and not evidence:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a PASS verdict requires at least one evidence reference",
            reasons=[redact(r) for r in slugs],
        )
    if kind == PASS and not observed:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a PASS verdict requires an observation",
            reasons=[redact(r) for r in slugs],
        )
    if kind != PASS and not slugs:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "a non-PASS verdict requires a reason slug",
            kind=kind,
        )
    return _Verdict(kind, slugs)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One artefact a check actually looked at or produced.

    ``path`` is repo-relative POSIX.  ``scope`` may only be ``simulation`` or
    ``host_real``; ``onsite`` is refused because nothing on this host can
    produce it (module docstring).

    This is deliberately *not* :class:`unipad.acceptance.record.EvidenceRef`.
    The two answer different questions: an ``EvidenceRef`` is a claim a bundle
    can be re-checked against, so it demands a digest and a closed kind set,
    while an :class:`Evidence` is a note about what a check opened -- its
    ``kind`` is free text ("config", "script", "doc", "source") and it carries
    no digest because nothing here is re-hashed later.  Forcing the stricter
    class would mean inventing digests, which is the fabrication the gate exists
    to prevent.

    What *is* shared is the vocabulary: the scope constants and the verdict
    kinds come from :mod:`unipad.acceptance.record` (F8), and the path rule is
    the same one ``relativise`` applies.
    """

    path: str
    kind: str
    scope: str = record.EvidenceScope.HOST_REAL
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", record.relativise(self.path))
        if self.scope not in SCOPES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "an evidence scope outside the producible set was requested",
                scope=self.scope,
                producible=list(SCOPES),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "scope": self.scope,
            "note": redact(self.note),
        }


@dataclass(frozen=True)
class Observation:
    """One thing a check saw, with whether it satisfied the condition."""

    label: str
    detail: str
    ok: Optional[bool] = None  # True satisfied, False not satisfied, None informational

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "detail": redact(self.detail),
            "ok": self.ok,
        }


@dataclass(frozen=True)
class P1Check:
    """One P1 bullet, evaluated.

    ``verdict`` is axis A (do the fixed conditions hold for this evidence).
    ``implementation`` / ``windows_status`` / ``onsite_status`` / ``blocker`` are
    axis B (where that evidence came from).  ``windows_status`` and
    ``onsite_status`` are derived properties, not writable fields, so no caller
    can promote one without the other.
    """

    bullet: str
    prd_citation: str
    requirement: str
    verdict: str
    reasons: tuple[str, ...]
    observations: tuple[Observation, ...]
    evidence: tuple[Evidence, ...]
    findings: tuple[str, ...] = ()
    implementation: str = "UNVERIFIED"
    blocker: str = ""
    probe: str = ""
    #: The machine-readable reason slugs behind :attr:`verdict`, kept separately
    #: from the operator-facing :attr:`reasons`.  ``_make_verdict`` validated
    #: them and then dropped them, so a FAIL could only ever be read as prose:
    #: an operator (or a script diffing two runs) had no stable token to act on.
    #: Every non-PASS construction site fills this in.
    verdict_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict_reasons", tuple(self.verdict_reasons))
        if self.verdict not in VERDICT_KINDS:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown verdict kind",
                kind=self.verdict,
                known_kinds=list(VERDICT_KINDS),
            )
        if self.verdict == PASS and not self.evidence:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a PASS check requires evidence",
                bullet=self.bullet,
            )
        if self.verdict == FAIL and not self.reasons:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a FAIL check requires a reason",
                bullet=self.bullet,
            )
        # F7: a non-PASS verdict must carry at least one machine-readable slug,
        # so "it failed" is never the whole answer.  A PASS needs none.
        if self.verdict != PASS and not self.verdict_reasons:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a non-PASS check requires a machine-readable reason slug",
                bullet=self.bullet,
                verdict=self.verdict,
            )
        for item in self.evidence:
            if not isinstance(item, Evidence):
                raise UniPadError(
                    ErrorCode.CONFIG_INVALID,
                    "check evidence must be Evidence instances",
                    bullet=self.bullet,
                    received_type=type(item).__name__,
                )

    # -- axis B ----------------------------------------------------------

    @property
    def windows_status(self) -> str:
        """One of the canonical :data:`~unipad.acceptance.record.WINDOWS_STATUSES`.

        ``SIMULATION PASS`` / ``FAIL`` / ``NOT RUN``.  Reads only the evidence
        this check produced on this host; it cannot see anything else, so a
        green suite elsewhere cannot raise it.  ``NOT RUN`` is reachable -- a
        check that produced no evidence at all reads ``NOT RUN`` -- which is why
        the vocabulary is imported rather than spelled out here as two words
        (F9).
        """
        if not self.observations and not self.evidence:
            return "NOT RUN"
        if self.verdict == FAIL:
            return "FAIL"
        return "SIMULATION PASS"

    @property
    def onsite_status(self) -> str:
        """``BLOCKED`` or ``UNVERIFIED`` -- never ``PASS`` from this module.

        No check here can construct an ``onsite`` :class:`Evidence`, so the
        guest-side claim is either known to be blocked (a named blocker) or
        unknown.  It is never a pass.
        """
        if self.blocker:
            return "BLOCKED"
        return "UNVERIFIED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bullet": self.bullet,
            "prd_citation": self.prd_citation,
            "requirement": self.requirement,
            "probe": self.probe,
            "verdict": self.verdict,
            "verdict_reasons": [redact(r) for r in self.verdict_reasons],
            "reasons": [redact(r) for r in self.reasons],
            "observations": [o.to_dict() for o in self.observations],
            "evidence": [e.to_dict() for e in self.evidence],
            "findings": [redact(f) for f in self.findings],
            "fields": {
                "implementation": self.implementation,
                "windows_status": self.windows_status,
                "onsite_status": self.onsite_status,
                "blocker": redact(self.blocker) if self.blocker else "-",
            },
        }


@dataclass(frozen=True)
class Bullet:
    """One PRD 3.2 P1 bullet, quoted verbatim."""

    bullet: str
    prd_citation: str
    requirement: str
    probe: str


#: The six bullets, in PRD order, with the PRD's own sentence as the text.
P1_BULLETS: tuple[Bullet, ...] = (
    Bullet(
        bullet="P1-1",
        prd_citation="docs/PRD.md:86",
        requirement="通过 VirtIO Net 等路线联网，优先出站网络和自控 HTTPS 服务。",
        probe="check_p1_1_networking",
    ),
    Bullet(
        bullet="P1-2",
        prd_citation="docs/PRD.md:87",
        requirement="更多自有或获授权的 IPA 兼容性测试。",
        probe="check_p1_2_compatibility",
    ),
    Bullet(
        bullet="P1-3",
        prd_citation="docs/PRD.md:88",
        requirement="应用卸载、更新、安装历史和清晰的错误分类。",
        probe="check_p1_3_lifecycle",
    ),
    Bullet(
        bullet="P1-4",
        prd_citation="docs/PRD.md:89",
        requirement="旋转、可选分辨率、音频和安装包体验。",
        probe="check_p1_4_display_audio_package",
    ),
    Bullet(
        bullet="P1-5",
        prd_citation="docs/PRD.md:90",
        requirement="真正经过验证的快照/恢复；必须说明保存的是磁盘还是整机状态。",
        probe="check_p1_5_snapshot_restore",
    ),
    Bullet(
        bullet="P1-6",
        prd_citation="docs/PRD.md:91",
        requirement="简洁的 Windows 启动器：开始、停止、导入 IPA、查看日志、恢复。",
        probe="check_p1_6_launcher",
    ),
)


# ---------------------------------------------------------------------------
# Shared inspection helpers
# ---------------------------------------------------------------------------


def _import(dotted: str) -> tuple[Optional[Any], str]:
    """Import ``dotted`` lazily, returning ``(module, error_text)``.

    A module that cannot be imported is reported, never assumed present: the
    sibling ``unipad`` packages are being edited concurrently.
    """
    try:
        module = __import__(dotted, fromlist=["__name__"])
    except Exception as exc:  # pragma: no cover - depends on the working tree
        return None, f"{type(exc).__name__}: {exc}"
    return module, ""


def _module_path(root: Path, dotted: str) -> Path:
    """Return the source path of ``dotted`` inside the tree."""
    return root / "src" / Path(*dotted.split(".")[:-1]) / (dotted.split(".")[-1] + ".py")


def _source_text(root: Path, dotted: str) -> tuple[str, str]:
    """Return ``(text, repo_relative_posix_path)`` for a module's source."""
    path = _module_path(root, dotted)
    try:
        return path.read_text(encoding="utf-8"), _rel(path, root)
    except OSError as exc:
        return "", f"<unreadable: {type(exc).__name__}>"


#: Top-level directories that are not the product tree: ``local-private`` is
#: gitignored scratch and dot-directories are tool state.  A gate that counted
#: another agent's scratch as product evidence would be reporting the wrong tree.
#: ``local-private`` is pruned during the walk as well as filtered afterwards,
#: because on this host it holds tens of gigabytes of unrelated scratch and
#: descending into it costs seconds per scan.
_NON_PRODUCT_TOP = frozenset({"local-private"})


def _is_product_path(path: Path, root: Path) -> bool:
    """True when ``path`` is part of the product tree rather than scratch."""
    try:
        relative = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    for part in relative.parts:
        if part.startswith(".") or part in _NON_PRODUCT_TOP:
            return False
    return True


def _product_files(root: Path, pattern: str) -> list[Path]:
    """Return the product-tree files matching ``pattern``, in sorted order.

    ``os.walk`` with pruning rather than ``Path.rglob``: ``rglob`` would descend
    into ``local-private/`` (tens of gigabytes on this host) and into ``.git``,
    turning a two-second check into a twelve-second one for no information.
    """
    from fnmatch import fnmatch

    matches: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".") and name not in _NON_PRODUCT_TOP
        ]
        for name in filenames:
            if not fnmatch(name, pattern):
                continue
            candidate = Path(current) / name
            if _is_product_path(candidate, root):
                matches.append(candidate)
    return sorted(matches)


#: The ``实现状态`` row of an ADR's four-field table.  The row is the ADR's own
#: claim about how far its code got, which is exactly the thing a gate must
#: compare against the tree rather than repeat.  The state is matched greedily
#: up to the *last* pipe on the line: a markdown cell may itself contain a pipe
#: (``--transport channel|https`` does), and a non-greedy match would silently
#: find no row at all -- which would read as "the record agrees" and be the
#: opposite of the truth.
_ADR_STATE_ROW = re.compile(r"^\|\s*实现状态\s*\|(?P<state>.*)\|\s*$", re.MULTILINE)

#: Wordings that mean "the code is not written yet".  Compared against the tree
#: rather than trusted, so a stale ADR row is reported instead of repeated.
_ADR_UNWRITTEN_MARKERS: tuple[str, ...] = ("代码未写", "未写", "NOT STARTED", "设计已定")


def _adr_implementation_state(text: str) -> str:
    """Return an ADR's recorded 实现状态, or ``""`` when it has no such row."""
    match = _ADR_STATE_ROW.search(text)
    return match.group("state").strip() if match else ""


def _cli_subcommands(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(subcommand names, names wired to a handler)`` from the CLI source.

    Read out of the parser table rather than from a run, because the question is
    what the product exposes, and a parser entry with no handler is exactly the
    kind of half-wired surface a gate should notice.
    """
    text, _ = _source_text(root, "unipad.controller.cli")
    declared = tuple(re.findall(r"sub\.add_parser\(\s*[\"']([a-z0-9_.-]+)[\"']", text))
    wired = tuple(re.findall(r"set_defaults\(\s*handler\s*=\s*cmd_([a-z0-9_]+)", text))
    return declared, wired


def _machine_config(root: Path) -> tuple[dict[str, Any], str]:
    """Return ``(config/machine.json parsed, repo-relative path)``."""
    path = root / "config" / "machine.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")), _rel(path, root)
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"<unreadable: {type(exc).__name__}>"


def _tmpdir(prefix: str) -> Path:
    """Return a private temporary directory for a probe that must write."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def _cleanup(path: Optional[Path]) -> None:
    """Remove a probe directory, ignoring a failure to do so."""
    if path is None:
        return
    shutil.rmtree(path, ignore_errors=True)


def _error_code(exc: BaseException) -> str:
    """Return the stable code of a :class:`UniPadError`, else the type name."""
    code = getattr(exc, "code", None)
    return str(code) if code else type(exc).__name__


def _count(values: Iterable[Any]) -> dict[str, int]:
    """Return a ``value -> count`` mapping with string keys."""
    return {str(key): int(count) for key, count in Counter(values).items()}


# ---------------------------------------------------------------------------
# P1 (1) -- docs/PRD.md:86
# ---------------------------------------------------------------------------


def check_p1_1_networking(root: Path) -> P1Check:
    """通过 VirtIO Net 等路线联网，优先出站网络和自控 HTTPS 服务。 (PRD:86)

    What is host-runnable, and is run here:

    * the real ``QemuLauncher.build_args`` is called with the network enabled and
      the resulting ``-nic`` fragment is inspected;
    * the real :class:`unipad.host.httpsvc.PackageServer` is started on loopback
      with a freshly generated self-signed certificate and fetched over TLS with
      :mod:`urllib` -- a real handshake, not a mocked one.

    What is not, and is reported as blocked: whether a guest can actually reach
    ``10.0.2.2`` under ``restrict=on`` is QEMU/slirp behaviour that only a real
    boot can answer.

    Known gap, encoded honestly
    ---------------------------
    ``PackageServer`` is a complete self-controlled HTTPS service with **no
    production caller**.  The scan below walks every module under ``src/`` and
    reports exactly which ones reference it.  If that is still only
    ``host/httpsvc.py`` itself, the bullet's second half is code that nothing
    starts, and the check says so rather than crediting a service nobody serves.
    """
    bullet = P1_BULLETS[0]
    observations: list[Observation] = []
    evidence: list[Evidence] = []
    findings: list[str] = []

    # -- the VirtIO Net route -------------------------------------------
    net_route_ok = False
    qemu, qemu_error = _import("unipad.host.qemu")
    _, qemu_rel = _source_text(root, "unipad.host.qemu")
    if qemu is None:
        observations.append(
            Observation(
                "virtio_net_route", f"qemu module unavailable: {qemu_error}", False
            )
        )
        findings.append(
            "unipad.host.qemu could not be imported, so the network route could "
            "not be exercised."
        )
    else:
        from unipad.models import MachineConfig

        config, config_rel = _machine_config(root)
        net = dict((config.get("devices") or {}).get("net") or {})
        net_enabled = bool(net.get("enabled", False))
        net_mode = net.get("mode")
        evidence.append(Evidence(qemu_rel, "source", "host_real", "QEMU argv builder"))
        evidence.append(Evidence(config_rel, "config", "host_real", "shipped machine configuration"))

        probe = _tmpdir("unipad-p1-net-")
        try:
            for name in ("fw.fd", "sys.img", "data.img"):
                (probe / name).write_bytes(b"\0" * 2048)
            (probe / "wd").mkdir()
            # The shipped configuration disables the network for the MVP boot
            # gates; enabling it is what P1 (1) asks about, so the probe turns
            # it on and adds the ``mode`` the builder requires.
            enabled_net = {
                **net,
                "enabled": True,
                "mode": "user",
                "host_forwards": [{"host_port": 9443, "guest_port": 8443}],
            }
            machine = MachineConfig.from_dict(
                {**config, "devices": {**(config.get("devices") or {}), "net": enabled_net}}
            )
            argv = qemu.QemuLauncher(machine).build_args(
                probe / "fw.fd", probe / "sys.img", probe / "data.img", probe / "wd", 4444
            )
            line = " ".join(argv)
            nic = next((a for a in argv if a.startswith("user,id=unipad-net,")), "")
            has_model = "model=virtio-net" in nic
            has_restrict = "restrict=on" in nic
            has_forward = "hostfwd=tcp:127.0.0.1:9443-10.0.2.15:8443" in line
            net_route_ok = bool(nic) and has_model and has_restrict and has_forward
            observations.append(
                Observation(
                    "virtio_net_route",
                    "build_args with the network enabled emitted "
                    f"-nic {nic or '<absent>'}"
                    f" (model=virtio-net: {has_model}, restrict=on: {has_restrict}, "
                    f"hostfwd rule: {has_forward})",
                    net_route_ok,
                )
            )
            observations.append(
                Observation(
                    "outbound_default",
                    "restrict defaults to on, so the default route is loopback-only; "
                    "public egress requires restrict=off and explicit authorisation",
                    True,
                )
            )
        except Exception as exc:
            observations.append(
                Observation(
                    "virtio_net_route",
                    f"build_args raised {_error_code(exc)}: {exc}",
                    False,
                )
            )
            findings.append(
                "The QEMU argv could not be built with the network enabled, so the "
                "VirtIO Net route is unproven on this host."
            )
        finally:
            _cleanup(probe)

        if not net_enabled:
            observations.append(
                Observation(
                    "shipped_config_network",
                    "config/machine.json has devices.net.enabled = false "
                    "(the note says 'disabled for MVP boot gates')",
                    False,
                )
            )
            findings.append(
                "The shipped configuration leaves the guest network off, so no run "
                "started from config/machine.json has any network device."
            )
        if not net_mode:
            observations.append(
                Observation(
                    "shipped_config_mode",
                    "config/machine.json devices.net has no 'mode', and the builder "
                    "refuses to enable the network without one (config_invalid)",
                    False,
                )
            )
            findings.append(
                "Flipping devices.net.enabled to true in the shipped configuration "
                "fails until 'mode' is added: the enabling edit is not a one-key change."
            )

    # -- the self-controlled HTTPS service -------------------------------
    https_service_ok = False
    httpsvc, httpsvc_error = _import("unipad.host.httpsvc")
    _, httpsvc_rel = _source_text(root, "unipad.host.httpsvc")
    if httpsvc is None:
        observations.append(
            Observation("https_service", f"httpsvc module unavailable: {httpsvc_error}", False)
        )
        findings.append("unipad.host.httpsvc could not be imported.")
    else:
        evidence.append(
            Evidence(
                httpsvc_rel, "source", "host_real", "self-controlled HTTPS package service"
            )
        )
        guest_url = httpsvc.guest_fetch_url()
        forward_spec = httpsvc.hostfwd_spec(9443, 8443)
        probe = _tmpdir("unipad-p1-https-")
        try:
            packages = probe / "packages"
            packages.mkdir()
            certs = probe / "certs"
            certs.mkdir()
            payload = packages / "unipad-p1-probe.ipa"
            payload.write_bytes(b"PK\x03\x04" + b"probe" * 16)
            served_port = 0
            with httpsvc.PackageServer(packages, cert_directory=certs, port=0) as server:
                served_port = int(server.port)
                index_url = f"https://127.0.0.1:{served_port}/index.json"
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                try:
                    with urllib.request.urlopen(index_url, timeout=15, context=context) as reply:
                        body = json.loads(reply.read().decode("utf-8"))
                        status = int(reply.status)
                    served = body.get("packages") or []
                    https_service_ok = status == 200 and len(served) == 1
                    observations.append(
                        Observation(
                            "https_service",
                            "PackageServer served a real TLS response on loopback "
                            f"(status {status}); the index lists {len(served)} package(s) "
                            f"({served[0].get('name') if served else 'none'}, "
                            f"{served[0].get('size') if served else 0} bytes)",
                            https_service_ok,
                        )
                    )
                except Exception as exc:
                    observations.append(
                        Observation(
                            "https_service",
                            f"the TLS fetch failed: {type(exc).__name__}: {exc}",
                            False,
                        )
                    )
                    findings.append(
                        "The self-controlled HTTPS package service did not answer a "
                        "real TLS request on this host."
                    )
            # Outside the context manager: the port must be free again.  Probing
            # it by binding is stronger than asking the object, because the
            # object's own flag is not the thing a second caller would trip on.
            released = False
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as prober:
                    prober.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    prober.bind(("127.0.0.1", served_port))
                    released = True
            except OSError:
                released = False
            observations.append(
                Observation(
                    "https_released",
                    f"after the context manager exits, port {served_port} can be bound again: "
                    f"{released}",
                    released,
                )
            )
            if not released:
                findings.append(
                    "The package service did not release its port on exit, so a second "
                    "import in the same process would find the address in use."
                )
            observations.append(
                Observation(
                    "guest_fetch_url",
                    f"the guest-side URL is {guest_url} (QEMU user-mode gateway, no hostfwd); "
                    f"hostfwd_spec(9443, 8443) = {forward_spec}",
                    True,
                )
            )
        finally:
            _cleanup(probe)

    # -- the known gap: is there a production caller? ---------------------
    # The question is not "who mentions httpsvc" -- ``host/__init__.py``
    # re-exports the name and ``host/qemu.py`` uses ``hostfwd_spec`` -- it is
    # "who constructs and starts PackageServer".  Only a construction site is a
    # caller; a re-export is not, and counting one would hide the gap.
    constructions: list[str] = []
    reexports: list[str] = []
    test_constructions: list[str] = []
    src_root = root / "src"
    tests_root = root / "tests"
    for base, sink in ((src_root, constructions), (tests_root, test_constructions)):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if not _is_product_path(path, root):
                continue
            rel = _rel(path, root)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            # The module itself defines the class; a probe that starts it to
            # measure it is not the production caller either.
            if "PackageServer(" not in text:
                if "PackageServer" in text and base is src_root:
                    reexports.append(rel)
                continue
            if rel == "src/unipad/host/httpsvc.py" or rel.startswith("src/unipad/acceptance/"):
                continue
            sink.append(rel)
    observations.append(
        Observation(
            "production_caller",
            "modules under src/ that construct and start PackageServer, excluding the "
            "module itself and this acceptance probe: "
            + (", ".join(constructions) if constructions else "none"),
            bool(constructions),
        )
    )
    observations.append(
        Observation(
            "reexports_and_tests",
            f"src/ modules that only name PackageServer without constructing it: "
            f"{reexports if reexports else 'none'}; test modules that construct it: "
            f"{len(test_constructions)}",
            None,
        )
    )
    protocol_fields_present = False
    protocol_text, protocol_rel = _source_text(root, "unipad.guest.protocol")
    if protocol_text:
        evidence.append(Evidence(protocol_rel, "source", "host_real", "host -> guest install request"))
        protocol_fields_present = (
            "package_url" in protocol_text and "cert_fingerprint" in protocol_text
        )
        observations.append(
            Observation(
                "install_request_fields",
                "unipad.guest.protocol.InstallRequest carries package_url/cert_fingerprint: "
                f"{protocol_fields_present}",
                protocol_fields_present,
            )
        )
    transfer_text, transfer_rel = _source_text(root, "unipad.ipa.transfer")
    if transfer_text:
        evidence.append(Evidence(transfer_rel, "source", "host_real", "host -> guest package transfer"))
        # Both halves are read out of the file rather than asserted about it:
        # ``transfer`` owns the channel boundary (a Protocol plus
        # ``send_package``) and owns no https route of its own.  This
        # observation used to hardcode ``ok=False`` with prose claiming "no
        # https transport selection exists" -- a statement about the whole
        # product that this file cannot support, and which the tree now
        # contradicts (the route lives in the controller, not here).  A
        # hardcoded ``ok`` with prose asserting a fact is exactly the
        # fabrication this gate exists to catch, so the value is derived and
        # the prose is scoped to the file it was read from.
        declares_protocol = (
            re.search(r"class\s+GuestTransport\s*\(\s*Protocol", transfer_text) is not None
        )
        declares_send_package = "def send_package(" in transfer_text
        selects_https = bool(
            re.search(r"[\"']https[\"']", transfer_text)
            or re.search(r"class\s+\w*Https\w*Transport", transfer_text)
        )
        transfer_route_ok = (
            declares_protocol and declares_send_package and not selects_https
        )
        observations.append(
            Observation(
                "transfer_route",
                "unipad.ipa.transfer declares its transport boundary as the "
                f"GuestTransport Protocol (present: {declares_protocol}) with "
                f"send_package(name, data) -> TransferReceipt (present: "
                f"{declares_send_package}); it selects no https route of its own "
                f"(an https literal or an https transport class appears in it: "
                f"{selects_https})",
                transfer_route_ok,
            )
        )
        # Where an https route *does* live, so the reader is not told the product
        # has none when the route simply lives in another module.  Informational:
        # it is not a condition of this bullet, so ``ok`` is None rather than a
        # verdict dressed up as an observation.
        cli_text, cli_rel = _source_text(root, "unipad.controller.cli")
        https_selection = bool(
            re.search(r"TRANSPORT_HTTPS\s*=\s*[\"']https[\"']", cli_text)
        )
        default_transport = re.search(
            r"DEFAULT_TRANSPORT\s*=\s*TRANSPORT_([A-Z_]+)", cli_text
        )
        observations.append(
            Observation(
                "https_route_selection",
                "the https package route is selected in unipad.controller.cli "
                f"({cli_rel if cli_text else '<unreadable>'}): an https transport is "
                f"declared there ({https_selection}) and the default transport is "
                f"{default_transport.group(1).lower() if default_transport else 'unknown'}; "
                "unipad.ipa.transfer carries the bytes on whichever channel it is "
                "handed and makes no route choice itself",
                None,
            )
        )
        if cli_text:
            evidence.append(
                Evidence(
                    cli_rel, "source", "host_real", "transport selection and PackageServer caller"
                )
            )

    caller_ok = bool(constructions)
    if not caller_ok:
        findings.append(
            "KNOWN GAP (PRD:86): the self-controlled HTTPS package server has no "
            "production caller, so nothing starts it. The only construction sites outside "
            "its own module are tests and this acceptance probe. IPA bytes still travel the "
            "guest-channel message path, not the network path."
        )
    if not protocol_fields_present:
        findings.append(
            "unipad.guest.protocol.InstallRequest has no package_url or cert_fingerprint "
            "field, so a caller could not tell the guest where to fetch the package even "
            "if one existed. ADR-0008 records this decision as 'code not written'."
        )
    adr = root / "docs" / "decisions" / "ADR-0008-package-server-caller.md"
    if adr.is_file():
        evidence.append(
            Evidence(_rel(adr, root), "doc", "host_real", "ADR recording the caller decision")
        )
        # The ADR's own four-field row is *read*, and compared against the tree,
        # instead of being asserted to agree with it.  This block used to emit an
        # unconditional finding -- "records 实现状态 as 'code not written'; the tree
        # still matches that record" -- the moment the file existed.  When the
        # caller was wired in (cli.py constructs and starts PackageServer inside
        # _TransferWindow.start()) that sentence became false and contradicted the
        # production_caller observation two lines above it in the same report.  A
        # finding that asserts a fact without checking it is the same fabrication
        # the gate exists to catch, so the row is parsed and the disagreement is
        # what gets reported.
        try:
            adr_text = adr.read_text(encoding="utf-8", errors="replace")
        except OSError:
            adr_text = ""
        recorded_state = _adr_implementation_state(adr_text)
        # Fail closed: an ADR whose 实现状态 row cannot be read is never reported
        # as agreeing with the tree.  Claiming agreement we could not check is
        # the same shape as the unconditional finding this replaced.
        if not recorded_state:
            record_matches_tree = False
        else:
            adr_says_unwritten = any(
                marker in recorded_state for marker in _ADR_UNWRITTEN_MARKERS
            )
            record_matches_tree = adr_says_unwritten == (not caller_ok)
        observations.append(
            Observation(
                "adr_record_matches_tree",
                f"docs/decisions/ADR-0008-package-server-caller.md records 实现状态 as "
                f"'{recorded_state or '<no 实现状态 row found>'}'; the tree "
                f"{'has a production caller (' + ', '.join(constructions) + ')' if caller_ok else 'has no production caller'}; "
                f"the record and the tree agree: {record_matches_tree}",
                record_matches_tree,
            )
        )
        if not record_matches_tree:
            findings.append(
                "docs/decisions/ADR-0008-package-server-caller.md records 实现状态 as "
                f"'{recorded_state or '<no 实现状态 row found>'}', but the tree "
                + (
                    "constructs and starts PackageServer in " + ", ".join(constructions)
                    if caller_ok
                    else "has no production caller at all"
                )
                + ". The decision itself is implemented; the ADR's four-field row is "
                "stale and must be corrected."
            )

    # -- verdict ----------------------------------------------------------
    blocker = (
        "no real iPadOS guest: whether the guest can reach 10.0.2.2 under restrict=on "
        "is QEMU/slirp behaviour only a real boot can answer"
    )
    if qemu is None or httpsvc is None or not net_route_ok or not https_service_ok:
        verdict = _make_verdict(
            FAIL,
            reasons=["net_route_or_self_controlled_https_service_could_not_be_exercised"],
            evidence=evidence,
            observed=True,
        )
        implementation = "PARTIAL"
        extra_reasons = (
            "the VirtIO Net route or the self-controlled HTTPS service could not be "
            "exercised on this host",
        )
    elif not caller_ok:
        verdict = _make_verdict(
            BLOCKED,
            reasons=[
                "no_production_caller_for_package_server",
                "install_request_has_no_package_url",
            ],
            evidence=evidence,
            observed=True,
        )
        implementation = "PARTIAL"
        extra_reasons = (
            "the self-controlled HTTPS package server has no production caller, so nothing "
            "starts it; IPA bytes still travel the guest-channel message path",
        )
    else:
        verdict = _make_verdict(
            BLOCKED,
            reasons=["guest_outbound_requires_a_real_ipados_guest"],
            evidence=evidence,
            observed=True,
        )
        implementation = "CODE COMPLETE"
        extra_reasons = (
            "guest outbound connectivity under restrict=on has never been observed; only a "
            "real boot can answer it",
        )

    return P1Check(
        bullet=bullet.bullet,
        prd_citation=bullet.prd_citation,
        requirement=bullet.requirement,
        probe=bullet.probe,
        verdict=verdict.kind,
        verdict_reasons=verdict.reasons,
        reasons=extra_reasons,
        observations=tuple(observations),
        evidence=tuple(evidence),
        findings=tuple(findings),
        implementation=implementation,
        blocker=blocker,
    )


# ---------------------------------------------------------------------------
# P1 (2) -- docs/PRD.md:87
# ---------------------------------------------------------------------------


def check_p1_2_compatibility(root: Path) -> P1Check:
    """更多自有或获授权的 IPA 兼容性测试。 (PRD:87)

    The matrix's gates are exercised for real against a temporary file, because
    they are host logic: an unauthorised package must be refused, a positive
    verdict must name the condition it established, and a key must not be
    nameless.

    The bullet itself is about *tests actually run*.  The scan below counts what
    the tree holds: recorded observations and archived Device IPA binaries.  Both
    numbers are reported as found, not as assumed.
    """
    bullet = P1_BULLETS[1]
    observations: list[Observation] = []
    evidence: list[Evidence] = []
    findings: list[str] = []

    matrix, matrix_error = _import("unipad.ipa.matrix")
    matrix_rel = ""
    gates_ok = False
    if matrix is None:
        observations.append(
            Observation("matrix_module", f"unipad.ipa.matrix unavailable: {matrix_error}", False)
        )
        findings.append("The compatibility matrix module could not be imported.")
    else:
        _, matrix_rel = _source_text(root, "unipad.ipa.matrix")
        evidence.append(Evidence(matrix_rel, "source", "host_real", "compatibility matrix"))
        key = matrix.MatrixKey("1.0.0", "21H461", "tcg-ramfb")
        refusals: dict[str, str] = {}
        probe = _tmpdir("unipad-p1-matrix-")
        try:
            store = matrix.CompatibilityMatrix(probe / "matrix.jsonl")
            store.record(
                matrix.MatrixEntry(
                    key=key,
                    authorisation=matrix.Authorisation.OWN_SOURCE,
                    trust_category=matrix.TrustCategory.DEVELOPMENT_SIGNED,
                    bundle_id="com.unipad.probe",
                    package_sha256="a" * 64,
                    verdict=matrix.Verdict.UNTESTED,
                )
            )
            for label, kwargs in (
                ("not_authorised", {"authorisation": matrix.Authorisation.NOT_AUTHORISED}),
                ("absent_authorisation", {"authorisation": None}),
                (
                    "absent_trust_category",
                    {
                        "authorisation": matrix.Authorisation.OWN_SOURCE,
                        "trust_category": None,
                    },
                ),
            ):
                try:
                    matrix.MatrixEntry(
                        key=key, bundle_id="com.unipad.probe", package_sha256="b" * 64, **kwargs
                    )
                    refusals[label] = "recorded"
                except Exception as exc:
                    refusals[label] = _error_code(exc)
            try:
                matrix.MatrixEntry(
                    key=key,
                    authorisation=matrix.Authorisation.OWN_SOURCE,
                    trust_category=matrix.TrustCategory.DEVELOPMENT_SIGNED,
                    bundle_id="com.unipad.probe",
                    package_sha256="c" * 64,
                    verdict=matrix.Verdict.INSTALLS_AND_RUNS,
                )
                refusals["positive_without_condition"] = "recorded"
            except Exception as exc:
                refusals["positive_without_condition"] = _error_code(exc)
            try:
                matrix.MatrixKey("1.0.0", "", "tcg-ramfb")
                refusals["blank_key_part"] = "accepted"
            except Exception as exc:
                refusals["blank_key_part"] = type(exc).__name__
            gates_ok = (
                refusals.get("not_authorised") == "blocked"
                and refusals.get("absent_authorisation") == "blocked"
                and refusals.get("absent_trust_category") == "blocked"
                and refusals.get("positive_without_condition") == "evidence_incomplete"
                and refusals.get("blank_key_part") == "ValueError"
            )
            observations.append(
                Observation(
                    "authorisation_gate",
                    "an entry is refused unless it names an authorisation and a trust "
                    f"category; refusals observed: {refusals}",
                    gates_ok,
                )
            )
            observations.append(
                Observation(
                    "closed_vocabularies",
                    "authorisations "
                    f"{[str(a) for a in matrix.AUTHORISATIONS]}; trust categories "
                    f"{[str(t) for t in matrix.TRUST_CATEGORIES]}; verdicts "
                    f"{[str(v) for v in matrix.VERDICTS]}; established conditions "
                    f"{[str(c) for c in matrix.ESTABLISHED_CONDITIONS]}",
                    True,
                )
            )
            observations.append(
                Observation(
                    "append_only",
                    f"the matrix is JSON Lines and reported {len(store)} current entr(y/ies) "
                    f"after one append; unreadable lines: {store.unreadable_lines}",
                    True,
                )
            )
        except Exception as exc:
            observations.append(
                Observation(
                    "authorisation_gate",
                    f"the gate probe raised {_error_code(exc)}: {exc}",
                    False,
                )
            )
            findings.append("The compatibility matrix gates could not be exercised.")
        finally:
            _cleanup(probe)

    # -- what the tree actually holds -------------------------------------
    #
    # Both counts below read as little of each file as the question needs.  The
    # matrix is append-only and grows for the life of the project, and an
    # archived IPA is a build product that can be hundreds of megabytes, so a
    # scan that materialised either one would get slower the day real evidence
    # landed in the tree -- on a check whose whole job is to report quickly
    # whether there is anything to report.
    recorded_observations = 0
    matrix_files: list[str] = []
    for path in _product_files(root, "*.jsonl"):
        if "matrix" not in path.name:
            continue
        matrix_files.append(_rel(path, root))
        try:
            # Iterated rather than ``read_text().splitlines()``: the handle
            # yields one line at a time, so the count costs a line, not the
            # file.  Same universal-newline semantics as ``read_text``, and a
            # narrower idea of a line break -- ``splitlines`` would also split on
            # a vertical tab or U+2028, neither of which separates two JSON Lines
            # records.  A record separator that JSON does not recognise is not a
            # second observation.
            with path.open("r", encoding="utf-8") as handle:
                recorded_observations += sum(1 for line in handle if line.strip())
        except OSError:
            continue
    ipa_files: list[str] = []
    for path in _product_files(root, "*.ipa"):
        rel = _rel(path, root)
        try:
            # Four bytes: a real IPA is a ZIP archive, and the magic is the
            # whole of what "is this an archive?" needs.  A 10-byte ASCII stub
            # is test scratch, and the size beside it comes from the stat.
            with path.open("rb") as handle:
                head = handle.read(4)
            size = path.stat().st_size
        except OSError:
            head, size = b"", -1
        # A real IPA is a ZIP archive; a 10-byte ASCII stub is test scratch.
        ipa_files.append(f"{rel} ({'zip' if head[:2] == b'PK' else 'not-a-zip'}, {size} bytes)")
    observations.append(
        Observation(
            "recorded_observations",
            "compatibility-matrix data files in the product tree: "
            f"{len(matrix_files)} ({', '.join(matrix_files) if matrix_files else 'none'}); "
            f"recorded observations: {recorded_observations}",
            recorded_observations > 0,
        )
    )
    observations.append(
        Observation(
            "archived_ipas",
            "files named *.ipa in the product tree (gitignored local-private/ scratch and "
            "dot-directories excluded): "
            + ("; ".join(ipa_files) if ipa_files else "none"),
            False,
        )
    )
    declared, wired = _cli_subcommands(root)
    has_matrix_command = "matrix" in declared and "matrix" in wired
    observations.append(
        Observation(
            "cli_surface",
            "CLI subcommands that read or write the matrix: "
            + (", ".join(sorted(set(declared) & {"matrix"})) if has_matrix_command else "none"),
            has_matrix_command,
        )
    )
    if recorded_observations == 0:
        findings.append(
            "No compatibility observation is recorded anywhere in the tree, so 'more "
            "compatibility testing' has no evidence behind it yet."
        )
    if not ipa_files:
        findings.append(
            "There is no archived Device IPA binary in the tree to test against; the "
            "samples that exist are build products, not archives."
        )
    elif all("not-a-zip" in item for item in ipa_files):
        findings.append(
            "The only *.ipa files in the tree are non-ZIP test scratch, not Device IPA archives."
        )
    if not has_matrix_command:
        findings.append(
            "The compatibility matrix has no CLI surface, so an operator cannot record an "
            "observation through the product."
        )

    evidence.append(
        Evidence("config/sources.lock.json", "config", "host_real", "frozen system identity")
    )

    # The 实现状态 row is derived from the code surface, not declared.  Three
    # things are code and all three are checkable on this host: the matrix module
    # imports, its gates refuse what they must refuse, and a CLI subcommand is
    # declared *and* wired so an operator can record an observation through the
    # product rather than only through a probe.
    #
    # What is deliberately NOT in this expression: ``recorded_observations`` and
    # ``archived_ipas``.  Both are field facts -- no own-source or authorised
    # archive to test, and no guest to test one on -- and CLAUDE.md 代码阶段与现场阶段分离
    # forbids reading a missing field as unfinished code.  They are reported as
    # findings and recorded in ``blocker`` and in the onsite field instead.  A
    # second hardcoded string here would say "the code is not written" about code
    # that is, which is the same overstatement in the other direction.
    code_surface_complete = matrix is not None and gates_ok and has_matrix_command
    implementation = "CODE COMPLETE" if code_surface_complete else "PARTIAL"

    if matrix is None or not gates_ok:
        verdict = _make_verdict(
            FAIL,
            reasons=["compatibility_matrix_or_one_of_its_gates_is_not_working"],
            evidence=evidence,
            observed=True,
        )
        caveats = (
            "the compatibility matrix or one of its gates is not working",
        )
    else:
        verdict = _make_verdict(
            BLOCKED,
            reasons=[
                "no_recorded_compatibility_observations",
                "no_archived_device_ipa",
                "installs_and_runs_requires_a_real_ipados_guest",
            ],
            evidence=evidence,
            observed=True,
        )
        caveats = (
            "the gates are host logic and were exercised; what is missing is a test that "
            "actually ran against a package on a device",
        )

    return P1Check(
        bullet=bullet.bullet,
        prd_citation=bullet.prd_citation,
        requirement=bullet.requirement,
        probe=bullet.probe,
        verdict=verdict.kind,
        verdict_reasons=verdict.reasons,
        reasons=caveats,
        observations=tuple(observations),
        evidence=tuple(evidence),
        findings=tuple(findings),
        implementation=implementation,
        blocker=(
            "no archived own-source or authorised Device IPA binary and no iPadOS guest, so "
            "no compatibility verdict above 'untested' can be produced"
        ),
    )


# ---------------------------------------------------------------------------
# P1 (3) -- docs/PRD.md:88
# ---------------------------------------------------------------------------


def check_p1_3_lifecycle(root: Path) -> P1Check:
    """应用卸载、更新、安装历史和清晰的错误分类。 (PRD:88)

    Everything in this bullet is host-side state, so it is all exercised here
    for real: an append-only ledger in a temporary directory is driven through
    install -> uninstall -> update, the closed classification table is checked
    against :class:`unipad.errors.ErrorCode`, and the real ledger under
    ``artifacts/`` is read (never written) and reported as found.
    """
    bullet = P1_BULLETS[2]
    observations: list[Observation] = []
    evidence: list[Evidence] = []
    findings: list[str] = []

    lifecycle, lifecycle_error = _import("unipad.ipa.lifecycle")
    ledger_ok = False
    if lifecycle is None:
        observations.append(
            Observation("ledger_module", f"unipad.ipa.lifecycle unavailable: {lifecycle_error}", False)
        )
        findings.append("The install lifecycle module could not be imported.")
    else:
        _, lifecycle_rel = _source_text(root, "unipad.ipa.lifecycle")
        evidence.append(
            Evidence(
                lifecycle_rel, "source", "host_real", "install ledger and classification"
            )
        )
        from unipad.models import InstallResult

        probe = _tmpdir("unipad-p1-ledger-")
        try:
            ledger = lifecycle.InstallLedger(probe / "ledger.jsonl")
            first = "a" * 64
            second = "b" * 64
            ledger.record_install(
                InstallResult(package_sha256=first, bundle_id="com.unipad.probe", stage="installed")
            )
            # An update over a bundle that is present must name the entry it
            # replaced; an update after a removal has nothing to name and must
            # not invent one.
            update_over_install = ledger.record_install(
                InstallResult(
                    package_sha256=second, bundle_id="com.unipad.probe", stage="registered"
                ),
                action=lifecycle.LifecycleAction.UPDATE,
            )
            ledger.record_uninstall(
                InstallResult(package_sha256=second, bundle_id="com.unipad.probe", stage="removed")
            )
            # Nothing is installed now, so a second removal must be refused
            # rather than fabricate one.
            double_uninstall = "accepted"
            try:
                ledger.record_uninstall(
                    InstallResult(
                        package_sha256=first, bundle_id="com.unipad.probe", stage="removed"
                    )
                )
            except ValueError:
                double_uninstall = "ValueError"
            update_after_removal = ledger.record_install(
                InstallResult(
                    package_sha256=second, bundle_id="com.unipad.probe", stage="installed"
                ),
                action=lifecycle.LifecycleAction.UPDATE,
            )
            reloaded = lifecycle.InstallLedger(probe / "ledger.jsonl")
            history = reloaded.history("com.unipad.probe")
            actions = [entry.action.value for entry in history]
            current = reloaded.installed("com.unipad.probe")
            ledger_ok = (
                len(history) == 4
                and actions == ["install", "update", "uninstall", "update"]
                and update_over_install.supersedes == "le-00000001"
                and update_after_removal.supersedes is None
                and current is not None
                and current.entry_id == update_after_removal.entry_id
                and double_uninstall == "ValueError"
                and reloaded.unreadable_lines == 0
            )
            observations.append(
                Observation(
                    "append_only_history",
                    f"a temporary ledger driven install -> update -> uninstall -> update kept "
                    f"{len(history)} entries in order {actions}; the update over an installed "
                    f"bundle names the entry it supersedes ({update_over_install.supersedes}), "
                    f"the update after a removal names none ({update_after_removal.supersedes}); "
                    f"installed() derives {current.entry_id if current else None} "
                    f"from the tail; unreadable lines: {reloaded.unreadable_lines}",
                    ledger_ok,
                )
            )
            observations.append(
                Observation(
                    "uninstall_refuses_absent",
                    f"record_uninstall on a bundle that is not installed raises {double_uninstall}, "
                    "so a removal cannot be fabricated",
                    double_uninstall == "ValueError",
                )
            )
        except Exception as exc:
            # The condition keeps its own label even when the probe blew up.
            observations.append(
                Observation(
                    "append_only_history",
                    f"the ledger probe raised {_error_code(exc)}: {exc}",
                    False,
                )
            )
            findings.append("The install ledger could not be driven through its transitions.")
        finally:
            _cleanup(probe)

    # -- the closed error classification table ----------------------------
    classification_ok = False
    if lifecycle is not None:
        from unipad.errors import ErrorCode

        table = dict(lifecycle._CLASS_BY_CODE)
        classes = set(lifecycle.FAILURE_CLASSES)
        codes = {
            value
            for name, value in vars(ErrorCode).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        unknown_values = sorted({value for value in table.values()} - classes)
        unknown_keys = sorted(set(table) - codes)
        unknown_default = (
            lifecycle.classify_failure("") == "unknown"
            and lifecycle.classify_failure(None) == "unknown"
            and lifecycle.classify_failure("a_code_nobody_declared") == "unknown"
        )
        guest_refusals = [
            "signature_or_policy_rejected",
            "registration_failed",
            "missing_dependency",
            "dyld_failed",
        ]
        guest_mapped = all(
            lifecycle.classify_failure(code) == "guest_refused" for code in guest_refusals
        )
        classification_ok = (
            not unknown_values and unknown_default and guest_mapped and bool(table)
        )
        observations.append(
            Observation(
                "closed_classification",
                f"FAILURE_CLASSES {sorted(classes)}; {len(table)} code rows; every value is a "
                f"declared class: {not unknown_values}; an unrecognised or absent code is "
                f"'unknown' (never a neighbouring guess): {unknown_default}; the four "
                f"guest-refusal codes classify as guest_refused: {guest_mapped}",
                classification_ok,
            )
        )
        if unknown_keys:
            findings.append(
                "The classification table maps "
                f"{len(unknown_keys)} key(s) that are not unipad.errors.ErrorCode members: "
                + ", ".join(unknown_keys)
                + ". They are dead rows unless a caller passes a reason slug where a code "
                "is documented."
            )
        unmapped_install_codes = sorted(
            code
            for code in codes
            if code
            in {
                "invalid_package",
                "unsupported_platform",
                "unsupported_arch",
                "transfer_failed",
                "install_service_unavailable",
                "image_hash_mismatch",
            }
            if code not in table
        )
        if unmapped_install_codes:
            findings.append(
                "Install-chain error codes with no classification row: "
                + ", ".join(unmapped_install_codes)
            )

    # -- the CLI surface ---------------------------------------------------
    declared, wired = _cli_subcommands(root)
    present = sorted(set(declared) & {"history", "uninstall", "update"})
    wired_present = sorted(set(wired) & {"history", "uninstall", "update"})
    cli_ok = present == ["history", "uninstall", "update"] and wired_present == present
    observations.append(
        Observation(
            "cli_surface",
            f"subcommands declared: {declared}; history/uninstall/update declared: {present}; "
            f"wired to a handler: {wired_present}",
            cli_ok,
        )
    )
    if not cli_ok:
        findings.append("The CLI does not expose all of history, uninstall and update.")

    # -- the ledger that is really on disk ---------------------------------
    ledger_path = root / "artifacts" / "lifecycle" / "install-ledger.jsonl"
    real_ledger_ok = False
    if ledger_path.is_file() and lifecycle is not None:
        evidence.append(
            Evidence(_rel(ledger_path, root), "ledger", "host_real", "real install ledger on disk")
        )
        try:
            real = lifecycle.InstallLedger(ledger_path)
            actions = _count(entry.action.value for entry in real.entries)
            stages = _count(entry.stage for entry in real.entries)
            classes = _count(entry.error_class for entry in real.entries)
            real_ledger_ok = real.unreadable_lines == 0 and "uninstall" in actions
            observations.append(
                Observation(
                    "real_ledger",
                    f"artifacts/lifecycle/install-ledger.jsonl holds {len(real.entries)} "
                    f"entries, {real.unreadable_lines} unreadable; actions {actions}; "
                    f"stages {stages}; error classes {classes}",
                    real_ledger_ok,
                )
            )
            findings.append(
                "The ledger entries are host-side records: none of them names a guest "
                "observation, and every stage in it was produced by the controller or a "
                "test, not by a device."
            )
        except Exception as exc:
            observations.append(
                Observation(
                    "real_ledger",
                    f"the real ledger could not be read: {_error_code(exc)}",
                    False,
                )
            )
            findings.append("The install ledger under artifacts/ could not be read.")
    else:
        observations.append(
            Observation("real_ledger", "no install ledger exists under artifacts/", False)
        )
        findings.append("No install ledger exists under artifacts/, so no history is recorded.")

    if lifecycle is None or not ledger_ok or not classification_ok or not cli_ok:
        verdict = _make_verdict(
            FAIL,
            reasons=[
                "install_history_transitions_or_error_classification_did_not_behave_as_required"
            ],
            evidence=evidence,
            observed=True,
        )
        implementation = "PARTIAL"
        caveats = (
            "the install history, the transitions or the closed error classification did "
            "not behave as the bullet requires",
        )
    else:
        verdict = _make_verdict(
            PASS,
            reasons=["host_side_lifecycle_surface_exercised"],
            evidence=evidence,
            observed=True,
        )
        implementation = "CODE COMPLETE"
        caveats = (
            "the host-side lifecycle surface is complete and was exercised here",
            "uninstall and update were recorded as host transitions; neither has been "
            "confirmed against a bundle on a real guest",
        )

    return P1Check(
        bullet=bullet.bullet,
        prd_citation=bullet.prd_citation,
        requirement=bullet.requirement,
        probe=bullet.probe,
        verdict=verdict.kind,
        verdict_reasons=verdict.reasons,
        reasons=caveats,
        observations=tuple(observations),
        evidence=tuple(evidence),
        findings=tuple(findings),
        implementation=implementation,
        blocker=(
            "no real iPadOS guest install service: uninstall and update are recorded here as "
            "host transitions, never confirmed against a device"
        ),
    )


# ---------------------------------------------------------------------------
# P1 (4) -- docs/PRD.md:89
# ---------------------------------------------------------------------------


def check_p1_4_display_audio_package(root: Path) -> P1Check:
    """旋转、可选分辨率、音频和安装包体验。 (PRD:89)

    Rotation, resolution and audio are host-side decisions that reach the guest
    through the argv, so all three are exercised for real through
    :func:`unipad.host.display.decide`, including the refusals: an off-catalogue
    resolution must be refused rather than snapped, and a non-quarter turn must
    be refused rather than rounded.

    The install-package experience is split honestly: the installer's own stage
    vocabulary is checked for drift, and the tree is scanned for a distributable
    Windows installer package, which is a different thing and is reported as
    whatever is actually there.
    """
    bullet = P1_BULLETS[3]
    observations: list[Observation] = []
    evidence: list[Evidence] = []
    findings: list[str] = []

    display, display_error = _import("unipad.host.display")
    mechanics_ok = False
    if display is None:
        observations.append(
            Observation("display_module", f"unipad.host.display unavailable: {display_error}", False)
        )
        findings.append("The display/audio decision module could not be imported.")
    else:
        _, display_rel = _source_text(root, "unipad.host.display")
        evidence.append(Evidence(display_rel, "source", "host_real", "display/audio decision"))
        config, config_rel = _machine_config(root)
        evidence.append(Evidence(config_rel, "config", "host_real", "shipped machine configuration"))
        try:
            base = display.decide()
            turns: dict[str, Any] = {}
            fragments_ok = True
            for turn in display.ROTATIONS:
                decision = display.decide(rotation=turn)
                turns[str(turn)] = [decision.framebuffer[0], decision.framebuffer[1]]
                # The geometry is one *fragment* of the fw_cfg item, so the
                # fragments have to be joined before looking for it: an ``in``
                # test against the list would compare whole argv pieces.
                fragments = " ".join(decision.to_qemu_fragments())
                expected = f"string={decision.framebuffer[0]}x{decision.framebuffer[1]}"
                if expected not in fragments:
                    fragments_ok = False
            modes_ok = all(
                display.decide(width=mode.width, height=mode.height).mode_name == mode.name
                for mode in display.DISPLAY_MODES
            )
            refusals: dict[str, str] = {}
            for label, call in (
                ("off_catalogue_resolution", lambda: display.decide(width=1000, height=800)),
                ("non_quarter_turn", lambda: display.decide(rotation=45)),
                ("half_specified_resolution", lambda: display.decide(width=1376)),
            ):
                try:
                    call()
                    refusals[label] = "accepted"
                except Exception as exc:
                    refusals[label] = _error_code(exc)
            audio = display.AudioDecision("hda")
            plain = display.AudioDecision("none")
            mechanics_ok = (
                fragments_ok
                and modes_ok
                and all(value == "config_invalid" for value in refusals.values())
                and turns.get("90") == [1024, 1376]
                and turns.get("0") == [1376, 1024]
                and audio.emits_device()
                and not plain.emits_device()
                and audio.guest_audio_verified is False
            )
            observations.append(
                Observation(
                    "rotation",
                    f"decide() on the shipped geometry gives {base.to_dict()['framebuffer']}; "
                    f"framebuffer per quarter turn {turns}; the rotated geometry reaches the "
                    f"fw_cfg item in every case: {fragments_ok}",
                    fragments_ok,
                )
            )
            observations.append(
                Observation(
                    "optional_resolution",
                    f"the closed catalogue holds {len(display.DISPLAY_MODES)} modes "
                    f"({', '.join(mode.name for mode in display.DISPLAY_MODES)}); an "
                    f"off-catalogue or half-specified resolution is refused, not snapped: "
                    f"{refusals}",
                    modes_ok and all(v == "config_invalid" for v in refusals.values()),
                )
            )
            observations.append(
                Observation(
                    "audio",
                    f"backends {list(display.AUDIO_MODES)}; 'hda' emits "
                    f"{' '.join(audio.to_qemu_fragments())} with a null host driver, so no "
                    f"host sound device is opened; 'none' emits nothing; "
                    f"guest_audio_verified is {audio.guest_audio_verified}",
                    True,
                )
            )
            observations.append(
                Observation(
                    "audio_unverified_list",
                    "what the hda decision explicitly leaves unproved: "
                    + "; ".join(display.AUDIO_UNVERIFIED),
                    None,
                )
            )
            if not fragments_ok:
                findings.append(
                    "A rotated framebuffer did not reach the fw_cfg item, so the guest driver "
                    "would keep the unrotated geometry."
                )
            if not modes_ok:
                findings.append("A catalogue display mode did not resolve to its own name.")
            if any(value != "config_invalid" for value in refusals.values()):
                findings.append(
                    "A resolution or rotation outside the closed vocabulary was accepted "
                    "rather than refused."
                )
        except Exception as exc:
            # Every condition this check owns keeps its own label even when the
            # probe blew up, so a reader (and a test) always finds them.
            for label in ("rotation", "optional_resolution", "audio"):
                observations.append(
                    Observation(
                        label,
                        f"the display probe raised {_error_code(exc)}: {exc}",
                        False,
                    )
                )
            findings.append("The display/audio decision could not be exercised.")

        # -- install-package experience -----------------------------------
        try:
            drift = display.stage_vocabulary_drift()
        except Exception as exc:  # pragma: no cover - depends on the tree
            drift = (f"drift probe failed: {_error_code(exc)}",)
        observations.append(
            Observation(
                "install_progress_vocabulary",
                f"the install stage vocabulary used for progress matches unipad.ipa.faults: "
                f"drift = {list(drift)}",
                not drift,
            )
        )
        if drift:
            findings.append(
                "The install progress vocabulary drifted from the installer's own stages: "
                + ", ".join(str(item) for item in drift)
            )
        faults, _ = _import("unipad.ipa.faults")
        if faults is not None:
            observations.append(
                Observation(
                    "install_progress_stages",
                    "progress stages come from the installer's own vocabulary "
                    f"{[str(stage) for stage in faults.InstallStage]}, so a stage the "
                    "installer can no longer produce cannot be rendered",
                    True,
                )
            )

    # -- a distributable installer package? --------------------------------
    installer_suffixes = ("*.iss", "*.nsi", "*.wxs", "*.msi", "*.bat", "*.cmd")
    installer_candidates: list[str] = []
    for suffix in installer_suffixes:
        for path in sorted(root.glob(suffix)):
            installer_candidates.append(_rel(path, root))
    for path in sorted((root / "scripts").rglob("*.ps1")) if (root / "scripts").is_dir() else []:
        if "setup" in path.name.lower() or "installer" in path.name.lower():
            installer_candidates.append(_rel(path, root))
    observations.append(
        Observation(
            "installer_package",
            "Windows installer-package artefacts in the tree (Inno Setup / NSIS / WiX / MSI / "
            f"setup script): {installer_candidates if installer_candidates else 'none'}",
            bool(installer_candidates),
        )
    )
    if not installer_candidates:
        findings.append(
            "'安装包体验' as a distributable Windows installer package is not present: the "
            "tree has no Inno Setup, NSIS, WiX or MSI artefact. What exists is the install "
            "*progress* vocabulary inside the import chain, which is a different claim."
        )

    if display is None or not mechanics_ok:
        verdict = _make_verdict(
            FAIL,
            reasons=["rotation_resolution_or_audio_did_not_behave_as_the_bullet_requires"],
            evidence=evidence,
            observed=True,
        )
        implementation = "PARTIAL"
        caveats = (
            "rotation, resolution or audio did not behave as the bullet requires",
        )
    else:
        verdict = _make_verdict(
            PASS,
            reasons=["rotation_resolution_audio_and_install_stage_vocabulary_exercised"],
            evidence=evidence,
            observed=True,
        )
        implementation = "CODE COMPLETE"
        caveats = (
            "rotation, the closed resolution catalogue and the audio backend decision "
            "were all exercised on this host, and the install stage vocabulary matches "
            "the installer's own",
            "guest audio has never been heard on this host and no configuration can claim "
            "otherwise",
            "rotation and resolution do not reach the guest on the virtio-gpu route "
            "(ADR-0004); only the ramfb path carries the fw_cfg geometry",
        )

    return P1Check(
        bullet=bullet.bullet,
        prd_citation=bullet.prd_citation,
        requirement=bullet.requirement,
        probe=bullet.probe,
        verdict=verdict.kind,
        verdict_reasons=verdict.reasons,
        reasons=caveats,
        observations=tuple(observations),
        evidence=tuple(evidence),
        findings=tuple(findings),
        implementation=implementation,
        blocker=(
            "no real iPadOS guest: nothing here proves a rotated or resized framebuffer "
            "renders correctly on a device, or that guest audio was heard"
        ),
    )


# ---------------------------------------------------------------------------
# P1 (5) -- docs/PRD.md:90
# ---------------------------------------------------------------------------


def check_p1_5_snapshot_restore(root: Path) -> P1Check:
    """真正经过验证的快照/恢复；必须说明保存的是磁盘还是整机状态。 (PRD:90)

    The second clause is the testable one, and it is exercised for real: a
    checkpoint is produced with :meth:`ImageManager.backup`, its manifest is read
    back, and :meth:`ImageManager.restore_checkpoint` is run into a fresh
    directory with digest verification.  The refusals are exercised too -- a
    ``machine`` scope that names nothing, and a manifest that does not declare a
    scope at all.

    What "真正经过验证" means for a guest is not testable here and is reported as
    blocked: a digest-verified file copy says nothing about whether the machine
    comes back.
    """
    bullet = P1_BULLETS[4]
    observations: list[Observation] = []
    evidence: list[Evidence] = []
    findings: list[str] = []

    images, images_error = _import("unipad.host.images")
    scope_ok = False
    roundtrip_ok = False
    if images is None:
        observations.append(
            Observation("images_module", f"unipad.host.images unavailable: {images_error}", False)
        )
        findings.append("The image manager module could not be imported.")
    else:
        _, images_rel = _source_text(root, "unipad.host.images")
        evidence.append(
            Evidence(images_rel, "source", "host_real", "image manager and scope vocabulary")
        )
        try:
            vocabulary = tuple(images.ScopeRequired.ALL)
            declaration = images.declare_scope(images.ScopeRequired.DISK)
            excludes = tuple(declaration.get("scope_excludes") or ())
            required_excludes = {
                "guest_ram",
                "device_state",
                "fw_cfg",
                "install_history",
                "network_state",
            }
            machine_refusal = "accepted"
            try:
                images.declare_scope(images.ScopeRequired.MACHINE, covered=[])
            except ValueError:
                machine_refusal = "ValueError"
            unknown_scope_refusal = "accepted"
            try:
                images.declare_scope("whole-tape")
            except ValueError:
                unknown_scope_refusal = "ValueError"
            undeclared_refusal = "accepted"
            try:
                images.read_scope({"entries": []})
            except Exception as exc:
                undeclared_refusal = _error_code(exc)
            no_manifest = images.read_scope(None)
            # One source of truth: the same value feeds the observation's ``ok``
            # and ``scope_ok``, so a test that flips the declaration is visible
            # in the report instead of only in a private boolean.
            exclusions_declared = required_excludes.issubset(set(excludes))
            scope_ok = (
                vocabulary == ("disk", "machine")
                and exclusions_declared
                and machine_refusal == "ValueError"
                and unknown_scope_refusal == "ValueError"
                and undeclared_refusal == "artifact_manifest_invalid"
                and no_manifest is None
            )
            observations.append(
                Observation(
                    "scope_vocabulary",
                    f"ScopeRequired.ALL = {list(vocabulary)}; a disk checkpoint declares "
                    f"scope_excludes {list(excludes)}; a machine scope with no covered list "
                    f"is {machine_refusal}; an unknown scope is {unknown_scope_refusal}; a "
                    f"manifest that declares no scope is refused at restore with "
                    f"{undeclared_refusal}; no manifest at all reads as {no_manifest} "
                    "(reported unverified, not refused)",
                    scope_ok,
                )
            )
        except Exception as exc:
            observations.append(
                Observation(
                    "scope_vocabulary",
                    f"the scope probe raised {_error_code(exc)}: {exc}",
                    False,
                )
            )
            findings.append("The checkpoint scope vocabulary could not be exercised.")

        probe = _tmpdir("unipad-p1-snap-")
        try:
            source = probe / "data.img"
            payload = bytes(range(256)) * 64
            source.write_bytes(payload)
            manager = images.ImageManager(probe)
            backup = manager.backup(source, backup_dir=probe / "ckpt", note="p1 probe")
            manifest_path = probe / "ckpt" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            restore = manager.restore_checkpoint(probe / "ckpt", target_dir=probe / "restored")
            restored_file = probe / "restored" / source.name
            roundtrip_ok = (
                manifest.get("scope") == "disk"
                and bool(manifest.get("scope_excludes"))
                and restore.get("scope") == "disk"
                and restore.get("verified") is True
                and restored_file.is_file()
                and restored_file.read_bytes() == payload
                and backup.get("scope") == "disk"
            )
            evidence.append(
                Evidence(
                    "artifacts/lifecycle/install-ledger.jsonl",
                    "ledger",
                    "host_real",
                    "state a disk checkpoint explicitly excludes",
                )
            )
            observations.append(
                Observation(
                    "restore_roundtrip",
                    f"backup() declared scope {backup.get('scope')} with excludes "
                    f"{list(backup.get('scope_excludes') or [])}; the manifest carries the "
                    f"same declaration; restore_checkpoint() into a fresh directory reported "
                    f"scope {restore.get('scope')}, verified={restore.get('verified')}, and "
                    f"the restored bytes match the original: "
                    f"{restored_file.is_file() and restored_file.read_bytes() == payload}",
                    roundtrip_ok,
                )
            )
            observations.append(
                Observation(
                    "scope_stated_explicitly",
                    "the answer to the bullet's second clause, as the tree declares it: a "
                    "checkpoint saves DISK images only, and declare_scope() names what it "
                    "does not cover: "
                    + ", ".join(excludes)
                    + f"; that covers the five things an operator must be told are absent: "
                    f"{exclusions_declared}",
                    exclusions_declared,
                )
            )
        except Exception as exc:
            observations.append(
                Observation(
                    "restore_roundtrip",
                    f"the roundtrip raised {_error_code(exc)}: {exc}",
                    False,
                )
            )
            findings.append("A checkpoint could not be produced and restored on this host.")
        finally:
            _cleanup(probe)

    # -- has any real checkpoint ever been produced? -----------------------
    checkpoint_manifests: list[str] = []
    for path in _product_files(root, "manifest.json"):
        rel = _rel(path, root)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(document, dict) and document.get("scope"):
            checkpoint_manifests.append(rel)
    observations.append(
        Observation(
            "real_checkpoints",
            "checkpoint manifests in the tree that declare a scope: "
            + (", ".join(checkpoint_manifests) if checkpoint_manifests else "none"),
            bool(checkpoint_manifests),
        )
    )
    if not checkpoint_manifests:
        findings.append(
            "No checkpoint manifest exists anywhere in the tree, so the scope declaration "
            "has never been produced by a real backup -- only by a probe."
        )

    # F7: name what was observed.  The FAIL branch used to collapse to one
    # sentence -- "the checkpoint scope declaration or the digest-verified restore
    # did not behave as the bullet requires" -- which tells an operator that
    # something is wrong and nothing about what.  The slugs below mirror the
    # condition exactly, one per sub-probe, so the reason and the observations
    # cannot drift apart.
    failed_observations: list[str] = []
    if images is None:
        failed_observations.append("images_module_unavailable")
    if images is not None and not scope_ok:
        failed_observations.append("checkpoint_scope_vocabulary_not_as_required")
    if images is not None and not roundtrip_ok:
        failed_observations.append("digest_verified_restore_roundtrip_failed")

    if failed_observations:
        verdict = _make_verdict(
            FAIL,
            reasons=failed_observations,
            evidence=evidence,
            observed=True,
        )
        implementation = "PARTIAL"
        caveats = (
            "the bullet's second clause is not satisfied: "
            + "; ".join(failed_observations)
            + " -- see the matching observations above for the values that were read",
        )
    else:
        verdict = _make_verdict(
            PASS,
            reasons=["checkpoint_scope_declared_and_restore_digest_verified"],
            evidence=evidence,
            observed=True,
        )
        implementation = "CODE COMPLETE"
        caveats = (
            "a checkpoint must declare whether it saved disk or whole-machine state, an "
            "undeclared manifest is refused, and a restore is digest-verified file by "
            "file -- all exercised on this host",
            "a digest-verified file copy does not prove the guest comes back: restore "
            "correctness on a real iPadOS guest has never been observed",
        )

    return P1Check(
        bullet=bullet.bullet,
        prd_citation=bullet.prd_citation,
        requirement=bullet.requirement,
        probe=bullet.probe,
        verdict=verdict.kind,
        verdict_reasons=verdict.reasons,
        reasons=caveats,
        observations=tuple(observations),
        evidence=tuple(evidence),
        findings=tuple(findings),
        implementation=implementation,
        blocker=(
            "no real iPadOS guest to restore into, so '真正经过验证' for the machine itself "
            "is unproven; only the disk-file round trip has been verified"
        ),
    )


# ---------------------------------------------------------------------------
# P1 (6) -- docs/PRD.md:91
# ---------------------------------------------------------------------------


def _powershell() -> Optional[str]:
    """Return the PowerShell executable, or ``None`` when it is absent."""
    return shutil.which("powershell") or shutil.which("powershell.exe")


def _first_json_object(text: str) -> dict[str, Any]:
    """Return the first JSON object in ``text``, or ``{}``.

    ``Write-StructuredError`` emits a single-line object, but ``ConvertTo-Json``
    pretty-prints the launcher plan across many lines, so parsing line by line
    would find nothing.  The whole text is tried first, then a brace-balanced
    scan, so both shapes work.
    """
    stripped = text.strip()
    if not stripped:
        return {}
    for candidate in (stripped,):
        try:
            document = json.loads(candidate)
        except json.JSONDecodeError:
            document = None
        if isinstance(document, dict):
            return document
    depth = 0
    start = -1
    for index, character in enumerate(stripped):
        if character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    document = json.loads(stripped[start : index + 1])
                except json.JSONDecodeError:
                    document = None
                    start = -1
                    continue
                if isinstance(document, dict):
                    return document
                start = -1
    return {}


#: Every launcher invocation this module actually performed, appended by
#: :func:`_run_launcher`.  It exists so a test can tell "the launcher ran" from
#: "the check read the script's text and wrote a plausible answer": a check that
#: stops spawning the process stops filling this list.  Only the argv is kept --
#: never the payload, which carries absolute paths (PRD 9.2).
LAUNCHER_INVOCATIONS: list[tuple[str, ...]] = []


def _run_launcher(script: Path, args: Sequence[str], root: Path) -> tuple[int, str, str]:
    """Run the real launcher script and return ``(exit_code, stdout, stderr)``.

    ``root`` is the tree being judged and is handed to the child as its working
    directory.  That is load-bearing, not tidiness: the launcher resolves a
    relative ``-Artifact`` against the caller's CWD
    (``scripts/windows/UniPad-Launcher.ps1``:541 ``GetFullPath(Combine($callerCwd,
    $Artifact))``) and then guards that resolved path for non-ASCII characters
    (its ``Test-UniPadAsciiPath`` pre-flight, :579-606).  Without an explicit
    ``cwd=`` the child inherits the operator's shell, so the *same tree* reads
    ``PASS`` from the repo root and ``FAIL`` from this host's own non-ASCII
    ``%TEMP%`` -- the guard fires, the process exits 2, and the check reports a
    defect that belongs to the caller's working directory rather than to the
    tree.  A verdict that moves with the CWD is not a verdict, so the CWD is
    pinned to the root here and the answer becomes a property of the tree.
    """
    executable = _powershell()
    if executable is None:  # pragma: no cover - Windows-only host
        return -1, "", "powershell is not on PATH"
    argv = (executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args)
    LAUNCHER_INVOCATIONS.append(tuple(argv))
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        cwd=str(root),
    )
    return completed.returncode, completed.stdout, completed.stderr


def check_p1_6_launcher(root: Path) -> P1Check:
    """简洁的 Windows 启动器：开始、停止、导入 IPA、查看日志、恢复。 (PRD:91)

    This is the one bullet whose product surface *is* this Windows host, so the
    launcher is run for real here as a child process -- three invocations, none
    of which can reach QEMU: an unknown action (a usage error), a ``-WhatIf`` dry
    run (which launches nothing), and a ``logs`` request for a run that does not
    exist.  The exit codes and the JSON payloads are the evidence.

    Only machine-readable fields are kept from the payloads: the launcher's plan
    and error objects embed absolute paths, and PRD 9.2 keeps those out of a
    report.
    """
    bullet = P1_BULLETS[5]
    observations: list[Observation] = []
    evidence: list[Evidence] = []
    findings: list[str] = []

    shim = root / "scripts" / "launcher.cmd"
    script = root / "scripts" / "windows" / "UniPad-Launcher.ps1"
    delegate = root / "scripts" / "windows" / "Start-UniPad.ps1"
    doc = root / "docs" / "launcher.md"

    present = {
        "scripts/launcher.cmd": shim.is_file(),
        "scripts/windows/UniPad-Launcher.ps1": script.is_file(),
        "scripts/windows/Start-UniPad.ps1": delegate.is_file(),
        "docs/launcher.md": doc.is_file(),
    }
    for rel, exists in present.items():
        if exists:
            evidence.append(
            Evidence(
                rel, "script" if rel.endswith((".cmd", ".ps1")) else "doc", "host_real"
            )
        )
    observations.append(
        Observation(
            "launcher_files",
            "launcher artefacts present: " + ", ".join(f"{k}={v}" for k, v in present.items()),
            all(present.values()),
        )
    )
    if not all(present.values()):
        findings.append(
            "A launcher artefact is missing: "
            + ", ".join(k for k, v in present.items() if not v)
        )

    shim_ok = False
    if shim.is_file():
        shim_text = shim.read_text(encoding="utf-8", errors="replace")
        shim_ok = (
            "windows\\UniPad-Launcher.ps1" in shim_text
            and "ExecutionPolicy" in shim_text
            and "ERRORLEVEL" in shim_text
        )
        observations.append(
            Observation(
                "cmd_shim",
                "scripts/launcher.cmd forwards every argument to windows\\UniPad-Launcher.ps1 "
                f"with -ExecutionPolicy Bypass and preserves the exit code: {shim_ok}",
                shim_ok,
            )
        )

    # -- real run 1: the action table -------------------------------------
    verbs_ok = False
    verbs: tuple[str, ...] = ()
    if script.is_file():
        exit_code, stdout, stderr = _run_launcher(script, ["-Action", "bogus-action"], root)
        error_document = _first_json_object(stderr or stdout).get("error")
        error_document = error_document if isinstance(error_document, dict) else {}
        message = str(error_document.get("message", ""))
        verbs = tuple(
            token.strip() for token in message.split("valid:", 1)[-1].split(",") if token.strip()
        )
        expected_verbs = ("start", "stop", "install", "logs", "restore")
        verbs_ok = (
            exit_code == 2
            and error_document.get("code") == "unsupported_command"
            and verbs == expected_verbs
        )
        observations.append(
            Observation(
                "five_actions",
                f"an unknown -Action exits {exit_code} with error code "
                f"'{error_document.get('code')}' and lists the valid actions as "
                f"{list(verbs)}; the bullet's five verbs are exactly those",
                verbs_ok,
            )
        )
        if exit_code != 2:
            findings.append(
                f"An unknown -Action exited {exit_code}, not the usage code 2 the CLI and "
                "the launcher contract both specify."
            )
        if verbs != ("start", "stop", "install", "logs", "restore"):
            findings.append(
                "The launcher's action table does not match the bullet's five verbs: "
                + ", ".join(verbs)
            )

    # -- real run 2: the dry run ------------------------------------------
    plan_ok = False
    plan_delegate: Optional[str] = None
    if script.is_file():
        code, out, _ = _run_launcher(
            script,
            ["-Action", "start", "-Artifact", "artifacts/manifest.json", "-WhatIf"],
            root,
        )
        document = _first_json_object(out)
        plan_action = str(document.get("action", ""))
        plan_delegate = document.get("delegates_to")
        command = [str(item) for item in document.get("command") or []]
        plan_ok = (
            code == 0
            and plan_action == "start"
            and plan_delegate == "Start-UniPad.ps1"
            and "unipad.controller" not in " ".join(command)
        )
        observations.append(
            Observation(
                "dry_run_plan",
                f"'-Action start -WhatIf' exits {code} and prints one plan object "
                f"(schema {document.get('schema')}, action {plan_action}, delegates_to "
                f"{plan_delegate}); the plan carries the Start-UniPad.ps1 delegation, so "
                "start does not reimplement the TCG guard",
                plan_ok,
            )
        )
        if code != 0:
            findings.append("The launcher dry run did not exit 0.")
        if plan_delegate != "Start-UniPad.ps1":
            findings.append(
                "The start action does not delegate to Start-UniPad.ps1, so the TCG guard "
                "would live in two places."
            )

    # -- real run 3: the logs action --------------------------------------
    logs_ok = False
    if script.is_file():
        logs_code, out, err = _run_launcher(
            script, ["-Action", "logs", "-Run", "no-such-run-p1"], root
        )
        error_document = _first_json_object(err or out).get("error")
        error_document = error_document if isinstance(error_document, dict) else {}
        logs_error_code = str(error_document.get("code", ""))
        logs_ok = logs_code == 1 and logs_error_code == "artifact_missing"
        observations.append(
            Observation(
                "logs_action",
                f"'-Action logs -Run <absent>' exits {logs_code} with error code "
                f"'{logs_error_code}', i.e. the logs action really reads "
                "runs/<run>/serial.log from disk rather than starting anything",
                logs_ok,
            )
        )
        if not logs_ok:
            findings.append(
                "The logs action did not report a missing serial log as artifact_missing."
            )

    # F7: name which of the bullet's five actions (plus the artefacts and the
    # shim) did not behave.  The FAIL branch used to collapse to "the launcher
    # did not satisfy the bullet's five actions on this host", which under F1
    # left an operator with nothing to act on.  Each slug mirrors one
    # observation that was actually emitted, so the reason set and the report
    # cannot disagree -- and a probe that never ran is never named as a probe
    # that failed.
    observed_labels = {o.label for o in observations}
    subresults: tuple[tuple[str, str, bool], ...] = (
        ("launcher_files", "launcher_artefacts_missing", not all(present.values())),
        ("cmd_shim", "cmd_shim_does_not_forward", not shim_ok),
        ("five_actions", "action_table_is_not_the_five_verbs", not verbs_ok),
        ("dry_run_plan", "dry_run_plan_not_as_documented", not plan_ok),
        ("logs_action", "logs_action_not_as_documented", not logs_ok),
    )
    failed_actions = [
        slug for label, slug, failed in subresults if failed and label in observed_labels
    ]

    if not failed_actions:
        verdict = _make_verdict(
            PASS,
            reasons=["launcher_exists_with_the_five_actions_and_three_executed"],
            evidence=evidence,
            observed=True,
        )
        implementation = "CODE COMPLETE"
        caveats = (
            "the launcher exists, exposes exactly the bullet's five actions, and three "
            "of them were really executed on this Windows host with the documented exit "
            "codes",
            "start and install were only driven through the -WhatIf plan; neither has "
            "reached a booted guest",
        )
    else:
        verdict = _make_verdict(
            FAIL,
            reasons=failed_actions,
            evidence=evidence,
            observed=True,
        )
        implementation = "PARTIAL"
        caveats = (
            "the launcher did not satisfy the bullet: "
            + "; ".join(failed_actions)
            + " -- see the matching observations above for the exit codes and payloads read",
        )

    return P1Check(
        bullet=bullet.bullet,
        prd_citation=bullet.prd_citation,
        requirement=bullet.requirement,
        probe=bullet.probe,
        verdict=verdict.kind,
        verdict_reasons=verdict.reasons,
        reasons=caveats,
        observations=tuple(observations),
        evidence=tuple(evidence),
        findings=tuple(findings),
        implementation=implementation,
        blocker=(
            "start and install need a real booted guest; the launcher is an entry point and "
            "the boot itself is still blocked by the missing kernel image"
        ),
    )


# ---------------------------------------------------------------------------
# The registry and the aggregate
# ---------------------------------------------------------------------------

#: The six checks, in PRD bullet order.  There is no default probe and no
#: "unregistered means passed" path: every bullet has exactly one function.
CHECKS: tuple[Callable[[Path], P1Check], ...] = (
    check_p1_1_networking,
    check_p1_2_compatibility,
    check_p1_3_lifecycle,
    check_p1_4_display_audio_package,
    check_p1_5_snapshot_restore,
    check_p1_6_launcher,
)


def run_checks(root: Optional[Path] = None) -> tuple[P1Check, ...]:
    """Run every P1 check against the real tree and return the results.

    A check that raises is converted into a ``FAIL`` carrying the exception type
    rather than being dropped: a crashed check is a finding, not a silence.
    """
    base = Path(root) if root is not None else _repo_root()
    results: list[P1Check] = []
    for bullet, probe in zip(P1_BULLETS, CHECKS):
        try:
            results.append(probe(base))
        except Exception as exc:
            results.append(
                P1Check(
                    bullet=bullet.bullet,
                    prd_citation=bullet.prd_citation,
                    requirement=bullet.requirement,
                    probe=bullet.probe,
                    verdict=FAIL,
                    reasons=(f"the check itself raised {type(exc).__name__}",),
                    verdict_reasons=(f"check_raised:{type(exc).__name__}",),
                    observations=(
                        Observation("check_error", redact(f"{type(exc).__name__}: {exc}"), False),
                    ),
                    evidence=(),
                    findings=(redact(f"{bullet.probe} raised {type(exc).__name__}: {exc}"),),
                    implementation="UNVERIFIED",
                    blocker=f"the check could not complete: {type(exc).__name__}",
                )
            )
    return tuple(results)


#: The roll-up.  Re-exported from :mod:`unipad.acceptance.record` so a P1
#: aggregate and a milestone aggregate can never be computed by two different
#: badness orders (F8).  The empty set fails closed to ``UNVERIFIED`` here too:
#: this module's old copy seeded ``worst = PASS``, which would have let an empty
#: check list read as a pass.
worst_of = record.worst_of


def summarise(checks: Sequence[P1Check]) -> dict[str, Any]:
    """Return the aggregate judgement and the field-unverified list.

    The aggregate reads the four fields, not just the verdicts: with no
    ``onsite`` evidence producible on this host, the release judgement is
    ``BLOCKED`` with ``no_onsite_evidence`` even if every check read ``PASS``.
    """
    verdicts = [check.verdict for check in checks]
    aggregate = worst_of(verdicts)
    reasons: list[str] = []
    if aggregate == FAIL:
        reasons.extend(f"blocking_failed:{check.bullet}" for check in checks if check.verdict == FAIL)
    if aggregate == BLOCKED:
        reasons.extend(f"blocking_by:{check.bullet}" for check in checks if check.verdict == BLOCKED)
    if aggregate == UNVERIFIED:
        reasons.extend(
            f"blocking_unverified:{check.bullet}" for check in checks if check.verdict == UNVERIFIED
        )
    reasons.append("no_onsite_evidence: no check on this host can produce guest evidence")
    field_unverified = [
        {
            "bullet": check.bullet,
            "prd_citation": check.prd_citation,
            "onsite_status": check.onsite_status,
            "blocker": redact(check.blocker) if check.blocker else "-",
        }
        for check in checks
        if check.onsite_status != "PASS"
    ]
    return {
        "aggregate": {
            "verdict": aggregate,
            "reasons": reasons,
            "checks": {check.bullet: check.verdict for check in checks},
        },
        "four_fields": [
            {
                "bullet": check.bullet,
                "implementation": check.implementation,
                "windows_status": check.windows_status,
                "onsite_status": check.onsite_status,
                "blocker": redact(check.blocker) if check.blocker else "-",
            }
            for check in checks
        ],
        "field_unverified": field_unverified,
    }


def to_dict(checks: Sequence[P1Check]) -> dict[str, Any]:
    """Return the whole report as a JSON-serialisable mapping."""
    return {
        "acceptance_gate": {"id": "unipad.acceptance.p1", "version": "1.0.0"},
        "authority": "docs/PRD.md:84-91",
        "checks": [check.to_dict() for check in checks],
        "summary": summarise(checks),
    }


def render_text(checks: Sequence[P1Check]) -> str:
    """Return a plain-text report: one block per bullet, then the aggregate."""
    lines: list[str] = []
    for check in checks:
        lines.append(f"{check.bullet} {check.verdict}  {check.prd_citation}")
        lines.append(f"  requirement : {check.requirement}")
        lines.append(f"  implementation={check.implementation}  "
                     f"windows={check.windows_status}  onsite={check.onsite_status}")
        if check.blocker:
            lines.append(f"  blocker     : {redact(check.blocker)}")
        for reason in check.reasons:
            lines.append(f"  reason      : {redact(reason)}")
        for observation in check.observations:
            mark = {True: "+", False: "-", None: " "}[observation.ok]
            lines.append(f"  [{mark}] {observation.label}: {redact(observation.detail)}")
        for finding in check.findings:
            lines.append(f"  ! {redact(finding)}")
        lines.append("")
    summary = summarise(checks)
    lines.append(f"aggregate: {summary['aggregate']['verdict']}")
    for reason in summary["aggregate"]["reasons"]:
        lines.append(f"  {redact(reason)}")
    lines.append("")
    lines.append("four fields:")
    for row in summary["four_fields"]:
        lines.append(
            f"  {row['bullet']:6} {row['implementation']:14} "
            f"{row['windows_status']:17} {row['onsite_status']:12} {redact(row['blocker'])}"
        )
    lines.append("")
    lines.append("field-unverified items:")
    for row in summary["field_unverified"]:
        lines.append(f"  {row['bullet']} ({row['prd_citation']}): {redact(row['blocker'])}")
    return "\n".join(lines)
