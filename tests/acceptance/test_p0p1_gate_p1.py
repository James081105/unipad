"""Acceptance tests for the six PRD 3.2 P1 bullets (docs/PRD.md:86-91).

What these tests are
--------------------
They run :mod:`unipad.acceptance.p1` -- one check per bullet, verbatim from the
PRD -- against the real tree on this Windows host, and then assert the things a
gate has to be able to say:

* the registry covers **exactly** the six bullets, in PRD order, with the PRD's
  own sentence as the requirement and the right line citation;
* every check produced a well-formed result, and the aggregate is never a pass
  while there is no guest evidence;
* the four fields are reported per bullet, and **no bullet can read
  ``onsite_status == PASS``** on this host, because no check here can construct
  an ``onsite`` evidence reference;
* the anti-fabrication gates hold: a ``PASS`` verdict cannot be built without
  evidence, and an evidence path cannot be absolute;
* the known P1 (1) gap -- the self-controlled HTTPS package server has no
  production caller, so nothing starts it -- is reported, not papered over.

What these tests deliberately do not do
---------------------------------------
They do not start QEMU, do not boot a guest and do not install an IPA.  Every
guest-dependent claim is asserted to be *blocked*, which is the honest state on
a host with no iPadOS guest.  Field status stays ``BLOCKED``/``UNVERIFIED``.

Load-bearing, and brittle on purpose
-------------------------------------
The "current state" assertions below pin facts about the tree as it is now.  If
one of them fails because someone *fixed* the thing it pins, that is the gate
doing its job: update the assertion, not the check.  The docstring on each such
test says which fact it pins.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

# Repository root: tests/acceptance/test_p0p1_gate_p1.py -> repo.
ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.acceptance


# ---------------------------------------------------------------------------
# Loading the module under test
# ---------------------------------------------------------------------------


def _load_p1() -> ModuleType:
    """Import ``unipad.acceptance.p1``, falling back to a direct file load.

    The package has no ``__init__.py`` of its own yet (other agents own it), and
    a sibling module mid-edit must not take this module down with it.  The direct
    load keeps this file runnable either way.
    """
    try:
        return importlib.import_module("unipad.acceptance.p1")
    except Exception:
        path = ROOT / "src" / "unipad" / "acceptance" / "p1.py"
        spec = importlib.util.spec_from_file_location("unipad_acceptance_p1_under_test", path)
        if spec is None or spec.loader is None:  # pragma: no cover - broken checkout
            raise
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


p1 = _load_p1()


# ---------------------------------------------------------------------------
# The six bullets, read back from the PRD itself
# ---------------------------------------------------------------------------

#: The verbatim bullets, transcribed from docs/PRD.md lines 86-91.  The test
#: re-reads the file so a PRD edit that moves these lines fails here instead of
#: silently re-pointing the gate at the wrong requirement.
EXPECTED_BULLETS: tuple[tuple[str, str, str], ...] = (
    ("P1-1", "86", "通过 VirtIO Net 等路线联网，优先出站网络和自控 HTTPS 服务。"),
    ("P1-2", "87", "更多自有或获授权的 IPA 兼容性测试。"),
    ("P1-3", "88", "应用卸载、更新、安装历史和清晰的错误分类。"),
    ("P1-4", "89", "旋转、可选分辨率、音频和安装包体验。"),
    ("P1-5", "90", "真正经过验证的快照/恢复；必须说明保存的是磁盘还是整机状态。"),
    ("P1-6", "91", "简洁的 Windows 启动器：开始、停止、导入 IPA、查看日志、恢复。"),
)


def _prd_line(number: str) -> str:
    """Return one line of docs/PRD.md by 1-based line number."""
    lines = (ROOT / "docs" / "PRD.md").read_text(encoding="utf-8").splitlines()
    return lines[int(number) - 1]


@pytest.fixture(scope="module")
def checks() -> tuple:
    """Run every P1 check once; the suite is the expensive part, not the asserts."""
    return p1.run_checks()


@pytest.fixture(scope="module")
def summary(checks) -> dict:
    return p1.summarise(checks)


def _by_bullet(checks, bullet: str):
    for check in checks:
        if check.bullet == bullet:
            return check
    raise AssertionError(f"no check registered for {bullet}")


def _observation(check, label: str):
    for observation in check.observations:
        if observation.label == label:
            return observation
    return None


# ---------------------------------------------------------------------------
# The registry: six bullets, verbatim, correctly cited
# ---------------------------------------------------------------------------


def test_registry_covers_exactly_the_six_prd_bullets() -> None:
    """The registry must hold one check per bullet and nothing else."""
    assert [bullet.bullet for bullet in p1.P1_BULLETS] == [row[0] for row in EXPECTED_BULLETS]
    assert [probe.__name__ for probe in p1.CHECKS] == [
        bullet.probe for bullet in p1.P1_BULLETS
    ]


@pytest.mark.parametrize(("bullet", "line", "text"), EXPECTED_BULLETS)
def test_requirement_text_is_the_prds_own_sentence(bullet: str, line: str, text: str) -> None:
    """Each check's requirement is the PRD sentence, not a paraphrase.

    Read back from ``docs/PRD.md`` so a PRD edit that moves or rewords a bullet
    fails here rather than leaving the gate quoting a requirement that is gone.
    """
    definition = next(item for item in p1.P1_BULLETS if item.bullet == bullet)
    assert definition.prd_citation == f"docs/PRD.md:{line}"
    assert definition.requirement == text
    assert _prd_line(line).strip() == f"- {text}"


def test_every_check_reports_the_prd_citation(checks) -> None:
    for check in checks:
        assert check.prd_citation.startswith("docs/PRD.md:"), check.bullet
        assert check.requirement, check.bullet


# ---------------------------------------------------------------------------
# Well-formedness: the vocabulary, and PASS is not constructible
# ---------------------------------------------------------------------------


def test_every_check_returns_a_known_verdict(checks) -> None:
    for check in checks:
        assert check.verdict in p1.VERDICT_KINDS, (check.bullet, check.verdict)


def test_every_check_produced_evidence_or_said_why_not(checks) -> None:
    """A PASS must carry evidence; anything else must say what is missing."""
    for check in checks:
        if check.verdict == p1.PASS:
            assert check.evidence, check.bullet
            assert check.reasons, check.bullet
        assert check.observations, check.bullet
        assert check.blocker, check.bullet


def test_a_pass_verdict_cannot_be_built_without_evidence() -> None:
    from unipad.errors import UniPadError

    reference = p1.Evidence("docs/PRD.md", "doc", "host_real")
    with pytest.raises(UniPadError):
        p1._make_verdict(p1.PASS, reasons=("x",), evidence=(), observed=True)
    with pytest.raises(UniPadError):
        p1._make_verdict(p1.PASS, reasons=("x",), evidence=(reference,), observed=False)


def test_a_fail_verdict_requires_a_reason() -> None:
    from unipad.errors import UniPadError

    with pytest.raises(UniPadError):
        p1._make_verdict(p1.FAIL, reasons=(), evidence=(), observed=True)


def test_a_blocked_verdict_requires_the_missing_material() -> None:
    from unipad.errors import UniPadError

    with pytest.raises(UniPadError):
        p1._make_verdict(p1.BLOCKED, reasons=(), evidence=(), observed=True)


def test_an_unknown_verdict_kind_is_refused() -> None:
    from unipad.errors import UniPadError

    with pytest.raises(UniPadError):
        p1._make_verdict("probably-fine", reasons=("x",))


def test_an_absolute_evidence_path_is_refused() -> None:
    """PRD 9.2: an absolute user path cannot enter the evidence structure."""
    from unipad.errors import UniPadError

    with pytest.raises(UniPadError):
        p1.Evidence("C:/Users/someone/secret.log", "log", "host_real")
    with pytest.raises(UniPadError):
        p1.Evidence("/home/someone/secret.log", "log", "host_real")


def test_an_onsite_evidence_scope_cannot_be_constructed() -> None:
    """No check on this host can produce guest evidence, so none may claim it."""
    from unipad.errors import UniPadError

    assert "onsite" not in p1.SCOPES
    with pytest.raises(UniPadError):
        p1.Evidence("runs/exp-1/serial.log", "serial_log", "onsite")


def test_every_evidence_path_is_repo_relative_posix(checks) -> None:
    for check in checks:
        for reference in check.evidence:
            assert not reference.path.startswith("/"), reference.path
            assert "\\" not in reference.path, reference.path
            assert ":" not in reference.path, reference.path
            assert reference.scope in p1.SCOPES, reference.scope


# ---------------------------------------------------------------------------
# The four fields, and the release judgement
# ---------------------------------------------------------------------------


def test_four_fields_are_reported_for_every_bullet(checks) -> None:
    for check in checks:
        assert check.implementation in {
            "CODE COMPLETE",
            "PARTIAL",
            "NOT STARTED",
            "UNVERIFIED",
        }, check.bullet
        assert check.windows_status in {"SIMULATION PASS", "FAIL", "NOT RUN"}, check.bullet
        assert check.onsite_status in {"PASS", "UNVERIFIED", "BLOCKED"}, check.bullet


def test_no_bullet_can_read_onsite_pass_on_this_host(checks) -> None:
    """The load-bearing half of "SIMULATION PASS is not 现场 PASS".

    ``onsite_status`` is derived from the evidence scope, and no check here can
    construct an ``onsite`` reference, so a green host run cannot promote the
    field.  Pinning it means a future edit that makes the field writable, or
    that lets a check mint guest evidence, fails here.
    """
    for check in checks:
        assert check.onsite_status != "PASS", check.bullet
        assert check.onsite_status == "BLOCKED", check.bullet
        assert check.blocker, check.bullet


def test_the_aggregate_is_never_a_pass_without_guest_evidence(summary) -> None:
    assert summary["aggregate"]["verdict"] != p1.PASS
    assert any(
        reason.startswith("no_onsite_evidence")
        for reason in summary["aggregate"]["reasons"]
    )


def test_the_field_unverified_list_names_every_bullet(summary) -> None:
    """CLAUDE.md: the final report must list the field-unverified items."""
    listed = {row["bullet"] for row in summary["field_unverified"]}
    assert listed == {row[0] for row in EXPECTED_BULLETS}
    for row in summary["field_unverified"]:
        assert row["blocker"] and row["blocker"] != "-", row


def test_windows_status_is_derived_from_the_verdict(checks) -> None:
    """The field is read-only and follows the verdict, never the reverse."""
    for check in checks:
        if check.verdict == p1.FAIL:
            assert check.windows_status == "FAIL", check.bullet
        elif check.evidence:
            assert check.windows_status == "SIMULATION PASS", check.bullet


def test_the_report_is_json_serialisable(checks) -> None:
    import json

    document = p1.to_dict(checks)
    assert json.loads(json.dumps(document)) == document


def test_the_text_report_renders_every_bullet(checks) -> None:
    text = p1.render_text(checks)
    for bullet, _, requirement in EXPECTED_BULLETS:
        assert bullet in text
        assert requirement in text


# ---------------------------------------------------------------------------
# Redaction (PRD 9.2, docs/PRD.md:397)
# ---------------------------------------------------------------------------


def test_the_report_carries_no_absolute_user_path(checks) -> None:
    text = p1.render_text(checks)
    # The same lookbehind the module uses: ``https://`` and ``10.0.2.2:8443``
    # are not drive letters, so the test must not read them as paths either.
    assert not re.search(r"(?<![\w.])[A-Za-z]:[\\/]", text), "a drive path reached the report"
    assert not re.search(r"\\\\[^\s]", text), "a UNC path reached the report"
    assert not re.search(r"/(?:home|Users|root)/", text), "a POSIX home path reached the report"


def test_the_report_carries_no_credential_shaped_run(checks) -> None:
    text = p1.render_text(checks)
    assert "ghp_" not in text
    assert "Bearer " not in text
    assert "-----BEGIN" not in text


def test_redact_keeps_a_scheme_url_intact() -> None:
    """``https://10.0.2.2:8443`` is a URL, not a drive letter."""
    assert p1.redact("https://10.0.2.2:8443/index.json") == "https://10.0.2.2:8443/index.json"


def test_redact_strips_a_drive_path() -> None:
    assert p1.redact("file=C:/Users/someone/x.log") == "file=<path>"


# ---------------------------------------------------------------------------
# P1 (1) -- networking (docs/PRD.md:86)
# ---------------------------------------------------------------------------


def test_p1_1_exercises_the_virtio_net_route(checks) -> None:
    """The real ``build_args`` ran and put a VirtIO net device on the argv."""
    check = _by_bullet(checks, "P1-1")
    observation = _observation(check, "virtio_net_route")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "model=virtio-net" in observation.detail
    assert "restrict=on" in observation.detail


def test_p1_1_serves_a_real_tls_response(checks) -> None:
    """The self-controlled HTTPS service answered a real handshake on this host."""
    check = _by_bullet(checks, "P1-1")
    observation = _observation(check, "https_service")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "status 200" in observation.detail


def test_p1_1_the_known_gap_is_closed_and_the_remaining_question_is_onsite(checks) -> None:
    """Pins the *current* truth about P1-1's self-controlled HTTPS service.

    This test was ``test_p1_1_known_gap_no_production_caller_still_true`` and it
    pinned the opposite fact: that ``PackageServer`` had no production caller.
    That gap is **closed**.  ``src/unipad/controller/cli.py`` now constructs and
    starts the service inside ``_TransferWindow.start()`` (ADR-0008 was
    implemented while this gate was being audited), and
    ``unipad.guest.protocol.InstallRequest`` now carries ``package_url`` and
    ``cert_fingerprint``.  The test failed because the tree moved forward, which
    is the gate doing its job; per the old test's own docstring the assertion is
    updated rather than the check weakened.

    What is pinned now, and what is *not* claimed:

    * a production caller exists and is named -- the "nothing starts it" gap is
      gone, so a regression that deletes the caller must fail here;
    * the install request can name a package source;
    * the transport default is still ``channel``, because ``https`` has no
      guest-side implementation to meet;
    * the bullet is still **BLOCKED**, never ``PASS``: the honest remaining
      question is whether a real guest can reach the service at all, and that is
      an on-site question no Windows host can answer.
    """
    check = _by_bullet(checks, "P1-1")

    caller = _observation(check, "production_caller")
    assert caller is not None
    assert caller.ok is True, caller.detail
    assert "src/unipad/controller/cli.py" in caller.detail, caller.detail

    fields = _observation(check, "install_request_fields")
    assert fields is not None
    assert fields.ok is True, fields.detail

    default = _observation(check, "https_route_selection")
    assert default is not None
    assert "the default transport is channel" in default.detail, default.detail

    # The gap-closing findings are gone; nothing may still claim there is no
    # caller while a caller is named.
    assert not any("no production caller" in finding.lower() for finding in check.findings), (
        check.findings
    )

    # BLOCKED, not PASS: a green Windows suite does not answer the on-site
    # question, and the reason names it.
    assert check.verdict == p1.BLOCKED, check.verdict
    assert "guest outbound" in " ".join(check.reasons).lower()
    assert "restrict=on" in check.blocker
    assert "10.0.2.2" in check.blocker
    assert check.onsite_status == "BLOCKED"
    assert check.windows_status == "SIMULATION PASS"


def test_p1_1_the_adr_record_is_compared_with_the_tree_not_repeated(checks, monkeypatch) -> None:
    """F4: the ADR-0008 finding is derived, so it cannot contradict itself.

    It used to be unconditional -- emitted whenever the ADR file existed, with
    prose asserting "the tree still matches that record".  When the caller was
    wired in that sentence became false and contradicted the
    ``production_caller`` observation two lines above it.  The row is now read
    out of the ADR and compared, so the observation and the finding agree by
    construction.

    What is asserted here is the *invariant*, not today's wording: whatever the
    ADR's 实现状态 row says, the observation must agree with it.  Pinning the
    current text would just move the staleness this test exists to catch, because
    the ADR is a document another owner is editing while this gate runs.
    """
    check = _by_bullet(checks, "P1-1")
    observation = _observation(check, "adr_record_matches_tree")
    assert observation is not None, "the ADR record comparison disappeared"
    assert "实现状态" in observation.detail

    real_state = p1._adr_implementation_state
    caller_ok = _observation(check, "production_caller")
    assert caller_ok is not None and caller_ok.ok is True

    # The ADR claims the code is not written; the tree has a caller, so the two
    # must disagree and a finding must say so.
    monkeypatch.setattr(p1, "_adr_implementation_state", lambda text: "设计已定；代码未写")
    stale = p1.check_p1_1_networking(ROOT)
    stale_observation = _observation(stale, "adr_record_matches_tree")
    assert stale_observation is not None and stale_observation.ok is False, stale_observation
    disagreement = [
        finding for finding in stale.findings if "ADR-0008" in finding and "stale" in finding
    ]
    assert len(disagreement) == 1, stale.findings
    assert "the tree still matches that record" not in disagreement[0]

    # The ADR claims the code is written; the tree has a caller, so the two agree
    # and there is no finding.  This is the direction the old code could never
    # produce: it asserted agreement whether or not it held.
    monkeypatch.setattr(
        p1,
        "_adr_implementation_state",
        lambda text: "**CODE COMPLETE（代码阶段）**。`--transport channel|https`，默认 `channel`",
    )
    current = p1.check_p1_1_networking(ROOT)
    current_observation = _observation(current, "adr_record_matches_tree")
    assert current_observation is not None and current_observation.ok is True, current_observation
    assert not any(
        "ADR-0008" in finding and "stale" in finding for finding in current.findings
    ), current.findings

    # An unreadable row is never reported as agreement.
    monkeypatch.setattr(p1, "_adr_implementation_state", lambda text: "")
    unreadable = p1.check_p1_1_networking(ROOT)
    unreadable_observation = _observation(unreadable, "adr_record_matches_tree")
    assert (
        unreadable_observation is not None and unreadable_observation.ok is False
    ), unreadable_observation

    monkeypatch.setattr(p1, "_adr_implementation_state", real_state)


def test_the_adr_state_row_is_parsed_from_a_table_cell_containing_a_pipe() -> None:
    """The parser must survive a markdown cell that itself contains a pipe.

    ADR-0008's 实现状态 row spells ``--transport channel|https`` inside the cell.
    A non-greedy ``[^|]+?`` match finds no row at all, which the caller reads as
    "could not verify" -- so the gate would report a disagreement for a reason
    that has nothing to do with the tree.  The real row is exercised directly.
    """
    row = (
        "| 实现状态 | **CODE COMPLETE（代码阶段）**。`_import_ipa` 的传输阶段持有 "
        "`_TransferWindow`（`cli.py`），`--transport channel|https` 挂在 `install` 与 "
        "`update` 上，默认 `channel`；`https` 时以 `PackageServer` 发布 IPA 自身目录 |"
    )
    state = p1._adr_implementation_state("## 四字段\n\n" + row + "\n")
    assert state.startswith("**CODE COMPLETE"), state
    assert "channel|https" in state, state

    # The other shapes the row takes, including a row that is absent entirely.
    assert p1._adr_implementation_state("| 实现状态 | 设计已定；代码未写 |") == "设计已定；代码未写"
    assert p1._adr_implementation_state("| Windows/模拟测试状态 | PASS |") == ""
    assert p1._adr_implementation_state("no table here") == ""


def test_p1_1_guest_outbound_is_blocked_not_claimed(checks) -> None:
    check = _by_bullet(checks, "P1-1")
    assert check.onsite_status == "BLOCKED"
    assert "guest" in check.blocker.lower()


def test_p1_1_transfer_route_is_read_from_the_tree_not_hardcoded(checks, monkeypatch) -> None:
    """F3: the ``transfer_route`` observation must track the file it names.

    It used to be ``Observation("transfer_route", "unipad.ipa.transfer moves
    bytes through the GuestTransport channel (send_package); no https transport
    selection exists", False)`` -- a hardcoded ``ok`` with prose asserting a fact
    about a file the check never read, and a fact that had stopped being true of
    the product (the https route lives in ``unipad.controller.cli``).  A
    hardcoded value plus confident prose is the fabrication the gate exists to
    prevent, so this test feeds the check a *different* ``transfer`` source and
    requires the observation to move with it.
    """
    check = _by_bullet(checks, "P1-1")
    observation = _observation(check, "transfer_route")
    assert observation is not None, "the transfer_route observation disappeared"
    assert observation.ok is True, observation.detail
    # The prose must be scoped to the file it was read from, not the product.
    assert "unipad.ipa.transfer" in observation.detail

    real_source_text = p1._source_text

    def with_transfer(text: str):
        def loader(root, dotted):
            if dotted == "unipad.ipa.transfer":
                return text, "src/unipad/ipa/transfer.py"
            return real_source_text(root, dotted)

        return loader

    # 1. Drop the Protocol declaration: the boundary is no longer declared there.
    monkeypatch.setattr(
        p1, "_source_text", with_transfer("def send_package(name, data):\n    pass\n")
    )
    broken = p1.check_p1_1_networking(ROOT)
    dropped = _observation(broken, "transfer_route")
    assert dropped is not None and dropped.ok is False, dropped
    assert "present: False" in dropped.detail, dropped.detail

    # 2. Keep the Protocol but add an https route of its own: now it *does*
    #    select one, and the observation must say so rather than repeating the
    #    old sentence.
    monkeypatch.setattr(
        p1,
        "_source_text",
        with_transfer(
            "class GuestTransport(Protocol):\n"
            "    def send_package(self, name, data):\n"
            "        ...\n"
            "HTTPS_ROUTE = 'https'\n"
        ),
    )
    routed = p1.check_p1_1_networking(ROOT)
    observation = _observation(routed, "transfer_route")
    assert observation is not None and observation.ok is False, observation
    assert "selects no https route of its own (an https literal" in observation.detail

    # 3. The companion observation must name where the https route really lives,
    #    so a reader is not told the product has none.
    selection = _observation(routed, "https_route_selection")
    assert selection is not None, "the https_route_selection observation is missing"
    assert selection.ok is None, "an informational row must not read as a condition"
    assert "unipad.controller.cli" in selection.detail
    assert "channel" in selection.detail

def test_p1_1_https_route_selection_reports_the_cli_default(checks) -> None:
    """The transport default is a fact about the tree, and it is still ``channel``.

    P1-1's on-site question is not settled by a default: it is whether the guest
    can reach the service at all.  This pins the default so a silent flip to
    ``https`` -- which would make every run take the network path the bullet
    asks about -- is noticed rather than absorbed.
    """
    check = _by_bullet(checks, "P1-1")
    observation = _observation(check, "https_route_selection")
    assert observation is not None
    assert "the default transport is channel" in observation.detail, observation.detail
    # The bullet is still honestly blocked on the guest, not on the default.
    assert check.onsite_status == "BLOCKED"


# ---------------------------------------------------------------------------
# P1 (2) -- compatibility testing (docs/PRD.md:87)
# ---------------------------------------------------------------------------


def test_p1_2_refuses_an_unauthorised_matrix_entry(checks) -> None:
    check = _by_bullet(checks, "P1-2")
    observation = _observation(check, "authorisation_gate")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "blocked" in observation.detail


def test_p1_2_reports_zero_recorded_observations(checks) -> None:
    """Pins the current state: no compatibility observation is on disk."""
    check = _by_bullet(checks, "P1-2")
    observation = _observation(check, "recorded_observations")
    assert observation is not None
    assert observation.ok is False, observation.detail
    assert "recorded observations: 0" in observation.detail


def test_p1_2_positive_verdicts_are_unreachable(checks) -> None:
    """``installs-and-runs`` needs a guest, so the bullet cannot pass here."""
    check = _by_bullet(checks, "P1-2")
    assert check.verdict != p1.PASS
    assert check.onsite_status == "BLOCKED"


# ---------------------------------------------------------------------------
# P1 (2) -- the product-tree scan must cost what it looks at
# ---------------------------------------------------------------------------


def _record_path_calls(monkeypatch, attribute: str) -> list[tuple[Path, int]]:
    """Record ``(path, payload size)`` for every ``Path.<attribute>`` call.

    The gate's own cost is part of what it reports: a probe that answers a
    four-byte question by materialising a whole file is a probe that gets slower
    the day a real artifact lands in the tree.  Recording the calls is the only
    way to see that from outside, because the answer it returns is identical
    either way -- which is exactly why the defect survives a behavioural test.
    """
    real = getattr(Path, attribute)
    seen: list[tuple[Path, int]] = []

    def recording(self, *args, **kwargs):
        payload = real(self, *args, **kwargs)
        seen.append((self, len(payload)))
        return payload

    monkeypatch.setattr(Path, attribute, recording)
    return seen


def _p1_2_tree(tmp_path: Path) -> Path:
    """A throwaway product tree carrying just enough source for P1 (2) to run."""
    root = tmp_path / "tree"
    (root / "src" / "unipad" / "ipa").mkdir(parents=True)
    shutil.copy2(
        ROOT / "src" / "unipad" / "ipa" / "matrix.py",
        root / "src" / "unipad" / "ipa" / "matrix.py",
    )
    return root


def test_p1_2_reads_four_bytes_of_an_ipa_not_the_whole_archive(
    tmp_path: Path, monkeypatch
) -> None:
    """The probe needs the ZIP magic, so it must not materialise the archive.

    ``head = path.read_bytes()[:4]`` answered "is this a ZIP?" by reading all of
    it.  An archived Device IPA is a build product measured in hundreds of
    megabytes -- PRD 8.4 caps what the workflow will even upload at 2 GiB -- and
    this is the same walk that already pays a ``stat`` per file.  Reading one
    archive to inspect four bytes is what turns a two-second check into a
    minute-long one the day a real archive appears in the tree.

    The recorded call list is the proof: nothing named ``*.ipa`` may come back
    through ``read_bytes`` at all, whatever its size.  The answer itself has to
    stay right, because a probe that stopped reading the file would also stop
    knowing what it is.
    """
    root = _p1_2_tree(tmp_path)
    # A stand-in for an archived Device IPA: a ZIP header and then bulk.  The
    # size is arbitrary -- what the test pins is that the probe never pulls the
    # whole thing back through ``read_bytes``, so a real 500 MB archive would
    # fail it exactly the same way.
    archive_size = 256 << 10
    archive = root / "HelloUniPad.ipa"
    with archive.open("wb") as handle:
        handle.write(b"PK\x03\x04")
        handle.write(b"\x00" * archive_size)

    reads = _record_path_calls(monkeypatch, "read_bytes")
    check = p1.check_p1_2_compatibility(root)

    materialised = [(path.name, size) for path, size in reads if path.suffix == ".ipa"]
    assert materialised == [], (
        "the product-tree probe pulled a whole .ipa through read_bytes to look at "
        f"four bytes: {materialised}"
    )
    observation = _observation(check, "archived_ipas")
    assert observation is not None
    assert f"HelloUniPad.ipa (zip, {archive_size + 4} bytes)" in observation.detail, (
        observation.detail
    )


def test_p1_2_counts_matrix_observations_by_streaming(tmp_path: Path, monkeypatch) -> None:
    """Counting the recorded observations is a stream, not a read of the file.

    ``read_text(encoding="utf-8").splitlines()`` holds the whole file *and* a
    list of every line in it, only to count the non-blank ones.  The matrix is
    append-only and grows for the life of the project, so the cost of this probe
    grows with it -- on the question that is supposed to answer "is there any
    evidence at all?".

    The count itself must not move while the cost does: blank and whitespace-only
    lines are not observations, and three real ones are three.
    """
    root = _p1_2_tree(tmp_path)
    (root / "compatibility-matrix.jsonl").write_text(
        '{"observation": 1}\n'
        "\n"
        "   \n"
        '{"observation": 2}\n'
        "\t\n"
        '{"observation": 3}\n',
        encoding="utf-8",
    )

    reads = _record_path_calls(monkeypatch, "read_text")
    check = p1.check_p1_2_compatibility(root)

    materialised = [path.name for path, _ in reads if path.suffix == ".jsonl"]
    assert materialised == [], (
        "counting the matrix observations materialised the whole JSONL file "
        f"through read_text: {materialised}"
    )
    observation = _observation(check, "recorded_observations")
    assert observation is not None
    assert "recorded observations: 3" in observation.detail, observation.detail
    assert observation.ok, observation.detail


# ---------------------------------------------------------------------------
# P1 (3) -- lifecycle and error classification (docs/PRD.md:88)
# ---------------------------------------------------------------------------


def test_p1_3_drives_the_append_only_ledger(checks) -> None:
    check = _by_bullet(checks, "P1-3")
    observation = _observation(check, "append_only_history")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "le-00000001" in observation.detail


def test_p1_3_refuses_to_fabricate_a_removal(checks) -> None:
    check = _by_bullet(checks, "P1-3")
    observation = _observation(check, "uninstall_refuses_absent")
    assert observation is not None
    assert observation.ok, observation.detail


def test_p1_3_classification_table_is_closed_and_total(checks) -> None:
    check = _by_bullet(checks, "P1-3")
    observation = _observation(check, "closed_classification")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "neighbouring guess): True" in observation.detail
    assert "guest_refused: True" in observation.detail


def test_p1_3_exposes_history_uninstall_and_update(checks) -> None:
    check = _by_bullet(checks, "P1-3")
    observation = _observation(check, "cli_surface")
    assert observation is not None
    assert observation.ok, observation.detail
    for command in ("history", "uninstall", "update"):
        assert command in observation.detail


def test_p1_3_reads_the_real_ledger_without_writing_it() -> None:
    """The on-disk ledger is evidence; a check must not append to it."""
    path = ROOT / "artifacts" / "lifecycle" / "install-ledger.jsonl"
    before = path.read_bytes() if path.is_file() else b""
    check = p1.check_p1_3_lifecycle(ROOT)
    after = path.read_bytes() if path.is_file() else b""
    assert before == after
    observation = _observation(check, "real_ledger")
    assert observation is not None
    assert observation.ok, observation.detail


# ---------------------------------------------------------------------------
# P1 (4) -- rotation, resolution, audio, install package (docs/PRD.md:89)
# ---------------------------------------------------------------------------


def test_p1_4_rotation_reaches_the_fw_cfg_geometry(checks) -> None:
    check = _by_bullet(checks, "P1-4")
    observation = _observation(check, "rotation")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "'90': [1024, 1376]" in observation.detail


def test_p1_4_an_off_catalogue_resolution_is_refused(checks) -> None:
    check = _by_bullet(checks, "P1-4")
    observation = _observation(check, "optional_resolution")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "config_invalid" in observation.detail


def test_p1_4_guest_audio_is_never_claimed(checks) -> None:
    check = _by_bullet(checks, "P1-4")
    observation = _observation(check, "audio")
    assert observation is not None
    assert "guest_audio_verified is False" in observation.detail


def test_p1_4_the_check_notices_when_guest_audio_becomes_verified(checks) -> None:
    """承重 test: the check must fail the moment the host claims guest audio.

    ``GUEST_AUDIO_VERIFIED`` is a property of the *host* that nothing in the
    decision module can set, so the only way it becomes ``True`` is somebody
    editing that constant -- which is exactly the edit that would let a
    configuration claim audio was heard.  Flipping it here and restoring it
    proves the P1 (4) check is wired to the live value and not to a hardcoded
    string in its own report.
    """
    from unipad.host import display

    original = display.GUEST_AUDIO_VERIFIED
    display.GUEST_AUDIO_VERIFIED = True
    try:
        flipped = p1.check_p1_4_display_audio_package(ROOT)
    finally:
        display.GUEST_AUDIO_VERIFIED = original
    observation = _observation(flipped, "audio")
    assert observation is not None
    assert "guest_audio_verified is True" in observation.detail, observation.detail
    assert flipped.verdict != p1.PASS, flipped.verdict
    # And the unflipped check still passes, so the flip was the only difference.
    assert _by_bullet(checks, "P1-4").verdict == p1.PASS


def test_p1_4_the_install_stage_vocabulary_matches_the_installer(checks) -> None:
    check = _by_bullet(checks, "P1-4")
    observation = _observation(check, "install_progress_vocabulary")
    assert observation is not None
    assert observation.ok, observation.detail


def test_p1_4_reports_the_absent_installer_package(checks) -> None:
    """Pins the current state: no distributable Windows installer exists.

    ``安装包体验`` is read here as the install *progress* vocabulary plus,
    separately, whatever installer package the tree actually holds.  If an Inno
    Setup or WiX artefact appears, this assertion is the one to update.
    """
    check = _by_bullet(checks, "P1-4")
    observation = _observation(check, "installer_package")
    assert observation is not None
    assert observation.ok is False, observation.detail
    assert "none" in observation.detail


# ---------------------------------------------------------------------------
# P1 (5) -- snapshot / restore (docs/PRD.md:90)
# ---------------------------------------------------------------------------


def test_p1_5_the_scope_vocabulary_is_closed(checks) -> None:
    check = _by_bullet(checks, "P1-5")
    observation = _observation(check, "scope_vocabulary")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "['disk', 'machine']" in observation.detail


def test_p1_5_a_disk_checkpoint_names_what_it_excludes(checks) -> None:
    """The bullet's second clause: state whether disk or whole-machine is saved.

    The observation text is *derived* from what ``declare_scope`` actually
    returns, so this assertion reads the declaration and not a sentence the
    check wrote about itself.
    """
    check = _by_bullet(checks, "P1-5")
    observation = _observation(check, "scope_stated_explicitly")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "declare_scope() names what it does not cover" in observation.detail
    for excluded in ("guest_ram", "device_state", "fw_cfg", "install_history", "network_state"):
        assert excluded in observation.detail, excluded


def test_p1_5_the_exclusion_list_is_the_modules_own() -> None:
    """Pins the constant, so a check that merely prints prose cannot fake it."""
    from unipad.host import images

    assert set(images.DISK_SCOPE_EXCLUDES) == {
        "guest_ram",
        "device_state",
        "fw_cfg",
        "install_history",
        "network_state",
    }
    declaration = images.declare_scope(images.ScopeRequired.DISK)
    assert declaration["scope"] == "disk"
    assert set(declaration["scope_excludes"]) == set(images.DISK_SCOPE_EXCLUDES)
    assert "disk images only" in declaration["scope_note"]


def test_p1_5_the_check_notices_a_declaration_that_names_nothing(monkeypatch) -> None:
    """承重 test: a ``disk`` declaration that stops naming its exclusions fails.

    ``declare_scope`` is patched to return the real declaration with
    ``scope_excludes`` emptied -- the shape an "it's just a disk copy, nobody
    needs to know what is missing" edit would produce.  The check must refuse to
    call that a declared scope, and the observation must say so.
    """
    from unipad.host import images

    original = images.declare_scope

    def degraded(scope, *, covered=()):
        document = dict(original(scope, covered=covered))
        document["scope_excludes"] = []
        return document

    monkeypatch.setattr(images, "declare_scope", degraded)
    try:
        flipped = p1.check_p1_5_snapshot_restore(ROOT)
    finally:
        monkeypatch.setattr(images, "declare_scope", original)
    observation = _observation(flipped, "scope_stated_explicitly")
    assert observation is not None
    assert observation.ok is False, observation.detail
    assert flipped.verdict != p1.PASS, flipped.verdict


def test_p1_5_a_restore_is_digest_verified(checks) -> None:
    check = _by_bullet(checks, "P1-5")
    observation = _observation(check, "restore_roundtrip")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "verified=True" in observation.detail


def test_p1_5_an_undeclared_manifest_is_refused_at_restore() -> None:
    """Run for real: a manifest that does not declare its scope is refused."""
    from unipad.host import images

    with pytest.raises(Exception) as excinfo:
        images.read_scope({"entries": []})
    assert getattr(excinfo.value, "code", None) == "artifact_manifest_invalid"


# ---------------------------------------------------------------------------
# P1 (6) -- the Windows launcher (docs/PRD.md:91)
# ---------------------------------------------------------------------------


def test_p1_6_the_launcher_scripts_exist(checks) -> None:
    check = _by_bullet(checks, "P1-6")
    observation = _observation(check, "launcher_files")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "scripts/launcher.cmd=True" in observation.detail


def test_p1_6_the_five_actions_match_the_bullet(checks) -> None:
    """The bullet names five verbs; the launcher must expose exactly those."""
    check = _by_bullet(checks, "P1-6")
    observation = _observation(check, "five_actions")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "['start', 'stop', 'install', 'logs', 'restore']" in observation.detail


def test_p1_6_the_dry_run_plan_delegates_start(checks) -> None:
    check = _by_bullet(checks, "P1-6")
    observation = _observation(check, "dry_run_plan")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "delegates_to Start-UniPad.ps1" in observation.detail


def test_p1_6_the_logs_action_reads_the_serial_log(checks) -> None:
    check = _by_bullet(checks, "P1-6")
    observation = _observation(check, "logs_action")
    assert observation is not None
    assert observation.ok, observation.detail
    assert "artifact_missing" in observation.detail


def test_p1_6_the_launcher_is_really_invoked(checks) -> None:
    """Guard against the check degenerating into reading the script's text.

    Three real child processes must have been spawned: an unknown action, a
    ``-WhatIf`` plan and a ``logs`` request.  :data:`p1.LAUNCHER_INVOCATIONS` is
    only filled by the one function that spawns a process, so a check that
    answers from source inspection -- or from a hardcoded tuple -- leaves it
    empty and fails here.  Only the argv is recorded, never the payload, which
    carries absolute paths (PRD 9.2).
    """
    assert len(p1.LAUNCHER_INVOCATIONS) >= 3, p1.LAUNCHER_INVOCATIONS
    script = str(ROOT / "scripts" / "windows" / "UniPad-Launcher.ps1")
    invoked = [" ".join(argv) for argv in p1.LAUNCHER_INVOCATIONS]
    assert any(script in item and "-Action" in item for item in invoked), invoked
    assert any("bogus-action" in item for item in invoked), invoked
    assert any("-WhatIf" in item for item in invoked), invoked
    assert any("-Action" in item and "logs" in item for item in invoked), invoked


def test_p1_6_the_reported_exit_codes_match_a_fresh_run(checks) -> None:
    """A differential check: the numbers the check reported are the real ones.

    The launcher is invoked a second time, independently of the check, and the
    exit code and error code must be the same.  A check that invented its
    numbers, or that ran against a different script than the one in the tree,
    diverges here.
    """
    check = _by_bullet(checks, "P1-6")
    script = ROOT / "scripts" / "windows" / "UniPad-Launcher.ps1"
    executable = shutil.which("powershell") or shutil.which("powershell.exe")
    if executable is None:  # pragma: no cover - Windows-only host
        pytest.skip("powershell.exe is not on PATH")
    completed = subprocess.run(
        [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-Action", "bogus-action"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=False,
    )
    assert completed.returncode == 2
    assert '"code":"unsupported_command"' in (completed.stderr or completed.stdout)
    observation = _observation(check, "five_actions")
    assert observation is not None
    assert "exits 2" in observation.detail
    assert "unsupported_command" in observation.detail


def test_p1_6_the_verdict_is_a_property_of_the_tree_not_the_cwd(checks, tmp_path) -> None:
    """F1: the P1-6 verdict must not move when the operator's working directory does.

    ``_run_launcher`` used to spawn powershell with a relative ``-Artifact
    artifacts/manifest.json`` and no ``cwd=``.  The launcher resolves that
    relative path against the caller's CWD
    (``scripts/windows/UniPad-Launcher.ps1``:541) and then guards the *resolved*
    path for non-ASCII characters (:579-606).  So from this host's own non-ASCII
    ``%TEMP%`` the guard fired, the process exited 2, and the check read ``FAIL``
    -- a verdict describing the shell the operator was standing in, not the tree.

    This runs the whole check from a foreign working directory and requires every
    observation and the verdict itself to match what the same tree produced from
    the repository root.  ``LAUNCHER_INVOCATIONS`` is asserted to have grown, so a
    check that short-circuited instead of spawning the child cannot pass by
    agreeing with itself.
    """
    script = ROOT / "scripts" / "windows" / "UniPad-Launcher.ps1"
    if not script.is_file():
        pytest.skip("the launcher script is absent from this tree")
    at_root = _by_bullet(checks, "P1-6")

    before = len(p1.LAUNCHER_INVOCATIONS)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    original = os.getcwd()
    try:
        os.chdir(elsewhere)
        from_elsewhere = p1.check_p1_6_launcher(ROOT)
    finally:
        os.chdir(original)

    assert len(p1.LAUNCHER_INVOCATIONS) > before, "the check must really spawn the launcher"
    assert from_elsewhere.verdict == at_root.verdict, (
        f"verdict moved with the CWD: {at_root.verdict} at the repo root, "
        f"{from_elsewhere.verdict} from {elsewhere.name}"
    )
    assert [
        (item.label, item.ok) for item in from_elsewhere.observations
    ] == [(item.label, item.ok) for item in at_root.observations], (
        "an observation moved with the CWD: the check is reading the caller's shell, "
        "not the tree"
    )


# ---------------------------------------------------------------------------
# 对质 (load-bearing / injection) tests
# ---------------------------------------------------------------------------
#
# Everything above asserts the check *reports* the right thing when the tree is
# as it is.  These tests break one input at a time and require the check to
# stop passing: that is what separates a gate from a rubber stamp.  Each one
# patches a single seam in the module under test, re-runs one check, and
# restores the seam (``monkeypatch`` undoes it even when the assertion fails).
#
# The mutations these were written against, and which they now catch:
#
# * the VirtIO net fragment is not read from the real argv -- both by removing
#   the builder's vocabulary and by dropping the ``hostfwd`` rule from the
#   emitted fragment, so a check that stops comparing the real string fails;
# * the matrix authorisation gate is not actually exercised -- both gates
#   (authorisation *and* trust category) are made permissive while still
#   coercing, so an entry nobody cleared becomes constructible;
# * the closed classification table is not actually consulted;
# * the ledger transitions are not actually driven -- both by stopping the
#   ledger from appending at all and by stopping it from recording removals;
# * the checkpoint manifest's declared scope is not actually verified -- the
#   manifest is made to lie while the restore still reports ``disk``;
# * the digest-verified restore round trip is not actually performed;
# * a ``disk`` declaration that stops naming its exclusions is accepted, in the
#   scope vocabulary as well as in the explicit-scope observation.


def _require(module, name: str):
    """Return ``module.name`` or skip, so a rename elsewhere is not a false alarm."""
    target = getattr(module, name, None)
    if target is None:
        pytest.skip(f"{module.__name__}.{name} is not present in this tree")
    return target


def test_p1_1_notices_when_the_argv_carries_no_net_device(monkeypatch) -> None:
    """Break the argv builder's vocabulary and require the check to fail."""
    from unipad.host import qemu

    modes = _require(qemu, "NETWORK_MODES")
    monkeypatch.setattr(qemu, "NETWORK_MODES", ("none",))
    broken = p1.check_p1_1_networking(ROOT)
    assert broken.verdict != p1.PASS, broken.verdict
    observation = _observation(broken, "virtio_net_route")
    assert observation is not None and observation.ok is False, observation.detail
    assert modes == ("none", "user")  # the patch was the only difference


def test_p1_1_notices_when_the_route_loses_its_forward_rule(monkeypatch) -> None:
    """Keep the build viable but drop the ``hostfwd`` rule from the fragment.

    The previous test breaks the build, which the check already reports.  This
    one removes a *part* of a fragment the real builder still emits, so the only
    thing that can change the verdict is the check's own comparison of the
    string it was handed.  A check that stopped reading ``has_forward`` -- or
    that hardcoded the route as fine -- reports the route as proven here.
    """
    from unipad.host import qemu

    forwards = _require(qemu, "_host_forwards")
    monkeypatch.setattr(qemu, "_host_forwards", lambda net: [])
    broken = p1.check_p1_1_networking(ROOT)
    assert broken.verdict != p1.PASS, broken.verdict
    observation = _observation(broken, "virtio_net_route")
    assert observation is not None, "the route observation disappeared"
    assert observation.ok is False, observation.detail
    assert "hostfwd rule: False" in observation.detail, observation.detail
    assert callable(forwards)


def test_p1_2_notices_when_the_authorisation_gate_stops_refusing(monkeypatch) -> None:
    """Make an unauthorised entry constructible and require the check to fail.

    Both gates have to be opened at once.  Opening only the authorisation gate
    leaves the trust-category gate to refuse the entry, so the check would still
    report a refusal and would look load-bearing while proving nothing.  The
    permissive stand-ins still coerce, because a raw string would break
    ``MatrixEntry.to_dict`` for a reason that has nothing to do with the gate.
    """
    from unipad.ipa import matrix

    _require(matrix.MatrixEntry, "_authorisation")
    _require(matrix.MatrixEntry, "_trust_category")

    def permissive_authorisation(self):
        if self.authorisation is None:
            return None
        return matrix.Authorisation.coerce(self.authorisation)

    def permissive_trust(self):
        if self.trust_category is None:
            return None
        return matrix.TrustCategory.coerce(self.trust_category)

    monkeypatch.setattr(matrix.MatrixEntry, "_authorisation", permissive_authorisation)
    monkeypatch.setattr(matrix.MatrixEntry, "_trust_category", permissive_trust)
    broken = p1.check_p1_2_compatibility(ROOT)
    assert broken.verdict != p1.PASS, broken.verdict
    observation = _observation(broken, "authorisation_gate")
    assert observation is not None, "the gate observation disappeared"
    assert observation.ok is False, observation.detail
    assert "'not_authorised': 'recorded'" in observation.detail, observation.detail


def test_p1_3_notices_when_the_classification_table_is_emptied(monkeypatch) -> None:
    """Empty the code -> class table and require the check to fail."""
    from unipad.ipa import lifecycle

    _require(lifecycle, "_CLASS_BY_CODE")
    monkeypatch.setattr(lifecycle, "_CLASS_BY_CODE", {})
    broken = p1.check_p1_3_lifecycle(ROOT)
    assert broken.verdict != p1.PASS, broken.verdict
    observation = _observation(broken, "closed_classification")
    assert observation is not None and observation.ok is False, observation.detail


def test_p1_3_notices_when_the_ledger_refuses_to_record(monkeypatch) -> None:
    """Stop the ledger appending and require the check to fail."""
    from unipad.ipa import lifecycle

    _require(lifecycle.InstallLedger, "record_install")
    monkeypatch.setattr(lifecycle.InstallLedger, "record_install", lambda self, *a, **k: None)
    broken = p1.check_p1_3_lifecycle(ROOT)
    assert broken.verdict != p1.PASS, broken.verdict
    observation = _observation(broken, "append_only_history")
    assert observation is not None and observation.ok is False, observation.detail


def test_p1_3_notices_when_the_ledger_stops_recording_removals(monkeypatch) -> None:
    """Keep the ledger appending, but drop the removals.

    The previous test makes the ledger raise, which the check already reports as
    a failure.  This one keeps every call succeeding and only loses the
    ``uninstall`` transition, so the history comes back with the wrong length and
    the wrong order while nothing blows up.  That is the shape a check which
    hardcodes the transition sequence would still call fine.
    """
    from unipad.ipa import lifecycle

    _require(lifecycle.InstallLedger, "record_uninstall")
    monkeypatch.setattr(lifecycle.InstallLedger, "record_uninstall", lambda self, *a, **k: None)
    broken = p1.check_p1_3_lifecycle(ROOT)
    assert broken.verdict != p1.PASS, broken.verdict
    observation = _observation(broken, "append_only_history")
    assert observation is not None, "the history observation disappeared"
    assert observation.ok is False, observation.detail
    assert "kept 3 entries" in observation.detail, observation.detail
    assert "'uninstall'" not in observation.detail, observation.detail


def test_p1_5_notices_when_the_manifest_declares_the_wrong_scope(monkeypatch) -> None:
    """Write a ``machine`` manifest where ``backup`` wrote a ``disk`` one.

    ``read_scope`` accepts it, so the restore still runs and still verifies --
    only the *declared scope* is wrong.  A check that does not compare the
    manifest's scope against ``disk`` reports the round trip as fine.  The
    manifest still names its exclusions and ``read_scope`` still reports
    ``disk``, so the manifest's own scope word is the only thing that differs.
    """
    import json

    from unipad.host import images

    _require(images, "_merge_manifest")
    original_read = images.read_scope

    def scoped(document):
        if not isinstance(document, dict) or not document.get("scope"):
            return original_read(document)
        return {
            "scope": "disk",
            "scope_note": "patched by the load-bearing test",
            "scope_excludes": list(images.DISK_SCOPE_EXCLUDES),
        }

    def machine_manifest(manifest_path, entries, stamp, note, scope_fields=None):
        document = {
            "schema_version": 1,
            "updated_at": stamp,
            "entries": list(entries),
            "scope": "machine",
            "scope_excludes": list(images.DISK_SCOPE_EXCLUDES),
            "scope_note": "patched by the load-bearing test",
        }
        Path(manifest_path).write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(images, "_merge_manifest", machine_manifest)
    monkeypatch.setattr(images, "read_scope", scoped)
    broken = p1.check_p1_5_snapshot_restore(ROOT)
    assert broken.verdict != p1.PASS, broken.verdict
    observation = _observation(broken, "restore_roundtrip")
    assert observation is not None, "the round-trip observation disappeared"
    assert observation.ok is False, observation.detail


def test_p1_5_notices_when_the_backup_reports_no_scope(monkeypatch) -> None:
    """Return a backup result with no scope declaration at all."""
    from unipad.host import images

    _require(images.ImageManager, "backup")
    monkeypatch.setattr(images.ImageManager, "backup", lambda self, *a, **k: {})
    broken = p1.check_p1_5_snapshot_restore(ROOT)
    assert broken.verdict != p1.PASS, broken.verdict
    observation = _observation(broken, "restore_roundtrip")
    assert observation is not None and observation.ok is False, observation.detail


def test_p1_5_the_disk_exclusion_guard_is_isolated(monkeypatch) -> None:
    """Isolate the disk-exclusion property from every other refusal.

    Only the ``disk`` declaration loses its exclusions; the ``machine`` and
    unknown-scope refusals keep working.  That way the only thing which can
    make this check fail is the guard that requires the exclusions to be named
    -- so a check that stops requiring them is caught instead of being masked
    by an unrelated refusal.
    """
    from unipad.host import images

    original = images.declare_scope

    def degraded(scope, *, covered=()):
        document = dict(original(scope, covered=covered))
        if document.get("scope") == images.ScopeRequired.DISK:
            document["scope_excludes"] = []
        return document

    monkeypatch.setattr(images, "declare_scope", degraded)
    flipped = p1.check_p1_5_snapshot_restore(ROOT)
    vocabulary = _observation(flipped, "scope_vocabulary")
    assert vocabulary is not None, "the scope-vocabulary observation disappeared"
    assert vocabulary.ok is False, vocabulary.detail
    stated = _observation(flipped, "scope_stated_explicitly")
    assert stated is not None
    assert stated.ok is False, stated.detail
    assert flipped.verdict != p1.PASS, flipped.verdict


# ---------------------------------------------------------------------------
# F7: a non-PASS verdict must name what was observed, not collapse to prose
# ---------------------------------------------------------------------------


def test_every_check_carries_a_machine_readable_reason(checks) -> None:
    """F7: no check may report a non-PASS verdict with nothing to act on.

    ``P1Check.reasons`` is operator-facing prose.  ``verdict_reasons`` is the
    stable slug set behind the same verdict, and every non-PASS construction
    site fills it in.  A check that reports a non-PASS verdict with an empty slug
    set is a row neither an operator nor a script diffing two runs can act on.
    """
    for check in checks:
        if check.verdict == p1.PASS:
            continue
        assert check.verdict_reasons, (
            f"{check.bullet} reports {check.verdict} with no machine-readable reason; "
            "the operator-facing prose is all there is"
        )
        for slug in check.verdict_reasons:
            assert slug == slug.strip() and " " not in slug, (
                f"{check.bullet} carries a prose sentence where a slug belongs: {slug!r}"
            )


def test_a_non_pass_check_cannot_be_built_without_a_slug() -> None:
    """F7 (construction guard): the invariant is enforced, not merely observed.

    A check returning ``FAIL``/``BLOCKED``/``UNVERIFIED`` with no slug is
    refused at construction.  Without this the property above holds only for as
    long as nobody forgets to pass the field.
    """
    bullet = p1.P1_BULLETS[4]
    base = dict(
        bullet=bullet.bullet,
        prd_citation=bullet.prd_citation,
        requirement=bullet.requirement,
        probe=bullet.probe,
        reasons=("something went wrong",),
        observations=(),
        evidence=(p1.Evidence("docs/PRD.md", "doc"),),
    )
    for kind in (p1.FAIL, p1.BLOCKED, p1.UNVERIFIED):
        with pytest.raises(Exception) as raised:
            p1.P1Check(verdict=kind, **base)
        assert "machine-readable reason" in str(raised.value), (kind, str(raised.value))
    # A PASS needs no slug: it is not a claim that something is missing.
    p1.P1Check(verdict=p1.PASS, **base)


def test_make_verdict_refuses_a_non_pass_with_no_reason() -> None:
    """F7 (helper guard): ``_make_verdict`` keeps the slugs it validates.

    The old signature returned the bare kind string and dropped the reasons it
    had just checked -- which is why a FAIL could only be read back as prose.
    The returned record must carry them, and must refuse to be built without
    them.
    """
    evidence = (p1.Evidence("docs/PRD.md", "doc"),)
    passing = p1._make_verdict(p1.PASS, evidence=evidence, observed=True)
    assert passing.kind == p1.PASS
    assert passing.reasons == ()
    failing = p1._make_verdict(p1.FAIL, reasons=["a_slug"], evidence=evidence, observed=True)
    assert failing.kind == p1.FAIL
    assert failing.reasons == ("a_slug",)
    for kind in (p1.FAIL, p1.BLOCKED, p1.UNVERIFIED):
        with pytest.raises(Exception) as raised:
            p1._make_verdict(kind, evidence=evidence, observed=True)
        assert "reason slug" in str(raised.value), (kind, str(raised.value))


def test_p1_5_names_the_observation_that_failed(monkeypatch) -> None:
    """F7: a broken P1-5 must say *which* of its probes broke.

    The image manager is made unavailable, so exactly one of the three slugs can
    apply.  A check whose FAIL branch collapses to one sentence reports "it
    failed"; a check that names the failing observation reports
    ``images_module_unavailable``.
    """
    monkeypatch.setattr(p1, "_import", lambda dotted: (None, f"{dotted} is not importable"))
    broken = p1.check_p1_5_snapshot_restore(ROOT)
    assert broken.verdict == p1.FAIL, broken.verdict
    assert broken.verdict_reasons == ("images_module_unavailable",), broken.verdict_reasons
    observation = _observation(broken, "images_module")
    assert observation is not None, "the images_module observation disappeared"
    assert observation.ok is False, observation.detail


def test_p1_5_names_the_failing_probe_when_only_the_roundtrip_breaks(monkeypatch) -> None:
    """F7: the round-trip probe gets its own slug, not the shared one.

    Only ``restore_checkpoint`` is degraded; the scope vocabulary still holds.
    The slug must therefore be the round-trip one and *not* the scope one --
    which is what separates a check that names the failing probe from one that
    reports "the check failed".
    """
    from unipad.host import images

    _require(images.ImageManager, "restore_checkpoint")

    def unverified(self, *args, **kwargs):
        return {"scope": "disk", "verified": False}

    monkeypatch.setattr(images.ImageManager, "restore_checkpoint", unverified)
    broken = p1.check_p1_5_snapshot_restore(ROOT)
    assert broken.verdict == p1.FAIL, broken.verdict
    assert broken.verdict_reasons == ("digest_verified_restore_roundtrip_failed",), (
        broken.verdict_reasons
    )
    scope = _observation(broken, "scope_vocabulary")
    assert scope is not None and scope.ok is True, scope.detail if scope else "gone"


def test_p1_6_names_the_action_that_did_not_behave(monkeypatch) -> None:
    """F7: a broken P1-6 must name *which* of the five actions broke.

    ``_run_launcher`` is made to answer every invocation with a usage error, so
    the action table, the dry run and the logs action all misbehave at once and
    all three must be named.  The slugs mirror the observations one for one, so
    the reason set and the report cannot disagree.
    """
    calls: list[list[str]] = []

    def usage_error(script, args, root=None):
        calls.append(list(args))
        return 2, "", '{"error":{"code":"unsupported_command","message":"bogus"}}'

    monkeypatch.setattr(p1, "_run_launcher", usage_error)
    broken = p1.check_p1_6_launcher(ROOT)
    assert broken.verdict == p1.FAIL, broken.verdict
    assert set(broken.verdict_reasons) == {
        "action_table_is_not_the_five_verbs",
        "dry_run_plan_not_as_documented",
        "logs_action_not_as_documented",
    }, broken.verdict_reasons
    assert len(calls) == 3, calls
    # The launcher itself and the shim are untouched, so they must not be blamed.
    for slug in ("launcher_artefacts_missing", "cmd_shim_does_not_forward"):
        assert slug not in broken.verdict_reasons, slug
    for label in ("five_actions", "dry_run_plan", "logs_action"):
        observation = _observation(broken, label)
        assert observation is not None, f"the {label} observation disappeared"
        assert observation.ok is False, observation.detail


def test_p1_6_names_a_missing_artefact_and_does_not_run_the_launcher(monkeypatch) -> None:
    """F7: an absent artefact is named as such, and nothing is executed.

    The launcher script is removed from a scratch copy of the tree, so the three
    real runs must not happen at all.  A check that reports "the launcher did not
    satisfy the bullet" here is indistinguishable from one that ran the launcher
    and watched it misbehave.
    """
    scratch = Path(tempfile.mkdtemp(prefix="unipad-p1-6-f7-"))
    try:
        shutil.copytree(ROOT / "scripts", scratch / "scripts")
        (scratch / "scripts" / "windows" / "UniPad-Launcher.ps1").unlink()
        (scratch / "docs").mkdir()
        shutil.copy2(ROOT / "docs" / "launcher.md", scratch / "docs" / "launcher.md")

        def refuse(script, args, root=None):
            raise AssertionError("the launcher must not run when its script is absent")

        monkeypatch.setattr(p1, "_run_launcher", refuse)
        broken = p1.check_p1_6_launcher(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    assert broken.verdict == p1.FAIL, broken.verdict
    assert "launcher_artefacts_missing" in broken.verdict_reasons, broken.verdict_reasons
    # The script is gone, so the three real runs never happened.  Naming them
    # would blame probes that never ran -- the report and the reason set have to
    # agree, and there is no five_actions / dry_run_plan / logs_action
    # observation to agree with.
    for slug in (
        "action_table_is_not_the_five_verbs",
        "dry_run_plan_not_as_documented",
        "logs_action_not_as_documented",
    ):
        assert slug not in broken.verdict_reasons, slug
    assert _observation(broken, "five_actions") is None


def test_a_raised_check_still_names_a_reason(monkeypatch) -> None:
    """F7: ``run_checks`` converts a crash into a named FAIL, not a silence.

    The conversion must carry a slug built from the exception type, so a crashed
    check is a finding an operator can search for rather than prose.
    """

    def explode(root):
        raise RuntimeError("probe exploded")

    registry = list(p1.CHECKS)
    registry[4] = explode
    monkeypatch.setattr(p1, "CHECKS", tuple(registry))
    results = p1.run_checks(ROOT)
    broken = [c for c in results if c.bullet == p1.P1_BULLETS[4].bullet]
    assert len(broken) == 1
    assert broken[0].verdict == p1.FAIL, broken[0].verdict
    assert broken[0].verdict_reasons == ("check_raised:RuntimeError",), broken[0].verdict_reasons
    assert broken[0].observations, "a crashed check must still record what it saw"
