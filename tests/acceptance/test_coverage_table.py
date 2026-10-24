"""``scripts/windows/build-coverage-table.py``: the table that cannot rot.

Why this is tested at all
-------------------------
The goal document (§18 step 1) asks for a coverage table mapping requirement →
implementation → runner verification → Windows measurement → evidence path.  A
hand-written table is a table that stops being true the first time a check
changes, so the table is *rendered from a real gate run* and this file is
committed as output.  What is tested here is therefore the renderer's contract,
not any particular row:

* the four state columns must all appear, because CLAUDE.md forbids treating
  them as one column;
* an evidence path must appear when the verdict has one, and an em dash when it
  does not -- a blank cell reads as "no evidence needed";
* the privacy guard must actually refuse, because the last two leaks in this
  repo were an absolute path written somewhere nobody thought to look;
* a Python ``repr`` must never reach a table cell, because ``['M13:BLOCKED',
  ...]`` is a table nobody can read;
* the two verdict shapes the bundle really uses must both render -- the first
  draft of the renderer crashed on the second one.

None of this runs the gate: that takes a minute and belongs to the generator's
own invocation, not to a unit test.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "windows" / "build-coverage-table.py"

pytestmark = pytest.mark.acceptance


def _load_module():
    """Import the generator, whose filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("build_coverage_table", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator():
    if not SCRIPT.is_file():  # pragma: no cover - the script is committed
        pytest.skip(f"{SCRIPT} is not present")
    return _load_module()


def _bundle() -> dict:
    """A bundle shaped like a real one, with both verdict shapes in it."""
    return {
        "run_id": "synthetic",
        "written_at": "2026-10-25T00:00:00+00:00",
        "verifier": {"id": "unipad.controller.verifier", "version": "1.1.0"},
        "verdict": "BLOCKED",
        "aggregate": {
            "verdict": "BLOCKED",
            "blocking_failures": [],
            "advisory_failures": ["M13:BLOCKED", "M14:BLOCKED"],
            "reasons": ["blocking_by:M10a:no real guest install service"],
        },
        "scope_summary": {"simulation": 3, "host_real": 0, "onsite": 0},
        "coverage_gaps": (),
        "field_unverified": [
            {"check_id": "M4", "blocker": "no UniXNU kernel image on this host"}
        ],
        "checks": [
            {
                "check": {
                    "check_id": "M0",
                    "prd_citation": "docs/PRD.md:206",
                    "requirement": "依赖盘点与构建成立",
                    "tier": "blocking",
                },
                "four_fields": {
                    "implementation": "UNVERIFIED",
                    "windows_status": "FAIL",
                    "onsite_status": "BLOCKED",
                    "blocker": "config/sources.lock.json is absent",
                },
                "verdict": {
                    "kind": "BLOCKED",
                    "evidence": [
                        {"path": "acceptance/artifacts/m0/ai-smoke.json"},
                        {"path": "acceptance/config/sources.lock.json"},
                    ],
                },
            },
            {
                "check": {
                    "check_id": "M10a",
                    "prd_citation": "docs/PRD.md:219",
                    "requirement": "IPA 接受和执行策略明确",
                    "tier": "blocking",
                },
                "four_fields": {
                    "implementation": "UNVERIFIED",
                    "windows_status": "NOT RUN",
                    "onsite_status": "BLOCKED",
                    "blocker": "no real guest install service",
                },
                "verdict": {"kind": "BLOCKED", "evidence": []},
            },
        ],
        "cases": [
            {
                "case_id": "case1_start_new_qemu",
                "prd_citation": "docs/PRD.md:210",
                "verdict": "BLOCKED",
                "reasons": ["blocked_by:M9:no kernel image"],
                "check_ids": ["M9", "M10a"],
            }
        ],
    }


def test_the_four_state_columns_all_appear(generator) -> None:
    """Implementation, simulation, runner and on-site are four columns, not one.

    CLAUDE.md forbids letting a green in one stand in for the others.  A table
    that dropped a column would let a CODE COMPLETE row read as verified, which
    is the exact conflation the four-field rule exists to prevent.
    """
    text = generator.render(_bundle(), "synthetic")
    header = next(line for line in text.splitlines() if line.startswith("| 检查 |"))
    for column in ("实现状态", "Windows/模拟", "运行器验证", "现场（Windows 实测）"):
        assert column in header, (column, header)
    assert "证据路径" in header, header
    assert "阻断原因" in header, header


def test_every_state_cell_carries_its_value(generator) -> None:
    """Each check's four states reach the table under its own check id."""
    text = generator.render(_bundle(), "synthetic")
    rows = {line.split("|")[1].strip(): line for line in text.splitlines()
            if line.startswith("| `M")}
    assert set(rows) == {"`M0`", "`M10a`"}, sorted(rows)

    m0 = rows["`M0`"]
    for value in ("UNVERIFIED", "FAIL", "BLOCKED"):
        assert value in m0, (value, m0)
    # A check with no evidence gets an em dash, not an empty cell: an empty
    # cell reads as "no evidence required", a dash reads as "none found".
    assert "—" in rows["`M10a`"], rows["`M10a`"]
    assert "`acceptance/artifacts/m0/ai-smoke.json`" in m0, m0


def test_a_case_verdict_spelled_as_a_word_renders(generator) -> None:
    """``cases`` carry a bare verdict word; the first draft crashed on it.

    ``checks`` carry a ``Verdict`` object and ``cases`` carry the word, because
    a case verdict is a composite of its checks and has no evidence of its own.
    A renderer that reads only one shape raises ``AttributeError`` half way
    through printing the table -- which is how the first draft failed, after the
    check table had already been written to stdout.
    """
    text = generator.render(_bundle(), "synthetic")
    case_rows = [line for line in text.splitlines() if line.startswith("| `case1_")]
    assert len(case_rows) == 1, case_rows
    assert "BLOCKED" in case_rows[0], case_rows[0]
    assert "blocked_by:M9:no kernel image" in case_rows[0], case_rows[0]


def test_no_python_repr_reaches_a_table_cell(generator) -> None:
    """A list is rendered as prose, because a repr is a table nobody reads."""
    text = generator.render(_bundle(), "synthetic")
    assert "['" not in text, "a Python list repr reached the rendered table"
    assert "M13:BLOCKED, M14:BLOCKED" in text, text


#: A stand-in identity for the privacy tests.  The real account name is
#: deliberately *not* written here: this repository must never contain it, not
#: even as fixture data, so the guard is exercised against an identity the test
#: itself supplies through the generator's own environment.
CANARY = "unipad-leak-canary"
CANARY_HOME = f"C:\\Users\\{CANARY}"


def test_the_privacy_guard_refuses_user_identity(generator) -> None:
    """The guard that stops PRD 9.2 leaks actually stops one.

    ``host/inventory.py`` and ``controller/cli.py:816`` each leaked an absolute
    path containing the Windows account name into evidence, and both were found
    by a test written *after* the fact.  This is the same defence, placed where
    the output is produced rather than where it was consumed.
    """
    with pytest.raises(SystemExit) as refused:
        generator._assert_no_user_data(
            f"主机：{CANARY_HOME}\\AppData", [CANARY, CANARY_HOME]
        )
    message = str(refused.value)
    assert "PRD 9.2" in message, message
    assert "refusing to print" in message, message


def test_the_privacy_guard_passes_a_clean_table(generator) -> None:
    """...and does not refuse a table that carries no user data."""
    text = generator.render(_bundle(), "synthetic")
    generator._assert_no_user_data(text, [CANARY, CANARY_HOME])


def test_the_renderer_runs_from_a_bundle_on_disk(tmp_path: Path) -> None:
    """``--bundle`` renders a real file without running the gate."""
    bundle = tmp_path / "acceptance.json"
    bundle.write_text(json.dumps(_bundle()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle", str(bundle)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert "| `M0` |" in result.stdout, result.stdout
    assert "门禁结论 | **BLOCKED**" in result.stdout, result.stdout


def test_the_generator_refuses_a_table_that_leaks_the_account_name(
    tmp_path: Path,
) -> None:
    """End to end: a leaking bundle is refused rather than printed.

    The guard is only worth anything if it is wired into the output path, so
    this feeds the generator a bundle whose host label *is* an account home
    directory and requires a non-zero exit with nothing on stdout.  The identity
    is supplied through the child's environment, which is where the generator
    reads it from -- so the test proves the guard fires on whatever identity the
    host actually has, without this repository having to contain one.
    """
    bundle = _bundle()
    bundle["checks"][0]["environment"] = {
        "host_label": f"{CANARY_HOME}\\AppData\\Local\\Temp",
        "accel": "tcg",
        "python_version": "3.12.9",
    }
    path = tmp_path / "acceptance.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")

    import os

    leaked_env = {
        **os.environ,
        "USERNAME": CANARY,
        "USERPROFILE": CANARY_HOME,
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--bundle", str(path)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=300,
        env=leaked_env,
    )
    assert result.returncode != 0, "a table containing the account name was printed"
    assert CANARY not in result.stdout, result.stdout
    assert CANARY_HOME not in result.stdout, result.stdout
    assert "PRD 9.2" in (result.stderr + result.stdout), result.stderr
