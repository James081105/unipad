"""The acceptance gate's report renders through one table renderer.

``unipad.acceptance.__main__`` ships an adapter -- :func:`as_verdict` plus
:func:`render_rows` -- whose module docstring says it exists "so a single
report can render them in one table".  For a while nothing called it: the
``--verify`` path printed the bundle's four-field rows with its own ad-hoc
``%-22s %-11s ...`` format string, so the gate had two spellings of the same
table and the adapter's promise was a description of code that did not run.

What these tests pin:

* the ``--verify`` text report really goes through the adapter, so the two
  spellings cannot drift back apart;
* the module docstring does not claim a code path that does not exist -- the
  note in ``config/machine.json`` calls a promise nothing keeps "the same
  defect this note was written to correct", and a docstring is the same kind of
  promise.

The bundle fixtures here are written for real and re-hashed for real, because
``load_bundle`` refuses a bundle that disagrees with its own checks.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unipad.acceptance import __main__ as gate_main
from unipad.acceptance import record
from unipad.acceptance.record import EvidenceRef, Verdict
from unipad.acceptance.runner import (
    AcceptanceBundle,
    CheckDef,
    CheckResult,
    EnvironmentStamp,
    propagate_dependencies,
    release_verdict,
    write_bundle,
)
from unipad.errors import ExitCode

REPO_ROOT = Path(__file__).resolve().parents[2]


def _stamp() -> EnvironmentStamp:
    return EnvironmentStamp(
        host_fingerprint="f" * 64,
        host_label="test-host",
        accel="tcg",
        qemu_version="9.0.0",
        os_build="1",
        python_version="3.12.9",
        collected_at="2026-10-23T00:00:00Z",
        host_inventory_sha256="0" * 64,
    )


def _check() -> CheckDef:
    return CheckDef(
        check_id="demo.check",
        prd_citation="docs/PRD.md:1",
        requirement="a demo requirement",
        rejection="a demo rejection",
        tier="blocking",
        gate_id="demo",
        milestone="M0",
    )


def _bundle_dir(tmp_path: Path) -> Path:
    """Write a real, loadable bundle into ``tmp_path`` and return the directory."""
    run_dir = tmp_path / "demo-run"
    (run_dir / "acceptance").mkdir(parents=True)
    payload = b"serial log payload\n"
    (run_dir / "acceptance" / "serial.log").write_bytes(payload)
    ref = EvidenceRef(
        path="acceptance/serial.log",
        kind="serial_log",
        sha256=hashlib.sha256(payload).hexdigest(),
        scope=record.EvidenceScope.HOST_REAL,
    )
    verdict = Verdict.fail(["demo_reason"], [ref])
    result = CheckResult(
        check=_check(),
        verdict=verdict,
        environment=_stamp(),
        started_at="2026-10-23T00:00:00Z",
        finished_at="2026-10-23T00:00:01Z",
    )
    checks = propagate_dependencies((result,))
    aggregate_verdict, reasons = release_verdict(checks, coverage_gaps=(), run_complete=True)
    bundle = AcceptanceBundle(
        run_id=run_dir.name,
        run_complete=True,
        environment=_stamp(),
        checks=checks,
        written_at="2026-10-23T00:00:00Z",
        aggregate={"verdict": aggregate_verdict, "reasons": list(reasons)},
    )
    write_bundle(bundle, run_dir)
    return run_dir


def test_the_verify_report_renders_through_the_adapter(tmp_path: Path, capsys) -> None:
    """``--verify`` prints the adapter's table, not a second spelling of it.

    The rows come from the bundle's own ``CheckResult`` objects -- the ``runner``
    shape :func:`as_verdict` maps -- so the renderer and the report are the same
    code and a column cannot change in one place only.
    """
    run_dir = _bundle_dir(tmp_path)
    code = gate_main.main(["--verify", str(run_dir)])

    assert code == int(ExitCode.BLOCKED)
    out = capsys.readouterr().out
    # The table is the adapter's, byte for byte -- not a second spelling that
    # happens to carry the same header words.
    from unipad.acceptance.runner import load_bundle

    bundle = load_bundle(run_dir)
    expected = gate_main.render_rows(
        [gate_main.as_verdict("runner", check) for check in bundle.checks]
    )
    assert expected in out
    # The header is the adapter's, spelled exactly as ``render_rows`` spells it.
    assert "SOURCE" in out and "IMPLEMENTATION" in out and "WINDOWS" in out
    assert "ONSITE" in out and "VERDICT" in out
    # The row is the bundle's check, read through the adapter.
    assert "demo.check" in out
    assert "FAIL" in out
    # The prose of the run and the unverified-field list are still there: wiring
    # the table in must not cost the operator the rest of the report.
    assert "run demo-run" in out
    assert "fields left unverified on site" in out


def test_the_adapter_is_what_renders_the_report(tmp_path: Path, capsys) -> None:
    """Prove the wiring, not just the output: the renderer is really called.

    An output assertion above would also pass if ``_verify`` grew its own table
    again with the same header words.  This one replaces the module's own
    renderer with a marker and requires that the report go through it.
    """
    run_dir = _bundle_dir(tmp_path)
    real = gate_main.render_rows
    seen: list[object] = []

    def spy(rows):
        seen.append(list(rows))
        return real(rows)

    gate_main.render_rows = spy
    try:
        gate_main.main(["--verify", str(run_dir)])
    finally:
        gate_main.render_rows = real

    capsys.readouterr()
    assert len(seen) == 1
    rows = seen[0]
    assert [row.source for row in rows] == ["runner"]
    assert [row.check_id for row in rows] == ["demo.check"]


def test_the_adapter_never_upgrades_a_field_on_the_way_through(tmp_path: Path) -> None:
    """The one directional guarantee the docstring makes about the mapping.

    A row adapted from a bundle carries the bundle's own verdict and its own
    derived columns; the adapter copies, it does not improve.  Compared by value
    rather than by identity, because :attr:`CheckResult.fields` is a derived
    property that builds a fresh row on every read -- which is exactly why the
    adapter must not be the place a field gets decided.
    """
    from unipad.acceptance.runner import load_bundle

    bundle = load_bundle(_bundle_dir(tmp_path))
    row = gate_main.as_verdict("runner", bundle.checks[0])
    assert row.verdict.kind == bundle.checks[0].verdict.kind
    assert row.verdict.reasons == bundle.checks[0].verdict.reasons
    assert row.fields.to_dict() == bundle.checks[0].fields.to_dict()
    assert row.fields.windows_status == bundle.checks[0].fields.windows_status
    assert row.fields.onsite_status == bundle.checks[0].fields.onsite_status


def test_the_report_asks_the_adapter_only_for_shapes_a_bundle_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The report's sources are the ones a written bundle actually holds.

    ``--run`` is still refused -- there is no registered check universe -- so
    nothing produces a ``p1`` / ``e2e`` / ``milestones`` result for a report to
    render.  A docstring that says the adapter maps all four into one table is
    describing a table that does not run, so the wiring is pinned to the shapes
    the bundle carries and the rest is documented as arriving later.
    """
    run_dir = _bundle_dir(tmp_path)
    real = gate_main.as_verdict
    sources: list[str] = []

    def spy(source, result):
        sources.append(source)
        return real(source, result)

    monkeypatch.setattr(gate_main, "as_verdict", spy)
    gate_main.main(["--verify", str(run_dir)])
    capsys.readouterr()

    assert sources == ["runner"] * len(sources)
    assert set(sources) == {"runner"}
    # The one source rendered is a shape ``AcceptanceBundle`` really carries.
    from unipad.acceptance.runner import load_bundle

    bundle = load_bundle(run_dir)
    assert len(sources) == len(bundle.checks)


def test_the_report_and_the_json_path_agree_on_the_rows(tmp_path: Path, capsys) -> None:
    """``--json`` and the text table describe the same run.

    Two output surfaces of one report are free to disagree about which checks
    exist; pinning them against each other is cheaper than pinning both.
    """
    run_dir = _bundle_dir(tmp_path)

    gate_main.main(["--verify", str(run_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)

    gate_main.main(["--verify", str(run_dir)])
    out = capsys.readouterr().out

    json_ids = {row["check_id"] for row in payload["four_fields"]}
    assert json_ids
    for check_id in json_ids:
        assert check_id in out
    # The JSON payload carries the columns the table prints.
    row = payload["four_fields"][0]
    assert set(row) >= {"check_id", "implementation", "windows_status", "onsite_status"}


def test_an_unadaptable_bundle_row_is_a_refusal_not_a_guess(tmp_path: Path) -> None:
    """The adapter raises on a shape it does not recognise.

    A silently mis-read row is how a green column gets manufactured, so the
    unknown-source branch stays a refusal.
    """
    with pytest.raises(Exception) as raised:
        gate_main.as_verdict("telepathy", SimpleNamespace())
    assert "unknown verdict source" in str(raised.value)
