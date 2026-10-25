"""Milestone gate for the UniPad executable acceptance gate (PRD 6, 6.1, 7.4, 9.2).

This module owns **one check per milestone**: the sixteen PRD 6 rows (M0, M1, M2,
M3, M4, M5, M6, M7, M8, M9, M9a, M9b, M10, M10a, M11, M12) plus the five PRD 6.1
work packages (WP00-WP04).  Every check is *data* -- the PRD goal, the PRD pass
evidence, the PRD "not enough to pass" column -- and the algorithm that turns that
data into a verdict is shared by all of them, so no milestone can be given a
private reason to pass.

Why this file is separate from ``unipad.controller.verifier``
-------------------------------------------------------------
``verifier.py`` answers one question: *do the fixed conditions hold for the
evidence handed in?*  It has no idea whether that evidence came from a fake guest,
from this host's real QEMU, or from the target iPadOS -- PRD 7.4 gives it no such
axis.  This module adds the second axis PRD 6 actually asks for, the milestone's
own pass evidence and its own veto column, and refuses to conflate the two.  So
this module never calls ``Verifier.evaluate_detailed``: that call belongs to the
aggregation layer, which owns ``run_dir`` / ``facts`` / ``EvidenceSet``.  What it
gets from here instead is :attr:`MilestoneCheck.gate_id`, the registered gate id
each milestone maps to, so the two layers cannot drift apart silently.

The four-value verdict (PRD 7.4 plus the CLAUDE.md four-field rule)
-------------------------------------------------------------------
``PASS``       every declared pass-evidence requirement is satisfied by evidence
               that was actually observed, and no veto fired.
``FAIL``       a veto fired: the PRD "not enough to pass" column describes exactly
               the shape of evidence we hold.
``BLOCKED``    a named structural precondition is unmet, so the milestone was never
               attempted.  This is what PRD 7.4 asks for when there is no progress
               and not enough evidence, and it always carries the reason.
``UNVERIFIED`` preconditions hold, the veto does not apply, and the pass evidence
               is simply absent (PRD 9.2: missing evidence is unverified and may
               not be completed by prose).

The ordering is load-bearing: **blockers, then the veto column, then evidence.**
A milestone that was never attempted cannot be judged by its failure mode -- M2 on
a host with no UniXNU kernel is BLOCKED ("no kernel to load"), not FAIL ("only a
static picture appeared").  The veto stays live and reachable: it fires the moment
the milestone *is* attempted, which the tests prove with synthetic evidence rather
than with today's answer.

The load-bearing invariant
--------------------------
:class:`Verdict` cannot be constructed as PASS without at least one evidence
reference and an observation, and a milestone whose pass evidence declares an
``onsite`` requirement cannot reach PASS without an ``onsite`` reference carrying a
witness.  So "there is no real guest on this host" is not a convention somebody has
to remember -- it is the only reachable outcome.  Tomorrow, when a built UniXNU
kernel and a guest log exist, the same data goes green without editing a line of
code, because the blockers are probes and not constants.

Relationship to the package record contract
-------------------------------------------
:mod:`unipad.acceptance.record` holds the package-wide record contract.  This
module deliberately defines its own :class:`Verdict` and :class:`EvidenceRef`
instead of importing them, for one concrete reason: here a ``BLOCKED`` or
``UNVERIFIED`` verdict must still cite the host evidence it *does* hold, so the
four-field report can say "the firmware boot is proven, the guest display is
blocked" instead of a bare reason slug.  The shared factory signature does not
carry evidence for those two kinds, and editing another owner's frozen file to add
it is not an option.

The two are structurally identical -- same field names, same PASS invariants --
so reconciling them is an *append* to the shared contract, not an edit to either
side.  ``tests/acceptance/test_p0p1_gate_milestones.py`` asserts the field-name
match so drift on either side is caught rather than discovered at integration.
"""

from __future__ import annotations

import codecs
import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

from ..errors import ErrorCode, UniPadError
from . import record
from .record import (
    CODE_STATES,
    ONSITE_STATUSES,
    WINDOWS_STATUSES,
    canonical_windows_status,
)

__all__ = [
    "EvidenceScope",
    "EvidenceRef",
    "VerdictKind",
    "Verdict",
    "Observation",
    "HostView",
    "EvidenceRequirement",
    "Refusal",
    "Blocker",
    "MilestoneCheck",
    "MilestoneResult",
    "CHECKS",
    "BLOCKING_TIER",
    "WORK_PACKAGE_TIER",
    "check_ids",
    "get_check",
    "evaluate",
    "evaluate_all",
    "release_verdict",
    "field_unverified",
    "default_view",
    "WINDOWS_STATUSES",
    "ONSITE_STATUSES",
]


# ---------------------------------------------------------------------------
# 1. Axis A: the verdict
# ---------------------------------------------------------------------------


#: Where an evidence item was produced.  Imported from
#: :mod:`unipad.acceptance.record` rather than re-declared: three modules each
#: had their own copy with the same three values, and a fourth spelled them as
#: bare strings (F8).
EvidenceScope = record.EvidenceScope

_SCOPES = (EvidenceScope.SIMULATION, EvidenceScope.HOST_REAL, EvidenceScope.ONSITE)

#: A witness names the exact line that only the target object could have written.
_WITNESS_RE = re.compile(r"^[^:]+:\d+:.+$")


#: One evidence item: where it is, what it hashes to, and in which scope.
#: This is :class:`unipad.acceptance.record.EvidenceRef` narrowed to the kinds
#: a milestone probe can actually produce (F8).
#:
#: The local copy it replaces differed from the canonical one in four ways, and
#: every one was a hole:
#:
#: * it accepted a whitespace-only ``digest_reason``, so ``"   "`` counted as
#: an explanation (F5);
#: * it did not validate ``kind`` at all, so a typo'd kind was carried silently
#:   into a milestone row;
#: * it used its own path rules.  That local validator -- ``_check_relative_path``
#:   -- was deleted rather than kept: it had no caller once the canonical
#:   :class:`~unipad.acceptance.record.EvidenceRef` took over, and its comment
#:   claimed the canonical :func:`~unipad.acceptance.record.relativise` "also
#:   rejects user-profile markers".  It does not.  ``relativise`` refuses a
#:   leading drive letter, a backslash, a leading slash and empty/dot/parent
#:   segments; the local copy additionally refused a ``users/`` substring
#:   anywhere, which the canonical one permits.  Two path validators with
#:   different rules is the "two guards, one hole" shape, so the dead stronger
#:   one is gone and what actually stops a profile path reaching a report is
#:   :func:`~unipad.acceptance.record.assert_payload_redacted`, which walks the
#:   whole payload and refuses to write;
#: * it had no ``from_dict``, so a milestone row read back from JSON could not
#:   be rebuilt at all.
#:
#: ``trace_log`` is the one kind the bundle vocabulary has no word for: PRD 6
#: milestones are judged from QEMU's own trace output, which a bundle does not
#: store.  :attr:`~unipad.acceptance.record.EvidenceRef.ALLOWED_KINDS` is the
#: extension point for exactly that.
#:
#: The scope default also changes from ``host_real`` to ``simulation``.  That is
#: deliberate: a milestone's evidence is produced by tests and fakes unless a
#: probe says otherwise, and defaulting to the stronger scope let an unlabelled
#: reference count as host-provable.
MILESTONE_EVIDENCE_KINDS: tuple[str, ...] = tuple(
    sorted(set(record.EVIDENCE_KINDS) | {"trace_log"})
)


@dataclass(frozen=True)
class EvidenceRef(record.EvidenceRef):
    """The canonical evidence reference, with the milestone kind set."""

    ALLOWED_KINDS: ClassVar[tuple[str, ...]] = MILESTONE_EVIDENCE_KINDS

#: The four condition-axis values, and the badness order used by every roll-up.
#: Both come from :mod:`unipad.acceptance.record`; see the note on
#: :data:`unipad.acceptance.record.VERDICT_KINDS` about why four private copies
#: were a defect (F8).
VerdictKind = record.VerdictKind
_VERDICT_KINDS = record.VERDICT_KINDS


#: One condition-axis conclusion.  This is :class:`unipad.acceptance.record.
#: Verdict`, not a second implementation of it.
#:
#: The local copy this replaces differed from the canonical one in four ways,
#: and every one of them was a hole:
#:
#: * it required a reason slug only for ``FAIL``, so a ``BLOCKED`` or
#:   ``UNVERIFIED`` verdict could be built with no reason at all;
#: * it did not require a ``PASS`` to rest on digestable evidence, so a
#:   milestone could pass on a reference nobody could re-hash (C2);
#: * ``fail()`` set ``observed=bool(evidence)``, so a FAIL that cited nothing
#:   was recorded as an unobserved claim;
#: * ``blocked()``/``unverified()`` took evidence positionally in a different
#:   order, which is why consolidating them meant extending the canonical
#:   factories to accept evidence at all.
#:
#: Adopting the canonical class is therefore a tightening, not a rename: the
#: shapes the local copy allowed are exactly the shapes the audit found.
Verdict = record.Verdict

# ---------------------------------------------------------------------------
# 2. Axis B: what this host can actually show us
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """What one probe found.

    ``ok`` answers whether the thing this probe looks for is there.  ``refs`` are
    the evidence items it inspected -- recorded even when the answer is no, so a
    refusal can be argued from the same file a pass would have rested on.
    ``facts`` are the machine-readable values the veto and blocker predicates read.
    """

    probe_id: str
    ok: bool
    detail: str
    refs: tuple[EvidenceRef, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "ok": self.ok,
            "detail": self.detail,
            "refs": [ref.to_dict() for ref in self.refs],
            "facts": {key: value for key, value in self.facts.items()},
        }


def _fact(
    snapshot: Mapping[str, Observation], probe_id: str, key: str, default: Any = None
) -> Any:
    """Read one fact out of the snapshot; a missing probe is never an error."""
    observation = snapshot.get(probe_id)
    if observation is None:
        return default
    value = observation.facts.get(key, default)
    return default if value is None else value


def _ok(snapshot: Mapping[str, Observation], probe_id: str) -> bool:
    observation = snapshot.get(probe_id)
    return bool(observation is not None and observation.ok)


def _onsite_ok(snapshot: Mapping[str, Observation], probe_id: str) -> bool:
    """True only when the probe saw the thing *and* witnessed the target object.

    Guest-side blockers use this: a fake guest record that claims XNU came up must
    not unblock the milestones that sit on top of XNU, because its reference is
    simulation-scoped and carries no witness.
    """
    observation = snapshot.get(probe_id)
    if observation is None or not observation.ok:
        return False
    return any(ref.scope == EvidenceScope.ONSITE for ref in observation.refs)


def _observed_guest_events(snapshot: Mapping[str, Observation]) -> set[str]:
    """Guest event ids that were actually *observed*, simulated or not.

    ``missing`` is deliberately excluded: an event a probe did not find is not an
    event, and treating it as one would let "we enumerated the device" be read off
    a probe that reported the opposite.
    """
    events: set[str] = set()
    for observation in snapshot.values():
        events.update(observation.facts.get("present", []) or [])
    return events


# ---------------------------------------------------------------------------
# 3. Host view: where evidence lives and how paths are spelled
# ---------------------------------------------------------------------------

#: Directories never walked when looking for run evidence.
_PRUNE = {".git", "__pycache__", ".pytest_cache", "node_modules", "local-private"}

#: Path fragments that mark a *simulation* producer.  Evidence found under one of
#: these can never satisfy an ``onsite`` requirement: a witness that points at a
#: fake guest is not a witness.
_SIMULATION_MARKERS = ("fake", "fakes", "simulation", "/tests/", "tests\\")

#: Guest-side channels.  A serial log is deliberately not one of them: PRD 7.4
#: says serial text locates, it does not prove.
_GUEST_LOG_GLOBS = (
    "**/guest.log",
    "**/guest-*.log",
    "**/*guest*.log",
    "**/guest.json",
    "**/guest-*.json",
)


def _repo_root() -> Path:
    """Locate the repository root from this file, without assuming a layout."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "docs" / "PRD.md").is_file() and (candidate / "pyproject.toml").is_file():
            return candidate
    return here.parents[3] if len(here.parents) > 3 else Path.cwd()


@dataclass(frozen=True)
class HostView:
    """The filesystem this gate is allowed to look at.

    ``roots`` are searched for run evidence; ``repo_root`` anchors the relative
    paths written into every :class:`EvidenceRef` (PRD 9.2 forbids absolute user
    paths, so a path that cannot be made relative is refused rather than leaked).
    """

    roots: tuple[Path, ...]
    repo_root: Path
    max_files: int = 80_000
    max_bytes: int = 4 << 20
    _cache: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def files(self, root: Path) -> tuple[str, ...]:
        """Every file under ``root`` as a repo-relative POSIX path (cached)."""
        key = str(root)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        found: list[str] = []
        root = Path(root)
        if not root.is_dir():
            self._cache[key] = ()
            return ()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _PRUNE]
            for name in filenames:
                found.append(self.relativise(Path(dirpath) / name))
                if len(found) >= self.max_files:
                    break
            if len(found) >= self.max_files:
                break
        result = tuple(sorted(found))
        self._cache[key] = result
        return result

    def all_files(self) -> tuple[str, ...]:
        seen: set[str] = set()
        for root in self.roots:
            seen.update(self.files(root))
        return tuple(sorted(seen))

    def relativise(self, absolute: Path) -> str:
        """Spell ``absolute`` relative to the repo root, or refuse it.

        The string prefix test comes first: this runs once per file in a tree
        with tens of thousands of entries, and ``Path.relative_to`` raises on
        every miss.
        """
        text = str(absolute)
        for root in (self.repo_root, *self.roots):
            prefix = str(root)
            if not text.startswith(prefix):
                continue
            tail = text[len(prefix):]
            if not tail or tail[0] not in "/\\":
                continue
            return tail.lstrip("/\\").replace("\\", "/")
        try:
            return absolute.resolve().relative_to(self.repo_root).as_posix()
        except ValueError as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "evidence path is outside every configured root",
                path=str(absolute),
            ) from exc

    def resolve(self, relative: str) -> Path:
        """Turn a repo-relative path back into an absolute one."""
        return self.repo_root / relative

    def basenames(self) -> dict[str, tuple[str, ...]]:
        """basename -> paths, built once.  Glob tails match names, not full paths."""
        cached = self._cache.get("__basenames__")
        if cached is not None:
            return cached  # type: ignore[return-value]
        index: dict[str, list[str]] = {}
        for path in self.all_files():
            index.setdefault(path.rsplit("/", 1)[-1], []).append(path)
        frozen = {name: tuple(sorted(paths)) for name, paths in index.items()}
        self._cache["__basenames__"] = frozen
        return frozen

    def find(self, pattern: str) -> tuple[str, ...]:
        """All known files matching ``pattern`` (repo-relative POSIX glob)."""
        key = "find:" + pattern
        cached = self._cache.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        result: tuple[str, ...]
        if pattern.startswith("**/") and "/" not in pattern[3:]:
            tail = pattern[3:]
            index = self.basenames()
            names = [name for name in index if fnmatch.fnmatch(name, tail)]
            result = tuple(sorted(path for name in names for path in index[name]))
        else:
            result = tuple(
                path for path in self.all_files() if fnmatch.fnmatch(path, pattern)
            )
        self._cache[key] = result
        return result

    def read_text(self, relative: str) -> str:
        """Up to :attr:`max_bytes` of ``relative``, decoded as UTF-8.

        The window is read as bytes and decoded once, rather than decoded whole
        and sliced afterwards: a caller asking for a 4 MiB window of a boot log
        must not pay for the whole log, and these logs are exactly the files that
        grow without bound on a host that has been running QEMU for a while.

        The window is a *byte* count, so its end can land inside a multi-byte
        UTF-8 sequence.  That tail is dropped rather than decoded: an incremental
        decoder holds an incomplete sequence back instead of turning it into
        U+FFFD, so the returned text ends on a character boundary and never grows
        a replacement character that is not in the file.  What it does not do is
        make a truncated window whole -- a document cut here still does not
        parse, and every caller that reads JSON through this method already has
        to handle that, because the window has always been able to cut one.

        A file that cannot be opened is ``""``, as before: an unreadable file is
        an absence of evidence, not a crash in the middle of a gate.
        """
        try:
            with self.resolve(relative).open("rb") as handle:
                window = handle.read(self.max_bytes)
        except OSError:
            return ""
        # "replace" rather than "strict" for the same reason the caller's
        # ``errors="replace"`` was there: a damaged byte in the middle of a log
        # is not a reason to lose the markers either side of it.
        return codecs.getincrementaldecoder("utf-8")("replace").decode(window)

    def sha256(self, relative: str) -> str | None:
        path = self.resolve(relative)
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            return None
        return digest.hexdigest()


def default_view() -> HostView:
    """The view of this host: the repo, its run/artifact dirs, and the boot logs.

    The boot roots are listed individually rather than as ``local-private``
    because the whole tree is gigabytes of IPSW extracts and would blow
    ``max_files`` before the gate reached anything.  ``boot7`` is the directory
    every real boot on this host has read its kernel, device tree and EFI
    loaders from -- it was missing, so the gate could not see the kernel it
    was booting and reported "no built UniXNU kernel image" while one ran.
    ``out/components`` is where the IPSW members the board pins by digest
    live, which is what lets a probe say *which* image an experiment attached.
    ``boot-inputs`` is deliberately not a separate root: it lies wholly inside
    ``boot7`` (436 of its 518 files), so listing both walks the same tree twice.
    """
    root = _repo_root()
    roots = [
        root,
        root / "runs",
        root / "artifacts",
        root / "local-private" / "out" / "real-boot",
        root / "local-private" / "out" / "components",
        root / "local-private" / "boot7",
    ]
    return HostView(roots=tuple(path for path in roots if path.is_dir()), repo_root=root)


def _ref(
    view: HostView,
    relative: str,
    kind: str,
    scope: str = EvidenceScope.HOST_REAL,
    witness: str | None = None,
) -> EvidenceRef:
    digest = view.sha256(relative)
    return EvidenceRef(
        path=view.relativise(view.resolve(relative)),
        kind=kind,
        sha256=digest,
        digest_reason=None if digest else "unreadable",
        scope=scope,
        witness=witness,
    )


def _marker_hits(text: str, literal: str) -> int:
    """1-based line number of the first line containing ``literal``, else 0."""
    for index, line in enumerate(text.splitlines(), start=1):
        if literal in line:
            return index
    return 0


def _is_simulation_path(relative: str) -> bool:
    lowered = relative.lower()
    return any(marker in lowered for marker in _SIMULATION_MARKERS)


# ---------------------------------------------------------------------------
# 4. Probes: the (few) mechanisms that gather evidence
# ---------------------------------------------------------------------------

_PROBES: dict[str, Callable[[HostView], Observation]] = {}


def _probe(probe_id: str, description: str) -> Callable[..., Any]:
    def register(function: Callable[[HostView], Observation]) -> Callable[[HostView], Observation]:
        _PROBES[probe_id] = function
        return function

    return register


# --- committed configuration and host measurement -------------------------


@_probe("p.host_inventory", "repo host-inventory.json is a real host measurement")
def _probe_host_inventory(view: HostView) -> Observation:
    relative = "host-inventory.json"
    if not (view.repo_root / relative).is_file():
        return Observation("p.host_inventory", False, "host-inventory.json is absent")
    try:
        data = json.loads(view.read_text(relative))
    except json.JSONDecodeError as exc:
        return Observation("p.host_inventory", False, "host-inventory.json is not JSON: %s" % exc)
    needed = ("schema_version", "collected_at", "host", "cpu", "memory")
    missing = [key for key in needed if not data.get(key)]
    ok = not missing and bool(data.get("toolchain"))
    detail = "collected_at=%s collector=%s" % (data.get("collected_at"), data.get("collector"))
    if missing:
        detail += " missing=" + ",".join(missing)
    return Observation(
        "p.host_inventory",
        ok,
        detail,
        refs=(_ref(view, relative, "manifest"),),
        facts={"collected_at": data.get("collected_at"), "collector": data.get("collector")},
    )


@_probe("p.source_pins", "config/sources.lock.json pins commits, tools and the system identity")
def _probe_source_pins(view: HostView) -> Observation:
    relative = "config/sources.lock.json"
    if not (view.repo_root / relative).is_file():
        return Observation("p.source_pins", False, "sources.lock.json is absent")
    try:
        data = json.loads(view.read_text(relative))
    except json.JSONDecodeError as exc:
        return Observation("p.source_pins", False, "sources.lock.json is not JSON: %s" % exc)
    hex40 = re.compile(r"^[0-9a-f]{40}$")
    hex64 = re.compile(r"^[0-9a-f]{64}$")
    problems: list[str] = []
    pins = 0
    for section in ("unixnu", "device_repo"):
        entry = data.get(section) or {}
        if hex40.match(str(entry.get("commit", ""))):
            pins += 1
        else:
            problems.append(section + ".commit")
    for label, entries in (("submodules", data.get("submodules")), ("feeds", data.get("feeds"))):
        for entry in _as_entries(entries):
            name = str(entry.get("path") or entry.get("name") or "?")
            if hex40.match(str(entry.get("commit", ""))) or hex64.match(
                str(entry.get("sha256", ""))
            ):
                pins += 1
            else:
                problems.append(label + "." + name + ".commit")
    identity = data.get("system_identity") or {}
    if not identity.get("resolved_to"):
        problems.append("system_identity.resolved_to")
    ipsw = data.get("ipsw") or {}
    if not hex64.match(str(ipsw.get("sha256", ""))) or not ipsw.get("size"):
        problems.append("ipsw.sha256/size")
    qemu = (data.get("tools") or {}).get("qemu") or {}
    if not hex64.match(str(qemu.get("sha256", ""))) or not qemu.get("version"):
        problems.append("tools.qemu.sha256/version")
    return Observation(
        "p.source_pins",
        not problems,
        "pins=%d problems=%s" % (pins, ",".join(problems) or "none"),
        refs=(_ref(view, relative, "manifest"),),
        facts={
            "pins": pins,
            "problems": problems,
            "frozen": bool(data.get("frozen")),
            "frozen_note": str(data.get("frozen_note", "")),
            "accel": qemu.get("accel"),
            "qemu_version": qemu.get("version"),
        },
    )


def _as_entries(section: Any) -> list[dict[str, Any]]:
    """A lock-file section is either a list of records or a mapping of records."""
    if isinstance(section, list):
        return [item for item in section if isinstance(item, dict)]
    if isinstance(section, dict):
        return [value for value in section.values() if isinstance(value, dict)]
    return []


def _lock_json(view: HostView) -> dict[str, Any] | None:
    try:
        data = json.loads(view.read_text("config/sources.lock.json"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@_probe("p.pinned_firmware", "the firmware pinned in sources.lock.json is on disk with that digest")
def _probe_pinned_firmware(view: HostView) -> Observation:
    data = _lock_json(view)
    if data is None:
        return Observation("p.pinned_firmware", False, "sources.lock.json is unreadable")
    firmware = ((data.get("tools") or {}).get("qemu") or {}).get("firmware") or {}
    relative = str(firmware.get("path", ""))
    if not relative:
        return Observation("p.pinned_firmware", False, "no firmware path pinned")
    path = view.repo_root / relative
    if not path.is_file():
        return Observation(
            "p.pinned_firmware",
            False,
            "pinned firmware is absent: %s" % relative,
            refs=(_ref(view, relative, "artifact"),),
        )
    size = path.stat().st_size
    digest = view.sha256(relative)
    size_ok = size == firmware.get("size")
    digest_ok = digest == firmware.get("sha256")
    return Observation(
        "p.pinned_firmware",
        bool(size_ok and digest_ok),
        "firmware=%s size=%d size_match=%s digest_match=%s" % (relative, size, size_ok, digest_ok),
        refs=(_ref(view, relative, "artifact"),),
        facts={"size_match": size_ok, "digest_match": digest_ok, "size": size},
    )


@_probe("p.pinned_qemu", "the pinned QEMU binary is on disk with the pinned digest")
def _probe_pinned_qemu(view: HostView) -> Observation:
    data = _lock_json(view)
    if data is None:
        return Observation("p.pinned_qemu", False, "sources.lock.json is unreadable")
    qemu = (data.get("tools") or {}).get("qemu") or {}
    relative = str(qemu.get("binary", ""))
    if not relative:
        return Observation("p.pinned_qemu", False, "no qemu binary pinned")
    path = view.repo_root / relative
    if not path.is_file():
        return Observation(
            "p.pinned_qemu",
            False,
            "pinned qemu binary is absent: %s" % relative,
            refs=(_ref(view, relative, "artifact"),),
        )
    digest = view.sha256(relative)
    digest_ok = digest == qemu.get("sha256")
    accel_ok = qemu.get("accel") == "tcg"
    return Observation(
        "p.pinned_qemu",
        bool(digest_ok and accel_ok),
        "qemu=%s version=%s digest_match=%s accel=%s"
        % (relative, qemu.get("version"), digest_ok, qemu.get("accel")),
        refs=(_ref(view, relative, "artifact"),),
        facts={
            "version": qemu.get("version"),
            "digest_match": digest_ok,
            "accel": qemu.get("accel"),
        },
    )


@_probe("p.machine_config", "config/machine.json pins the TCG board actually booted")
def _probe_machine_config(view: HostView) -> Observation:
    relative = "config/machine.json"
    if not (view.repo_root / relative).is_file():
        return Observation("p.machine_config", False, "machine.json is absent")
    try:
        data = json.loads(view.read_text(relative))
    except json.JSONDecodeError:
        return Observation("p.machine_config", False, "machine.json is not JSON")
    needed = ("accel", "cpu_model", "machine_type", "memory_mb", "display")
    missing = [key for key in needed if not data.get(key)]
    accel = data.get("accel")
    display = data.get("display") or {}
    return Observation(
        "p.machine_config",
        not missing and accel == "tcg",
        "accel=%s cpu=%s machine=%s memory_mb=%s display=%sx%s missing=%s"
        % (
            accel,
            data.get("cpu_model"),
            data.get("machine_type"),
            data.get("memory_mb"),
            display.get("width"),
            display.get("height"),
            ",".join(missing) or "none",
        ),
        refs=(_ref(view, relative, "manifest"),),
        facts={
            "accel": accel,
            "cpu_model": data.get("cpu_model"),
            "memory_mb": data.get("memory_mb"),
            "geometry": "%sx%s" % (display.get("width"), display.get("height")),
        },
    )


@_probe("p.candidate_repos", "the candidate upstream repositories are present as git checkouts")
def _probe_candidate_repos(view: HostView) -> Observation:
    present: list[str] = []
    missing: list[str] = []
    refs: list[EvidenceRef] = []
    for name in ("UniXNU", "UniXNU-qemu", "edk2"):
        relative = "local-private/upstream/%s" % name
        if (view.repo_root / relative / ".git").is_dir():
            present.append(name)
            head = relative + "/.git/HEAD"
            if (view.repo_root / head).is_file():
                refs.append(_ref(view, head, "manifest"))
        else:
            missing.append(name)
    board = "local-private/upstream/UniXNU-qemu/board.toml"
    if (view.repo_root / board).is_file():
        refs.append(_ref(view, board, "manifest"))
    return Observation(
        "p.candidate_repos",
        not missing,
        "present=%s missing=%s" % (",".join(present) or "none", ",".join(missing) or "none"),
        refs=tuple(refs),
        facts={"present": present, "missing": missing},
    )


@_probe("p.divergence_record", "the lock file carries the divergence and upstream-convention audit")
def _probe_divergence_record(view: HostView) -> Observation:
    data = _lock_json(view)
    if data is None:
        return Observation("p.divergence_record", False, "sources.lock.json is unreadable")
    description = str(data.get("description", ""))
    notes = [
        str((data.get(section) or {}).get("note", ""))
        for section in ("unixnu", "device_repo", "ipsw", "tools")
    ]
    resolution = str((data.get("system_identity") or {}).get("resolution", ""))
    recorded = bool(description) and (bool(resolution) or any(notes))
    return Observation(
        "p.divergence_record",
        recorded,
        "description=%d chars resolution=%d chars notes=%d"
        % (len(description), len(resolution), sum(1 for note in notes if note)),
        refs=(_ref(view, "config/sources.lock.json", "manifest"),),
        facts={"recorded": recorded},
    )


@_probe("p.clean_build_manifest", "a clean build left a manifest with per-item hashes and tools")
def _probe_clean_build_manifest(view: HostView) -> Observation:
    candidates = set(view.find("**/artifact-manifest.json")) | set(view.find("**/build-manifest.json"))
    refs: list[EvidenceRef] = []
    items = 0
    tools = 0
    for relative in sorted(candidates):
        try:
            data = json.loads(view.read_text(relative))
        except json.JSONDecodeError:
            continue
        entries = data.get("artifacts") or data.get("items") or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and re.fullmatch(
                r"[0-9a-f]{64}", str(entry.get("sha256", ""))
            ):
                items += 1
        if data.get("toolchain") or data.get("tools"):
            tools += 1
        refs.append(_ref(view, relative, "manifest"))
    return Observation(
        "p.clean_build_manifest",
        bool(items and tools),
        "manifests=%d hashed_items=%d with_tool_versions=%d" % (len(refs), items, tools),
        refs=tuple(refs),
        facts={"hashed_items": items, "manifests": len(refs)},
    )


@_probe("p.ai_interface_smoke", "an AI interface smoke test was actually run and recorded")
def _probe_ai_interface_smoke(view: HostView) -> Observation:
    patterns = (
        "**/ai-smoke*.json",
        "**/ai-smoke*.log",
        "**/*ai*smoke*.json",
        "**/*ai*smoke*.log",
    )
    hits: set[str] = set()
    for pattern in patterns:
        hits.update(view.find(pattern))
    return Observation(
        "p.ai_interface_smoke",
        bool(hits),
        "records=%d" % len(hits),
        refs=tuple(_ref(view, relative, "probe_log") for relative in sorted(hits)),
        facts={"records": len(hits)},
    )


# --- firmware / boot chain evidence that exists on this host --------------


def _pe_summary(view: HostView, relative: str) -> dict[str, Any] | None:
    """Parse a PE/EFI image header with the standard library only."""
    try:
        blob = view.resolve(relative).read_bytes()[:4096]
    except OSError:
        return None
    if len(blob) < 0x40 or blob[:2] != b"MZ":
        return None
    pe_offset = int.from_bytes(blob[0x3C:0x40], "little")
    if blob[pe_offset:pe_offset + 4] != b"PE\x00\x00":
        return None
    coff = pe_offset + 4
    machine = int.from_bytes(blob[coff:coff + 2], "little")
    optional_size = int.from_bytes(blob[coff + 16:coff + 18], "little")
    optional = coff + 20
    if optional_size < 70:
        return None
    magic = int.from_bytes(blob[optional:optional + 2], "little")
    subsystem = int.from_bytes(blob[optional + 68:optional + 70], "little")
    return {
        "machine": machine,
        "pe32_plus": magic == 0x20B,
        "subsystem": subsystem,
        "efi_subsystem": subsystem in (10, 11, 12),
    }


@_probe("p.efi_loaders", "the three EFI loaders are built and PE-valid (PE32+, ARM64, EFI)")
def _probe_efi_loaders(view: HostView) -> Observation:
    """docs/implementation-plan.md Tier-1 table: "UniXNU.efi / DarwinBoot.efi /
    QemuRamfb.efi" are PRESENT + PE-VALIDATED -- PE32+, machine 0xaa64, EFI
    subsystem, parsed with a pure-Python PE header reader.  A PE header check is
    a build fact, not an execution fact: nothing here has booted.
    """
    wanted = ("UniXNU.efi", "DarwinBoot.efi", "QemuRamfb.efi")
    found: dict[str, dict[str, Any]] = {}
    refs: list[EvidenceRef] = []
    for name in wanted:
        for relative in view.find("**/%s" % name):
            summary = _pe_summary(view, relative)
            if summary is None:
                continue
            if summary["machine"] == 0xAA64 and summary["pe32_plus"] and summary["efi_subsystem"]:
                found[name] = {
                    "path": relative,
                    "size": (view.repo_root / relative).stat().st_size,
                    "sha256": view.sha256(relative),
                    "subsystem": summary["subsystem"],
                }
                refs.append(_ref(view, relative, "artifact"))
                break
    missing = [name for name in wanted if name not in found]
    return Observation(
        "p.efi_loaders",
        not missing,
        "validated=%s missing=%s"
        % (
            ",".join("%s:%dB" % (name, info["size"]) for name, info in sorted(found.items()))
            or "none",
            ",".join(missing) or "none",
        ),
        refs=tuple(refs),
        facts={"validated": sorted(found), "missing": missing, "count": len(found)},
    )


@_probe("p.edk2_firmware_boot", "a real QEMU run reached an interactive UEFI Shell prompt")
def _probe_edk2_firmware_boot(view: HostView) -> Observation:
    """docs/implementation-plan.md Tier-1 table: "EDK2 固件真实启动" is
    VERIFIED-ON-HOST -- PEI to DXE to BDS, BDS starting Boot0001 "EFI Internal
    Shell", reaching an interactive Shell> prompt.  That table also records what
    it does not prove: nothing about XNU, UniXNU, iPadOS or any payload of ours,
    because the shell is the firmware own firmware-volume option and no ESP was
    attached for that run.  The probe therefore demands the shell prompt itself,
    not merely a firmware banner.
    """
    literals = (
        "UEFI firmware (version edk2-stable202408",
        'BdsDxe: starting Boot0001 "EFI Internal Shell"',
        "Shell>",
    )
    for relative in view.find("**/serial*.log"):
        text = view.read_text(relative)
        if not text.strip():
            continue
        hits = {literal: _marker_hits(text, literal) for literal in literals}
        if all(hits.values()):
            return Observation(
                "p.edk2_firmware_boot",
                True,
                "the firmware reached the interactive UEFI Shell in %s" % relative,
                refs=(_ref(view, relative, "serial_log"),),
                facts={"log": relative, "markers": sorted(hits)},
            )
    return Observation(
        "p.edk2_firmware_boot",
        False,
        "no serial log records the interactive UEFI Shell prompt",
    )


@_probe("p.esp_efi_chain", "a real QEMU run mounted the FAT ESP and started BOOTAA64.EFI")
@_probe("p.esp_efi_chain", "the ESP to BOOTAA64.EFI chain ran, and whether DarwinBoot handed off")
def _probe_esp_efi_chain(view: HostView) -> Observation:
    """docs/implementation-plan.md Tier-1 table: "ESP -> BOOTAA64.EFI -> 访客代码".

    The real firmware enumerated virtual PCI, found virtio-blk, mounted the FAT
    ESP and started BOOTAA64.EFI.  What that log then shows depends on which
    payload the ESP carried, and this probe used to stop at the first log whose
    payload printed a name -- which was an old experiment's *test* payload
    (``MiniEfi: alive``), so ``darwinboot`` came out False on a host where
    DarwinBoot had since run for real.  The veto on PRD 209 ("仅打印组件名称")
    reads that fact, so a stale selection turned a real handoff into a refusal.

    It now prefers a log carrying the handoff line itself.  The literal is
    ``DARWINBOOT: hand-off to the kernel`` and **not** the bare ``DARWINBOOT:``
    an earlier version of this probe used: ``_marker_hits`` is a per-line
    substring test, so the short form is satisfied by ``Error: DARWINBOOT: image
    not found`` or by a comment mentioning the loader -- and a mention is exactly
    what PRD 209's veto exists to refuse.  Measured: with the short form, a log
    containing only that error line set ``darwinboot`` True and suppressed the
    veto, where the previous probe correctly recorded False.  The handoff line
    cannot be produced by anything but a handoff.
    """
    best: Optional[Observation] = None
    for relative in view.find("**/serial*.log"):
        text = view.read_text(relative)
        if not text.strip():
            continue
        load_at = _marker_hits(text, "BdsDxe: loading Boot0001")
        if not load_at:
            continue
        handoff_at = _marker_hits(text, "DARWINBOOT: hand-off to the kernel")
        miniefi_at = _marker_hits(text, "MiniEfi:")
        if not (handoff_at or miniefi_at):
            continue
        observation = Observation(
            "p.esp_efi_chain",
            True,
            "the ESP to BOOTAA64.EFI chain ran in %s; %s at line %d"
            % (
                relative,
                "DarwinBoot handed off to the kernel" if handoff_at
                else "the test payload printed",
                handoff_at or miniefi_at,
            ),
            refs=(_ref(view, relative, "serial_log"),),
            facts={
                "log": relative,
                "payload_line": handoff_at or miniefi_at,
                "payload_literal": "DARWINBOOT: hand-off to the kernel" if handoff_at
                else "MiniEfi: alive",
                "darwinboot": handoff_at > 0,
            },
        )
        if handoff_at:
            return observation
        if best is None:
            best = observation
    if best is not None:
        return best
    return Observation(
        "p.esp_efi_chain",
        False,
        "no serial log records the ESP to BOOTAA64.EFI chain",
    )


@_probe("p.el1_entry", "a real run entered the kernel image and dropped to EL1")
def _probe_el1_entry(view: HostView) -> Observation:
    """docs/implementation-plan.md Tier-1 table: "免固件进入 kernelcache EL1 入口"
    is VERIFIED-ON-HOST -- CurrentEL=0x4 (EL2) dropping to EL1, with the trace
    recording the first Undefined Instruction and its ELR.  The same table states
    the boundary in its own words: this proves the CPU entered kernel code and
    does not prove kernel initialisation.  The probe hard-codes that boundary as
    ``initialised: False`` so no downstream check can borrow it.
    """
    candidates = [
        relative
        for relative in view.find("**/run.log")
        if "stock-entry" in relative
    ]
    for relative in candidates:
        text = view.read_text(relative)
        if "CurrentEL=0x0000000000000004" not in text:
            continue
        directory = relative.rsplit("/", 1)[0]
        trace = directory + "/trace.log"
        trace_text = view.read_text(trace) if (view.repo_root / trace).is_file() else ""
        exception_at = _marker_hits(trace_text, "Taking exception 1 [Undefined Instruction]")
        el1_at = _marker_hits(trace_text, "to EL1 PC 0x")
        if not (exception_at and el1_at):
            continue
        refs = [_ref(view, relative, "serial_log")]
        if trace_text:
            refs.append(_ref(view, trace, "trace_log"))
        return Observation(
            "p.el1_entry",
            True,
            "CurrentEL=0x4 dropped to EL1 in %s; the first block is at trace.log:%d"
            % (relative, exception_at),
            refs=tuple(refs),
            facts={
                "run_log": relative,
                "trace_log": trace,
                "el1_pc_line": el1_at,
                "exception_line": exception_at,
                "initialised": False,
            },
        )
    return Observation(
        "p.el1_entry",
        False,
        "no run records entering the kernel image from EL2",
    )


@_probe(
    "p.real_run_directory",
    "a QEMU run directory holds both a non-empty serial log and a control-port log",
)
def _probe_real_run_directory(view: HostView) -> Observation:
    """The signature of a real QEMU run: it left a serial log *and* a QMP log.

    A directory with only a manifest or a stray text file is scratch, not a run,
    so it is not allowed to stand in for "we ran QEMU".
    """
    serial_logs = {
        relative.rsplit("/", 1)[0]
        for relative in view.find("**/serial*.log")
        if (view.repo_root / relative).is_file()
        and (view.repo_root / relative).stat().st_size > 0
    }
    control_logs = {
        relative.rsplit("/", 1)[0]
        for relative in sorted(set(view.find("**/qmp*.log")) | set(view.find("**/*qmp*.log")))
        if (view.repo_root / relative).is_file()
        and (view.repo_root / relative).stat().st_size > 0
    }
    both = sorted(serial_logs & control_logs)
    if both:
        directory = both[0]
        refs = tuple(
            _ref(view, member, "serial_log")
            for member in sorted(view.find(directory + "/*.log"))
        )
        return Observation(
            "p.real_run_directory",
            True,
            "run directory %s holds a serial log and a control-port log" % directory,
            refs=refs,
            facts={"directory": directory},
        )
    return Observation(
        "p.real_run_directory",
        False,
        "no run directory holds both a non-empty serial log and a control-port log",
    )


@_probe("p.qmp_control_log", "a control-port (QMP) log with a real greeting or query answer")
def _probe_qmp_control_log(view: HostView) -> Observation:
    for relative in sorted(set(view.find("**/qmp*.log")) | set(view.find("**/*qmp*.log"))):
        text = view.read_text(relative)
        if not text.strip():
            continue
        if '"QMP"' in text or "query-status" in text or '"return"' in text:
            return Observation(
                "p.qmp_control_log",
                True,
                "control-port log %s carries a QMP exchange" % relative,
                refs=(_ref(view, relative, "qmp_log"),),
                facts={"log": relative},
            )
    return Observation(
        "p.qmp_control_log",
        False,
        "no control-port log carries a QMP exchange",
    )


@_probe("p.screendump", "a timestamped screendump artifact exists")
def _probe_screendump(view: HostView) -> Observation:
    hits: set[str] = set()
    for pattern in ("**/*.ppm", "**/screenshot*.png", "**/screendump*", "**/screenshots/*"):
        hits.update(view.find(pattern))
    hits = {hit for hit in hits if (view.repo_root / hit).is_file()}
    return Observation(
        "p.screendump",
        bool(hits),
        "screendumps=%d" % len(hits),
        refs=tuple(_ref(view, relative, "screenshot") for relative in sorted(hits)),
        facts={"count": len(hits), "paths": sorted(hits)},
    )


@_probe("p.failure_forensics", "a verifier record that refused a gate for incomplete evidence")
def _probe_failure_forensics(view: HostView) -> Observation:
    for relative in view.find("**/verification.json"):
        try:
            data = json.loads(view.read_text(relative))
        except json.JSONDecodeError:
            continue
        reasons = [str(item) for item in (data.get("failure_reasons") or [])]
        if data.get("passed") is False and any("evidence_incomplete" in item for item in reasons):
            return Observation(
                "p.failure_forensics",
                True,
                "%s refused the gate: %s" % (relative, ";".join(reasons[:3])),
                refs=(_ref(view, relative, "verification_json"),),
                facts={"log": relative, "reasons": reasons},
            )
    return Observation(
        "p.failure_forensics",
        False,
        "no verification.json records a refusal for incomplete evidence",
    )


@_probe("p.first_block_record", "the first real block is recorded and reproducible")
def _probe_first_block_record(view: HostView) -> Observation:
    for relative in view.find("**/trace.log"):
        text = view.read_text(relative)
        exception_at = _marker_hits(text, "Taking exception")
        if exception_at:
            return Observation(
                "p.first_block_record",
                True,
                "%s records the first block at line %d" % (relative, exception_at),
                refs=(_ref(view, relative, "trace_log"),),
                facts={"log": relative, "line": exception_at},
            )
    return Observation(
        "p.first_block_record",
        False,
        "no trace log records a first block",
    )


@_probe("p.next_hypothesis", "the next experiment hypothesis is written down")
def _probe_next_hypothesis(view: HostView) -> Observation:
    for relative in ("docs/kernel-build-windows.md", "docs/implementation-plan.md"):
        if not (view.repo_root / relative).is_file():
            continue
        text = view.read_text(relative)
        if "Q5" in text or "hypothes" in text.lower() or "阻断" in text:
            return Observation(
                "p.next_hypothesis",
                True,
                "the next-step ordering is recorded in %s" % relative,
                refs=(_ref(view, relative, "manifest"),),
                facts={"document": relative},
            )
    return Observation(
        "p.next_hypothesis",
        False,
        "no recorded next experiment hypothesis",
    )


#: The symbol ``emit_prelink.py`` requires and the stock kernelcache lacks.  The
#: UniXNU patch ``xnu-10063.141.1-sparse-index.patch`` adds it to ``kalloc.c``, so
#: a UniXNU-built kernel carries it by construction -- which is exactly why this
#: string separates "a kernel we built" from "the kernel Apple shipped".
_SPARSE_INDEX_SYMBOL = b"_kalloc_sparse_prelink_index_version"

#: Where the extracted stock kernelcache lives, used only to recognise a staging
#: copy of it.  ``None`` when it is absent, and the probe says so.
_STOCK_KERNELCACHE = "local-private/out/lzfse/kernelcache-arm64.macho"

#: Bytes read per chunk when looking for the symbol.  The scan is chunked rather
#: than one ``read()`` so a 52 MB image costs 52 MB of I/O and a bounded amount
#: of memory, and the chunks overlap by one byte less than the needle so a symbol
#: straddling a boundary is still found.
_SCAN_CHUNK_BYTES = 4 << 20


def _has_sparse_index_symbol(path: Path) -> bool:
    """Whether the whole file carries the symbol, not merely its tail.

    This used to read the last 8 MiB, on the reasoning that the Mach-O string
    table lives in ``__LINKEDIT`` at the end of the image.  That is true of a
    *linked* image and false of the prelinked kernel this host boots: its symbol
    sits at offset 17,058,661 of 51,859,456 bytes, and an 8 MiB tail window
    starts at 43,470,848 -- so the scan reported "no kalloc sparse-index symbol"
    for a kernel that has one.  The rejection was a false negative, and it was
    indistinguishable from a real one because both produce the same slug.

    Measured 2026-10-25: the stock kernelcache has zero occurrences anywhere in
    its 46,415,872 bytes, so *its* rejection was correct and stays correct.  Only
    the window was wrong, and the fix is to scan everything.
    """
    needle = _SPARSE_INDEX_SYMBOL
    carry = len(needle) - 1
    try:
        with path.open("rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(_SCAN_CHUNK_BYTES)
                if not chunk:
                    return False
                # ``tail`` is at most ``len(needle) - 1`` bytes, so this
                # concatenation is one extra chunk-sized copy per iteration and
                # nothing more; it is what keeps a symbol that straddles a
                # boundary from being missed.
                if needle in tail + chunk:
                    return True
                tail = chunk[-carry:] if carry else b""
    except OSError:
        return False


@_probe("p.unixnu_kernel_image", "a UniXNU kernel image built from the pinned sources exists")
def _probe_unixnu_kernel_image(view: HostView) -> Observation:
    """A *built* UniXNU kernel, never the Apple kernelcache.

    ``emit_prelink.py`` ends with ``output = bytearray(native_data)`` -- the tool
    cannot write a kernel byte -- and the stock kernelcache fails four of its own
    gates: it lacks ``_kalloc_sparse_prelink_index_version``, it lacks
    ``_native_prelink_security_init_version``, its prelink segment is non-empty
    where ``append_segment`` requires an empty placeholder, and its rebased
    components collide with the native segment.  So a kernel image counts here
    only when it carries the symbol the patch adds.

    There used to be a second rejection here -- "byte-identical to the stock
    kernelcache" -- and it was removed because it cannot fire.  To reach it a
    candidate must already carry ``_kalloc_sparse_prelink_index_version``, and the
    stock kernelcache carries it zero times anywhere in its 46,415,872 bytes, so
    no copy of it can get that far: the symbol check strictly dominates it.  It
    read as a second structural protection while being unreachable, which is the
    kind of claim this module is supposed not to make.  The name and provenance
    guards below it *are* reachable and are tested.
    """
    stock_path = view.repo_root / _STOCK_KERNELCACHE
    stock_sha = view.sha256(_STOCK_KERNELCACHE) if stock_path.is_file() else None
    stock_size = stock_path.stat().st_size if stock_path.is_file() else None

    accepted: list[str] = []
    rejected: list[str] = []
    refs: list[EvidenceRef] = []
    candidates = sorted(
        set(view.find("**/*.macho")) | set(view.find("**/mach.development*"))
    )
    for relative in candidates:
        path = view.repo_root / relative
        lowered = relative.lower()
        if "kernelcache" in lowered or "/lzfse/" in lowered:
            rejected.append(relative + " (named kernelcache)")
            continue
        if "unixnu" not in lowered and "build" not in lowered:
            rejected.append(relative + " (no build provenance)")
            continue
        size = path.stat().st_size if path.is_file() else 0
        if size <= 0:
            rejected.append(relative + " (empty)")
            continue
        if not _has_sparse_index_symbol(path):
            rejected.append(relative + " (no kalloc sparse-index symbol)")
            continue
        accepted.append(relative)
        refs.append(_ref(view, relative, "artifact"))
    if refs:
        return Observation(
            "p.unixnu_kernel_image",
            True,
            "a UniXNU kernel image is present: %s" % ", ".join(ref.path for ref in refs[:3]),
            refs=tuple(refs),
            facts={"images": [ref.path for ref in refs], "rejected": rejected},
        )
    return Observation(
        "p.unixnu_kernel_image",
        False,
        "no built UniXNU kernel image: %d candidate(s) rejected (%s); the stock "
        "kernelcache fails emit_prelink.py and cannot substitute"
        % (len(rejected), "; ".join(rejected[:3]) or "none"),
        facts={"rejected": rejected, "stock_sha256_known": bool(stock_sha)},
    )


@_probe("p.archived_ipa_samples", "the two PRD 5.1 sample IPAs exist as archived binaries")
def _probe_archived_ipa_samples(view: HostView) -> Observation:
    wanted = ("HelloUniPad.ipa", "UniPadNotes.ipa")
    found: set[str] = set()
    for name in wanted:
        found.update(view.find("**/%s" % name))
    names = {path.rsplit("/", 1)[-1] for path in found}
    missing = [name for name in wanted if name not in names]
    return Observation(
        "p.archived_ipa_samples",
        not missing,
        "archived=%s missing=%s" % (",".join(sorted(found)) or "none", ",".join(missing) or "none"),
        refs=tuple(_ref(view, relative, "artifact") for relative in sorted(found)),
        facts={"found": sorted(found), "missing": missing},
    )


@_probe("p.install_record", "install records produced by the real installer")
def _probe_install_record(view: HostView) -> Observation:
    refs: list[EvidenceRef] = []
    stages: list[str] = []
    for relative in view.find("**/install.json"):
        try:
            data = json.loads(view.read_text(relative))
        except json.JSONDecodeError:
            continue
        stages.append(str(data.get("stage", "")))
        refs.append(_ref(view, relative, "verification_json"))
    copy_only = [stage for stage in stages if stage in ("received", "checked", "transferred")]
    return Observation(
        "p.install_record",
        any(stage in ("installed", "registered") for stage in stages),
        "records=%d stages=%s" % (len(stages), ",".join(stages) or "none"),
        refs=tuple(refs),
        facts={"stages": stages, "copy_only_stages": copy_only},
    )


# --- guest-side channels --------------------------------------------------


def _guest_records(view: HostView) -> list[tuple[str, int, dict[str, Any], bool]]:
    """Every guest-side record as ``(path, line, payload, simulation)``.

    Only guest channels count.  A serial log is not a guest channel (PRD 7.4), and
    a record produced under a simulation path is flagged so it can never satisfy an
    ``onsite`` requirement.
    """
    records: list[tuple[str, int, dict[str, Any], bool]] = []
    index = view.basenames()
    matched: set[str] = set()
    for pattern in _GUEST_LOG_GLOBS:
        tail = pattern[3:] if pattern.startswith("**/") else pattern
        for name in index:
            if fnmatch.fnmatch(name, tail):
                matched.update(index[name])
    for relative in sorted(matched):
        text = view.read_text(relative)
        if not text.strip():
            continue
        simulation = _is_simulation_path(relative)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            records.append((relative, 1, payload, simulation))
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            parsed: Any = None
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                records.append((relative, lineno, parsed, simulation))
            elif ":" in stripped:
                key, _, value = stripped.partition(":")
                records.append(
                    (relative, lineno, {key.strip(): value.strip()}, simulation)
                )
    return records


def _guest_events(view: HostView) -> dict[str, tuple[str, int, dict[str, Any], bool]]:
    """event id -> (path, line, payload, simulation) for every guest record.

    Memoised per view: every guest probe asks this question, and the scan walks
    the whole evidence tree.
    """
    cached = view._cache.get("__guest_events__")
    if cached is not None:
        return cached  # type: ignore[return-value]
    events: dict[str, tuple[str, int, dict[str, Any], bool]] = {}
    for relative, lineno, payload, simulation in _guest_records(view):
        event = str(payload.get("event", ""))
        if not event:
            continue
        events.setdefault(event, (relative, lineno, payload, simulation))
    view._cache["__guest_events__"] = events
    return events


def _guest_probe(probe_id: str, description: str, events: Sequence[str]) -> None:
    """Build a probe that requires every named guest event, from a guest channel."""

    @_probe(probe_id, description)
    def run(view: HostView) -> Observation:
        found = _guest_events(view)
        present: list[str] = []
        missing: list[str] = []
        refs: list[EvidenceRef] = []
        for event in events:
            hit = found.get(event)
            if hit is None:
                missing.append(event)
                continue
            relative, lineno, _payload, simulation = hit
            digest = view.sha256(relative)
            present.append(event)
            refs.append(
                EvidenceRef(
                    path=relative,
                    kind="guest_log",
                    sha256=digest,
                    digest_reason=None if digest else "unreadable",
                    scope=EvidenceScope.SIMULATION if simulation else EvidenceScope.ONSITE,
                    witness=None if simulation else "%s:%d:%s" % (relative, lineno, event),
                )
            )
        return Observation(
            probe_id,
            not missing,
            "guest events present=%s missing=%s"
            % (",".join(present) or "none", ",".join(missing) or "none"),
            refs=tuple(refs),
            facts={
                "present": present,
                "missing": missing,
                "simulated": {
                    event: found[event][3] for event in present if found[event][3]
                },
            },
        )

    run.__name__ = "_probe_" + probe_id.replace(".", "_").replace("-", "_")


_guest_probe("p.guest_display", "the guest itself displayed something", ("guest.display",))
_guest_probe("p.unixnu_config_read", "UniXNU read its configuration", ("unixnu.config_read",))
_guest_probe("p.unixnu_boot_entry", "UniXNU boot entry evidence", ("unixnu.boot_entry",))
_guest_probe("p.unixnu_load_evidence", "UniXNU load evidence", ("unixnu.load",))
_guest_probe(
    "p.darwinboot_input_validation",
    "DarwinBoot validated its inputs",
    ("darwinboot.input_validation",),
)
_guest_probe(
    "p.darwinboot_load_info",
    "DarwinBoot recorded load information",
    ("darwinboot.load_info",),
)
_guest_probe(
    "p.darwinboot_handoff",
    "the DarwinBoot handoff location is traceable",
    ("darwinboot.handoff",),
)
_guest_probe("p.xnu_page_tables", "XNU page tables established", ("xnu.page_tables",))
_guest_probe(
    "p.xnu_exception_state", "XNU exception vectors installed", ("xnu.exception_vectors",)
)
_guest_probe(
    "p.xnu_interrupt_state", "the XNU interrupt controller is dispatching", ("xnu.interrupts",)
)
_guest_probe("p.iokit_service_match", "IOKit service matching evidence", ("iokit.service_match",))
_guest_probe("p.iokit_driver_probe", "IOKit driver probe evidence", ("iokit.driver_probe",))
_guest_probe(
    "p.virtio_blk_read_back",
    "the guest read known content back from VirtIO block",
    ("virtio_blk.read_back",),
)
_guest_probe(
    "p.system_volume_identity", "guest-reported volume identity", ("system.volume_identity",)
)
_guest_probe("p.system_mount", "guest mount relations", ("system.mount",))
_guest_probe(
    "p.dyld_cache_read", "a guest process read the dyld shared cache", ("dyld.cache_read",)
)
_guest_probe("p.launchd_running", "the original launchd is running", ("launchd.running",))
_guest_probe("p.daemon_spawned", "later daemons started in order", ("daemon.spawned",))
_guest_probe("p.springboard_running", "SpringBoard is running", ("springboard.running",))
_guest_probe(
    "p.springboard_interaction",
    "mouse input changed the guest interface",
    ("springboard.interaction",),
)
_guest_probe("p.uikit_first_frame", "the test app first frame", ("uikit.first_frame",))
_guest_probe("p.uikit_update", "the test app updates its frame", ("uikit.update",))
_guest_probe("p.uikit_input_feedback", "the test app answers input", ("uikit.input_feedback",))
_guest_probe("p.data_written", "the guest wrote data", ("data.written",))
_guest_probe("p.data_shutdown", "the guest shut down normally", ("data.shutdown",))
_guest_probe(
    "p.data_read_back", "a fresh QEMU process read the data back", ("data.read_back",)
)
_guest_probe("p.install_registered", "the guest registered the install", ("install.registered",))
_guest_probe(
    "p.install_trust", "the guest reported how it trusts the package", ("install.trust",)
)
_guest_probe(
    "p.install_icon", "SpringBoard shows the real icon and bundle id", ("install.icon",)
)
_guest_probe("p.policy_signature", "the guest checked the signature", ("policy.signature",))
_guest_probe(
    "p.policy_entitlements", "the guest checked entitlements", ("policy.entitlements",)
)
_guest_probe(
    "p.policy_failure_reason",
    "a policy rejection carries a traceable reason",
    ("policy.failure_reason",),
)
_guest_probe("p.app_dyld", "dyld found the dependencies", ("app.dyld",))
_guest_probe("p.app_process", "the app process exists", ("app.process",))
_guest_probe("p.app_window", "the app window is visible", ("app.window",))
_guest_probe(
    "p.app_challenge", "the app shows this run challenge value", ("app.challenge",)
)
_guest_probe("p.input_tap", "a tap reached the app", ("input.tap",))
_guest_probe("p.input_drag", "a drag reached the app", ("input.drag",))
_guest_probe("p.input_home", "the Home affordance worked", ("input.home",))
_guest_probe("p.input_text", "text entry reached the app", ("input.text",))
@_probe(
    "p.input_delivered",
    "a host input journal records that events were delivered to the guest",
)
def _probe_input_delivered(view: HostView) -> Observation:
    """The host half of the PRD 221 pair.

    ``宿主窗口收到按键但客体无响应`` needs both halves: the host must have
    delivered something, and the guest must have answered.  Delivery is a
    host-side fact, so it is read from a host input journal -- not from a guest
    channel, which would beg the question.
    """
    refs: list[EvidenceRef] = []
    delivered = 0
    for relative in sorted(set(view.find("**/input-journal*.json")) | set(view.find("**/*input*.json"))):
        try:
            data = json.loads(view.read_text(relative))
        except json.JSONDecodeError:
            continue
        events = data.get("events") if isinstance(data, dict) else data
        if not isinstance(events, list):
            continue
        count = sum(
            1
            for event in events
            if isinstance(event, dict) and str(event.get("state", "delivered")) == "delivered"
        )
        if count:
            delivered += count
            refs.append(_ref(view, relative, "probe_log"))
    return Observation(
        "p.input_delivered",
        bool(delivered),
        "delivered input events=%d" % delivered,
        refs=tuple(refs),
        facts={"delivered": delivered},
    )


# ---------------------------------------------------------------------------
# 5. Predicates: the named conditions the vetoes and blockers read
# ---------------------------------------------------------------------------

_PREDICATES: dict[str, Callable[[Mapping[str, Observation]], bool]] = {}


def _predicate(predicate_id: str) -> Callable[..., Any]:
    def register(function: Callable[[Mapping[str, Observation]], bool]) -> Any:
        _PREDICATES[predicate_id] = function
        return function

    return register


# -- blockers ---------------------------------------------------------------


@_predicate("lock_file_readable")
def _lock_file_readable(snapshot: Mapping[str, Observation]) -> bool:
    return _ok(snapshot, "p.source_pins") and _fact(snapshot, "p.source_pins", "problems", ["x"]) == []


@_predicate("lock_frozen")
def _lock_frozen(snapshot: Mapping[str, Observation]) -> bool:
    return bool(_fact(snapshot, "p.source_pins", "frozen", False))


@_predicate("unixnu_kernel_image")
def _unixnu_kernel_image(snapshot: Mapping[str, Observation]) -> bool:
    return _ok(snapshot, "p.unixnu_kernel_image")


@_predicate("xnu_initialisation")
def _xnu_initialisation(snapshot: Mapping[str, Observation]) -> bool:
    """XNU got past its entry: page tables, exception vectors and interrupts."""
    return (
        _onsite_ok(snapshot, "p.xnu_page_tables")
        and _onsite_ok(snapshot, "p.xnu_exception_state")
        and _onsite_ok(snapshot, "p.xnu_interrupt_state")
    )


@_predicate("guest_install_service")
def _guest_install_service(snapshot: Mapping[str, Observation]) -> bool:
    return _onsite_ok(snapshot, "p.install_registered")


@_predicate("system_volume_mount")
def _system_volume_mount(snapshot: Mapping[str, Observation]) -> bool:
    return _onsite_ok(snapshot, "p.system_mount")


@_predicate("guest_boot_chain")
def _guest_boot_chain(snapshot: Mapping[str, Observation]) -> bool:
    """The whole chain a guest display needs: a kernel image plus XNU up."""
    return _unixnu_kernel_image(snapshot) and _xnu_initialisation(snapshot)


# -- vetoes -----------------------------------------------------------------


@_predicate("only_docs_scripts_or_downloads")
def _only_docs_scripts_or_downloads(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 206: "README 可读、脚本返回 0、仅下载二进制" is not a pass."""
    measurable = (
        _ok(snapshot, "p.clean_build_manifest")
        or _ok(snapshot, "p.ai_interface_smoke")
        or (
            _ok(snapshot, "p.source_pins")
            and _ok(snapshot, "p.pinned_qemu")
            and _ok(snapshot, "p.pinned_firmware")
        )
    )
    return not measurable


@_predicate("qemu_window_without_firmware_or_guest")
def _qemu_window_without_firmware_or_guest(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 207: "只运行出 QEMU 窗口" -- a window, no firmware log, no guest."""
    return (
        _ok(snapshot, "p.real_run_directory")
        and not _ok(snapshot, "p.edk2_firmware_boot")
        and not _ok(snapshot, "p.guest_display")
    )


@_predicate("static_image_without_guest_log")
def _static_image_without_guest_log(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 208: "仅出现静态图片"."""
    return _ok(snapshot, "p.screendump") and not _observed_guest_events(snapshot)


@_predicate("payload_name_print_only")
def _payload_name_print_only(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 209: "仅打印组件名称".

    The chain that ran printed a payload name and nothing that traces a handoff.
    That is exactly this veto, and it is observable on this host today.
    """
    chain = snapshot.get("p.esp_efi_chain")
    if chain is None or not chain.ok:
        return False
    return not bool(chain.facts.get("darwinboot")) and not _ok(
        snapshot, "p.darwinboot_handoff"
    )


@_predicate("single_banner_only")
def _single_banner_only(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 210: "单行 kernel banner"."""
    kernelish = {
        event
        for event in ("xnu.page_tables", "xnu.exception_vectors", "xnu.interrupts")
        if event in _observed_guest_events(snapshot)
    }
    return bool(kernelish) and not _ok(snapshot, "p.xnu_page_tables")


@_predicate("enumeration_without_working_evidence")
def _enumeration_without_working_evidence(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 211: "无法说明设备是否真正工作"."""
    matched = "iokit.service_match" in _observed_guest_events(snapshot)
    return matched and not _ok(snapshot, "p.iokit_driver_probe")


@_predicate("device_enumeration_without_readback")
def _device_enumeration_without_readback(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 212: "只枚举设备"."""
    enumerated = any(
        event.startswith("virtio_blk.") and event != "virtio_blk.read_back"
        for event in _observed_guest_events(snapshot)
    )
    return enumerated and not _ok(snapshot, "p.virtio_blk_read_back")


@_predicate("mockfs_process_only")
def _mockfs_process_only(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 213: "只启动 mockfs 测试进程"."""
    mockish = any(
        "mockfs" in event or "test_process" in event for event in _observed_guest_events(snapshot)
    )
    return mockish and not _ok(snapshot, "p.system_mount")


@_predicate("mockfs_pid1_as_launchd")
def _mockfs_pid1_as_launchd(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 214 / appendix A: a mockfs PID 1 test program is not system launchd."""
    observation = snapshot.get("p.launchd_running")
    if observation is None or not observation.ok:
        return False
    simulated = bool(observation.facts.get("simulated"))
    return simulated or not _ok(snapshot, "p.daemon_spawned")


@_predicate("homescreen_screenshot_only")
def _homescreen_screenshot_only(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 215: "只展示主屏幕截图"."""
    return _ok(snapshot, "p.screendump") and not _ok(snapshot, "p.springboard_interaction")


@_predicate("wallpaper_only")
def _wallpaper_only(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 216: "只显示桌面壁纸"."""
    return _ok(snapshot, "p.screendump") and not _ok(snapshot, "p.uikit_first_frame")


@_predicate("ram_disk_only")
def _ram_disk_only(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 217: "仅 RAM disk 内有效" -- written, never read back from disk."""
    return _ok(snapshot, "p.data_written") and not _ok(snapshot, "p.data_read_back")


@_predicate("copy_only_install")
def _copy_only_install(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 218: "仅解压或复制 `.app`" -- a copy stage is not an install."""
    return bool(_fact(snapshot, "p.install_record", "copy_only_stages", []))


@_predicate("no_drm_means_allowed")
def _no_drm_means_allowed(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 219: "假定无 DRM 就一定允许执行"."""
    events = _observed_guest_events(snapshot)
    allowed = "policy.allow" in events or "policy.allowed" in events
    return allowed and not _ok(snapshot, "p.policy_signature")


@_predicate("process_then_crash")
def _process_then_crash(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 220: "进程瞬间存在但随后崩溃"."""
    events = _observed_guest_events(snapshot)
    return "app.process" in events and any(
        event in events for event in ("app.crash", "app.exit", "dyld.failed")
    )


@_predicate("host_key_without_guest_response")
def _host_key_without_guest_response(snapshot: Mapping[str, Observation]) -> bool:
    """PRD 221: "宿主窗口收到按键但客体无响应"."""
    return _ok(snapshot, "p.input_delivered") and not any(
        _ok(snapshot, probe)
        for probe in ("p.input_tap", "p.input_drag", "p.input_home", "p.input_text")
    )


# ---------------------------------------------------------------------------
# 6. The registry: one check per milestone
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRequirement:
    """One thing the PRD pass-evidence column demands, and where it must come from."""

    evidence_id: str
    description: str
    scope: str
    probe: str

    def __post_init__(self) -> None:
        if self.probe not in _PROBES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "evidence requirement names an unknown probe",
                evidence_id=self.evidence_id,
                probe=self.probe,
            )
        if self.scope not in _SCOPES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "evidence requirement has an unknown scope",
                evidence_id=self.evidence_id,
                scope=self.scope,
            )


@dataclass(frozen=True)
class Refusal:
    """The PRD "not enough to pass" column, kept verbatim and made live."""

    refusal_id: str
    prd_citation: str
    text: str
    trigger: str

    def __post_init__(self) -> None:
        if self.trigger not in _PREDICATES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "refusal names an unknown trigger predicate",
                refusal_id=self.refusal_id,
                trigger=self.trigger,
            )
        if not self.text.strip():
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a refusal must quote the PRD veto column",
                refusal_id=self.refusal_id,
            )


@dataclass(frozen=True)
class Blocker:
    """A named structural precondition.  Unmet means BLOCKED, never PASS."""

    blocker_id: str
    reason: str
    satisfied_by: str

    def __post_init__(self) -> None:
        if self.satisfied_by not in _PREDICATES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "blocker names an unknown predicate",
                blocker_id=self.blocker_id,
                satisfied_by=self.satisfied_by,
            )


@dataclass(frozen=True)
class MilestoneCheck:
    """One milestone: PRD goal, PRD pass evidence, PRD veto, named blockers."""

    check_id: str
    milestone: str
    gate_id: str | None
    tier: str
    prd_citation: str
    goal: str
    implementation: str
    pass_evidence: tuple[EvidenceRequirement, ...]
    refusals: tuple[Refusal, ...]
    blockers: tuple[Blocker, ...]
    notes: tuple[str, ...] = ()
    field_unverified: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ids = [item.evidence_id for item in self.pass_evidence]
        if len(set(ids)) != len(ids):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "duplicate evidence requirement id",
                check_id=self.check_id,
            )
        if not self.pass_evidence:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "a milestone check must declare at least one pass-evidence requirement",
                check_id=self.check_id,
            )
        if self.tier not in ("blocking", "workpackage"):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown tier",
                check_id=self.check_id,
                tier=self.tier,
            )


def _req(evidence_id: str, description: str, scope: str, probe: str) -> EvidenceRequirement:
    return EvidenceRequirement(
        evidence_id=evidence_id, description=description, scope=scope, probe=probe
    )


_HOST = EvidenceScope.HOST_REAL
_ONSITE = EvidenceScope.ONSITE

#: The four-field vocabulary this module emits.  These are the **canonical**
#: words, imported from :mod:`unipad.acceptance.record` rather than re-declared
#: here (F9).
#:
#: This module used to speak a vocabulary of its own -- ``PASS`` /
#: ``UNVERIFIED`` / ``not-applicable`` in the Windows column -- so the same
#: milestone read ``PASS`` here and ``SIMULATION PASS`` in ``runner.py`` and
#: ``e2e.py``, and a reader diffing two rows could not tell whether those were
#: the same claim.  The Windows column reports the host-provable half of a
#: milestone, so it can be ``SIMULATION PASS`` while the on-site column is
#: ``BLOCKED`` -- that separation is the point, and it survives the rename
#: because the canonical word says *simulation* where the local word said a
#: bare ``PASS``.
#:
#: The local words still exist as the documented aliases in
#: :data:`unipad.acceptance.record.WINDOWS_STATUS_ALIASES`;
#: :func:`unipad.acceptance.record.canonical_windows_status` maps them and
#: refuses anything else, so a new local word cannot reach a bundle
#: unnormalised.
#:
#: ``ONSITE_STATUSES`` needs no alias: every module already emitted
#: ``PASS`` / ``BLOCKED`` / ``UNVERIFIED`` there.
WINDOWS_STATUSES = WINDOWS_STATUSES
ONSITE_STATUSES = ONSITE_STATUSES

_KERNEL_BLOCKER = Blocker(
    blocker_id="built_unixnu_kernel_image",
    reason=(
        "no UniXNU kernel image built from the pinned sources exists on this host. "
        "This used to be true and was recorded as the reason several milestones were "
        "BLOCKED; it is no longer true -- a prelinked UniXNU kernel with the "
        "admission symbols emit_prelink.py requires is present and boots, and the "
        "probe below is what found it.  It stays declared because the mechanism is "
        "the point: the moment such an image is absent again, every milestone that "
        "names this blocker goes back to BLOCKED with a reason, rather than quietly "
        "becoming UNVERIFIED"
    ),
    satisfied_by="unixnu_kernel_image",
)

_XNU_BLOCKER = Blocker(
    blocker_id="xnu_initialised",
    reason=(
        "XNU has not initialised on this host: the firmware-free route reached the "
        "kernelcache EL1 entry and stopped on its first undefined instruction, which "
        "proves the CPU entered kernel code and proves nothing about page tables, "
        "exception vectors, the interrupt controller or the clock"
    ),
    satisfied_by="xnu_initialisation",
)

_VOLUME_BLOCKER = Blocker(
    blocker_id="system_volume_mounted",
    reason=(
        "no writable system volume is mounted in the guest: this host has no APFS "
        "read/write implementation, so the 6.08 GB os-root raw image cannot be "
        "produced in the state the guest would see, and Data persistence has "
        "nothing to be persisted onto"
    ),
    satisfied_by="system_volume_mount",
)

_INSTALL_SERVICE_BLOCKER = Blocker(
    blocker_id="guest_install_service",
    reason=(
        "there is no real guest install service on this host: "
        "install_service_unavailable is injectable in the fake guest and has never "
        "once succeeded against a real object"
    ),
    satisfied_by="guest_install_service",
)

_FROZEN_BLOCKER = Blocker(
    blocker_id="frozen_source_baseline",
    reason=(
        "sources.lock.json still declares frozen=false, so the pinned inputs are not a "
        "frozen baseline and no clean build has been run against them"
    ),
    satisfied_by="lock_frozen",
)


CHECKS: dict[str, MilestoneCheck] = {}


def _register(check: MilestoneCheck) -> MilestoneCheck:
    if check.check_id in CHECKS:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "duplicate check id",
            check_id=check.check_id,
        )
    CHECKS[check.check_id] = check
    return check


_register(
    MilestoneCheck(
        check_id="M0",
        milestone="M0",
        gate_id="M0",
        tier="blocking",
        prd_citation="docs/PRD.md:206",
        goal="依赖盘点与构建成立",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "m0.pinned_source_identity",
                "固定提交、工具、配置: every repository, submodule, feed, the system "
                "identity and the tool chain carry a pinned digest",
                _HOST,
                "p.source_pins",
            ),
            _req(
                "m0.clean_build_artifacts",
                "清洁构建产物及哈希: a build manifest whose items carry per-item sha256 "
                "and the tool versions that produced them",
                _HOST,
                "p.clean_build_manifest",
            ),
            _req(
                "m0.ai_interface_smoke",
                "AI 接口冒烟测试: a recorded smoke run of the AI interface",
                _HOST,
                "p.ai_interface_smoke",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m0.veto.readme_scripts_downloads",
                prd_citation="docs/PRD.md:206",
                text="README 可读、脚本返回 0、仅下载二进制",
                trigger="only_docs_scripts_or_downloads",
            ),
        ),
        blockers=(
            Blocker(
                blocker_id="pinned_source_identity",
                reason="config/sources.lock.json is absent or unparseable, so there is "
                       "no pinned source identity to inventory",
                satisfied_by="lock_file_readable",
            ),
        ),
        field_unverified=("干净构建产物与哈希", "AI 接口冒烟测试（PRD:206 明列，本机无记录）"),
    )
)

# ``p.qemu_config`` composes the committed board configuration with the pinned tool
# identities, so M1's third requirement has a probe that actually runs here.
@_probe("p.qemu_config", "the committed TCG board configuration plus pinned tool digests")
def _probe_qemu_config(view: HostView) -> Observation:
    machine = _PROBES["p.machine_config"](view)
    qemu = _PROBES["p.pinned_qemu"](view)
    firmware = _PROBES["p.pinned_firmware"](view)
    return Observation(
        "p.qemu_config",
        machine.ok and qemu.ok and firmware.ok,
        "machine=%s qemu=%s firmware=%s" % (machine.ok, qemu.ok, firmware.ok),
        refs=(*machine.refs, *qemu.refs, *firmware.refs),
        facts={
            "accel": machine.facts.get("accel"),
            "geometry": machine.facts.get("geometry"),
            "qemu_version": qemu.facts.get("version"),
        },
    )




_register(
    MilestoneCheck(
        check_id="M1",
        milestone="M1",
        gate_id="M1",
        tier="blocking",
        prd_citation="docs/PRD.md:207",
        goal="Windows TCG 启动 UEFI",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "m1.firmware_boot_log",
                "固件日志: a real QEMU run whose serial log reaches an interactive "
                "UEFI Shell prompt",
                _HOST,
                "p.edk2_firmware_boot",
            ),
            _req(
                "m1.guest_display",
                "实际客体显示: the target guest own display, witnessed in a guest "
                "channel -- the EDK2 UiApp menu is the firmware screen, not the guest",
                _ONSITE,
                "p.guest_display",
            ),
            _req(
                "m1.qemu_config_evidence",
                "QEMU 配置证据: machine.json plus the pinned QEMU and firmware digests",
                _HOST,
                "p.qemu_config",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m1.veto.qemu_window_only",
                prd_citation="docs/PRD.md:207",
                text="只运行出 QEMU 窗口",
                trigger="qemu_window_without_firmware_or_guest",
            ),
        ),
        blockers=(
            Blocker(
                blocker_id="target_guest_boot_chain",
                reason="the target-guest boot chain does not exist on this host: no "
                       "guest display has been witnessed in a guest channel.  The "
                       "kernel image and XNU initialisation that used to be named "
                       "here are no longer missing -- a prelinked UniXNU kernel is "
                       "present and boots, and it initialises to the BSD stage "
                       "(implementation-plan.md fact 37) -- so naming them would "
                       "repeat the untrue reason this file was corrected for "
                       "elsewhere",
                satisfied_by="guest_boot_chain",
            ),
        ),
        field_unverified=("实际客体显示（现有截图是 EDK2 UiApp，不是 iPadOS）",),
    )
)


_register(
    MilestoneCheck(
        check_id="M2",
        milestone="M2",
        gate_id="M2",
        tier="blocking",
        prd_citation="docs/PRD.md:208",
        goal="UniXNU 运行",
        implementation="NOT STARTED",
        pass_evidence=(
            _req(
                "m2.config_read",
                "配置读取: UniXNU read its configuration, from a guest channel",
                _ONSITE,
                "p.unixnu_config_read",
            ),
            _req(
                "m2.boot_entry",
                "引导项: the UniXNU boot entry, from a guest channel",
                _ONSITE,
                "p.unixnu_boot_entry",
            ),
            _req(
                "m2.load_evidence",
                "加载证据: what UniXNU loaded, from a guest channel",
                _ONSITE,
                "p.unixnu_load_evidence",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m2.veto.static_image_only",
                prd_citation="docs/PRD.md:208",
                text="仅出现静态图片",
                trigger="static_image_without_guest_log",
            ),
        ),
        blockers=(_KERNEL_BLOCKER,),
        field_unverified=(
            "UniXNU 配置读取 / 引导项 / 加载——零证据",
            "本机不存在任何已构建的 UniXNU 内核",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M3",
        milestone="M3",
        gate_id="M3",
        tier="blocking",
        prd_citation="docs/PRD.md:209",
        goal="DarwinBoot 完成交接准备",
        implementation="PARTIAL",
        pass_evidence=(
            _req(
                "m3.efi_loaders_built",
                "the three EFI loaders are built and PE-valid (PE32+, ARM64, EFI "
                "subsystem) -- host-side, and provable on this host today",
                _HOST,
                "p.efi_loaders",
            ),
            _req(
                "m3.input_validation",
                "输入验证: DarwinBoot validated its inputs, from a guest channel",
                _ONSITE,
                "p.darwinboot_input_validation",
            ),
            _req(
                "m3.load_info",
                "加载信息: the kernelcache and prelink DarwinBoot loaded, from a guest "
                "channel",
                _ONSITE,
                "p.darwinboot_load_info",
            ),
            _req(
                "m3.handoff_location",
                "交接位置可追踪: where DarwinBoot handed off, from a guest channel",
                _ONSITE,
                "p.darwinboot_handoff",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m3.veto.component_names_only",
                prd_citation="docs/PRD.md:209",
                text="仅打印组件名称",
                trigger="payload_name_print_only",
            ),
        ),
        blockers=(_KERNEL_BLOCKER,),
        field_unverified=(
            "DarwinBoot 一行未执行；交接位置未真实追踪",
            "ESP 挂载的是 BOOTAA64.EFI（我方测试），未挂 UniXNU.efi / DarwinBoot.efi",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M4",
        milestone="M4",
        gate_id="M4",
        tier="blocking",
        prd_citation="docs/PRD.md:210",
        goal="XNU 进入并初始化关键底层",
        implementation="PARTIAL",
        pass_evidence=(
            _req(
                "m4.kernel_entry_located",
                "可定位内核执行: a real run entered the kernel image and dropped to "
                "EL1 -- provable on this host today",
                _HOST,
                "p.el1_entry",
            ),
            _req(
                "m4.page_tables",
                "页表: XNU page tables established, from a guest channel",
                _ONSITE,
                "p.xnu_page_tables",
            ),
            _req(
                "m4.exception_state",
                "异常状态: XNU exception vectors installed, from a guest channel",
                _ONSITE,
                "p.xnu_exception_state",
            ),
            _req(
                "m4.interrupt_state",
                "中断状态: the interrupt controller dispatching, from a guest channel",
                _ONSITE,
                "p.xnu_interrupt_state",
            ),
            _req(
                "m4.failure_forensics",
                "失败能取证: a recorded run where the verifier refused a gate for "
                "incomplete evidence instead of passing it",
                _HOST,
                "p.failure_forensics",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m4.veto.single_banner",
                prd_citation="docs/PRD.md:210",
                text="单行 kernel banner",
                trigger="single_banner_only",
            ),
        ),
        blockers=(_KERNEL_BLOCKER,),
        field_unverified=(
            "内核初始化（页表/异常/中断/时钟）——只到达 EL1 入口并撞一条未定义指令",
            "XNU 的 boot markers 从未在任何主机上被观测到",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M5",
        milestone="M5",
        gate_id="M5",
        tier="blocking",
        prd_citation="docs/PRD.md:211",
        goal="IOKit/平台层可用",
        implementation="NOT STARTED",
        pass_evidence=(
            _req(
                "m5.service_matching",
                "必要服务匹配: IOKit service matching, from a guest channel",
                _ONSITE,
                "p.iokit_service_match",
            ),
            _req(
                "m5.driver_probe",
                "驱动探测证据: driver probe results, from a guest channel",
                _ONSITE,
                "p.iokit_driver_probe",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m5.veto.cannot_say_devices_work",
                prd_citation="docs/PRD.md:211",
                text="无法说明设备是否真正工作",
                trigger="enumeration_without_working_evidence",
            ),
        ),
        blockers=(_XNU_BLOCKER,),
        field_unverified=("IOKit 服务匹配与驱动探测——零证据",),
    )
)

_register(
    MilestoneCheck(
        check_id="M6",
        milestone="M6",
        gate_id="M6",
        tier="blocking",
        prd_citation="docs/PRD.md:212",
        goal="VirtIO Block 可读",
        implementation="NOT STARTED",
        pass_evidence=(
            _req(
                "m6.known_content_read_back",
                "已知内容读取和校验正确: the guest read known sectors back and the "
                "content matched, from a guest channel",
                _ONSITE,
                "p.virtio_blk_read_back",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m6.veto.enumeration_only",
                prd_citation="docs/PRD.md:212",
                text="只枚举设备",
                trigger="device_enumeration_without_readback",
            ),
        ),
        blockers=(_XNU_BLOCKER,),
        field_unverified=(
            "客体侧 VirtIO block 已知内容读取与校验——零证据",
            "EDK2 枚举并读该块设备是固件的块IO驱动，不是 iPadOS 的 VirtIO 驱动",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M7",
        milestone="M7",
        gate_id="M7",
        tier="blocking",
        prd_citation="docs/PRD.md:213",
        goal="正确 RootFS/SystemOS 可用",
        implementation="NOT STARTED",
        pass_evidence=(
            _req(
                "m7.volume_identity",
                "卷身份: the guest-reported volume identity, from a guest channel",
                _ONSITE,
                "p.system_volume_identity",
            ),
            _req(
                "m7.mount_relations",
                "挂载关系: the guest mount relations, from a guest channel",
                _ONSITE,
                "p.system_mount",
            ),
            _req(
                "m7.dyld_cache_read",
                "dyld cache 读取证据: a guest process read the dyld shared cache",
                _ONSITE,
                "p.dyld_cache_read",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m7.veto.mockfs_test_process_only",
                prd_citation="docs/PRD.md:213",
                text="只启动 mockfs 测试进程",
                trigger="mockfs_process_only",
            ),
        ),
        blockers=(_XNU_BLOCKER,),
        notes=(
            "宿主侧镜像工具 PARTIAL：UDIF→raw 转换、APFS 读取器、LZFSE 解码器均已证明"
            "可产出，但那是宿主对自己的文件做分析，不是客体挂载。",
        ),
        field_unverified=(
            "客体侧卷身份 / 挂载关系 / dyld cache 读取——零证据",
            "本机无 APFS 读写实现，6.08 GB os-root raw 镜像无法以客体将看到的状态产出",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M8",
        milestone="M8",
        gate_id="M8",
        tier="blocking",
        prd_citation="docs/PRD.md:214",
        goal="原始用户态 launchd 运行",
        implementation="NOT STARTED",
        pass_evidence=(
            _req(
                "m8.original_launchd",
                "原始 launchd: the unmodified launchd binary running, from a guest "
                "channel",
                _ONSITE,
                "p.launchd_running",
            ),
            _req(
                "m8.later_daemons",
                "后续守护进程证据: the daemons that followed, in order, from a guest "
                "channel",
                _ONSITE,
                "p.daemon_spawned",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m8.veto.mockfs_pid1_as_launchd",
                prd_citation="docs/PRD.md:214",
                text="将 mockfs 中 PID 1 测试程序当成系统 launchd",
                trigger="mockfs_pid1_as_launchd",
            ),
        ),
        blockers=(_XNU_BLOCKER,),
        notes=(
            "本阶段最大的风险不是缺证据，而是未来极易把测试程序当 launchd；附录 A 与 "
            "testing-strategy D2 已把它列入「出现即判未验证」。",
        ),
        field_unverified=("原始 launchd 与后续守护进程——零证据",),
    )
)

_register(
    MilestoneCheck(
        check_id="M9",
        milestone="M9",
        gate_id="M9",
        tier="blocking",
        prd_citation="docs/PRD.md:215",
        goal="SpringBoard 可操作",
        implementation="NOT STARTED",
        pass_evidence=(
            _req(
                "m9.springboard_running",
                "进程: SpringBoard running in the guest, from a guest channel",
                _ONSITE,
                "p.springboard_running",
            ),
            _req(
                "m9.screen_and_interaction",
                "本机画面、进程及交互一致: mouse input changed the guest interface, "
                "from a guest channel",
                _ONSITE,
                "p.springboard_interaction",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m9.veto.homescreen_screenshot_only",
                prd_citation="docs/PRD.md:215",
                text="只展示主屏幕截图",
                trigger="homescreen_screenshot_only",
            ),
        ),
        blockers=(_XNU_BLOCKER,),
        field_unverified=(
            "SpringBoard 画面 / 进程 / 交互一致——零证据",
            "§5.3 性能基线一项未测，不得引用任何数字",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M9a",
        milestone="M9a",
        gate_id="M9a",
        tier="blocking",
        prd_citation="docs/PRD.md:216",
        goal="软件/最小图形路径满足 UIKit",
        implementation="PARTIAL",
        pass_evidence=(
            _req(
                "m9a.first_frame",
                "测试 App 的首帧, from a guest channel",
                _ONSITE,
                "p.uikit_first_frame",
            ),
            _req(
                "m9a.update",
                "更新, from a guest channel",
                _ONSITE,
                "p.uikit_update",
            ),
            _req(
                "m9a.input_feedback",
                "输入反馈正确, from a guest channel",
                _ONSITE,
                "p.uikit_input_feedback",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m9a.veto.wallpaper_only",
                prd_citation="docs/PRD.md:216",
                text="只显示桌面壁纸",
                trigger="wallpaper_only",
            ),
        ),
        blockers=(_XNU_BLOCKER,),
        notes=(
            "宿主侧 framebuffer 几何/旋转/音频决策代码完成并进入 argv，封闭分辨率目录"
            "拒绝而非吸附；只有 1376x1024 一个几何被钉住。",
        ),
        field_unverified=(
            "测试 App 首帧 / 更新 / 输入反馈",
            "除钉住的 1376x1024 外没有任何分辨率被证明能在真实 iPadOS 客体上正确渲染",
            "virtio-gpu 路线上旋转与分辨率到不了访客（ADR-0004）",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M9b",
        milestone="M9b",
        gate_id="M9b",
        tier="blocking",
        prd_citation="docs/PRD.md:217",
        goal="Data 持久化",
        implementation="PARTIAL",
        pass_evidence=(
            _req(
                "m9b.written",
                "写入: the guest wrote data, from a guest channel",
                _ONSITE,
                "p.data_written",
            ),
            _req(
                "m9b.normal_shutdown",
                "正常关机: the guest shut down normally, from a guest channel",
                _ONSITE,
                "p.data_shutdown",
            ),
            _req(
                "m9b.read_back",
                "全新 QEMU 进程重启读取: a fresh QEMU process read the data back",
                _ONSITE,
                "p.data_read_back",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m9b.veto.ram_disk_only",
                prd_citation="docs/PRD.md:217",
                text="仅 RAM disk 内有效",
                trigger="ram_disk_only",
            ),
        ),
        blockers=(_XNU_BLOCKER, _VOLUME_BLOCKER),
        notes=(
            "宿主侧 Data 镜像/账本/快照范围代码完成；fake guest 证明了宿主侧状态机行为，"
            "没有客体。",
        ),
        field_unverified=(
            "客体正常关机写入 + 全新 QEMU 进程重启读回",
            "上游 Data 是否 RAM disk 未复现也未推翻",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M10",
        milestone="M10",
        gate_id="M10",
        tier="blocking",
        prd_citation="docs/PRD.md:218",
        goal="IPA 安装",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "m10.archived_samples",
                "the two PRD 5.1 sample IPAs exist as archived binaries, not just "
                "builders",
                _HOST,
                "p.archived_ipa_samples",
            ),
            _req(
                "m10.registered",
                "注册: the guest registered the install, from a guest channel",
                _ONSITE,
                "p.install_registered",
            ),
            _req(
                "m10.trust",
                "信任: how the guest trusts the package, recorded not assumed",
                _ONSITE,
                "p.install_trust",
            ),
            _req(
                "m10.icon",
                "图标与记录: SpringBoard shows the real icon and bundle id",
                _ONSITE,
                "p.install_icon",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m10.veto.copy_or_unzip_only",
                prd_citation="docs/PRD.md:218",
                text="仅解压或复制 `.app`",
                trigger="copy_only_install",
            ),
        ),
        blockers=(_INSTALL_SERVICE_BLOCKER,),
        notes=(
            "宿主侧预检/传输/安装状态机/生命周期/错误分类代码完成；传输成功不能返回 "
            "installed 已被钉住。",
        ),
        field_unverified=(
            "客体信任 / 注册 / 图标；无真实客体安装服务",
            "无存档 IPA（只有 tests/fixtures 的生成器）",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M10a",
        milestone="M10a",
        gate_id="M10a",
        tier="blocking",
        prd_citation="docs/PRD.md:219",
        goal="IPA 接受和执行策略明确",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "m10a.signature",
                "自有包签名/信任方式: the guest checked the signature",
                _ONSITE,
                "p.policy_signature",
            ),
            _req(
                "m10a.entitlements",
                "权限: the guest checked entitlements",
                _ONSITE,
                "p.policy_entitlements",
            ),
            _req(
                "m10a.failure_reason",
                "失败原因可追踪: a policy rejection carries a traceable reason",
                _ONSITE,
                "p.policy_failure_reason",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m10a.veto.no_drm_means_allowed",
                prd_citation="docs/PRD.md:219",
                text="假定无 DRM 就一定允许执行",
                trigger="no_drm_means_allowed",
            ),
        ),
        blockers=(_INSTALL_SERVICE_BLOCKER,),
        notes=(
            "兼容性矩阵按 (package_version, system_build, run_config) 三段键记录；未授权包 "
            "以 ErrorCode.BLOCKED 拒绝，不能靠省略字段混入。",
        ),
        field_unverified=(
            "installs-and-runs / installs-only 在本机构造上不可达",
            "未签名 / 开发签名 / 客体所有者信任三类无法在客体上区分",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M11",
        milestone="M11",
        gate_id="M11",
        tier="blocking",
        prd_citation="docs/PRD.md:220",
        goal="IPA 启动",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "m11.dyld",
                "dyld 找到依赖并执行入口, from a guest channel",
                _ONSITE,
                "p.app_dyld",
            ),
            _req(
                "m11.process",
                "进程: the app process exists, from a guest channel",
                _ONSITE,
                "p.app_process",
            ),
            _req(
                "m11.window",
                "App 窗口, from a guest channel",
                _ONSITE,
                "p.app_window",
            ),
            _req(
                "m11.challenge",
                "挑战值一致: the app showed this run challenge value",
                _ONSITE,
                "p.app_challenge",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m11.veto.process_then_crash",
                prd_citation="docs/PRD.md:220",
                text="进程瞬间存在但随后崩溃",
                trigger="process_then_crash",
            ),
        ),
        blockers=(_INSTALL_SERVICE_BLOCKER,),
        field_unverified=(
            "dyld 找依赖并执行入口；App 窗口；挑战值一致",
            "「进程瞬存即崩」的甄别能力",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="M12",
        milestone="M12",
        gate_id="M12",
        tier="blocking",
        prd_citation="docs/PRD.md:221",
        goal="输入与应用交互",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "m12.tap",
                "点击用例通过, from a guest channel",
                _ONSITE,
                "p.input_tap",
            ),
            _req(
                "m12.drag",
                "拖动用例通过, from a guest channel",
                _ONSITE,
                "p.input_drag",
            ),
            _req(
                "m12.home",
                "Home 用例通过, from a guest channel",
                _ONSITE,
                "p.input_home",
            ),
            _req(
                "m12.text",
                "文本用例通过, from a guest channel",
                _ONSITE,
                "p.input_text",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="m12.veto.host_key_no_guest_response",
                prd_citation="docs/PRD.md:221",
                text="宿主窗口收到按键但客体无响应",
                trigger="host_key_without_guest_response",
            ),
        ),
        blockers=(_INSTALL_SERVICE_BLOCKER,),
        notes=(
            "宿主侧输入汇三层 + 坐标数学 + Home/文本槽位代码完成；客体输入设备型号"
            "（virtio tablet vs mouse）与 Home 的 QEMU 按键映射均 UNVERIFIED。",
        ),
        field_unverified=(
            "客体响应",
            "客体输入设备型号与 Home 按键映射",
            "宿主窗口↔客体显示坐标缩放的真窗校准",
            "屏幕键盘或实际文字输入",
        ),
    )
)


# --- PRD 6.1 work packages -------------------------------------------------
# PRD 6.1's table has no "not enough to pass" column, so a work-package check
# borrows the veto of the milestone row whose evidence it delivers and cites the
# line the veto text comes from.  Nothing is invented: the citation is checkable
# against docs/PRD.md, and the test suite checks it.

_register(
    MilestoneCheck(
        check_id="WP00",
        milestone="WP00",
        gate_id=None,
        tier="workpackage",
        prd_citation="docs/PRD.md:232",
        goal="基线冻结：盘点 CPU/RAM/磁盘；获取候选仓库和锁文件；审计上游 AGENTS/贡献约定",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "wp00.host_inventory",
                "host-inventory.json: a real host measurement (CPU/RAM/disk/toolchain)",
                _HOST,
                "p.host_inventory",
            ),
            _req(
                "wp00.sources_lock",
                "sources.lock.json: every repository, submodule, feed, the system "
                "identity and the tool chain pinned with digests",
                _HOST,
                "p.source_pins",
            ),
            _req(
                "wp00.candidate_repos",
                "候选仓库: the candidate upstream repositories are present",
                _HOST,
                "p.candidate_repos",
            ),
            _req(
                "wp00.divergence_record",
                "差异清单: the divergence and upstream-convention audit record",
                _HOST,
                "p.divergence_record",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="wp00.veto.readme_scripts_downloads",
                prd_citation="docs/PRD.md:206",
                text="README 可读、脚本返回 0、仅下载二进制",
                trigger="only_docs_scripts_or_downloads",
            ),
        ),
        blockers=(_FROZEN_BLOCKER,),
        field_unverified=(
            "sources.lock.json frozen 仍为 false；未跑过干净构建",
            "gpu.vram_gb 仍为 to be measured",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="WP01",
        milestone="WP01",
        gate_id=None,
        tier="workpackage",
        prd_citation="docs/PRD.md:233",
        goal="干净构建：先原样构建设备仓库最小配置；记录 bootstrap、ABI 与缓存前提",
        implementation="UNVERIFIED",
        pass_evidence=(
            _req(
                "wp01.build_log",
                "构建日志: a real build log from the minimal device-repo build",
                _HOST,
                "p.clean_build_manifest",
            ),
            _req(
                "wp01.tool_versions",
                "工具版本: the tool versions that produced the build",
                _HOST,
                "p.source_pins",
            ),
            _req(
                "wp01.artifact_inventory",
                "产物清单: a per-item hashed inventory of the build outputs",
                _HOST,
                "p.clean_build_manifest",
            ),
            _req(
                "wp01.minimal_reproduction",
                "失败时保留最小复现: the recorded minimal reproduction of the build "
                "failure",
                _HOST,
                "p.next_hypothesis",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="wp01.veto.readme_scripts_downloads",
                prd_citation="docs/PRD.md:206",
                text="README 可读、脚本返回 0、仅下载二进制",
                trigger="only_docs_scripts_or_downloads",
            ),
        ),
        blockers=(
            Blocker(
                blocker_id="frozen_build_inputs",
                reason="sources.lock.json is not frozen, and the Windows cross-build "
                       "chain still lacks iig, csh, the six SETUP host tools, Darwin "
                       "freestanding headers and the two Apple-clang builtins "
                       "(__builtin_xnu_type_summary / __builtin_xnu_types_compatible), "
                       "so no clean build can be run against these pins",
                satisfied_by="lock_frozen",
            ),
        ),
        field_unverified=(
            "完整构建日志 + 产物清单（逐项哈希）不存在",
            "Actions 预算未授权；本机无 Xcode",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="WP02",
        milestone="WP02",
        gate_id=None,
        tier="workpackage",
        prd_citation="docs/PRD.md:234",
        goal="Windows UEFI：使用与候选版本匹配的固件和最小设备组合",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "wp02.matched_firmware",
                "与候选版本匹配的固件: the firmware pinned in the lock file, on disk "
                "with the pinned digest",
                _HOST,
                "p.pinned_firmware",
            ),
            _req(
                "wp02.run_directory",
                "M1 运行目录: a run directory holding non-empty artifacts",
                _HOST,
                "p.real_run_directory",
            ),
            _req(
                "wp02.serial_log",
                "串口: a real serial log from that run",
                _HOST,
                "p.edk2_firmware_boot",
            ),
            _req(
                "wp02.control_port",
                "控制端口: a control-port log with a QMP exchange",
                _HOST,
                "p.qmp_control_log",
            ),
            _req(
                "wp02.screenshot",
                "截图: a timestamped screendump artifact",
                _HOST,
                "p.screendump",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="wp02.veto.qemu_window_only",
                prd_citation="docs/PRD.md:207",
                text="只运行出 QEMU 窗口",
                trigger="qemu_window_without_firmware_or_guest",
            ),
        ),
        blockers=(_KERNEL_BLOCKER,),
        field_unverified=(
            "真实 iPadOS/UEFI 客体显示：现有截图是 EDK2 UiApp 菜单 / ramfb 像素",
            "kernel.macho 无法在本机产出",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="WP03",
        milestone="WP03",
        gate_id=None,
        tier="workpackage",
        prd_citation="docs/PRD.md:235",
        goal="引导交接：跑通 UniXNU 与 DarwinBoot，不同时加入 IPA/GPU 工作",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "wp03.efi_loaders",
                "the EFI loaders are built and PE-valid",
                _HOST,
                "p.efi_loaders",
            ),
            _req(
                "wp03.actual_addresses",
                "实际地址: the recorded EL1 entry addresses",
                _HOST,
                "p.el1_entry",
            ),
            _req(
                "wp03.configuration",
                "配置: the pinned TCG board configuration",
                _HOST,
                "p.qemu_config",
            ),
            _req(
                "wp03.unixnu_run_evidence",
                "M2 证据: UniXNU configuration read, boot entry and load evidence",
                _ONSITE,
                "p.unixnu_load_evidence",
            ),
            _req(
                "wp03.darwinboot_handoff",
                "M3 证据: the DarwinBoot handoff, traceable",
                _ONSITE,
                "p.darwinboot_handoff",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="wp03.veto.component_names_only",
                prd_citation="docs/PRD.md:209",
                text="仅打印组件名称",
                trigger="payload_name_print_only",
            ),
            Refusal(
                refusal_id="wp03.veto.static_image_only",
                prd_citation="docs/PRD.md:208",
                text="仅出现静态图片",
                trigger="static_image_without_guest_log",
            ),
        ),
        blockers=(_KERNEL_BLOCKER,),
        notes=(
            "milestones.md 记录 WP03 的 Windows/模拟列是六门禁里唯一可正当写 PASS 的，"
            "且必须带上「真实客体两层」的限定语；写成「引导交接已完成」即越界——本机跑通的"
            "是 EDK2 自带 shell 与我方最小测试 payload，UniXNU 与 DarwinBoot 两者都没跑。",
        ),
        field_unverified=(
            "UniXNU 与 DarwinBoot 从未被跑通",
            "两个真实客体都不是 iPadOS/XNU；我方 payload 一行未执行（就 EDK2 那次运行而言）",
        ),
    )
)

_register(
    MilestoneCheck(
        check_id="WP04",
        milestone="WP04",
        gate_id=None,
        tier="workpackage",
        prd_citation="docs/PRD.md:236",
        goal="内核进入：固定 TCG CPU、GIC、时钟、内存与页配置；定位首次真实阻塞",
        implementation="CODE COMPLETE",
        pass_evidence=(
            _req(
                "wp04.fixed_board_config",
                "固定 TCG CPU/GIC/时钟/内存与页配置",
                _HOST,
                "p.qemu_config",
            ),
            _req(
                "wp04.first_block",
                "定位首次真实阻塞: the recorded first block",
                _HOST,
                "p.first_block_record",
            ),
            _req(
                "wp04.next_hypothesis",
                "下一条实验假设",
                _HOST,
                "p.next_hypothesis",
            ),
            _req(
                "wp04.m4_evidence",
                "M4 证据: XNU page tables, exception vectors and interrupt state",
                _ONSITE,
                "p.xnu_page_tables",
            ),
            _req(
                "wp04.xnu_exception_state",
                "M4 证据（异常）",
                _ONSITE,
                "p.xnu_exception_state",
            ),
            _req(
                "wp04.xnu_interrupt_state",
                "M4 证据（中断）",
                _ONSITE,
                "p.xnu_interrupt_state",
            ),
        ),
        refusals=(
            Refusal(
                refusal_id="wp04.veto.single_banner",
                prd_citation="docs/PRD.md:210",
                text="单行 kernel banner",
                trigger="single_banner_only",
            ),
        ),
        blockers=(_KERNEL_BLOCKER,),
        notes=(
            "免固件路线记录的 Undefined Instruction 是 probe 自己造的边界测试结果，不是 "
            "XNU panic；XNU boot markers 从未在任何主机上出现。",
        ),
        field_unverified=(
            "XNU 初始化证据：页表、异常向量表、GIC 派发、时钟之一均无取证",
            "完整内核链接未达成（61/123 osfmk/kern TU）",
        ),
    )
)


BLOCKING_TIER = tuple(
    check.check_id for check in CHECKS.values() if check.tier == "blocking"
)
WORK_PACKAGE_TIER = tuple(
    check.check_id for check in CHECKS.values() if check.tier == "workpackage"
)


# ---------------------------------------------------------------------------
# 7. Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MilestoneResult:
    """One milestone check, judged, plus its four-field record."""

    check_id: str
    milestone: str
    gate_id: str | None
    tier: str
    prd_citation: str
    goal: str
    verdict: Verdict
    satisfied: tuple[str, ...]
    missing: tuple[str, ...]
    fired_refusals: tuple[str, ...]
    unmet_blockers: tuple[str, ...]
    implementation: str
    windows_status: str
    onsite_status: str
    blocker: str
    evidence: tuple[EvidenceRef, ...]
    field_unverified: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "milestone": self.milestone,
            "gate_id": self.gate_id,
            "tier": self.tier,
            "prd_citation": self.prd_citation,
            "goal": self.goal,
            "verdict": self.verdict.to_dict(),
            "satisfied": list(self.satisfied),
            "missing": list(self.missing),
            "fired_refusals": list(self.fired_refusals),
            "unmet_blockers": list(self.unmet_blockers),
            "implementation": self.implementation,
            "windows_status": self.windows_status,
            "onsite_status": self.onsite_status,
            "blocker": self.blocker,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "field_unverified": list(self.field_unverified),
        }


def check_ids() -> tuple[str, ...]:
    """Every registered check id, in registry order."""
    return tuple(CHECKS)


def get_check(check_id: str) -> MilestoneCheck:
    try:
        return CHECKS[check_id]
    except KeyError:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown milestone check",
            check_id=check_id,
            known=check_ids(),
        ) from None


def _snapshot(view: HostView) -> dict[str, Observation]:
    """Run every probe once, fail closed.

    A probe that raises becomes ``ok=False`` with ``probe_error:<type>`` in its
    detail: a crashed check must never read as a pass.  ``BaseException`` is not
    caught, so a cancellation still stops the run.
    """
    cached = view._cache.get("__snapshot__")
    if cached is not None:
        return cached  # type: ignore[return-value]
    snapshot: dict[str, Observation] = {}
    for probe_id in sorted(_PROBES):
        try:
            observation = _PROBES[probe_id](view)
        except Exception as exc:  # noqa: BLE001 - fail closed, never swallow into a pass
            observation = Observation(
                probe_id, False, "probe_error:%s" % type(exc).__name__
            )
        if not isinstance(observation, Observation):
            observation = Observation(
                probe_id, False, "probe_returned_non_observation:%s" % type(observation).__name__
            )
        snapshot[probe_id] = observation
    view._cache["__snapshot__"] = snapshot
    return snapshot


def _requirement_satisfied(
    requirement: EvidenceRequirement, snapshot: Mapping[str, Observation]
) -> tuple[bool, tuple[EvidenceRef, ...]]:
    """A requirement holds only if its probe found something *in that scope*."""
    observation = snapshot.get(requirement.probe)
    if observation is None or not observation.ok:
        return False, ()
    refs = tuple(ref for ref in observation.refs if ref.scope == requirement.scope)
    return bool(refs), refs


def evaluate(
    check_id: str,
    view: HostView | None = None,
    snapshot: Mapping[str, Observation] | None = None,
) -> MilestoneResult:
    """Judge one milestone check against what this host can actually show.

    The order is load-bearing: **blockers, then the PRD veto, then pass evidence.**
    A milestone that was never attempted cannot be judged by its failure mode, so
    an unmet blocker is reported as BLOCKED with its name and wins over every other
    branch -- which is why a guest-less milestone cannot reach PASS here.  The pass
    evidence is still *collected* first, so a BLOCKED milestone can report the
    host-side evidence it does hold.  The veto stays reachable for the day the
    milestone *is* attempted.
    """
    check = get_check(check_id)
    view = view if view is not None else default_view()
    facts = snapshot if snapshot is not None else _snapshot(view)

    # What this host can actually show, requirement by requirement.  Computed
    # first so every branch of the verdict can report it: a BLOCKED milestone
    # still has to say which host-side evidence it *does* hold.
    satisfied: list[str] = []
    missing: list[str] = []
    held: list[EvidenceRef] = []
    for requirement in check.pass_evidence:
        ok, refs = _requirement_satisfied(requirement, facts)
        if ok:
            satisfied.append(requirement.evidence_id)
            held.extend(refs)
        else:
            missing.append(requirement.evidence_id)
    satisfied_tuple = tuple(satisfied)
    missing_tuple = tuple(missing)
    held_tuple = tuple(held)

    # (1) Blockers first.
    unmet = [
        blocker
        for blocker in check.blockers
        if not _PREDICATES[blocker.satisfied_by](facts)
    ]
    if unmet:
        return _result(
            check,
            Verdict.blocked(
                "blocked_by:%s:%s" % (unmet[0].blocker_id, unmet[0].reason),
                evidence=held_tuple,
            ),
            satisfied=satisfied_tuple,
            missing=missing_tuple,
            fired_refusals=(),
            unmet_blockers=tuple(blocker.blocker_id for blocker in unmet),
            evidence=held_tuple,
        )

    # (2) The PRD veto column: an explicit refusal, quoting the PRD.
    for refusal in check.refusals:
        if _PREDICATES[refusal.trigger](facts):
            return _result(
                check,
                Verdict.fail(
                    ("refused:%s:%s" % (refusal.refusal_id, refusal.text),),
                    evidence=held_tuple,
                ),
                satisfied=satisfied_tuple,
                missing=missing_tuple,
                fired_refusals=(refusal.refusal_id,),
                unmet_blockers=(),
                evidence=held_tuple,
            )

    # (3) Pass evidence: every declared requirement, in its declared scope.
    if missing:
        return _result(
            check,
            Verdict.unverified(
                "evidence_absent:%s" % ",".join(missing), evidence=held_tuple
            ),
            satisfied=satisfied_tuple,
            missing=missing_tuple,
            fired_refusals=(),
            unmet_blockers=(),
            evidence=held_tuple,
        )

    # (4) The onsite guard: a milestone that declares an onsite requirement cannot
    #     pass on host-real evidence alone, however much of it there is.
    if any(item.scope == EvidenceScope.ONSITE for item in check.pass_evidence) and not any(
        ref.scope == EvidenceScope.ONSITE for ref in held_tuple
    ):
        return _result(
            check,
            Verdict.unverified("onsite_evidence_absent", evidence=held_tuple),
            satisfied=satisfied_tuple,
            missing=(),
            fired_refusals=(),
            unmet_blockers=(),
            evidence=held_tuple,
        )

    return _result(
        check,
        Verdict.pass_(
            held_tuple,
            reasons=tuple("satisfied:%s" % item for item in satisfied),
        ),
        satisfied=satisfied_tuple,
        missing=(),
        fired_refusals=(),
        unmet_blockers=(),
        evidence=held_tuple,
    )


def _result(
    check: MilestoneCheck,
    verdict: Verdict,
    *,
    satisfied: tuple[str, ...],
    missing: tuple[str, ...],
    fired_refusals: tuple[str, ...],
    unmet_blockers: tuple[str, ...],
    evidence: tuple[EvidenceRef, ...],
) -> MilestoneResult:
    host_requirements = [
        item for item in check.pass_evidence if item.scope == EvidenceScope.HOST_REAL
    ]
    if not host_requirements:
        # Nothing was declared for this host, so nothing ran here: NOT RUN.
        windows_status = canonical_windows_status("not-applicable")
    elif all(item.evidence_id in satisfied for item in host_requirements):
        windows_status = canonical_windows_status("PASS")
    else:
        # Host requirements exist and are not all satisfied: the host-provable
        # half did not hold, which is FAIL, not "nothing was run".
        windows_status = canonical_windows_status("UNVERIFIED")
    assert windows_status in WINDOWS_STATUSES
    if verdict.kind == VerdictKind.PASS:
        onsite_status = "PASS"
    elif unmet_blockers:
        onsite_status = "BLOCKED"
    else:
        onsite_status = "UNVERIFIED"
    assert onsite_status in ONSITE_STATUSES
    if unmet_blockers:
        blocker = next(
            item.reason for item in check.blockers if item.blocker_id == unmet_blockers[0]
        )
    elif missing:
        blocker = "pass evidence absent: %s" % ", ".join(missing)
    else:
        blocker = ""
    return MilestoneResult(
        check_id=check.check_id,
        milestone=check.milestone,
        gate_id=check.gate_id,
        tier=check.tier,
        prd_citation=check.prd_citation,
        goal=check.goal,
        verdict=verdict,
        satisfied=satisfied,
        missing=missing,
        fired_refusals=fired_refusals,
        unmet_blockers=unmet_blockers,
        implementation=check.implementation,
        windows_status=windows_status,
        onsite_status=onsite_status,
        blocker=blocker,
        evidence=evidence,
        field_unverified=check.field_unverified,
    )


def evaluate_all(view: HostView | None = None) -> tuple[MilestoneResult, ...]:
    """Judge every registered check against one shared snapshot."""
    view = view if view is not None else default_view()
    facts = _snapshot(view)
    return tuple(evaluate(check_id, view=view, snapshot=facts) for check_id in CHECKS)


def release_verdict(results: Sequence[MilestoneResult]) -> tuple[str, tuple[str, ...]]:
    """The PRD 6 release verdict over the blocking tier (PRD:226).

    Two independent gates, in this order:

    1. the blocking tier (M0-M12) must be PASS on its own terms;
    2. even if it were, the release is BLOCKED unless at least one blocking check
       rests on ``onsite`` evidence.  Line 2 is the mechanical form of "a green
       suite does not substitute for a missing guest object" -- a suite that passes
       against fake guests cannot release the product.
    """
    blocking = [result for result in results if result.tier == "blocking"]
    if not blocking:
        return "UNVERIFIED", ("no_blocking_checks",)
    failed = tuple(
        "blocking_fail:%s" % result.check_id
        for result in blocking
        if result.verdict.kind == VerdictKind.FAIL
    )
    if failed:
        return "FAIL", failed
    blocked = tuple(
        "blocking_by:%s" % result.check_id
        for result in blocking
        if result.verdict.kind == VerdictKind.BLOCKED
    )
    if blocked:
        return "BLOCKED", blocked
    unverified = tuple(
        "blocking_unverified:%s" % result.check_id
        for result in blocking
        if result.verdict.kind == VerdictKind.UNVERIFIED
    )
    if unverified:
        return "UNVERIFIED", unverified
    if not any(
        ref.scope == EvidenceScope.ONSITE
        for result in blocking
        for ref in result.verdict.evidence
    ):
        return "BLOCKED", ("no_onsite_evidence",)
    return "PASS", ()


def field_unverified(
    results: Sequence[MilestoneResult],
) -> tuple[dict[str, str], ...]:
    """The field-unverified list the final report must carry (CLAUDE.md)."""
    rows: list[dict[str, str]] = []
    for result in results:
        if result.onsite_status == "PASS":
            continue
        for item in result.field_unverified:
            rows.append(
                {
                    "check_id": result.check_id,
                    "milestone": result.milestone,
                    "prd_citation": result.prd_citation,
                    "onsite_status": result.onsite_status,
                    "blocker": result.blocker,
                    "item": item,
                }
            )
    return tuple(rows)
