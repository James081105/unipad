"""Acceptance tests for the PRD 5.2 end-to-end gate (``unipad.acceptance.e2e``).

What is pinned here
-------------------
* **The ten PRD 5.2 cases are a sequence, not a list.**  ``case1`` is evaluated
  first and every later case declares its predecessor, so a case whose
  predecessor did not reach ``PASS`` is ``BLOCKED`` with a
  ``predecessor not satisfied`` reason -- never skipped, never ``PASS``.
* **Case 1 gates everything and is BLOCKED on this host.**  There is no built
  UniXNU kernel image, no system image and no guest install service here
  (``docs/implementation-plan.md`` "Tier-1 启动关键路径现状"), so the reason
  must *name* what is missing rather than reporting a pass or an omission.
* **Every case declares the evidence it needs and refuses ``PASS`` without it.**
* **The two axes never collapse.**  A run in which all ten cases read ``PASS``
  on simulation-scoped evidence still releases ``BLOCKED`` with
  ``no_onsite_evidence``; the same run with ``onsite`` evidence releases
  ``PASS``.  That pair is the load-bearing test of the four-field rule.
* **PRD 5.1's two samples are prerequisites.**  A sample that does not exist is
  ``BLOCKED`` with a reason; one that violates a build constraint is ``FAIL``.
  Neither is a pass, and neither disappears.

No real QEMU is launched and no network is touched: the ten actions are
performed by a stub runtime through the documented :class:`E2ERuntime` seam, and
the one place the real host is observed (case 1) only reads files and asks the
pinned QEMU binary for its version string.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from unipad.acceptance import e2e
from unipad.acceptance.e2e import (
    BLOCKED,
    EVIDENCE_KINDS,
    FAIL,
    HOST_REAL,
    ONSITE,
    PASS,
    SIMULATION,
    UNVERIFIED,
    CaseSpec,
    CaseVerdict,
    E2EEnvironment,
    E2ERuntime,
    E2ESequence,
    EvidenceRef,
    FourFields,
    HostFacts,
    StepObservation,
    assert_publishable,
    case_spec,
    coverage_gaps,
    observe_samples,
    release_verdict,
    verdict_blocked,
    verdict_fail,
    verdict_pass,
    verdict_unverified,
    verify_witness,
    worst_of,
)
from unipad.errors import ErrorCode, UniPadError
from tests.fixtures.ipa_builder import build_ipa, hello_ipa, notes_ipa

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The ten PRD 5.2 case ids, in the order PRD line 172-181 lists them.
PRD_CASE_IDS = tuple(f"case{index}" for index in range(1, 11))

#: The exact PRD line each case is cited to (docs/PRD.md:172-181).
PRD_CITATIONS = {
    "case1": "docs/PRD.md:172",
    "case2": "docs/PRD.md:173",
    "case3": "docs/PRD.md:174",
    "case4": "docs/PRD.md:175",
    "case5": "docs/PRD.md:176",
    "case6": "docs/PRD.md:177",
    "case7": "docs/PRD.md:178",
    "case8": "docs/PRD.md:179",
    "case9": "docs/PRD.md:180",
    "case10": "docs/PRD.md:181",
}

#: The PRD 5.1 sample names (docs/PRD.md:163-164).
SAMPLE_NAMES = ("HelloUniPad.ipa", "UniPadNotes.ipa")

#: The source manifest every PRD 5.1 sample must carry (docs/PRD.md:166).
SOURCE_MANIFEST: dict[str, Any] = {
    "own_source": True,
    "signature_mode": "recorded-by-source-manifest",
    "embedded_frameworks": [],
    "permissions": [],
    "trust_approach": "recorded-by-source-manifest",
    "source_root": "tests/fixtures",
}

STAMP = "2026-10-23T00:00:00Z"

_HEX = "a" * 64

#: A literal that is legal inside a witness: non-space first character, and it
#: must survive the ``<file>:<lineno>:<literal>`` grammar in e2e.py:229.
_WITNESSABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _\-.:]{0,80}")


# ---------------------------------------------------------------------------
# the stub runtime: performs the ten PRD 5.2 actions against a synthetic guest
# ---------------------------------------------------------------------------


class StubRuntime(E2ERuntime):
    """A runtime that performs every PRD 5.2 step and records what it observed.

    ``scope`` decides the environment axis: with ``ONSITE`` every reference
    carries a witness naming a line of the file it points at; with anything
    less, no witness is attached and the release verdict must refuse to pass.
    ``fail_steps`` turns individual steps into observed failures.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        scope: str = ONSITE,
        fail_steps: tuple[str, ...] = (),
        dead_framebuffer: bool = False,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.scope = scope
        self.fail_steps = set(fail_steps)
        self.dead_framebuffer = dead_framebuffer
        self.evidence_dir = self.run_dir / e2e.EVIDENCE_DIRNAME
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.calls: list[str] = []

    # -- helpers ----------------------------------------------------------

    def _ref(
        self,
        filename: str,
        payload: str | bytes,
        kind: str,
        *,
        witness: str | None = None,
    ) -> EvidenceRef:
        data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        (self.evidence_dir / filename).write_bytes(data)
        if self.scope != ONSITE:
            named: str | None = None
        else:
            # ``onsite`` evidence is refused outright without a witness
            # (e2e.py:onsite_evidence_without_witness), so name the first line
            # of the file we just wrote -- exactly what verify_witness re-reads.
            named = witness or self._first_line_witness(filename, data)
        return EvidenceRef(
            path=f"{e2e.EVIDENCE_DIRNAME}/{filename}",
            kind=kind,
            sha256=hashlib.sha256(data).hexdigest(),
            scope=self.scope,
            witness=named,
            observed_at=STAMP,
        )

    @staticmethod
    def _first_line_witness(filename: str, data: bytes) -> str:
        """A witness of the shape ``<file>:<lineno>:<literal>`` for line 1."""
        lines = data.decode("utf-8", errors="replace").splitlines()
        found = _WITNESSABLE.search(lines[0]) if lines else None
        if found is None:
            raise AssertionError(f"no witnessable literal on line 1 of {filename}")
        return f"{filename}:1:{found.group(0)}"

    def _obs(
        self,
        step: str,
        refs: tuple[EvidenceRef, ...],
        **facts: Any,
    ) -> StepObservation:
        self.calls.append(step)
        if step in self.fail_steps:
            return StepObservation(False, reason=f"{step}_refused_by_guest", evidence=refs)
        return StepObservation(True, evidence=refs, facts=dict(facts))

    # -- the ten PRD 5.2 actions ------------------------------------------

    def start_new_qemu_process(self) -> StepObservation:
        refs = (
            self._ref("case1-host-inventory.json", '{"schema_version": 1}\n', "host_inventory"),
            self._ref("case1-experiment.json", '{"schema_version": 1}\n', "experiment_json"),
            self._ref("case1-qemu-version.txt", "QEMU emulator version 11.1.0\n", "qemu_version"),
            self._ref("case1-serial.log", "M1: UEFI firmware started\n", "serial_log"),
            self._ref("case1-qmp.log", '{"QMP": {"version": {}}}\n', "qmp_log"),
        )
        return self._obs("case1", refs, argv=["-accel", "tcg"], pid=4242)

    def reach_springboard(self) -> StepObservation:
        before = b"P6\n4 4\n255\n" + bytes(48)
        after = b"P6\n4 4\n255\n" + bytes(range(48)) if not self.dead_framebuffer else before
        refs = (
            self._ref("case2-guest.log", "launchd[1]: SpringBoard up\n", "guest_log",
                      witness="case2-guest.log:1:SpringBoard up"),
            self._ref("case2-screen-before.ppm", before, "screendump"),
            self._ref("case2-screen-after.ppm", after, "screendump"),
            self._ref("case2-input-journal.jsonl", '{"events": []}\n', "input_journal"),
            self._ref("case2-process.txt", "pid 61 SpringBoard\n", "process_evidence"),
        )
        return self._obs(
            "case2", refs, springboard_pid=61,
            framebuffer_before=hashlib.sha256(before).hexdigest(),
            framebuffer_after=hashlib.sha256(after).hexdigest(),
        )

    def import_hello_ipa(self) -> StepObservation:
        refs = (
            self._ref("case3-package.sha256", _HEX + "\n", "package_sha256"),
            self._ref("case3-precheck.json", '{"verdict": "accept"}\n', "precheck_report"),
            self._ref("case3-preinstall.json", '{"installed": []}\n', "preinstall_status"),
        )
        return self._obs("case3", refs, package_sha256=_HEX, installed_before=[])

    def install_hello_ipa(self) -> StepObservation:
        refs = (
            self._ref("case4-precheck.json", '{"verdict": "accept"}\n', "precheck_report"),
            self._ref("case4-transfer.json", '{"verified": true}\n', "transfer_receipt"),
            self._ref("case4-install.json", '{"stage": "registered"}\n', "install_record"),
            self._ref("case4-icon.txt", "icon registered for com.unipad.hello\n", "icon_evidence"),
            self._ref("case4-bundle-id.txt", "com.unipad.hello\n", "bundle_id"),
        )
        return self._obs(
            "case4", refs, stage="registered", bundle_id="com.unipad.hello",
            icon=True, package_sha256=_HEX,
        )

    def launch_hello_ipa(self) -> StepObservation:
        refs = (
            self._ref("case5-process.txt", "pid 812 HelloUniPad\n", "process_evidence"),
            self._ref("case5-challenge.txt", _HEX + "\n", "challenge_value"),
            self._ref("case5-window.ppm", b"P6\n4 4\n255\n" + bytes(48), "screenshot"),
        )
        return self._obs(
            "case5", refs, pid=812, challenge_value=_HEX, expected_challenge=_HEX,
        )

    def tap_button_ten_times(self) -> StepObservation:
        refs = (
            self._ref("case6-input-journal.jsonl", '{"taps": 10}\n', "input_journal"),
            self._ref("case6-counter.txt", "1 2 3 4 5 6 7 8 9 10\n", "counter_trace"),
            self._ref("case6-relaunch.txt", "reopened after home\n", "relaunch_evidence"),
        )
        return self._obs(
            "case6", refs, tap_count=10, counter_sequence=list(range(1, 11)),
            relaunched=True,
        )

    def import_notes_ipa_and_save_text(self) -> StepObservation:
        marker = "unipad-e2e-marker-0001"
        refs = (
            self._ref("case7-install.json", '{"stage": "registered"}\n', "install_record"),
            self._ref("case7-package.sha256", _HEX + "\n", "package_sha256"),
            self._ref("case7-saved.txt", marker + "\n", "saved_text"),
            self._ref("case7-read-back.txt", marker + "\n", "read_back_text"),
        )
        return self._obs("case7", refs, saved_text=marker, read_back_text=marker)

    def shutdown_and_reboot_from_disk(self) -> StepObservation:
        refs = (
            self._ref("case8-shutdown.json", '{"graceful": true}\n', "shutdown_record"),
            self._ref("case8-new-process.txt", "pid 5151\n", "new_process_record"),
            self._ref(
                "case8-registry.json",
                '{"installed": ["com.unipad.hello", "com.unipad.notes"]}\n',
                "post_reboot_registry",
            ),
            self._ref("case8-marker.txt", "unipad-e2e-marker-0001\n", "post_reboot_marker"),
        )
        return self._obs(
            "case8", refs, old_pid=4242, new_pid=5151, graceful_shutdown=True,
            installed_bundle_ids=["com.unipad.hello", "com.unipad.notes"],
            marker_read_back="unipad-e2e-marker-0001",
        )

    def repeat_cold_boot_three_times(self) -> StepObservation:
        cycles = [
            {"cycle": index, "duration_s": 1.0 + index, "result": "retained",
             "evidence_path": f"{e2e.EVIDENCE_DIRNAME}/case9-cycle-{index}.json"}
            for index in (1, 2, 3)
        ]
        for cycle in cycles:
            self._ref(
                Path(cycle["evidence_path"]).name,
                json.dumps(cycle, sort_keys=True) + "\n",
                "cycle_results",
            )
        refs = (
            self._ref(
                "case9-timings.json",
                json.dumps([cycle["duration_s"] for cycle in cycles]) + "\n",
                "boot_timings",
            ),
            self._ref(
                "case9-cycles.json", json.dumps(cycles, sort_keys=True) + "\n",
                "cycle_results",
            ),
            self._ref(
                "case9-prior-failure.txt",
                "prior failure evidence kept; not overwritten\n",
                "failure_preserved",
            ),
        )
        return self._obs(
            "case9", refs, cycles=cycles,
            evidence_paths=[cycle["evidence_path"] for cycle in cycles],
        )

    def import_unsupported_platform_package(self) -> StepObservation:
        refs = (
            self._ref(
                "case10-precheck.json",
                '{"verdict": "reject", "fault_codes": ["unsupported_arch"]}\n',
                "precheck_report",
            ),
            self._ref(
                "case10-rejection.txt",
                "package targets a Simulator platform: Info.plist=iPhoneSimulator\n",
                "rejection_reason",
            ),
            self._ref(
                "case10-status.json",
                '{"installed": ["com.unipad.hello", "com.unipad.notes"]}\n',
                "post_rejection_status",
            ),
        )
        return self._obs(
            "case10", refs, error_code=ErrorCode.UNSUPPORTED_ARCH,
            reason="simulator platform",
            previously_installed_recoverable=True,
        )


# ---------------------------------------------------------------------------
# environment builders
# ---------------------------------------------------------------------------


def _sample_tree(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """Build both PRD 5.1 samples and their source manifests under ``tmp_path``."""
    root = tmp_path / "samples"
    root.mkdir(parents=True, exist_ok=True)
    hello_ipa(root / "HelloUniPad.ipa")
    notes_ipa(root / "UniPadNotes.ipa")
    manifests: dict[str, Path] = {}
    for name in SAMPLE_NAMES:
        manifest = tmp_path / f"{name}.source.json"
        manifest.write_text(json.dumps(SOURCE_MANIFEST, sort_keys=True), encoding="utf-8")
        manifests[name] = manifest
    return root, manifests


def _green_environment(
    tmp_path: Path,
    *,
    scope: str = ONSITE,
    fail_steps: tuple[str, ...] = (),
    dead_framebuffer: bool = False,
    samples: dict | None = None,
) -> E2EEnvironment:
    """An environment whose host facts claim a bootable guest exists.

    ``missing=()`` is the only lie this builder tells, and it is a lie about the
    *host*, not about the gate: it exists so the ten acceptance predicates can
    be exercised without a real QEMU.  Everything else -- the samples, the
    evidence, the dependency propagation -- is the real code path.
    """
    run_dir = tmp_path / "runs" / "exp-green"
    sample_root, manifests = _sample_tree(tmp_path)
    env = E2EEnvironment(
        repo_root=REPO_ROOT,
        run_dir=run_dir,
        sample_roots=(sample_root,),
        sample_source_manifests=manifests,
        verify_qemu_version=False,
    )
    evidence_dir = run_dir / e2e.EVIDENCE_DIRNAME
    evidence_dir.mkdir(parents=True, exist_ok=True)
    observed = samples if samples is not None else observe_samples(env, evidence_dir, STAMP)
    facts = HostFacts(
        host_inventory={"schema_version": 1, "host": {"os": "Microsoft Windows 11", "kernel": "10.0.26200"},
                        "cpu": {"cores_physical": 4, "threads_logical": 8},
                        "memory": {"total_gb": 15.8}},
        host_inventory_sha256=_HEX,
        host_label="4C/8T 15.8GB Microsoft Windows 11",
        accel="tcg",
        qemu_binary="local-private/tools/qemu/qemu-system-aarch64.exe",
        qemu_binary_present=True,
        qemu_version="QEMU emulator version 11.1.0",
        qemu_sha256="b" * 64,
        firmware_path="local-private/tools/qemu/share/edk2-aarch64-code.fd",
        kernel_path="artifacts/kernel.macho",
        kernel_sha256="c" * 64,
        system_image="artifacts/system.img",
        data_image="artifacts/data.img",
        guest_service_reachable=True,
        springboard_pid=61,
        system_version=(17, 7, 11),
        samples=observed,
        missing=(),
        notes=(),
    )
    return E2EEnvironment(
        repo_root=REPO_ROOT,
        run_dir=run_dir,
        sample_roots=(sample_root,),
        sample_source_manifests=manifests,
        runtime=StubRuntime(
            run_dir, scope=scope, fail_steps=fail_steps, dead_framebuffer=dead_framebuffer
        ),
        host_facts=facts,
        verify_qemu_version=False,
    )


# ---------------------------------------------------------------------------
# the registry: ten cases, in PRD order, with the right citations
# ---------------------------------------------------------------------------


def test_the_registry_has_the_ten_prd_cases_in_prd_order():
    """docs/PRD.md:172-181 -- ten cases, in the order the PRD lists them."""
    assert tuple(spec.case_id for spec in e2e.CASES) == PRD_CASE_IDS
    assert [spec.prd_citation for spec in e2e.CASES] == [
        PRD_CITATIONS[case_id] for case_id in PRD_CASE_IDS
    ]
    assert coverage_gaps() == ()


def test_every_case_declares_a_predecessor_except_the_first():
    """The dependency chain the PRD implies, declared as data.

    case 4 depends on 3, case 5 on 4, case 7 on 6 and case 8 on 7 are the
    load-bearing edges; the rest make the sequence total rather than partial.
    """
    declared = {spec.case_id: spec.depends_on for spec in e2e.CASES}
    assert declared["case1"] == ()
    for index in range(2, 11):
        assert declared[f"case{index}"] == (f"case{index - 1}",)
    for case_id, dep in (
        ("case4", "case3"),
        ("case5", "case4"),
        ("case7", "case6"),
        ("case8", "case7"),
    ):
        assert dep in declared[case_id]


def test_every_case_declares_the_evidence_it_needs():
    """A case with no declared evidence could pass on nothing (PRD 9.2)."""
    for spec in e2e.CASES:
        assert spec.required_evidence, spec.case_id
        unknown = sorted(set(spec.required_evidence) - EVIDENCE_KINDS)
        assert unknown == [], (spec.case_id, unknown)
    assert case_spec("case1").required_evidence == (
        "host_inventory",
        "experiment_json",
        "qemu_args",
        "qemu_version",
        "serial_log",
        "qmp_log",
    )


def test_a_case_spec_refuses_an_unregistered_probe_or_unknown_evidence():
    """Import-time validation: a typo is a CONFIG_INVALID, not a silent case."""
    with pytest.raises(UniPadError) as excinfo:
        CaseSpec(
            case_id="caseX",
            prd_citation="docs/PRD.md:172",
            title="a case with no probe",
            depends_on=(),
            required_evidence=("qemu_args",),
            probe="not_a_registered_probe",
        )
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    with pytest.raises(UniPadError) as excinfo:
        CaseSpec(
            case_id="caseX",
            prd_citation="docs/PRD.md:172",
            title="a case declaring an unknown evidence kind",
            depends_on=(),
            required_evidence=("telepathy",),
            probe="case1_start_new_qemu_process",
        )
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    with pytest.raises(UniPadError) as excinfo:
        CaseSpec(
            case_id="caseX",
            prd_citation="docs/PRD.md:172",
            title="a case declaring no evidence at all",
            depends_on=(),
            required_evidence=(),
            probe="case1_start_new_qemu_process",
        )
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_unknown_case_id_is_refused():
    with pytest.raises(UniPadError) as excinfo:
        case_spec("case11")
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_worst_of_orders_by_information_content():
    """A definite failure outranks an unknown block (DESIGN section 4).

    The empty set fails closed to ``UNVERIFIED``, not to ``PASS``.  This
    module's old private ``worst_of`` seeded ``worst = PASS``, so an e2e stage
    with no cases at all would have rolled up to a pass -- an empty stage
    reading as success is exactly the shape the gate exists to refuse.  The
    canonical roll-up in :mod:`unipad.acceptance.record` fails closed, and this
    assertion is what pins that the two can never drift apart again.
    """
    assert worst_of([]) == UNVERIFIED
    assert worst_of([PASS, PASS]) == PASS
    assert worst_of([PASS, UNVERIFIED]) == UNVERIFIED
    assert worst_of([UNVERIFIED, BLOCKED]) == BLOCKED
    assert worst_of([BLOCKED, FAIL]) == FAIL
    assert worst_of([PASS, FAIL, BLOCKED]) == FAIL
    with pytest.raises(UniPadError):
        worst_of(["MAYBE"])


def test_the_roll_up_is_the_canonical_one():
    """F8: e2e aggregates by record's badness order, not a private copy.

    ``worst_of`` is re-exported, so there is one roll-up in the gate.  The
    identity check is the point: a module that re-declared its own function
    would still pass every behavioural assertion above while disagreeing with
    the milestone gate about what "worst" means.
    """
    import unipad.acceptance.record as record

    assert worst_of is record.worst_of


# ---------------------------------------------------------------------------
# axis A: a PASS cannot be constructed without evidence
# ---------------------------------------------------------------------------


def test_a_pass_verdict_requires_evidence_and_an_observation():
    """The four combinations that would fake a success all raise."""
    ref = EvidenceRef(path="acceptance-e2e/x.txt", kind="manifest", sha256=_HEX)

    with pytest.raises(UniPadError) as excinfo:
        CaseVerdict(PASS, (), (), False)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    with pytest.raises(UniPadError) as excinfo:
        CaseVerdict(PASS, (), (), True)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    with pytest.raises(UniPadError) as excinfo:
        CaseVerdict(PASS, (), (ref,), False)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    with pytest.raises(UniPadError) as excinfo:
        verdict_pass(())
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    # ... and the real thing still constructs
    assert verdict_pass((ref,), ("ok",)).kind == PASS


def test_a_non_pass_verdict_requires_a_reason():
    ref = EvidenceRef(path="acceptance-e2e/x.txt", kind="manifest", sha256=_HEX)
    for build in (verdict_blocked, verdict_unverified):
        with pytest.raises(UniPadError) as excinfo:
            build("   ")
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    with pytest.raises(UniPadError) as excinfo:
        verdict_fail(())
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    assert verdict_blocked("no_kernel").kind == BLOCKED
    assert verdict_blocked("no_kernel").observed is False
    assert verdict_unverified("nobody_measured_it").kind == UNVERIFIED
    assert verdict_fail(("condition_not_met",), (ref,)).kind == FAIL


# ---------------------------------------------------------------------------
# evidence hygiene (PRD 9.2 line 397)
# ---------------------------------------------------------------------------


def test_evidence_ref_rejects_absolute_paths_and_unhashed_bytes():
    """No absolute user path, no traversal, no unexplained digest.

    The refusal *messages* are now the canonical ones from
    :class:`unipad.acceptance.record.EvidenceRef`, because that is the class
    doing the refusing (F8).  What the assertions pin is that each rule still
    fires and names itself -- not which spelling of the name it uses.
    """
    with pytest.raises(UniPadError) as excinfo:
        EvidenceRef(path="C:/Users/someone/runs/serial.log", kind="serial_log", sha256=_HEX)
    assert "absolute" in excinfo.value.message
    assert excinfo.value.details.get("absolute_path_in_evidence")

    for bad in ("/var/log/serial.log", "\\\\server\\share\\serial.log", "../escape.log",
                "runs/../../escape.log", "runs\\serial.log"):
        with pytest.raises(UniPadError):
            EvidenceRef(path=bad, kind="serial_log", sha256=_HEX)

    with pytest.raises(UniPadError) as excinfo:
        EvidenceRef(path="serial.log", kind="serial_log", sha256=None, digest_reason=None)
    assert "digest" in excinfo.value.message
    assert excinfo.value.details.get("digest_reason") is None

    with pytest.raises(UniPadError) as excinfo:
        EvidenceRef(path="serial.log", kind="serial_log", sha256="NOTHEX")
    assert "digest" in excinfo.value.message

    with pytest.raises(UniPadError) as excinfo:
        EvidenceRef(path="serial.log", kind="telepathy", sha256=_HEX)
    assert "unknown evidence kind" in excinfo.value.message

    # a streaming artefact is admissible when it says why it has no digest
    streaming = EvidenceRef(
        path="serial.log", kind="serial_log", sha256=None,
        digest_reason="streamed from the qemu pipe while the run was live",
    )
    assert streaming.digest_reason


def test_onsite_evidence_requires_a_witness_that_names_a_line():
    """``onsite`` without a witness cannot be constructed at all.

    The message is the canonical one (F8); the assertion is that the refusal
    happens and that it names the path it refused.
    """
    with pytest.raises(UniPadError) as excinfo:
        EvidenceRef(path="guest.log", kind="guest_log", sha256=_HEX, scope=ONSITE)
    assert "witness" in excinfo.value.message
    assert excinfo.value.details.get("onsite_evidence_without_witness") == "guest.log"

    with pytest.raises(UniPadError) as excinfo:
        EvidenceRef(
            path="guest.log", kind="guest_log", sha256=_HEX, scope=ONSITE,
            witness="not-a-witness",
        )
    assert "witness" in excinfo.value.message

    ok = EvidenceRef(
        path="guest.log", kind="guest_log", sha256=_HEX, scope=ONSITE,
        witness="guest.log:2:SpringBoard up",
    )
    assert ok.witness


def test_a_witness_is_re_found_in_the_file_it_names(tmp_path):
    """A hand-raised scope is only trusted when the literal is really there."""
    log = tmp_path / e2e.EVIDENCE_DIRNAME / "guest.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("launchd[1]: boot\nlaunchd[1]: SpringBoard up\n", encoding="utf-8")

    honest = EvidenceRef(
        path=f"{e2e.EVIDENCE_DIRNAME}/guest.log", kind="guest_log", sha256=_HEX,
        scope=ONSITE, witness=f"{e2e.EVIDENCE_DIRNAME}/guest.log:2:SpringBoard up",
    )
    assert verify_witness(honest, tmp_path) is True

    fabricated = EvidenceRef(
        path=f"{e2e.EVIDENCE_DIRNAME}/guest.log", kind="guest_log", sha256=_HEX,
        scope=ONSITE, witness=f"{e2e.EVIDENCE_DIRNAME}/guest.log:1:SpringBoard up",
    )
    assert verify_witness(fabricated, tmp_path) is False

    missing = EvidenceRef(
        path=f"{e2e.EVIDENCE_DIRNAME}/absent.log", kind="guest_log", sha256=_HEX,
        scope=ONSITE, witness=f"{e2e.EVIDENCE_DIRNAME}/absent.log:1:anything",
    )
    assert verify_witness(missing, tmp_path) is False

    host_only = EvidenceRef(
        path=f"{e2e.EVIDENCE_DIRNAME}/guest.log", kind="guest_log", sha256=_HEX,
        scope=HOST_REAL, witness=f"{e2e.EVIDENCE_DIRNAME}/guest.log:2:SpringBoard up",
    )
    assert verify_witness(host_only, tmp_path) is False


def test_the_publication_guard_refuses_paths_and_tokens():
    """PRD 9.2 line 397: a leak becomes a refusal, not a leaked bundle."""
    for leak in (
        "see C:/Users/someone/AppData/run.log",
        "see F:\\iosvirtual\\artifacts\\kernel.macho",
        "token ghp_ABCDEFGHIJKLMNOPQRSTUVWX",
        "Authorization: Bearer abcdefghijklmnop",
        "-----BEGIN RSA PRIVATE KEY-----",
    ):
        with pytest.raises(UniPadError) as excinfo:
            assert_publishable(leak)
        assert excinfo.value.code == ErrorCode.CONFIG_INVALID

    # a declared digest is evidence, not a secret
    assert_publishable(json.dumps({"sha256": _HEX, "path": "acceptance-e2e/x.json"}))
    assert_publishable("blocked: kernel_image, system_image")


# ---------------------------------------------------------------------------
# axis B: the four fields cannot be raised by the wrong environment
# ---------------------------------------------------------------------------


def test_simulation_evidence_cannot_raise_the_onsite_field():
    """DESIGN 9.6: a hundred simulation refs leave the field UNVERIFIED."""
    refs = tuple(
        EvidenceRef(path=f"acceptance-e2e/fake-{index}.json", kind="manifest",
                    sha256=hashlib.sha256(str(index).encode()).hexdigest(),
                    scope=SIMULATION)
        for index in range(50)
    )
    verdict = verdict_pass(refs)
    fields = FourFields(
        check_id="case1", verdict=verdict, evidence=refs,
        implementation_evidence=refs[:1], code_state="CODE COMPLETE",
    )
    assert fields.windows_status == "SIMULATION PASS"
    assert fields.onsite_status == "UNVERIFIED"
    assert fields.implementation == "CODE COMPLETE"
    assert fields.to_dict()["onsite_status"] == "UNVERIFIED"


def test_code_complete_alone_cannot_raise_the_onsite_field():
    """A green suite is not a guest."""
    refs = tuple(
        EvidenceRef(path=f"acceptance-e2e/host-{index}.json", kind="manifest",
                    sha256=hashlib.sha256(str(index).encode()).hexdigest(),
                    scope=HOST_REAL)
        for index in range(3)
    )
    fields = FourFields(
        check_id="case1", verdict=verdict_pass(refs), evidence=refs,
        implementation_evidence=refs[:1], code_state="CODE COMPLETE",
    )
    assert fields.implementation == "CODE COMPLETE"
    assert fields.windows_status == "SIMULATION PASS"
    assert fields.onsite_status == "UNVERIFIED"


def test_onsite_evidence_with_a_pass_verdict_is_the_only_way_to_pass():
    witness = "guest.log:1:SpringBoard up"
    refs = (
        EvidenceRef(path=f"{e2e.EVIDENCE_DIRNAME}/guest.log", kind="guest_log",
                    sha256=_HEX, scope=ONSITE, witness=witness),
    )
    fields = FourFields(
        check_id="case2", verdict=verdict_pass(refs), evidence=refs,
        blocker=None, implementation_evidence=refs, code_state="CODE COMPLETE",
    )
    assert fields.onsite_status == "PASS"
    assert fields.windows_status == "NOT RUN"  # no simulation/host_real ref at all


def test_a_blocked_field_cannot_carry_an_empty_blocker():
    """``onsite_status == BLOCKED`` needs a blocker; it is filled, not optional."""
    refs = (EvidenceRef(path="acceptance-e2e/x.json", kind="manifest", sha256=_HEX),)
    fields = FourFields(check_id="case1", verdict=verdict_blocked("kernel_image"), evidence=refs)
    assert fields.blocker == "kernel_image"
    assert fields.onsite_status == "BLOCKED"
    assert fields.windows_status == "FAIL"
    assert fields.to_dict()["blocker"] == "kernel_image"

    with pytest.raises(UniPadError) as excinfo:
        FourFields(check_id="case1", verdict=verdict_pass(refs), code_state="MAYBE")
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


def test_code_complete_without_implementation_evidence_degrades():
    refs = (EvidenceRef(path="acceptance-e2e/x.json", kind="manifest", sha256=_HEX),)
    fields = FourFields(
        check_id="case1", verdict=verdict_pass(refs), evidence=refs,
        code_state="CODE COMPLETE",
    )
    assert fields.implementation == "UNVERIFIED"


# ---------------------------------------------------------------------------
# case 1 on this host: BLOCKED, naming what is missing
# ---------------------------------------------------------------------------


def test_case1_is_blocked_on_this_host_and_names_what_is_missing(tmp_path):
    """PRD 5.2 case 1 (docs/PRD.md:172): the case that gates everything.

    This host has no built UniXNU kernel image, no configured system image and
    no guest install service, so the verdict must be ``BLOCKED`` and the reason
    must name each missing item rather than reporting a pass.
    """
    env = e2e.E2EEnvironment.create(tmp_path / "runs" / "exp-real-host")
    sequence = E2ESequence(env)

    report = sequence.run(run_id="exp-real-host")
    first = report.case("case1")

    assert first.verdict.kind == BLOCKED
    assert first.attempted is True
    reason = " ".join(first.verdict.reasons)
    assert "kernel" in reason
    assert "system image" in reason
    assert "guest install service" in reason
    assert "SpringBoard" in reason

    # it names the *things*, not a vague "environment problem"
    assert "kernel_image" in sequence.facts.missing
    assert "system_image" in sequence.facts.missing
    assert "guest_install_service" in sequence.facts.missing
    assert "springboard_process" in sequence.facts.missing

    # the host facts were really observed, not defaulted
    assert sequence.facts.qemu_binary_present is True
    assert sequence.facts.qemu_version.startswith("QEMU emulator version")
    assert sequence.facts.accel == "tcg"
    assert sequence.facts.host_inventory_sha256


def test_case1_records_the_real_host_evidence_it_did_collect(tmp_path):
    """The blocked case still leaves hashable evidence behind (PRD 9.2)."""
    run_dir = tmp_path / "runs" / "exp-real-host"
    sequence = E2ESequence(e2e.E2EEnvironment.create(run_dir))
    report = sequence.run()

    inventory = run_dir / e2e.EVIDENCE_DIRNAME / "host-inventory.json"
    assert inventory.is_file()
    digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
    assert sequence.facts.host_inventory_sha256 == digest
    assert report.environment["host_fingerprint"] == digest
    assert report.environment["accel"] == "tcg"

    # every recorded reference points at a file that exists and still hashes
    for row in report.evidence_index:
        target = run_dir / row["path"]
        assert target.is_file(), row["path"]
        if row["sha256"]:
            assert hashlib.sha256(target.read_bytes()).hexdigest() == row["sha256"]


def test_no_absolute_user_path_or_secret_reaches_the_report(tmp_path):
    """PRD 9.2 line 397, enforced on the real host run."""
    sequence = E2ESequence(e2e.E2EEnvironment.create(tmp_path / "runs" / "exp-real-host"))
    report = sequence.run()

    text = report.to_json()          # raises if anything leaks
    document = json.loads(text)
    assert document["aggregate"]["verdict"] == BLOCKED
    for outcome in document["cases"]:
        assert_publishable(json.dumps(outcome), where="case")
        for reason in outcome["verdict"]["reasons"]:
            assert_publishable(reason, where="reason")


# ---------------------------------------------------------------------------
# dependency propagation
# ---------------------------------------------------------------------------


def test_a_blocked_predecessor_blocks_every_dependent_case(tmp_path):
    """A case whose predecessor is BLOCKED is BLOCKED, not skipped, not PASS."""
    report = E2ESequence(
        e2e.E2EEnvironment.create(tmp_path / "runs" / "exp-real-host")
    ).run()

    assert [outcome.case_id for outcome in report.cases] == list(PRD_CASE_IDS)
    for outcome in report.cases:
        assert outcome.verdict.kind == BLOCKED, outcome.case_id

    for case_id in PRD_CASE_IDS[1:]:
        outcome = report.case(case_id)
        reasons = " ".join(outcome.verdict.reasons)
        assert "predecessor not satisfied" in reasons
        assert f"predecessor_not_satisfied:case{int(case_id[4:]) - 1}:BLOCKED" in reasons
        # the case is present in the report with a real verdict -- it did not
        # silently disappear, and it is not a pass
        assert outcome.attempted is False
        assert outcome.verdict.observed is False


def test_propagation_never_produces_a_pass_from_a_non_pass_predecessor(tmp_path):
    """DESIGN 7.2 rule 4: propagation can only ever lower a verdict."""
    report = E2ESequence(
        e2e.E2EEnvironment.create(tmp_path / "runs" / "exp-real-host")
    ).run()
    assert all(outcome.verdict.kind != PASS for outcome in report.cases)
    assert report.verdict == BLOCKED
    assert all("case_blocked:" in reason for reason in report.aggregate["reasons"])


def test_a_passing_predecessor_lets_the_successor_run_its_own_probe(tmp_path):
    """Propagation must not merely copy the predecessor's verdict.

    With a runtime that performs case 1 but refuses case 2, case 2 must be
    ``FAIL`` with its *own* reason -- the propagation rule never rewrites a
    definite outcome, so a real failure survives.
    """
    env = _green_environment(tmp_path, fail_steps=("case2",))
    report = E2ESequence(env).run()

    assert report.case("case1").verdict.kind == PASS
    second = report.case("case2")
    assert second.verdict.kind == FAIL
    assert "case2_step_failed:case2_refused_by_guest" in second.verdict.reasons
    assert not any("predecessor" in reason for reason in second.verdict.reasons)
    assert second.attempted is True


def test_a_screenshot_that_never_changes_is_not_an_operable_desktop(tmp_path):
    """PRD 7.4 line 294: a static frame proves nothing about input."""
    env = _green_environment(tmp_path, dead_framebuffer=True)
    report = E2ESequence(env).run()

    assert report.case("case1").verdict.kind == PASS
    second = report.case("case2")
    assert second.verdict.kind == FAIL
    assert "case2_springboard_not_operable" in second.verdict.reasons


def test_the_declared_chain_4_5_7_8_is_enforced(tmp_path):
    """The four edges the PRD names explicitly, each with its own blocker."""
    env = _green_environment(tmp_path, fail_steps=("case4",))
    report = E2ESequence(env).run()

    assert report.case("case3").verdict.kind == PASS
    fourth = report.case("case4")
    assert fourth.verdict.kind == FAIL
    assert "case4_step_failed" in fourth.verdict.reasons[0]

    # case 5 depends on 4 and inherits the block; case 6 is downstream of it
    fifth = report.case("case5")
    assert fifth.verdict.kind == BLOCKED
    assert "predecessor_not_satisfied:case4:FAIL" in fifth.verdict.reasons
    assert "predecessor not satisfied: case4 is FAIL" in fifth.verdict.reasons
    assert report.case("case6").verdict.kind == BLOCKED
    assert "predecessor_not_satisfied:case5:BLOCKED" in report.case("case6").verdict.reasons
    assert "predecessor not satisfied: case5 is BLOCKED" in report.case("case6").verdict.reasons


def test_the_runtime_is_asked_for_each_step_in_prd_order(tmp_path):
    """The sequence is walked in order; no case is asked before its turn."""
    env = _green_environment(tmp_path, fail_steps=("case3",))
    runtime = env.runtime
    assert isinstance(runtime, StubRuntime)
    E2ESequence(env).run()

    assert runtime.calls == list(PRD_CASE_IDS[:3])


# ---------------------------------------------------------------------------
# evidence requirements refuse a PASS
# ---------------------------------------------------------------------------


def test_a_case_that_produces_no_declared_evidence_cannot_pass(tmp_path):
    """DESIGN 9.1: no default probe, no evidence-free pass."""
    env = _green_environment(tmp_path)
    runtime = env.runtime
    assert isinstance(runtime, StubRuntime)
    # case 1 succeeds but hands back nothing at all
    runtime.start_new_qemu_process = lambda: StepObservation(True, evidence=(), facts={})
    report = E2ESequence(env).run()

    first = report.case("case1")
    assert first.verdict.kind == UNVERIFIED
    assert "missing_evidence:case1:" in first.verdict.reasons[0]
    for kind in first.verdict.reasons[0].split(":", 2)[2].split(","):
        assert kind in case_spec("case1").required_evidence
    # and the whole sequence follows it down
    assert report.case("case2").verdict.kind == BLOCKED
    assert report.verdict == BLOCKED


def test_the_full_sequence_passes_only_when_every_declared_kind_is_present(tmp_path):
    """All ten cases PASS on a complete, witnessed evidence set."""
    report = E2ESequence(_green_environment(tmp_path)).run()

    assert [outcome.verdict.kind for outcome in report.cases] == [PASS] * 10
    for outcome in report.cases:
        declared = set(case_spec(outcome.case_id).required_evidence)
        assert declared <= outcome.verdict.evidence_kinds, outcome.case_id
    assert report.run_complete is True
    assert report.coverage_gaps == ()


# ---------------------------------------------------------------------------
# the release verdict reads the field axis
# ---------------------------------------------------------------------------


def test_a_green_simulation_run_still_releases_blocked(tmp_path):
    """DESIGN 9.9: a green suite is not a guest.

    Every case reads ``PASS`` and every reference is real and hashed; because
    none of them is ``onsite``, the release verdict is ``BLOCKED``.
    """
    report = E2ESequence(_green_environment(tmp_path, scope=SIMULATION)).run()

    assert [outcome.verdict.kind for outcome in report.cases] == [PASS] * 10
    onsite = [
        ref
        for outcome in report.cases
        for ref in outcome.verdict.evidence
        if ref.scope == ONSITE
    ]
    assert onsite == []

    verdict, reasons = release_verdict(report)
    assert verdict == BLOCKED
    assert reasons == ("no_onsite_evidence",)
    assert report.verdict == BLOCKED
    assert report.aggregate["reasons"] == ["no_onsite_evidence"]

    # every case is listed as field-unverified, with its blocker
    assert len(report.field_unverified) == 10
    assert {row["case_id"] for row in report.field_unverified} == set(PRD_CASE_IDS)
    assert all(row["blocker"] for row in report.field_unverified)


def test_host_real_evidence_also_cannot_release(tmp_path):
    """``HOST_REAL`` is a real host, but the object is not the target guest."""
    report = E2ESequence(_green_environment(tmp_path, scope=HOST_REAL)).run()
    assert [outcome.verdict.kind for outcome in report.cases] == [PASS] * 10
    assert release_verdict(report) == (BLOCKED, ("no_onsite_evidence",))


def test_onsite_evidence_is_what_releases(tmp_path):
    """The same ten cases, with witnesses, release PASS."""
    report = E2ESequence(_green_environment(tmp_path, scope=ONSITE)).run()

    assert [outcome.verdict.kind for outcome in report.cases] == [PASS] * 10
    verdict, reasons = release_verdict(report)
    assert (verdict, reasons) == (PASS, ())
    assert report.field_unverified == ()
    assert report.to_dict()["aggregate"]["verdict"] == PASS


def test_a_failing_case_outranks_the_missing_guest(tmp_path):
    """FAIL dominates BLOCKED, so a definite failure is never buried."""
    report = E2ESequence(_green_environment(tmp_path, fail_steps=("case6",))).run()

    verdict, reasons = release_verdict(report)
    assert verdict == FAIL
    assert reasons == ("case_failed:case6:case6_step_failed:case6_refused_by_guest",)


def test_an_unverified_case_outranks_a_block(tmp_path):
    """``worst_of``: BLOCKED is worse than UNVERIFIED, so BLOCKED wins."""
    env = _green_environment(tmp_path)
    runtime = env.runtime
    assert isinstance(runtime, StubRuntime)
    runtime.start_new_qemu_process = lambda: StepObservation(True, evidence=(), facts={})
    report = E2ESequence(env).run()

    assert report.case("case1").verdict.kind == UNVERIFIED
    verdict, reasons = release_verdict(report)
    assert verdict == BLOCKED          # case2..case10 are blocked by case1
    # the aggregate names the first blocked case and the case that blocked it
    assert reasons[0].startswith("case_blocked:case2:")
    assert "case1 is UNVERIFIED" in reasons[0]
    assert "predecessor_not_satisfied:case1:UNVERIFIED" in report.case("case2").verdict.reasons


def test_an_incomplete_run_is_never_a_pass(tmp_path):
    """A run that did not finish cannot be read as success."""
    report = E2ESequence(_green_environment(tmp_path)).run()
    assert report.run_complete is True
    object.__setattr__(report, "run_complete", False)
    assert release_verdict(report) == (e2e.INCOMPLETE, ("run_incomplete",))
    assert release_verdict(report)[0] != PASS


# ---------------------------------------------------------------------------
# PRD 5.1 sample prerequisites
# ---------------------------------------------------------------------------


def test_a_missing_sample_is_blocked_not_passed(tmp_path):
    """PRD 5.1: a sample that does not exist is a BLOCKED with a reason."""
    env = e2e.E2EEnvironment.create(
        tmp_path / "runs" / "exp-no-samples", verify_qemu_version=False
    )
    observed = observe_samples(
        env, tmp_path / "runs" / "exp-no-samples" / e2e.EVIDENCE_DIRNAME, STAMP
    )

    assert set(observed) == set(SAMPLE_NAMES)
    for name in SAMPLE_NAMES:
        sample = observed[name]
        assert sample.exists is False
        assert sample.verdict == BLOCKED
        assert f"sample_ipa_not_found:{name}" in sample.reasons
        assert sample.accepted is False
        # the reason is publishable: it names the configured root, not a path
        for reason in sample.reasons:
            assert_publishable(reason, where="sample")


def test_a_sample_that_meets_the_build_constraints_passes(tmp_path):
    """PRD 5.1 lines 163-168: Device platform, ARM64, no DRM, minimal permissions."""
    env = _green_environment(tmp_path)
    observed = env.host_facts.samples
    assert set(observed) == set(SAMPLE_NAMES)

    for name in SAMPLE_NAMES:
        sample = observed[name]
        assert sample.verdict == PASS, (name, sample.reasons)
        assert sample.exists is True
        assert sample.size_bytes > 0
        assert len(sample.sha256) == 64
        assert sample.source_recorded is True

        report = sample.precheck
        assert report["verdict"] == "accept"
        assert report["has_arm64_slice"] is True
        assert report["supported_platforms"] == ["iPhoneOS"]
        assert report["encrypted"] is False
        assert report["cryptid"] == 0
        assert report["min_os"] == "17.0"
        assert report["permissions"] == []

        kinds = {ref.kind for ref in sample.evidence}
        assert "precheck_report" in kinds
        assert "manifest" in kinds


def test_a_sample_on_the_wrong_platform_fails(tmp_path):
    """PRD 5.1: a Simulator build is not a Device build (PRD 10 package precheck)."""
    root = tmp_path / "samples"
    root.mkdir(parents=True, exist_ok=True)
    build_ipa(root / "HelloUniPad.ipa", corrupt="simulator")
    notes_ipa(root / "UniPadNotes.ipa")
    manifests = {}
    for name in SAMPLE_NAMES:
        manifest = tmp_path / f"{name}.source.json"
        manifest.write_text(json.dumps(SOURCE_MANIFEST, sort_keys=True), encoding="utf-8")
        manifests[name] = manifest

    env = E2EEnvironment(
        repo_root=REPO_ROOT, run_dir=tmp_path / "runs" / "exp-sim",
        sample_roots=(root,), sample_source_manifests=manifests,
        verify_qemu_version=False,
    )
    observed = observe_samples(
        env, tmp_path / "runs" / "exp-sim" / e2e.EVIDENCE_DIRNAME, STAMP
    )

    hello = observed["HelloUniPad.ipa"]
    assert hello.verdict == FAIL
    assert any("sample_precheck_rejected" in reason for reason in hello.reasons)
    assert hello.precheck["supported_platforms"] == ["iPhoneSimulator"]
    assert hello.precheck["has_arm64_slice"] is False
    assert ErrorCode.UNSUPPORTED_ARCH in hello.precheck["fault_codes"]

    assert observed["UniPadNotes.ipa"].verdict == PASS


def test_a_drm_protected_sample_fails(tmp_path):
    """PRD 5.1 "无 DRM": cryptid != 0 is out of scope."""
    root = tmp_path / "samples"
    root.mkdir(parents=True, exist_ok=True)
    build_ipa(root / "HelloUniPad.ipa", corrupt="encrypted")
    notes_ipa(root / "UniPadNotes.ipa")
    manifests = {
        name: _write_manifest(tmp_path, name) for name in SAMPLE_NAMES
    }

    env = E2EEnvironment(
        repo_root=REPO_ROOT, run_dir=tmp_path / "runs" / "exp-drm",
        sample_roots=(root,), sample_source_manifests=manifests,
        verify_qemu_version=False,
    )
    observed = observe_samples(
        env, tmp_path / "runs" / "exp-drm" / e2e.EVIDENCE_DIRNAME, STAMP
    )

    hello = observed["HelloUniPad.ipa"]
    assert hello.verdict == FAIL
    assert hello.precheck["encrypted"] is True
    assert hello.precheck["cryptid"] != 0
    assert ErrorCode.UNSUPPORTED_PLATFORM in hello.precheck["fault_codes"]


def test_a_sample_asking_for_too_many_permissions_fails(tmp_path):
    """PRD 5.1 "最少权限": an unrecorded privacy claim is a violation."""
    root = tmp_path / "samples"
    root.mkdir(parents=True, exist_ok=True)
    build_ipa(
        root / "HelloUniPad.ipa",
        include_plist_extra={"NSContactsUsageDescription": "reads the address book"},
    )
    notes_ipa(root / "UniPadNotes.ipa")
    manifests = {name: _write_manifest(tmp_path, name) for name in SAMPLE_NAMES}

    env = E2EEnvironment(
        repo_root=REPO_ROOT, run_dir=tmp_path / "runs" / "exp-perm",
        sample_roots=(root,), sample_source_manifests=manifests,
        verify_qemu_version=False,
    )
    observed = observe_samples(
        env, tmp_path / "runs" / "exp-perm" / e2e.EVIDENCE_DIRNAME, STAMP
    )

    hello = observed["HelloUniPad.ipa"]
    assert hello.verdict == FAIL
    assert any(
        reason.startswith("sample_excessive_permissions:HelloUniPad.ipa:")
        for reason in hello.reasons
    )
    assert "NSContactsUsageDescription" in hello.precheck["permissions"]


def test_a_sample_whose_min_os_exceeds_the_system_fails(tmp_path):
    """PRD 5.1 "部署版本兼容所选系统"."""
    root = tmp_path / "samples"
    root.mkdir(parents=True, exist_ok=True)
    build_ipa(root / "HelloUniPad.ipa", min_os="99.0")
    notes_ipa(root / "UniPadNotes.ipa")
    manifests = {name: _write_manifest(tmp_path, name) for name in SAMPLE_NAMES}

    env = E2EEnvironment(
        repo_root=REPO_ROOT, run_dir=tmp_path / "runs" / "exp-minos",
        sample_roots=(root,), sample_source_manifests=manifests,
        verify_qemu_version=False,
    )
    observed = observe_samples(
        env, tmp_path / "runs" / "exp-minos" / e2e.EVIDENCE_DIRNAME, STAMP
    )
    hello = observed["HelloUniPad.ipa"]
    assert hello.verdict == FAIL
    assert any(
        reason.startswith("sample_min_os_incompatible:HelloUniPad.ipa")
        for reason in hello.reasons
    )


def test_a_sample_without_a_recorded_source_is_unverified(tmp_path):
    """PRD 5.1 line 166: signature, frameworks, permissions and trust must be recorded."""
    root = tmp_path / "samples"
    root.mkdir(parents=True, exist_ok=True)
    hello_ipa(root / "HelloUniPad.ipa")
    notes_ipa(root / "UniPadNotes.ipa")

    env = E2EEnvironment(
        repo_root=REPO_ROOT, run_dir=tmp_path / "runs" / "exp-nosrc",
        sample_roots=(root,), sample_source_manifests={},
        verify_qemu_version=False,
    )
    observed = observe_samples(
        env, tmp_path / "runs" / "exp-nosrc" / e2e.EVIDENCE_DIRNAME, STAMP
    )
    for name in SAMPLE_NAMES:
        sample = observed[name]
        assert sample.verdict == UNVERIFIED
        assert f"sample_source_not_recorded:{name}" in sample.reasons
        assert sample.source_recorded is False


def test_a_missing_sample_blocks_the_case_that_needs_it(tmp_path):
    """PRD 5.2 case 3 cannot import a sample this host does not have."""
    env = _green_environment(tmp_path)
    facts = env.host_facts
    assert facts is not None
    broken = HostFacts(
        host_inventory=facts.host_inventory,
        host_inventory_sha256=facts.host_inventory_sha256,
        host_label=facts.host_label,
        accel=facts.accel,
        qemu_binary=facts.qemu_binary,
        qemu_binary_present=facts.qemu_binary_present,
        qemu_version=facts.qemu_version,
        qemu_sha256=facts.qemu_sha256,
        kernel_path=facts.kernel_path,
        system_image=facts.system_image,
        data_image=facts.data_image,
        guest_service_reachable=True,
        springboard_pid=61,
        system_version=(17, 7, 11),
        samples={},
        missing=(),
    )
    env = E2EEnvironment(
        repo_root=REPO_ROOT, run_dir=env.run_dir,
        sample_roots=env.sample_roots, runtime=env.runtime, host_facts=broken,
        verify_qemu_version=False,
    )
    report = E2ESequence(env).run()

    third = report.case("case3")
    assert third.verdict.kind == BLOCKED
    assert "sample_not_registered:HelloUniPad.ipa" in third.verdict.reasons
    # the sample prerequisite is checked before the guest prerequisite
    assert not any("host_cannot_boot" in reason for reason in third.verdict.reasons)


# ---------------------------------------------------------------------------
# the report document
# ---------------------------------------------------------------------------


def test_the_report_carries_the_four_fields_for_every_case(tmp_path):
    """CLAUDE.md four-field rule: recorded per case, not recalled afterwards."""
    report = E2ESequence(_green_environment(tmp_path, scope=SIMULATION)).run()
    document = report.to_dict()

    assert [row["check_id"] for row in document["four_fields"]] == list(PRD_CASE_IDS)
    for row in document["four_fields"]:
        assert set(row) == {
            "check_id", "verdict", "implementation",
            "windows_status", "onsite_status", "blocker",
        }
        assert row["implementation"] == "CODE COMPLETE"
        assert row["windows_status"] == "SIMULATION PASS"
        assert row["onsite_status"] == "UNVERIFIED"
        assert row["verdict"] == PASS

    assert len(document["field_unverified"]) == 10
    assert document["aggregate"]["verdict"] == BLOCKED
    assert document["schema_version"] == 1
    assert document["coverage_gaps"] == []


def test_the_report_is_json_round_trippable_and_stable(tmp_path):
    report = E2ESequence(_green_environment(tmp_path, scope=ONSITE)).run()
    first = report.to_json()
    second = report.to_json()
    assert first == second

    document = json.loads(first)
    assert document["run_id"]
    assert document["environment"]["accel"] == "tcg"
    assert document["environment"]["python_version"]
    assert "Users" not in first and "C:/" not in first and "F:\\" not in first
    assert len(document["evidence_index"]) >= 10


def test_the_sequence_refuses_to_build_on_a_registry_with_gaps(monkeypatch):
    """A missing case is a failure, not a disappearance (DESIGN 9.22)."""
    monkeypatch.setattr(e2e, "CASES", e2e.CASES[:9])
    monkeypatch.setattr(e2e, "_CASE_INDEX", {s.case_id: s for s in e2e.CASES[:9]})
    assert e2e.coverage_gaps() == ("case_registry_mismatch",)
    with pytest.raises(UniPadError) as excinfo:
        E2ESequence(e2e.E2EEnvironment.create(Path("runs") / "unused"))
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert "coverage_gap" not in excinfo.value.message


# ---------------------------------------------------------------------------
# helpers used above
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, name: str) -> Path:
    manifest = tmp_path / f"{name}.source.json"
    manifest.write_text(json.dumps(SOURCE_MANIFEST, sort_keys=True), encoding="utf-8")
    return manifest
