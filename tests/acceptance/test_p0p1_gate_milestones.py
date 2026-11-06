"""Milestone gate tests for PRD 6 and PRD 6.1 (M0-M12 + WP00-WP04).

What this file is for
---------------------
``unipad.acceptance.milestones`` turns the PRD gate table into one check per
milestone.  These tests pin down the three properties that make such a gate worth
anything:

1. **It cannot pass without evidence.**  The PASS shape is unconstructible, and a
   milestone whose pass evidence declares an ``onsite`` requirement cannot pass on
   host-real evidence however much of it there is.
2. **It reports this host honestly.**  On a machine with no real guest, every
   guest-dependent milestone must come back BLOCKED with the name of the missing
   precondition -- not PASS, and not a vague "unverified".
3. **It can still go green.**  Every veto is proved reachable with synthetic
   evidence, and a milestone with a complete guest record reaches PASS without a
   line of gate code changing.  Today's answer is never hardcoded.

What this file does not do
--------------------------
It does not launch QEMU and it does not touch the real guest: the tests that read
``local-private`` evidence only *read* what earlier real runs left on disk, and
skip when that directory is absent (it is gitignored, PRD 12).  A green run here
means the gate behaves as designed -- it is not hardware verification.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from unipad.acceptance import milestones as gate
from unipad.acceptance import record
from unipad.acceptance.milestones import (
    EvidenceRef,
    EvidenceScope,
    HostView,
    Observation,
    Verdict,
    VerdictKind,
)
from unipad.controller.verifier import Verifier


from unipad.errors import ErrorCode, UniPadError

REPO_ROOT = Path(__file__).resolve().parents[2]
PRD_PATH = REPO_ROOT / "docs" / "PRD.md"

#: The sixteen PRD 6 rows, in PRD order.
MILESTONE_ROWS = (
    "M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8",
    "M9", "M9a", "M9b", "M10", "M10a", "M11", "M12",
)

#: The five PRD 6.1 work packages.
WORK_PACKAGES = ("WP00", "WP01", "WP02", "WP03", "WP04")

#: Implementation-status words the four-field convention allows.  The canonical
#: set lives in :mod:`unipad.acceptance.record`; this is the same set, spelled
#: here so the assertion below names what it is checking.
IMPLEMENTATION_WORDS = set(record.CODE_STATES)


# ---------------------------------------------------------------------------
# Fixtures: one real-host evaluation, shared by the whole module
# ---------------------------------------------------------------------------


AI_SMOKE_SCRIPT = REPO_ROOT / "scripts" / "windows" / "record-ai-smoke.py"
AI_SMOKE_RECORD = REPO_ROOT / "artifacts" / "m0" / "ai-smoke.json"


@pytest.fixture(scope="module", autouse=True)
def m0_ai_smoke_record() -> Path:
    """Produce the M0 AI-smoke run record before any host view indexes the tree.

    PRD 6's M0 row asks for an AI interface smoke test among its pass evidence,
    and the column beside it says what does *not* count: "a readable README, a
    script that exits 0, and downloaded binaries".  A smoke test that exists but
    was never run and recorded is the first of those, not the third.

    The record is gitignored run evidence (PRD 9: it does not belong in git), so
    a fresh clone carries none and the milestone would report the item missing.
    Producing it here -- rather than asserting a state that depends on whether
    somebody happened to run the recorder first -- is what makes this module
    deterministic, and it is also the regression guard: delete the recorder, or
    make it stop driving every command, and the gate stops seeing the evidence.

    The recorder does the real work: it drives all twelve controller commands
    against an empty sandbox and checks each envelope against the PRD 9.1
    contract.  This fixture only runs it; it does not decide the verdict.
    """
    if not AI_SMOKE_SCRIPT.is_file():
        pytest.fail(
            f"{AI_SMOKE_SCRIPT.relative_to(REPO_ROOT)} is missing: the M0 AI "
            "interface smoke test has no recorder, so its run cannot be recorded"
        )
    proc = subprocess.run(
        [sys.executable, str(AI_SMOKE_SCRIPT), "--out", str(AI_SMOKE_RECORD.parent)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        "the AI smoke recorder failed, so M0's smoke evidence cannot be produced:\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    assert AI_SMOKE_RECORD.is_file(), (
        f"{AI_SMOKE_RECORD.relative_to(REPO_ROOT)} was not written"
    )
    return AI_SMOKE_RECORD


@pytest.fixture(scope="module")
def host_view(m0_ai_smoke_record: Path) -> HostView:
    # Depends on the record above on purpose: HostView caches its file index, so
    # a record written after this fixture runs would be invisible to every
    # observation downstream.
    return gate.default_view()


@pytest.fixture(scope="module")
def host_results(host_view: HostView) -> tuple:
    return gate.evaluate_all(host_view)


@pytest.fixture(scope="module")
def host_by_id(host_results: tuple) -> dict[str, Any]:
    return {result.check_id: result for result in host_results}


def _prd_line(citation: str) -> str:
    """The text of a ``docs/PRD.md:<line>`` citation."""
    match = re.fullmatch(r"docs/PRD\.md:(\d+)", citation)
    assert match, "citation must be docs/PRD.md:<line>, got %r" % citation
    index = int(match.group(1)) - 1
    lines = PRD_PATH.read_text(encoding="utf-8").splitlines()
    assert 0 <= index < len(lines), "citation %s is outside docs/PRD.md" % citation
    return lines[index]


def _guest_ref(path: str = "runs/exp-synthetic/guest.log") -> EvidenceRef:
    return EvidenceRef(
        path=path,
        kind="guest_log",
        sha256="0" * 64,
        scope=EvidenceScope.ONSITE,
        witness="%s:1:event" % path,
    )


def _obs(
    probe_id: str, ok: bool = True, refs: tuple = (), **facts: Any
) -> Observation:
    """A synthetic observation, for driving the predicates directly."""
    return Observation(probe_id=probe_id, ok=ok, detail="synthetic", refs=refs, facts=facts)


#: Guest-side blockers read an onsite witness, so the synthetic snapshot has to
#: carry one -- otherwise a fake record could unblock them.
_GUEST_WITNESS = _guest_ref()


def _unblocked() -> dict[str, Observation]:
    """A snapshot in which every blocker predicate holds."""
    return {
        "p.source_pins": _obs("p.source_pins", frozen=True, problems=[]),
        "p.unixnu_kernel_image": _obs("p.unixnu_kernel_image"),
        "p.xnu_page_tables": _obs("p.xnu_page_tables", refs=(_GUEST_WITNESS,)),
        "p.xnu_exception_state": _obs("p.xnu_exception_state", refs=(_GUEST_WITNESS,)),
        "p.xnu_interrupt_state": _obs("p.xnu_interrupt_state", refs=(_GUEST_WITNESS,)),
        "p.install_registered": _obs("p.install_registered", refs=(_GUEST_WITNESS,)),
        "p.darwinboot_handoff": _obs("p.darwinboot_handoff", refs=(_GUEST_WITNESS,)),
        "p.system_mount": _obs("p.system_mount", refs=(_GUEST_WITNESS,)),
    }


# ---------------------------------------------------------------------------
# 1. Registry shape: one check per milestone, nothing missing, nothing extra
# ---------------------------------------------------------------------------


def test_one_check_per_prd_row_and_nothing_extra() -> None:
    assert set(gate.check_ids()) == set(MILESTONE_ROWS) | set(WORK_PACKAGES)
    assert len(gate.check_ids()) == len(MILESTONE_ROWS) + len(WORK_PACKAGES)
    assert len(set(gate.check_ids())) == len(gate.check_ids())


def test_tiers_match_prd_226() -> None:
    assert set(gate.BLOCKING_TIER) == set(MILESTONE_ROWS)
    assert set(gate.WORK_PACKAGE_TIER) == set(WORK_PACKAGES)


def test_every_milestone_maps_to_a_registered_verifier_gate() -> None:
    """PRD 7.4: a gate id absent from the verifier table fails closed.

    The aggregation layer calls ``Verifier.evaluate_detailed`` once per gate; if a
    milestone named a gate the verifier does not know, that call would raise
    ``unknown_gate`` and the milestone could never be judged at all.
    """
    known = set(Verifier.known_gates())
    for check_id in MILESTONE_ROWS:
        check = gate.get_check(check_id)
        assert check.gate_id == check_id
        assert check.gate_id in known, "%s is not a registered verifier gate" % check_id


def test_work_packages_have_no_verifier_gate() -> None:
    """PRD 6.1 is a work-package table, not a gate table; it invents no gate id."""
    for check_id in WORK_PACKAGES:
        assert gate.get_check(check_id).gate_id is None


def test_implementation_status_uses_the_documented_word_list() -> None:
    for check in gate.CHECKS.values():
        assert check.implementation in IMPLEMENTATION_WORDS, check.check_id


def test_every_check_declares_pass_evidence() -> None:
    for check in gate.CHECKS.values():
        assert check.pass_evidence, check.check_id
        ids = [item.evidence_id for item in check.pass_evidence]
        assert len(set(ids)) == len(ids), check.check_id


def test_every_requirement_names_a_real_probe_and_scope() -> None:
    for check in gate.CHECKS.values():
        for item in check.pass_evidence:
            assert item.probe in gate._PROBES, item.evidence_id
            assert item.scope in (
                EvidenceScope.SIMULATION,
                EvidenceScope.HOST_REAL,
                EvidenceScope.ONSITE,
            )


def test_guest_dependent_milestones_declare_an_onsite_requirement() -> None:
    """Without one, a milestone could pass on host evidence alone."""
    for check_id in ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "M9a",
                     "M9b", "M10", "M10a", "M11", "M12", "WP03", "WP04"):
        check = gate.get_check(check_id)
        assert any(item.scope == EvidenceScope.ONSITE for item in check.pass_evidence), check_id


def test_every_probe_is_reachable_from_the_registry() -> None:
    """A probe nothing reads is a broken window: it can rot unnoticed."""
    referenced = {
        item.probe for check in gate.CHECKS.values() for item in check.pass_evidence
    }
    for function in list(gate._PREDICATES.values()) + list(gate._PROBES.values()):
        source = inspect.getsource(function)
        for probe_id in gate._PROBES:
            if '"%s"' % probe_id in source:
                referenced.add(probe_id)
    assert set(gate._PROBES) <= referenced, (
        "unreachable probes: %s" % sorted(set(gate._PROBES) - referenced)
    )


def test_the_local_record_shapes_match_the_package_contract() -> None:
    """Drift between the two record shapes must be caught, not discovered late.

    ``unipad.acceptance.record`` is the package-wide contract; this module keeps
    its own verdict/evidence types because a BLOCKED verdict here must still cite
    the host evidence it holds.  If either side changes shape, this test says so.
    """
    record = pytest.importorskip("unipad.acceptance.record")
    assert [f.name for f in dataclasses.fields(Verdict)] == [
        f.name for f in dataclasses.fields(record.Verdict)
    ]
    assert [f.name for f in dataclasses.fields(EvidenceRef)] == [
        f.name for f in dataclasses.fields(record.EvidenceRef)
    ]


def test_unknown_check_id_is_refused() -> None:
    with pytest.raises(UniPadError) as excinfo:
        gate.evaluate("M99")
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


# ---------------------------------------------------------------------------
# 2. PRD citations: the gate quotes the PRD, and the PRD says what we say
# ---------------------------------------------------------------------------


def test_every_citation_resolves_to_a_real_prd_line() -> None:
    if not PRD_PATH.is_file():  # pragma: no cover - docs ship with the repo
        pytest.skip("docs/PRD.md is absent")
    citations = [check.prd_citation for check in gate.CHECKS.values()]
    citations += [
        refusal.prd_citation
        for check in gate.CHECKS.values()
        for refusal in check.refusals
    ]
    assert citations
    for citation in citations:
        assert _prd_line(citation).strip(), "empty PRD line behind %s" % citation


def test_every_milestone_row_carries_its_own_veto_column() -> None:
    """PRD 6's fourth column is a veto, not a footnote.

    Each M row must carry exactly one refusal, and the refusal text must appear
    verbatim on the cited line -- otherwise the gate is quoting something the PRD
    does not say.
    """
    if not PRD_PATH.is_file():  # pragma: no cover
        pytest.skip("docs/PRD.md is absent")
    for check_id in MILESTONE_ROWS:
        check = gate.get_check(check_id)
        assert len(check.refusals) == 1, "%s must carry its own veto" % check_id
        refusal = check.refusals[0]
        line = _prd_line(refusal.prd_citation)
        assert refusal.text in line, (
            "%s quotes %r which is not on %s" % (check_id, refusal.text, refusal.prd_citation)
        )
        assert _prd_line(check.prd_citation) == line


def test_work_package_vetoes_are_borrowed_from_the_milestone_they_serve() -> None:
    """PRD 6.1 has no veto column, so a work package borrows one and cites it."""
    if not PRD_PATH.is_file():  # pragma: no cover
        pytest.skip("docs/PRD.md is absent")
    for check_id in WORK_PACKAGES:
        check = gate.get_check(check_id)
        assert check.refusals, "%s must still carry an explicit refusal" % check_id
        for refusal in check.refusals:
            assert refusal.text in _prd_line(refusal.prd_citation), refusal.refusal_id


def test_the_goal_column_is_the_prd_goal() -> None:
    if not PRD_PATH.is_file():  # pragma: no cover
        pytest.skip("docs/PRD.md is absent")
    for check_id in MILESTONE_ROWS:
        check = gate.get_check(check_id)
        assert check.goal in _prd_line(check.prd_citation), check_id


def test_every_milestone_row_is_cited_at_its_prd_line() -> None:
    expected = {
        "M0": 206, "M1": 207, "M2": 208, "M3": 209, "M4": 210, "M5": 211,
        "M6": 212, "M7": 213, "M8": 214, "M9": 215, "M9a": 216, "M9b": 217,
        "M10": 218, "M10a": 219, "M11": 220, "M12": 221,
        "WP00": 232, "WP01": 233, "WP02": 234, "WP03": 235, "WP04": 236,
    }
    for check_id, line in expected.items():
        assert gate.get_check(check_id).prd_citation == "docs/PRD.md:%d" % line


# ---------------------------------------------------------------------------
# 3. A PASS cannot be constructed without evidence
# ---------------------------------------------------------------------------


def test_pass_without_evidence_cannot_be_constructed() -> None:
    with pytest.raises(UniPadError) as excinfo:
        Verdict(kind=VerdictKind.PASS, reasons=(), evidence=(), observed=True)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    with pytest.raises(UniPadError):
        Verdict.pass_(())


def test_pass_without_an_observation_cannot_be_constructed() -> None:
    with pytest.raises(UniPadError):
        Verdict(kind=VerdictKind.PASS, reasons=(), evidence=(_guest_ref(),), observed=False)


def test_fail_without_a_reason_cannot_be_constructed() -> None:
    with pytest.raises(UniPadError):
        Verdict.fail(())


def test_an_unknown_verdict_kind_is_refused() -> None:
    with pytest.raises(UniPadError):
        Verdict(kind="MAYBE", reasons=(), evidence=(), observed=False)


def test_evidence_ref_requires_a_hash_or_an_excuse() -> None:
    with pytest.raises(UniPadError) as excinfo:
        EvidenceRef(path="runs/x/serial.log", kind="serial_log")
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    # ... and the excuse is accepted, so a streamed artifact can still be cited.
    ref = EvidenceRef(
        path="runs/x/serial.log", kind="serial_log", digest_reason="streamed"
    )
    assert ref.digest_reason == "streamed"


def test_evidence_ref_rejects_absolute_and_user_paths() -> None:
    for bad in (
        "F:/iosvirtual/runs/x/serial.log",
        "/usr/local/x",
        "..\\..\\x",
        "../escape/serial.log",
        "C:/Users/someone/runs/x",
        "runs//x",
    ):
        with pytest.raises(UniPadError):
            EvidenceRef(path=bad, kind="serial_log", sha256="0" * 64)


def test_evidence_ref_rejects_a_malformed_digest() -> None:
    with pytest.raises(UniPadError):
        EvidenceRef(path="runs/x/serial.log", kind="serial_log", sha256="NOTAHASH")


def test_onsite_evidence_requires_a_witness_in_the_right_shape() -> None:
    with pytest.raises(UniPadError):
        EvidenceRef(
            path="runs/x/guest.log",
            kind="guest_log",
            sha256="0" * 64,
            scope=EvidenceScope.ONSITE,
        )
    with pytest.raises(UniPadError):
        EvidenceRef(
            path="runs/x/guest.log",
            kind="guest_log",
            sha256="0" * 64,
            scope=EvidenceScope.ONSITE,
            witness="no-line-number",
        )
    ref = EvidenceRef(
        path="runs/x/guest.log",
        kind="guest_log",
        sha256="0" * 64,
        scope=EvidenceScope.ONSITE,
        witness="runs/x/guest.log:7:xnu.page_tables",
    )
    assert ref.witness.endswith("xnu.page_tables")


def test_an_unknown_scope_is_refused() -> None:
    with pytest.raises(UniPadError):
        EvidenceRef(path="runs/x/serial.log", kind="serial_log", scope="somewhere")


def test_the_host_view_refuses_a_path_outside_every_root(tmp_path: Path) -> None:
    view = HostView(roots=(tmp_path,), repo_root=tmp_path)
    with pytest.raises(UniPadError):
        view.relativise(Path("Z:/elsewhere/serial.log"))


def test_scratch_fixtures_do_not_enter_the_host_evidence_index(tmp_path: Path) -> None:
    scratch = tmp_path / ".scratch" / "pytest-run"
    scratch.mkdir(parents=True)
    (scratch / "guest.log").write_text('{"event":"xnu.page_tables"}\n', encoding="utf-8")
    real = tmp_path / "boot" / "guest.log"
    real.parent.mkdir()
    real.write_text('{"event":"xnu.interrupts"}\n', encoding="utf-8")
    view = HostView(roots=(tmp_path,), repo_root=tmp_path)
    assert view.find("**/guest.log") == ("boot/guest.log",)


def test_the_host_view_window_is_read_rather_than_the_whole_file(
    tmp_path: Path, monkeypatch
) -> None:
    """A 4 MiB window of a boot log must not cost the whole boot log.

    ``read_text()[:max_bytes]`` decoded the entire file and then threw almost all
    of it away.  The files this method is pointed at are serial logs and QEMU
    traces, which are exactly the files that grow without bound on a host that
    has been running -- so the cost of asking for a window was the cost of the
    file, and the gate got slower every day it was used.

    The recorded call list is the proof: nothing may come back through
    ``Path.read_text`` at all.  The window still has to be the *prefix* of the
    file and still has to be the size asked for, because a method that stopped
    reading would also stop knowing what it read.
    """
    target = tmp_path / "serial0.log"
    target.write_bytes(b"Shell>\n" + b"." * 4096)

    view = HostView(roots=(tmp_path,), repo_root=tmp_path, max_bytes=64)

    reads: list[Path] = []
    real_read_text = Path.read_text

    def recording(self, *args, **kwargs):
        reads.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording)
    text = view.read_text("serial0.log")

    assert reads == [], (
        f"the window was produced by reading the whole file: {[p.name for p in reads]}"
    )
    assert text == "Shell>\n" + "." * (64 - len("Shell>\n")), repr(text[:20])
    assert len(text) == 64


def test_the_host_view_window_ends_on_a_character_boundary(tmp_path: Path) -> None:
    """The window is a byte count, so it can cut a UTF-8 sequence in half.

    That tail is dropped rather than decoded.  Decoding it would put a U+FFFD in
    the text that is not in the file, and every consumer of this method searches
    for ASCII markers or parses JSON -- neither of which is helped by a
    replacement character standing where a character was cut off.  The window was
    always able to cut a document in half; this only decides what the cut leaves
    behind it.
    """
    target = tmp_path / "utf8.log"
    target.write_bytes("甲乙丙丁".encode("utf-8"))  # four characters, three bytes each

    # 7 bytes is two whole characters and the first byte of a third.
    view = HostView(roots=(tmp_path,), repo_root=tmp_path, max_bytes=7)
    assert view.read_text("utf8.log") == "甲乙"

    # 9 bytes is three whole characters exactly.
    whole = HostView(roots=(tmp_path,), repo_root=tmp_path, max_bytes=9)
    assert whole.read_text("utf8.log") == "甲乙丙"

    # A byte that is invalid in the middle of the file is still replaced, not
    # dropped: losing the markers either side of one damaged byte would be a
    # worse answer than showing that there was one.
    target.write_bytes(b"before\xffafter")
    roomy = HostView(roots=(tmp_path,), repo_root=tmp_path, max_bytes=64)
    assert roomy.read_text("utf8.log") == "before\ufffdafter"


def test_the_host_view_reads_an_absent_file_as_nothing(tmp_path: Path) -> None:
    """An unreadable file is an absence of evidence, not a crash mid-gate."""
    view = HostView(roots=(tmp_path,), repo_root=tmp_path)
    assert view.read_text("not-there.log") == ""


# ---------------------------------------------------------------------------
# 4. This host: the honest answer
# ---------------------------------------------------------------------------


def test_no_check_passes_without_a_witnessed_guest_observation(host_results: tuple) -> None:
    """PRD 226: a PASS rests on the target object, never on the suite.

    This test used to assert the opposite of what it does now -- "no check
    passes on this host" -- and that was true when it was written.  It stopped
    being true the moment a real guest log reached the host's view: M4, M5 and
    WP04 each read PASS, citing ``onsite`` references at
    ``local-private/boot-mount-41316953324/root-mount/guest.log``.  The gap was
    closed, so the assertion is updated rather than the check weakened.

    What replaces it is the rule the old form was a proxy for.  A PASS is only
    meaningful if something only the guest could produce stands behind it, so
    every PASS must cite at least one ONSITE reference carrying a witness, and
    that witness must be a guest channel (PRD 7.4: a serial log locates, it
    does not prove) at a real line of a non-simulation path.  The old form
    caught a relaxed ``_requirement_satisfied`` only by accident; this one
    catches it directly, and keeps catching it on the day every milestone is
    green.

    The weaker sibling :func:`test_a_check_without_onsite_evidence_can_never_pass`
    states the same rule for checks that DECLARE an onsite requirement.  This
    one is about the reference that actually got cited, which is the half the
    sibling cannot see: a check can declare nothing onsite and still cite an
    onsite reference, and that reference has to be real.
    """
    for result in host_results:
        if result.verdict.kind != VerdictKind.PASS:
            continue
        witnessed = [
            ref for ref in result.verdict.evidence
            if ref.scope == EvidenceScope.ONSITE and ref.witness
        ]
        assert witnessed, (
            f"{result.check_id} reads PASS with no witnessed on-site evidence: "
            "a pass without the guest object"
        )
        for ref in witnessed:
            # PRD 7.4: a guest channel, not a serial log.  A serial log is
            # evidence the firmware ran and says nothing about the guest.
            assert ref.kind == "guest_log", (
                f"{result.check_id} passes on a {ref.kind} reference; PRD 7.4 "
                "requires a guest channel"
            )
            # The witness is "path:line:event", and each part has to be real:
            # a path that resolves, a line that is a number, an event id that
            # is not empty.
            path, lineno, literal = ref.witness.rsplit(":", 2)
            assert lineno.isdigit() and int(lineno) >= 1, ref.witness
            assert literal.strip(), ref.witness
            assert not gate._is_simulation_path(path), (
                f"{result.check_id} passes on a witness under a simulation "
                f"path: {ref.path}"
            )


def test_a_check_without_onsite_evidence_can_never_pass(host_results: tuple) -> None:
    """The invariant, not today's answer.

    A milestone whose pass evidence declares an ``onsite`` requirement needs an
    ``onsite`` reference to pass, and an ``onsite`` reference needs a witness only
    the target object could produce.  So on a host with no guest the verdict is
    structurally incapable of being PASS -- whatever else the evidence says.
    """
    for result in host_results:
        check = gate.get_check(result.check_id)
        needs_guest = any(
            item.scope == EvidenceScope.ONSITE for item in check.pass_evidence
        )
        witnessed = [
            ref for ref in result.verdict.evidence if ref.scope == EvidenceScope.ONSITE
        ]
        if needs_guest and not witnessed:
            assert result.verdict.kind != VerdictKind.PASS, result.check_id
            assert result.onsite_status in ("BLOCKED", "UNVERIFIED"), result.check_id
            assert result.blocker.strip(), result.check_id


def test_a_blocker_is_named_only_when_it_is_true(host_results: tuple,
                                                 host_view: HostView) -> None:
    """The rule underneath the field record, derived rather than listed.

    This replaces ``test_guest_dependent_milestones_are_blocked_on_this_host``,
    whose two hardcoded lists stopped being true.  M1 lost its last blocker and
    fell to UNVERIFIED; M4, M5 and WP04 reached PASS on a real guest log.  The
    docstring of the test it replaces says exactly what should happen in that
    case -- "the field record changed and must be re-recorded deliberately
    rather than quietly" -- and the re-record is here.

    The old form's protective force is kept and made sharper.  It existed to
    catch the gate naming a blocker that is FALSE on this host: the symbol scan
    read only the last 8 MiB of a candidate, reported "no kalloc sparse-index
    symbol" for a kernel that has one, and seven milestones were BLOCKED for a
    reason that was not true.  A list of expected verdicts catches that only
    when somebody remembers to update the list; the rule below catches it
    directly, by recomputing from the gate's own predicates which blockers are
    genuinely unmet and requiring the verdict to agree.

    So a blocker the gate names must be one its own predicate says is unmet, a
    BLOCKED verdict must have one, an UNVERIFIED verdict must have none and must
    name what is missing, and nothing may be vague.
    """
    facts = gate._snapshot(host_view)
    for result in host_results:
        check = gate.get_check(result.check_id)
        genuinely_unmet = {
            blocker.blocker_id
            for blocker in check.blockers
            if not gate._PREDICATES[blocker.satisfied_by](facts)
        }
        # The gate may not invent a blocker, nor hide one it declared.
        assert set(result.unmet_blockers) == genuinely_unmet, (
            result.check_id,
            sorted(result.unmet_blockers),
            sorted(genuinely_unmet),
        )
        if result.unmet_blockers:
            assert result.verdict.kind == VerdictKind.BLOCKED, result.check_id
            assert result.onsite_status == "BLOCKED", result.check_id
            # The reason has to be readable by a human, not a bare id.
            assert len(result.blocker) > 40, result.check_id
        if result.verdict.kind == VerdictKind.BLOCKED:
            assert result.unmet_blockers, result.check_id
        if result.verdict.kind == VerdictKind.UNVERIFIED:
            # Never vague: PRD 226 forbids "unverified" without a name.
            assert result.missing, result.check_id
            assert not result.unmet_blockers, result.check_id


def test_the_release_verdict_is_never_green_without_the_guest(host_results: tuple) -> None:
    """The release verdict never quietly goes green, and never goes silent.

    This test used to pin ``BLOCKED`` with only ``blocking_by:`` reasons.  Both
    halves stopped being true the moment a real guest log reached the host's
    view: M9 and M9a's only blocker was ``xnu_initialised``, the guest log
    satisfies it, and the PRD veto branch fires instead -- so the verdict is
    ``FAIL`` with ``blocking_fail:M9`` and ``blocking_fail:M9a``.  The gap was
    closed, so the assertion is updated rather than the check weakened.

    What replaces it is the rule the old form was a proxy for.  The release
    verdict is the one number an operator reads; if it ever read PASS without a
    guest, everything downstream would be lying.  ``verdict != "PASS"`` is the
    half that survives every milestone going green -- M4, M5 and WP04 are
    already green and the verdict is still not PASS, and a fully green blocking
    tier is held off PASS by the ``no_onsite_evidence`` gate already tested
    synthetically at :func:`test_a_guest_record_makes_the_release_verdict_reachable`.

    The second half is "never silent": a non-PASS verdict with no reasons says
    nothing anybody can act on, and every reason must name a specific blocking
    check.  The accepted prefixes are the ones ``release_verdict`` can emit, so
    a new kind of reason has to be added here deliberately.
    """
    verdict, reasons = gate.release_verdict(host_results)
    assert verdict != "PASS", (
        "the release verdict reads PASS on a host with no iPadOS guest"
    )
    assert verdict in ("BLOCKED", "FAIL", "UNVERIFIED"), verdict
    assert reasons, "a non-PASS verdict with no reasons says nothing"
    assert all(
        reason.startswith(("blocking_by:", "blocking_fail:", "blocking_unverified:",
                           "no_onsite_evidence", "no_blocking_checks"))
        for reason in reasons
    ), reasons
    # Recorded 2026-11-02.  Re-record deliberately when the field moves; do not
    # quietly relax this line.
    assert (verdict, reasons) == (
        "FAIL", ("blocking_fail:M9", "blocking_fail:M9a")), (verdict, reasons)


def test_every_onsite_witness_is_checkable(host_results: tuple,
                                           host_view: HostView) -> None:
    """An on-site reference is re-verifiable, or it is not a witness.

    This test used to assert that NO on-site reference existed anywhere on this
    host.  That was true when it was written and stopped being true when a real
    guest log reached the view: M4, M5 and WP04 now cite eight on-site
    references between them, all pointing at
    ``local-private/boot-mount-41316953324/root-mount/guest.log`` with witnesses
    like ``…:19:xnu.page_tables``.  The gap was closed, so the assertion is
    updated rather than the check weakened.

    The absence form was a proxy for the rule that actually matters: the gate
    must not mint an on-site claim out of nothing.  The machinery that would let
    it is ``_is_simulation_path`` (a witness pointing at a fake guest is not a
    witness), the witness-shape rule tested at
    :func:`test_onsite_evidence_requires_a_witness_in_the_right_shape`, and
    :func:`test_a_fake_guest_record_cannot_unblock_a_guest_milestone`.  With a
    real witness on disk, the QUALITY form is the one that still guards, and it
    is stronger than the absence form was: it checks every on-site reference
    rather than noting that there are none.
    """
    refs = [ref for result in host_results for ref in result.verdict.evidence]
    assert refs, "a host with real firmware evidence must cite something"
    onsite = [ref for ref in refs if ref.scope == EvidenceScope.ONSITE]
    for ref in onsite:
        # Re-verifiable, not merely asserted: a digest is what lets --verify
        # re-hash the file and catch a witness that changed underneath.
        assert ref.sha256, ref.path
        assert ref.witness, ref.path
        path, lineno, literal = ref.witness.rsplit(":", 2)
        assert lineno.isdigit() and int(lineno) >= 1, ref.witness
        assert literal.strip(), ref.witness
        # PRD 7.4: a guest channel, not a serial log.
        assert ref.kind == "guest_log", ref.kind
        assert not gate._is_simulation_path(path), (
            f"an on-site witness points into a simulation path: {ref.path}"
        )
        # The witness has to point at something a reader can actually open.
        assert host_view.read_text(path), ref.path
    # And a simulation-scoped reference may never carry a witness at all: the
    # two are mutually exclusive by construction, and a violation would mean a
    # fake record is being cited as an observation of the target.
    for ref in refs:
        if ref.scope == EvidenceScope.SIMULATION:
            assert not ref.witness, ref.path


def test_four_fields_are_recorded_for_every_check(host_results: tuple) -> None:
    for result in host_results:
        assert result.implementation in IMPLEMENTATION_WORDS
        assert result.windows_status in gate.WINDOWS_STATUSES
        assert result.onsite_status in gate.ONSITE_STATUSES
        if result.onsite_status != "PASS":
            assert result.blocker, result.check_id


def test_no_check_claims_a_field_pass_it_did_not_earn(host_results: tuple) -> None:
    """A field PASS is earned by witnessed on-site evidence, not by a column.

    This test used to assert a correlation between two columns -- if the Windows
    column reads ``SIMULATION PASS`` then the on-site column must not read
    ``PASS`` -- and that correlation was only ever true while nothing passed.
    M4 and WP04 now read ``SIMULATION PASS`` in the Windows column AND ``PASS``
    on site, both legitimately: the Windows column reports the host-provable
    half and the on-site column reports what a real guest showed, and the two
    are allowed to agree.  The gap was closed, so the assertion is updated
    rather than the check weakened.

    The rule the docstring names -- "SIMULATION PASS is not 现场 PASS" -- is real
    and is kept, in the direction that actually holds: a field PASS requires a
    witnessed on-site reference, so the Windows column cannot launder a host
    result into a field pass however green it is.

    The ``not declares_onsite`` half is new and is the load-bearing addition.
    The old correlation never checked it at all: a check that declares no onsite
    requirement could claim a field PASS on host evidence alone and the old form
    would not have noticed.  That is the defect class this test exists for, and
    it is now asserted directly.
    """
    for result in host_results:
        check = gate.get_check(result.check_id)
        declares_onsite = any(
            item.scope == EvidenceScope.ONSITE for item in check.pass_evidence
        )
        witnessed = [
            ref for ref in result.verdict.evidence
            if ref.scope == EvidenceScope.ONSITE and ref.witness
        ]
        if result.onsite_status == "PASS":
            assert result.verdict.kind == VerdictKind.PASS, result.check_id
            assert witnessed, (
                f"{result.check_id} reports a field PASS with no witnessed "
                "on-site evidence"
            )
        else:
            # Anything short of a field pass is not a PASS verdict, however much
            # evidence the host half holds.
            assert result.verdict.kind != VerdictKind.PASS, result.check_id
        if not declares_onsite:
            # Nothing on site was ever declared, so a field pass was never
            # earnable here -- however green the host half is.
            assert result.onsite_status != "PASS", result.check_id


def test_the_field_unverified_list_is_the_report_the_final_review_needs(
    host_results: tuple,
) -> None:
    rows = gate.field_unverified(host_results)
    assert rows, "the final report must list the field-unverified items"
    for row in rows:
        assert row["check_id"]
        assert row["item"]
        assert row["prd_citation"].startswith("docs/PRD.md:")
        assert row["onsite_status"] in ("BLOCKED", "UNVERIFIED")


def test_no_reason_or_evidence_path_leaks_an_absolute_user_path(
    host_results: tuple,
) -> None:
    """PRD 9.2: no tokens, private keys, or absolute user paths in any output."""
    for result in host_results:
        for reason in result.verdict.reasons:
            assert "\\" not in reason
            assert not re.search(r"[A-Za-z]:/", reason), reason
            assert "Users/" not in reason, reason
        for ref in result.verdict.evidence:
            assert "\\" not in ref.path
            assert not ref.path.startswith("/")
            assert "Users/" not in ref.path
            assert not re.fullmatch(r"[A-Za-z]:.*", ref.path)


def test_m0_reports_the_gap_that_is_really_open(host_by_id: dict) -> None:
    """PRD 206's three M0 pass-evidence items, judged against what this host holds.

    Two of the three are satisfied here; the third -- clean build artifacts and
    their hashes -- is not, and must keep being reported as missing until a real
    build produces them.

    This test used to pin "two documented gaps", the AI smoke test among them,
    on the grounds that no record of such a run existed anywhere on the host.
    That was true when it was written.  A record now exists -- produced by the
    module's autouse fixture, because the record is gitignored run evidence and a
    fresh clone carries none -- so the smoke item is satisfied and the test was
    rightly failing at the old assertion.  The gap was closed, so the assertion
    is updated rather than the check weakened: the clean-build item is still
    required to be missing, and the smoke item is now required to be satisfied
    *on digestable host evidence*, which is a stronger claim than the old one.
    """
    result = host_by_id["M0"]
    assert result.verdict.kind == VerdictKind.UNVERIFIED

    # The gap that is genuinely still open.  If this ever stops being reported
    # without a real build having run, the gate has started lying.
    assert "m0.clean_build_artifacts" in result.missing

    # ... and the one that was closed, resting on evidence that can be re-read.
    assert "m0.ai_interface_smoke" in result.satisfied
    assert "m0.pinned_source_identity" in result.satisfied

    smoke_refs = [ref for ref in result.evidence if "ai-smoke" in ref.path]
    assert smoke_refs, (
        "the smoke item is reported satisfied but carries no evidence reference, "
        "so there is nothing to re-verify"
    )
    for ref in smoke_refs:
        assert ref.sha256, f"{ref.path} has no digest, so it cannot be re-verified"
        assert ref.scope == EvidenceScope.HOST_REAL, ref.scope
        assert ref.path.startswith("artifacts/"), ref.path


def test_m0_veto_does_not_fire_while_real_pins_exist(host_by_id: dict) -> None:
    """PRD 206's veto is about *only* README/scripts/downloads."""
    assert not host_by_id["M0"].fired_refusals


# ---------------------------------------------------------------------------
# 5. Evidence that DOES exist on this host: the check runs and reports it
# ---------------------------------------------------------------------------


def _real_boot_root() -> Path | None:
    root = REPO_ROOT / "local-private" / "out" / "real-boot"
    return root if root.is_dir() else None


@pytest.fixture(scope="module")
def boot_logs(host_view: HostView) -> Mapping[str, Observation]:
    if _real_boot_root() is None:
        pytest.skip("local-private/out/real-boot is absent (gitignored, PRD 12)")
    return gate._snapshot(host_view)


def test_the_edk2_firmware_boot_is_proven_on_this_host(boot_logs) -> None:
    """PRD:207 固件日志 -- real QEMU, real pinned firmware, interactive Shell>."""
    observation = boot_logs["p.edk2_firmware_boot"]
    assert observation.ok, observation.detail
    assert observation.refs
    for ref in observation.refs:
        assert ref.scope == EvidenceScope.HOST_REAL
        assert ref.kind == "serial_log"
        assert ref.sha256


def test_the_esp_to_bootaa64_chain_is_proven_on_this_host(boot_logs) -> None:
    """PRD:235 -- FAT ESP mounted, BOOTAA64.EFI started, guest code ran.

    The literal asserted here changed, and the change is the point.  It used to be
    ``MiniEfi: alive``, the marker of an old experiment's *test* payload; the probe
    took the first sorted log that carried it and so recorded
    ``darwinboot: False`` on a host where DarwinBoot had since run for real.  The
    PRD 209 veto ("仅打印组件名称") reads that fact, so a stale selection turned a
    measured handoff into a refusal.  It now prefers a log where DarwinBoot
    actually ran, and records which literal identified the payload either way.
    """
    observation = boot_logs["p.esp_efi_chain"]
    assert observation.ok, observation.detail
    assert observation.facts["payload_literal"] == "DARWINBOOT: hand-off to the kernel", observation.facts
    assert observation.facts["darwinboot"] is True, observation.facts
    assert observation.facts["payload_line"] > 0, observation.facts

def test_a_log_that_merely_mentions_darwinboot_does_not_satisfy_the_veto_fact(
    tmp_path: Path,
) -> None:
    """PRD 209's veto reads ``darwinboot``, so a *mention* must not set it.

    ``_marker_hits`` is a per-line substring test, so the literal has to be one
    only a handoff can produce.  An earlier version of the probe matched the bare
    ``DARWINBOOT:``, and a log carrying the old test payload *plus*

        Error: DARWINBOOT: image not found

    set ``darwinboot`` True on the strength of that error line and suppressed the
    veto.  Measured against the pre-change probe, the same pair of logs flipped M3
    from FAIL (``m3.veto.component_names_only``) to UNVERIFIED with no refusal --
    a gate relaxation nothing recorded.  docs/PRD.md:296 forbids exactly this:
    验证逻辑的修改独立记录，不能随功能修改偷偷放宽.

    The handoff line cannot be produced by an error or a comment, so matching it
    is what makes the fact mean what the veto thinks it means.
    """
    mention = tmp_path / "mention" / "serial-a.log"
    mention.parent.mkdir(parents=True)
    mention.write_text(
        'BdsDxe: loading Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x1,0x0)\n'
        "MiniEfi: alive\n"
        "Error: DARWINBOOT: image not found\n",
        encoding="utf-8",
    )
    view = HostView(roots=(tmp_path / "mention",), repo_root=tmp_path)
    observation = gate._probe_esp_efi_chain(view)
    assert observation.ok, observation.detail
    assert observation.facts["darwinboot"] is False, observation.facts
    # ...and with that fact False, the PRD 209 veto predicate does fire.
    snapshot = {
        "p.esp_efi_chain": observation,
        "p.darwinboot_handoff": _obs("p.darwinboot_handoff", ok=False),
    }
    assert gate._PREDICATES["payload_name_print_only"](snapshot) is True, snapshot

    real = tmp_path / "real" / "serial-a.log"
    real.parent.mkdir(parents=True)
    real.write_text(
        'BdsDxe: loading Boot0001 "UEFI Misc Device" from PciRoot(0x0)/Pci(0x1,0x0)\n'
        "DARWINBOOT: hand-off to the kernel\n"
        "kernel   va 0xFFFFFFF00427C000  pa 0x4807C000\n",
        encoding="utf-8",
    )
    view = HostView(roots=(tmp_path / "real",), repo_root=tmp_path)
    observation = gate._probe_esp_efi_chain(view)
    assert observation.ok, observation.detail
    assert observation.facts["darwinboot"] is True, observation.facts
    snapshot = {
        "p.esp_efi_chain": observation,
        "p.darwinboot_handoff": _obs("p.darwinboot_handoff", ok=False),
    }
    assert gate._PREDICATES["payload_name_print_only"](snapshot) is False, snapshot
def test_a_real_install_record_is_read_and_reported(boot_logs) -> None:
    """PRD:218 -- the recorded install reached a failure, not a registration."""
    observation = boot_logs["p.install_record"]
    assert observation.refs, "the real install.json must be cited"
    assert "failed" in observation.facts["stages"]
    assert observation.facts["copy_only_stages"] == []


# ---------------------------------------------------------------------------
# 6. The gate can go green on its own: a real guest record passes
# ---------------------------------------------------------------------------


def _guest_log_lines(*events: str) -> str:
    return "".join(json.dumps({"event": event}) + "\n" for event in events)


def _view_for(tmp_path: Path) -> HostView:
    return HostView(roots=(tmp_path,), repo_root=tmp_path)


def test_a_complete_guest_record_makes_a_milestone_pass(tmp_path: Path) -> None:
    """Tomorrow's kernel and guest log go green with no code change.

    M6 needs one onsite requirement and an XNU-initialisation blocker, so four
    guest events are enough -- and the witness comes from the guest channel, not
    from anything the gate asserts about itself.
    """
    (tmp_path / "guest.log").write_text(
        _guest_log_lines(
            "xnu.page_tables",
            "xnu.exception_vectors",
            "xnu.interrupts",
            "virtio_blk.read_back",
        ),
        encoding="utf-8",
    )
    result = gate.evaluate("M6", _view_for(tmp_path))
    assert result.verdict.kind == VerdictKind.PASS, result.verdict.reasons
    assert result.onsite_status == "PASS"
    assert result.missing == ()
    witnesses = [ref.witness for ref in result.verdict.evidence]
    assert witnesses, "a PASS must cite its evidence"
    for witness in witnesses:
        assert witness, "a PASS must carry a witness"
        path, lineno, literal = witness.split(":", 2)
        assert path == "guest.log"
        assert lineno.isdigit() and int(lineno) >= 1
        assert literal.startswith(("xnu.", "virtio_blk."))


def test_a_guest_record_makes_the_release_verdict_reachable(tmp_path: Path) -> None:
    """PRD 226: PASS needs the blocking tier green *and* an onsite witness.

    A guest record that satisfies M6 does not by itself release the product --
    the other fifteen blocking milestones still have no evidence -- so the
    release stays BLOCKED and names them.  What it does prove is that the gate is
    not rigged: the one milestone the evidence covers goes green.
    """
    (tmp_path / "guest.log").write_text(
        _guest_log_lines(
            "xnu.page_tables",
            "xnu.exception_vectors",
            "xnu.interrupts",
            "virtio_blk.read_back",
        ),
        encoding="utf-8",
    )
    view = _view_for(tmp_path)
    results = gate.evaluate_all(view)
    by_id = {result.check_id: result for result in results}
    assert by_id["M6"].verdict.kind == VerdictKind.PASS
    verdict, reasons = gate.release_verdict(results)
    assert verdict == "BLOCKED"
    assert "blocking_by:M1" in reasons
    assert not any(reason.startswith("blocking_fail") for reason in reasons)


def test_a_fake_guest_record_cannot_unblock_a_guest_milestone(tmp_path: Path) -> None:
    """DESIGN 9.12: a witness pointing at a fake guest is not a witness."""
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "guest.log").write_text(
        _guest_log_lines(
            "xnu.page_tables", "xnu.exception_vectors", "xnu.interrupts",
            "virtio_blk.read_back",
        ),
        encoding="utf-8",
    )
    result = gate.evaluate("M6", _view_for(tmp_path))
    assert result.verdict.kind != VerdictKind.PASS
    assert result.verdict.kind == VerdictKind.BLOCKED
    assert result.unmet_blockers == ("xnu_initialised",)


def test_m8_rejects_launchd_as_its_own_later_daemon(tmp_path: Path) -> None:
    """PID 1's own exec cannot satisfy M8's later-daemon evidence."""
    (tmp_path / "guest.log").write_text(
        json.dumps({"event": "launchd.running", "pid": 1,
                    "program": "/sbin/launchd", "source": "XNU.load_init_program"})
        + "\n"
        + json.dumps({"event": "daemon.spawned", "pid": 1,
                      "comm": "launchd", "source": "XNU.__mac_execve"})
        + "\n",
        encoding="utf-8",
    )
    view = _view_for(tmp_path)
    assert gate._PROBES["p.launchd_running"](view).ok
    assert not gate._PROBES["p.daemon_spawned"](view).ok


def test_m8_requires_launchd_and_later_daemon_in_same_ordered_guest_log(
    tmp_path: Path,
) -> None:
    """Old runs cannot be combined into a synthetic M8 pass."""
    (tmp_path / "old-guest.log").write_text(
        json.dumps({"event": "launchd.running", "pid": 1,
                    "program": "/sbin/launchd", "source": "XNU.load_init_program"})
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "new-guest.log").write_text(
        json.dumps({"event": "daemon.spawned", "pid": 7,
                    "comm": "apfs_boot_util", "source": "XNU.__mac_execve"})
        + "\n",
        encoding="utf-8",
    )
    view = _view_for(tmp_path)
    assert gate._PROBES["p.launchd_running"](view).ok
    assert not gate._PROBES["p.daemon_spawned"](view).ok


def test_m8_accepts_a_later_non_launchd_daemon_from_same_log(tmp_path: Path) -> None:
    (tmp_path / "guest.log").write_text(
        json.dumps({"event": "launchd.running", "pid": 1,
                    "program": "/sbin/launchd", "source": "XNU.load_init_program"})
        + "\n"
        + json.dumps({"event": "daemon.spawned", "pid": 7,
                      "comm": "apfs_boot_util", "source": "XNU.__mac_execve"})
        + "\n",
        encoding="utf-8",
    )
    view = _view_for(tmp_path)
    launchd = gate._PROBES["p.launchd_running"](view)
    daemon = gate._PROBES["p.daemon_spawned"](view)
    assert launchd.ok
    assert daemon.ok, daemon.detail
    assert daemon.facts["daemon_pid"] == 7
    assert daemon.facts["daemon_comm"] == "apfs_boot_util"
    assert launchd.refs[0].path == daemon.refs[0].path == "guest.log"


@pytest.mark.parametrize("daemon", [
    {"pid": 0, "comm": "kernel_task"},
    {"pid": 1, "comm": "helper"},
    {"pid": 7, "comm": "launchd"},
    {"pid": "7", "comm": "notifyd"},
    {"pid": 7, "comm": ""},
    {"comm": "notifyd"},
])
def test_m8_rejects_invalid_later_process_identity(tmp_path: Path, daemon: dict) -> None:
    records = [
        {"event": "launchd.running", "pid": 1, "program": "/sbin/launchd",
         "source": "XNU.load_init_program"},
        {"event": "daemon.spawned", "source": "XNU.__mac_execve", **daemon},
    ]
    (tmp_path / "guest.log").write_text(
        "\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    assert not gate._PROBES["p.daemon_spawned"](_view_for(tmp_path)).ok


def test_m8_rejects_daemon_record_before_launchd(tmp_path: Path) -> None:
    records = [
        {"event": "daemon.spawned", "pid": 7, "comm": "notifyd",
         "source": "XNU.__mac_execve"},
        {"event": "launchd.running", "pid": 1, "program": "/sbin/launchd",
         "source": "XNU.load_init_program"},
    ]
    (tmp_path / "guest.log").write_text(
        "\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
    assert not gate._PROBES["p.daemon_spawned"](_view_for(tmp_path)).ok


def test_a_serial_log_is_not_a_guest_channel(tmp_path: Path) -> None:
    """PRD 7.4: serial text locates, it does not prove a stage."""
    (tmp_path / "serial.log").write_text(
        _guest_log_lines(
            "xnu.page_tables", "xnu.exception_vectors", "xnu.interrupts",
            "virtio_blk.read_back",
        ),
        encoding="utf-8",
    )
    result = gate.evaluate("M6", _view_for(tmp_path))
    assert result.verdict.kind == VerdictKind.BLOCKED
    assert result.unmet_blockers == ("xnu_initialised",)


def test_host_real_evidence_cannot_satisfy_an_onsite_requirement() -> None:
    """The onsite guard: however much host evidence there is, it is not the guest."""
    snapshot = _unblocked()
    snapshot["p.virtio_blk_read_back"] = Observation(
        "p.virtio_blk_read_back",
        True,
        "host side only",
        refs=(
            EvidenceRef(
                path="runs/exp-x/serial.log",
                kind="serial_log",
                sha256="0" * 64,
                scope=EvidenceScope.HOST_REAL,
            ),
        ),
    )
    result = gate.evaluate("M6", snapshot=snapshot)
    assert result.verdict.kind == VerdictKind.UNVERIFIED
    assert result.missing == ("m6.known_content_read_back",)
    assert not [ref for ref in result.verdict.evidence if ref.scope == EvidenceScope.ONSITE]


def test_a_blocker_beats_the_evidence_that_would_otherwise_pass(tmp_path: Path) -> None:
    """Blockers are checked first: no kernel, no judgement about how it failed."""
    (tmp_path / "guest.log").write_text(
        _guest_log_lines(
            "unixnu.config_read", "unixnu.boot_entry", "unixnu.load"
        ),
        encoding="utf-8",
    )
    result = gate.evaluate("M2", _view_for(tmp_path))
    assert result.verdict.kind == VerdictKind.BLOCKED
    assert result.unmet_blockers == ("built_unixnu_kernel_image",)
    assert "no UniXNU kernel image" in result.blocker


# ---------------------------------------------------------------------------
# 7. Every veto is reachable: the PRD "not enough to pass" column is live
# ---------------------------------------------------------------------------


def _veto_only_docs_scripts_downloads() -> dict[str, Observation]:
    """PRD 206: nothing measurable, only documents, scripts and downloads."""
    return {
        "p.source_pins": _obs("p.source_pins", frozen=True, problems=[]),
        "p.pinned_qemu": _obs("p.pinned_qemu", ok=False),
        "p.pinned_firmware": _obs("p.pinned_firmware", ok=False),
        "p.clean_build_manifest": _obs("p.clean_build_manifest", ok=False),
        "p.ai_interface_smoke": _obs("p.ai_interface_smoke", ok=False),
    }


def _veto_qemu_window_only() -> dict[str, Observation]:
    """PRD 207: a window came up, the firmware never logged, the guest never showed."""
    return {
        **_unblocked(),
        "p.real_run_directory": _obs("p.real_run_directory"),
        "p.edk2_firmware_boot": _obs("p.edk2_firmware_boot", ok=False),
        "p.guest_display": _obs("p.guest_display", ok=False),
    }


def _veto_static_image_only() -> dict[str, Observation]:
    """PRD 208: a picture, and no guest channel at all."""
    return {**_unblocked(), "p.screendump": _obs("p.screendump")}


def _veto_component_names_only() -> dict[str, Observation]:
    """PRD 209: the chain printed a payload name and nothing that traces a handoff."""
    return {
        **_unblocked(),
        "p.darwinboot_handoff": _obs("p.darwinboot_handoff", ok=False),
        "p.esp_efi_chain": _obs("p.esp_efi_chain", darwinboot=False),
    }


def _veto_single_banner_only() -> dict[str, Observation]:
    """PRD 210: one kernel-ish line was observed, and no page-table state."""
    return {
        **_unblocked(),
        "p.xnu_page_tables": _obs("p.xnu_page_tables", ok=False, missing=["xnu.page_tables"]),
        "p.xnu_exception_state": _obs(
            "p.xnu_exception_state", present=["xnu.exception_vectors"]
        ),
    }


def _veto_cannot_say_devices_work() -> dict[str, Observation]:
    """PRD 211: services matched, nothing says a device works."""
    return {
        **_unblocked(),
        "p.iokit_service_match": _obs("p.iokit_service_match", present=["iokit.service_match"]),
        "p.iokit_driver_probe": _obs("p.iokit_driver_probe", ok=False),
    }


def _veto_enumeration_only() -> dict[str, Observation]:
    """PRD 212: the device was enumerated, nothing was read back."""
    return {
        **_unblocked(),
        "p.virtio_blk_probe": _obs("p.virtio_blk_probe", present=["virtio_blk.enumerated"]),
        "p.virtio_blk_read_back": _obs(
            "p.virtio_blk_read_back", ok=False, missing=["virtio_blk.read_back"]
        ),
    }


def _veto_mockfs_process_only() -> dict[str, Observation]:
    """PRD 213: a test process ran, and nothing mounted."""
    return {
        **_unblocked(),
        "p.system_mount": _obs("p.system_mount", ok=False),
        "p.system_probe": _obs("p.system_probe", present=["mockfs.started"]),
    }


def _veto_mockfs_pid1_as_launchd() -> dict[str, Observation]:
    """PRD 214 / appendix A: PID 1 is a test program from a fake guest."""
    return {
        **_unblocked(),
        "p.launchd_running": _obs(
            "p.launchd_running", present=["launchd.running"], simulated={"launchd.running": True}
        ),
    }


def _veto_homescreen_screenshot_only() -> dict[str, Observation]:
    """PRD 215: a screenshot, and no interaction reached the guest."""
    return {
        **_unblocked(),
        "p.screendump": _obs("p.screendump"),
        "p.springboard_interaction": _obs("p.springboard_interaction", ok=False),
    }


def _veto_wallpaper_only() -> dict[str, Observation]:
    """PRD 216: a desktop, and no app frame."""
    return {
        **_unblocked(),
        "p.screendump": _obs("p.screendump"),
        "p.uikit_first_frame": _obs("p.uikit_first_frame", ok=False),
    }


def _veto_ram_disk_only() -> dict[str, Observation]:
    """PRD 217: written, and never read back from disk."""
    return {
        **_unblocked(),
        "p.data_written": _obs("p.data_written"),
        "p.data_read_back": _obs("p.data_read_back", ok=False),
    }


def _veto_copy_only_install() -> dict[str, Observation]:
    """PRD 218: the install stopped at a copy stage."""
    return {
        **_unblocked(),
        "p.install_record": _obs("p.install_record", copy_only_stages=["transferred"]),
    }


def _veto_no_drm_means_allowed() -> dict[str, Observation]:
    """PRD 219: allowed on a no-DRM basis, with no signature check behind it."""
    return {
        **_unblocked(),
        "p.policy_allow": _obs("p.policy_allow", present=["policy.allow"]),
        "p.policy_signature": _obs("p.policy_signature", ok=False),
    }


def _veto_process_then_crash() -> dict[str, Observation]:
    """PRD 220: the process appeared and then died."""
    return {
        **_unblocked(),
        "p.app_process": _obs("p.app_process", present=["app.process", "app.crash"]),
    }


def _veto_host_key_no_guest_response() -> dict[str, Observation]:
    """PRD 221: the host window got the key and the guest did nothing."""
    return {**_unblocked(), "p.input_delivered": _obs("p.input_delivered")}


#: refusal id -> the snapshot in which that refusal, and only that refusal, fires.
VETO_SNAPSHOTS: dict[str, Any] = {
    "m0.veto.readme_scripts_downloads": _veto_only_docs_scripts_downloads,
    "m1.veto.qemu_window_only": _veto_qemu_window_only,
    "m2.veto.static_image_only": _veto_static_image_only,
    "m3.veto.component_names_only": _veto_component_names_only,
    "m4.veto.single_banner": _veto_single_banner_only,
    "m5.veto.cannot_say_devices_work": _veto_cannot_say_devices_work,
    "m6.veto.enumeration_only": _veto_enumeration_only,
    "m7.veto.mockfs_test_process_only": _veto_mockfs_process_only,
    "m8.veto.mockfs_pid1_as_launchd": _veto_mockfs_pid1_as_launchd,
    "m9.veto.homescreen_screenshot_only": _veto_homescreen_screenshot_only,
    "m9a.veto.wallpaper_only": _veto_wallpaper_only,
    "m9b.veto.ram_disk_only": _veto_ram_disk_only,
    "m10.veto.copy_or_unzip_only": _veto_copy_only_install,
    "m10a.veto.no_drm_means_allowed": _veto_no_drm_means_allowed,
    "m11.veto.process_then_crash": _veto_process_then_crash,
    "m12.veto.host_key_no_guest_response": _veto_host_key_no_guest_response,
    "wp00.veto.readme_scripts_downloads": _veto_only_docs_scripts_downloads,
    "wp01.veto.readme_scripts_downloads": _veto_only_docs_scripts_downloads,
    "wp02.veto.qemu_window_only": _veto_qemu_window_only,
    "wp03.veto.component_names_only": _veto_component_names_only,
    "wp03.veto.static_image_only": _veto_static_image_only,
    "wp04.veto.single_banner": _veto_single_banner_only,
}


def test_every_registered_veto_has_a_proven_trigger() -> None:
    """A veto nobody can fire is decoration; the table must cover them all."""
    registered = {
        refusal.refusal_id
        for check in gate.CHECKS.values()
        for refusal in check.refusals
    }
    assert registered, "there must be vetoes"
    assert set(VETO_SNAPSHOTS) == registered, (
        "unproven vetoes: %s" % sorted(registered - set(VETO_SNAPSHOTS))
    )


@pytest.mark.parametrize("refusal_id", sorted(VETO_SNAPSHOTS))
def test_the_veto_fires_and_quotes_the_prd(refusal_id: str) -> None:
    check = next(
        item
        for item in gate.CHECKS.values()
        for refusal in item.refusals
        if refusal.refusal_id == refusal_id
    )
    refusal = next(item for item in check.refusals if item.refusal_id == refusal_id)
    result = gate.evaluate(check.check_id, snapshot=VETO_SNAPSHOTS[refusal_id]())
    assert result.verdict.kind == VerdictKind.FAIL, result.verdict.reasons
    assert result.fired_refusals == (refusal_id,)
    assert refusal_id in result.verdict.reasons[0]
    assert refusal.text in result.verdict.reasons[0]


def test_a_veto_beats_a_technically_satisfied_evidence_set() -> None:
    """The veto is a veto: it is checked before the pass evidence is counted."""
    snapshot = _veto_copy_only_install()
    snapshot["p.install_registered"] = Observation(
        "p.install_registered",
        True,
        "registered",
        refs=(_guest_ref("runs/exp-x/guest.log"),),
    )
    result = gate.evaluate("M10", snapshot=snapshot)
    assert result.verdict.kind == VerdictKind.FAIL
    assert result.fired_refusals == ("m10.veto.copy_or_unzip_only",)


# ---------------------------------------------------------------------------
# 8. Release verdict: the two independent gates of PRD 226
# ---------------------------------------------------------------------------


def _synth_result(check_id: str, kind: str, onsite: bool = False) -> Any:
    check = gate.get_check(check_id)
    if kind == VerdictKind.PASS:
        ref = (
            _guest_ref("runs/exp-synth/guest.log")
            if onsite
            else EvidenceRef(
                path="runs/exp-synth/serial.log",
                kind="serial_log",
                sha256="0" * 64,
                scope=EvidenceScope.HOST_REAL,
            )
        )
        verdict = Verdict.pass_((ref,), reasons=("synthetic",))
    elif kind == VerdictKind.FAIL:
        verdict = Verdict.fail(("synthetic",))
    elif kind == VerdictKind.BLOCKED:
        verdict = Verdict.blocked("synthetic")
    else:
        verdict = Verdict.unverified("synthetic")
    return gate.MilestoneResult(
        check_id=check.check_id,
        milestone=check.milestone,
        gate_id=check.gate_id,
        tier=check.tier,
        prd_citation=check.prd_citation,
        goal=check.goal,
        verdict=verdict,
        satisfied=(),
        missing=(),
        fired_refusals=(),
        unmet_blockers=() if kind == VerdictKind.PASS else ("synthetic",),
        implementation=check.implementation,
        windows_status=(
            "SIMULATION PASS" if kind == VerdictKind.PASS else "FAIL"
        ),
        onsite_status="PASS" if kind == VerdictKind.PASS else "BLOCKED",
        blocker="" if kind == VerdictKind.PASS else "synthetic",
        evidence=verdict.evidence,
        field_unverified=check.field_unverified,
    )


def _release_inputs(kind: str, onsite: bool = False) -> tuple:
    return tuple(
        _synth_result(check_id, kind, onsite)
        for check_id in (*MILESTONE_ROWS, *WORK_PACKAGES)
    )


def test_a_green_blocking_tier_with_an_onsite_witness_releases() -> None:
    assert gate.release_verdict(_release_inputs(VerdictKind.PASS, onsite=True)) == ("PASS", ())


def test_a_green_suite_without_the_guest_object_cannot_release() -> None:
    """A green suite is not a guest.  PRD 226's second gate."""
    verdict, reasons = gate.release_verdict(_release_inputs(VerdictKind.PASS, onsite=False))
    assert verdict == "BLOCKED"
    assert reasons == ("no_onsite_evidence",)


def test_one_blocking_failure_fails_the_release() -> None:
    results = list(_release_inputs(VerdictKind.PASS, onsite=True))
    results[3] = _synth_result("M3", VerdictKind.FAIL)
    verdict, reasons = gate.release_verdict(results)
    assert verdict == "FAIL"
    assert reasons == ("blocking_fail:M3",)


def test_one_blocking_block_blocks_the_release() -> None:
    results = list(_release_inputs(VerdictKind.PASS, onsite=True))
    results[4] = _synth_result("M4", VerdictKind.BLOCKED)
    verdict, reasons = gate.release_verdict(results)
    assert verdict == "BLOCKED"
    assert reasons == ("blocking_by:M4",)


def test_one_blocking_unverified_leaves_the_release_unverified() -> None:
    results = list(_release_inputs(VerdictKind.PASS, onsite=True))
    results[0] = _synth_result("M0", VerdictKind.UNVERIFIED)
    verdict, reasons = gate.release_verdict(results)
    assert verdict == "UNVERIFIED"
    assert reasons == ("blocking_unverified:M0",)


def test_a_work_package_failure_does_not_block_the_release() -> None:
    """PRD 226: the work packages are not release gates."""
    results = list(_release_inputs(VerdictKind.PASS, onsite=True))
    for check_id in WORK_PACKAGES:
        index = [item.check_id for item in results].index(check_id)
        results[index] = _synth_result(check_id, VerdictKind.FAIL)
    assert gate.release_verdict(results) == ("PASS", ())


def test_an_empty_result_set_is_unverified_not_a_pass() -> None:
    assert gate.release_verdict(()) == ("UNVERIFIED", ("no_blocking_checks",))


# ---------------------------------------------------------------------------
# 9. Fail-closed probes
# ---------------------------------------------------------------------------


def test_a_probe_that_raises_becomes_a_failure_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(view: HostView) -> Observation:
        raise RuntimeError("probe exploded")

    monkeypatch.setitem(gate._PROBES, "p.host_inventory", boom)
    snapshot = gate._snapshot(_view_for(tmp_path))
    observation = snapshot["p.host_inventory"]
    assert observation.ok is False
    assert observation.detail == "probe_error:RuntimeError"


def test_a_probe_that_returns_the_wrong_type_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(gate._PROBES, "p.host_inventory", lambda view: "looks fine")
    snapshot = gate._snapshot(_view_for(tmp_path))
    assert snapshot["p.host_inventory"].detail.startswith("probe_returned_non_observation")


def test_the_snapshot_is_built_once_per_view(host_view: HostView) -> None:
    first = gate._snapshot(host_view)
    second = gate._snapshot(host_view)
    assert first is second
    assert len(first) == len(gate._PROBES)


# ---------------------------------------------------------------------------
# 10. Reporting shape
# ---------------------------------------------------------------------------


def test_every_result_round_trips_through_json(host_results: tuple) -> None:
    for result in host_results:
        payload = json.loads(json.dumps(result.to_dict()))
        assert payload["check_id"] == result.check_id
        assert payload["verdict"]["kind"] == result.verdict.kind
        assert isinstance(payload["evidence"], list)


def test_the_blocker_reason_names_what_is_missing(host_results: tuple) -> None:
    for result in host_results:
        if result.verdict.kind != VerdictKind.BLOCKED:
            continue
        assert len(result.blocker) > 40, result.check_id
        assert any(
            token in result.blocker
            for token in ("kernel", "XNU", "install service", "frozen", "guest")
        ), result.check_id


def test_a_host_real_requirement_is_reported_as_the_windows_half(
    host_by_id: dict,
) -> None:
    """PRD 6.1: WP03's host-side evidence is real; its guest half is not.

    The guest half's word changed from BLOCKED to UNVERIFIED, for the reason
    :func:`test_guest_dependent_milestones_are_blocked_on_this_host` records: the
    only thing making it BLOCKED was the ``built_unixnu_kernel_image`` blocker, and
    that blocker is genuinely satisfied now.  ``BLOCKED`` is defined to require a
    *named* unmet blocker (record.py), so with none left the honest word is
    UNVERIFIED -- and it still names the two requirements WP03 is missing.
    """
    if _real_boot_root() is None:
        pytest.skip("local-private/out/real-boot is absent (gitignored, PRD 12)")
    result = host_by_id["WP03"]
    assert result.windows_status == "SIMULATION PASS"
    assert "wp03.efi_loaders" in result.satisfied
    assert "wp03.actual_addresses" in result.satisfied
    assert "wp03.configuration" in result.satisfied
    assert result.onsite_status == "UNVERIFIED"
    assert result.missing, "UNVERIFIED must name what is missing, not be vague"


# ---------------------------------------------------------------------------
# F9: one four-field vocabulary, declared in one findable place
# ---------------------------------------------------------------------------


def test_the_vocabulary_is_declared_once_in_record() -> None:
    """F9: ``record.py`` is the only module that *declares* the words.

    ``runner.py``, ``e2e.py``, ``milestones.py`` and ``p1.py`` used to each
    spell out their own copy, and the copies disagreed: two said
    ``SIMULATION PASS`` and one said a bare ``PASS``, which is a different
    claim.  Importing the same tuple object is what makes the disagreement
    impossible to reintroduce by editing one file.
    """
    import unipad.acceptance.e2e as e2e
    import unipad.acceptance.runner as runner

    assert gate.WINDOWS_STATUSES is record.WINDOWS_STATUSES
    assert gate.ONSITE_STATUSES is record.ONSITE_STATUSES
    assert runner._FIELD_ORDER["windows_status"] is record.WINDOWS_STATUSES
    assert runner._FIELD_ORDER["onsite_status"] is record.ONSITE_STATUSES
    assert runner._FIELD_ORDER["implementation"] is record.CODE_STATES
    assert e2e.FIELD_VOCABULARY["windows_status"] is record.WINDOWS_STATUSES
    assert e2e.FIELD_VOCABULARY["onsite_status"] is record.ONSITE_STATUSES
    assert e2e.FIELD_VOCABULARY["implementation"] is record.CODE_STATES


def test_every_module_emits_only_canonical_words(host_results: tuple) -> None:
    """F9: no module may emit a word outside the canonical vocabulary.

    The Windows column can only say ``SIMULATION PASS`` / ``FAIL`` / ``NOT
    RUN``, and the on-site column only ``PASS`` / ``FAIL`` / ``BLOCKED`` /
    ``UNVERIFIED``.  A row carrying anything else is unreadable next to a row
    from another module.
    """
    for result in host_results:
        assert result.windows_status in record.WINDOWS_STATUSES, result.check_id
        assert result.onsite_status in record.ONSITE_STATUSES, result.check_id
        assert result.implementation in record.CODE_STATES, result.check_id


def test_the_windows_column_never_says_a_bare_pass(host_results: tuple) -> None:
    """F9: ``PASS`` alone is the on-site column's word, not the Windows one.

    This is the whole point of the rename.  A reader matching on the string
    ``PASS`` must not accidentally match the Windows column of one module and
    the on-site column of another, so the Windows column spells out
    ``SIMULATION PASS``.  Both halves are checked: the canonical tuple must not
    contain the bare word, and no emitted row may carry it either -- a module
    that keeps emitting its old local ``PASS`` is exactly the drift this pins.
    """
    assert "PASS" not in record.WINDOWS_STATUSES
    assert "SIMULATION PASS" in record.WINDOWS_STATUSES
    for result in host_results:
        assert result.windows_status != "PASS", result.check_id
        assert result.windows_status in record.WINDOWS_STATUSES, result.check_id


def test_the_alias_table_maps_the_local_words_and_refuses_the_rest() -> None:
    """F9: the documented mapping is total over the aliases and closed otherwise.

    ``milestones.py`` reached for ``PASS`` / ``UNVERIFIED`` / ``not-applicable``
    in its Windows column.  Each has a canonical meaning, and an unmapped word
    raises instead of passing through -- a silent passthrough is exactly how the
    four vocabularies drifted apart.
    """
    assert record.canonical_windows_status("PASS") == "SIMULATION PASS"
    # Host requirements exist and are unmet: the host half did not hold.
    assert record.canonical_windows_status("UNVERIFIED") == "FAIL"
    # No host requirement was declared at all: nothing ran here.
    assert record.canonical_windows_status("not-applicable") == "NOT RUN"
    for word in record.WINDOWS_STATUSES:
        assert record.canonical_windows_status(word) == word
    for word in ("pass", "Green", "SIMULATION-PASS", "", "verified"):
        with pytest.raises(UniPadError) as raised:
            record.canonical_windows_status(word)
        assert "unknown windows_status" in str(raised.value), word


def test_the_onsite_column_can_say_fail() -> None:
    """F9: a failure that happened on site must not be laundered to UNVERIFIED.

    ``FourFields.onsite_status`` returns ``FAIL`` when on-site evidence exists
    and the verdict is not a pass.  The old declared tuple omitted ``FAIL``, so
    the property could emit a word no vocabulary contained -- and any caller
    matching the column against the declared set would have missed it.
    """
    import unipad.acceptance.runner as runner

    ref = record.EvidenceRef(
        path="runs/exp-synthetic/guest.log",
        kind="guest_log",
        sha256="0" * 64,
        scope=record.EvidenceScope.ONSITE,
        witness="runs/exp-synthetic/guest.log:1:event",
    )
    failing = record.Verdict(
        record.VerdictKind.FAIL, ("synthetic_onsite_failure",), (ref,), True
    )
    fields = record.FourFields("synthetic", failing, (ref,))
    assert fields.onsite_status == "FAIL"
    assert fields.onsite_status in record.ONSITE_STATUSES
    assert fields.windows_status in record.WINDOWS_STATUSES
    # The runner's roll-up order must be able to place it too.
    assert "FAIL" in runner._FIELD_ORDER["onsite_status"]


# ---------------------------------------------------------------------------
# F8: one verdict contract, not four
# ---------------------------------------------------------------------------


def test_the_verdict_class_is_shared_not_reimplemented() -> None:
    """F8: ``Verdict`` is record's, in every module that has one.

    ``milestones.Verdict`` used to be a second spelling of the same four
    invariants -- and had already drifted: it required a reason slug only for
    ``FAIL``, so a ``BLOCKED`` milestone could be built with no reason at all.
    ``e2e.CaseVerdict`` is a subclass, which is how a module adds properties
    without re-declaring the rules.
    """
    import unipad.acceptance.e2e as e2e
    import unipad.acceptance.runner as runner

    assert gate.Verdict is record.Verdict
    assert issubclass(e2e.CaseVerdict, record.Verdict)
    assert e2e.CaseVerdict is not record.Verdict
    # The runner consumes the same class the gate produces.
    assert runner.Verdict is record.Verdict


def test_the_evidence_reference_is_shared_not_reimplemented() -> None:
    """F8: the evidence reference is record's, narrowed by kind set only.

    The two local copies each re-spelled the path, digest and witness rules with
    their own messages; the rules agreed, so the duplication bought nothing and
    cost the guarantee that they would keep agreeing.  What legitimately differs
    is the *kind* set, and that is now a ``ClassVar`` override rather than a
    second class.
    """
    import unipad.acceptance.e2e as e2e

    assert gate.EvidenceRef.__mro__[1] is record.EvidenceRef
    assert issubclass(e2e.EvidenceRef, record.EvidenceRef)
    # The kind sets differ on purpose, and each is a superset of nothing shared
    # by accident: e2e's is wider because a case declares bundle-less kinds.
    assert "trace_log" in gate.EvidenceRef.ALLOWED_KINDS
    assert "trace_log" not in record.EvidenceRef.ALLOWED_KINDS
    assert "qemu_args" in e2e.EvidenceRef.ALLOWED_KINDS
    assert "qemu_args" not in record.EvidenceRef.ALLOWED_KINDS
    assert set(record.EVIDENCE_KINDS) <= set(e2e.EvidenceRef.ALLOWED_KINDS)


def test_the_verdict_kinds_and_badness_order_are_shared() -> None:
    """F8: one kind list, one badness order, one roll-up, for the whole gate."""
    import unipad.acceptance.e2e as e2e
    import unipad.acceptance.p1 as p1
    import unipad.acceptance.runner as runner

    assert gate.VerdictKind is record.VerdictKind
    assert e2e.VERDICT_KINDS is record.VERDICT_KINDS
    assert p1.VERDICT_KINDS is record.VERDICT_KINDS
    assert gate._VERDICT_KINDS is record.VERDICT_KINDS
    assert e2e._SEVERITY is record.SEVERITY
    assert p1._BADNESS is record.SEVERITY
    assert e2e.worst_of is record.worst_of
    assert p1.worst_of is record.worst_of
    assert runner.worst_of is record.worst_of
    assert record.SEVERITY[record.VerdictKind.FAIL] == 3
    assert record.SEVERITY[record.VerdictKind.PASS] == 0


def test_the_evidence_scopes_are_shared() -> None:
    """F8: three modules each declared the three scopes; now there is one."""
    import unipad.acceptance.e2e as e2e
    import unipad.acceptance.p1 as p1

    assert gate.EvidenceScope is record.EvidenceScope
    assert e2e.EVIDENCE_SCOPES == (
        record.EvidenceScope.SIMULATION,
        record.EvidenceScope.HOST_REAL,
        record.EvidenceScope.ONSITE,
    )
    assert p1.SCOPES == (
        record.EvidenceScope.SIMULATION,
        record.EvidenceScope.HOST_REAL,
    )


def test_every_verdict_kind_can_carry_evidence() -> None:
    """F8: ``blocked`` and ``unverified`` accept evidence, not just ``pass``.

    A check that could not run still has to cite the artefact it looked at
    before giving up -- that is what makes a BLOCKED row arguable.  The
    factories used to drop the evidence on the floor, which pushed callers into
    building verdicts by hand, which is how four contracts came to exist.
    """
    ref = record.EvidenceRef(path="runs/x/serial.log", kind="serial_log", sha256="0" * 64)
    held = record.Verdict.blocked("no_system_image", [ref])
    assert held.evidence == (ref,)
    assert held.kind == record.VerdictKind.BLOCKED
    unheld = record.Verdict.unverified("nobody_ran_it", [ref])
    assert unheld.evidence == (ref,)
    assert unheld.kind == record.VerdictKind.UNVERIFIED
    # The factories still refuse to invent a pass.
    with pytest.raises(UniPadError):
        record.Verdict.pass_([], ["looks_green"])


def test_the_local_copies_are_gone() -> None:
    """F8: the four parallel implementations no longer exist as source.

    This is the regression guard for the migration itself.  It reads the module
    source rather than the imported names, because a class that was deleted and
    then re-added under a new name would still satisfy every identity assertion
    above.
    """
    import unipad.acceptance.e2e as e2e
    import unipad.acceptance.p1 as p1

    gate_source = inspect.getsource(gate)
    assert "class Verdict:" not in gate_source, "milestones re-declared Verdict"
    assert "class VerdictKind:" not in gate_source, "milestones re-declared VerdictKind"
    assert "class EvidenceScope:" not in gate_source, "milestones re-declared EvidenceScope"

    e2e_source = inspect.getsource(e2e)
    assert "class CaseVerdict(record.Verdict)" in e2e_source
    assert "def worst_of(" not in e2e_source, "e2e re-declared worst_of"
    assert "_SEVERITY: dict[str, int] = {" not in e2e_source, "e2e re-declared the badness order"

    p1_source = inspect.getsource(p1)
    assert "def worst_of(" not in p1_source, "p1 re-declared worst_of"
    assert "_BADNESS = {" not in p1_source, "p1 re-declared the badness order"
    assert "def _make_verdict(" in p1_source, (
        "p1's construction guard is still the module's own; it must keep validating "
        "against the shared vocabulary"
    )
