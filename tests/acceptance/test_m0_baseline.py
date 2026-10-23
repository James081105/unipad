"""M0 baseline acceptance (PRD 6 M0 / WP00, PRD 9.2 evidence set).

What this file pins
-------------------
M0 is "dependency inventory and a build that stands up" -- *not* "the README is
readable and the script returned 0" (PRD 6, "不足以通过的情况").  The
machine-readable artefacts that every later milestone depends on are therefore
asserted here as **data**, never as prose:

* ``unipad doctor`` prints exactly one JSON envelope carrying the host
  inventory (``schema_version`` 1), the budget report and the tool findings;
* a missing ``qemu-system-aarch64`` is a **finding** inside a successful
  envelope -- never a crash and never a success (``ready_for_run`` stays
  ``False``);
* ``config/sources.lock.json`` exists, records ``frozen: false`` as an open
  item, and carries the build-number discrepancy field PRD 2.1 E04 demands;
  the controller refuses to build from a moving branch while it is unfrozen;
* ``host-inventory.json`` at the repository root parses and matches the
  inventory schema the live collector emits;
* the live collector never fabricates: an unmeasurable value is ``None`` with
  a note explaining why (PRD 9.2: evidence may not be invented).

Field status: everything here is host-side behaviour proved on Windows with
the real controller.  It is *not* evidence that a guest, a display or a disk
exists (docs/testing-strategy.md D2/D3).
"""

from __future__ import annotations

import io
import json
import platform
from pathlib import Path

import pytest

from unipad.controller import cli
from unipad.errors import ErrorCode
from unipad.host.inventory import (
    INVENTORY_SCHEMA_VERSION,
    collect_host_inventory,
    write_host_inventory,
)
from unipad.models import read_json

pytestmark = pytest.mark.acceptance

REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE_CONFIG = REPO_ROOT / "config" / "machine.json"
SOURCES_LOCK = REPO_ROOT / "config" / "sources.lock.json"
BUDGET_CONFIG = REPO_ROOT / "config" / "budget.json"
HOST_INVENTORY = REPO_ROOT / "host-inventory.json"

#: Top-level blocks every host inventory must carry (PRD 9.2 / inventory.py).
REQUIRED_INVENTORY_BLOCKS = ("host", "cpu", "memory", "gpu", "disks")

#: Build numbers PRD 2.1 E04 leaves unresolved; M0 must keep both visible.
BUILD_CANDIDATES = ("21H161", "21H461")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> tuple[int, dict]:
    """Run the controller and return ``(exit_code, parsed envelope)``.

    ``json.loads`` on the whole buffer is the assertion that the command
    printed **exactly one** JSON object (PRD 9.1): a second document, a
    traceback or a bare print would make this raise.
    """
    buffer = io.StringIO()
    exit_code = cli.main(argv, stream=buffer)
    return exit_code, json.loads(buffer.getvalue())


def _cli_paths(tmp_path: Path) -> list[str]:
    """Controller options that keep every write inside ``tmp_path``."""
    return [
        "--runs-root",
        str(tmp_path / "runs"),
        "--budget-config",
        str(BUDGET_CONFIG),
        "--budget-usage",
        str(tmp_path / "budget-usage.json"),
        "--sources-lock",
        str(SOURCES_LOCK),
        "--machine-config",
        str(MACHINE_CONFIG),
        "--artifacts-root",
        str(tmp_path / "artifacts"),
    ]


def _finding_codes(document: dict) -> set[str]:
    """Return the machine-readable finding codes of a doctor envelope."""
    return {str(finding.get("code")) for finding in document["data"]["findings"]}


def _is_dotted_version(value: object) -> bool:
    """True for ``11.1.0`` / ``1.2.3-rc1`` -- a measured version, not a label."""
    if not isinstance(value, str):
        return False
    head = value.split("-", 1)[0]
    parts = head.split(".")
    return bool(parts) and all(part.isdigit() and part != "" for part in parts)


# ---------------------------------------------------------------------------
# doctor: one machine-readable envelope (PRD 9.1)
# ---------------------------------------------------------------------------


def test_doctor_prints_one_json_envelope_with_inventory_and_budget(tmp_path):
    """PRD 9.1/6 WP00: doctor returns inventory (schema 1), budget and tools."""
    exit_code, document = _run_cli(["doctor", *_cli_paths(tmp_path)])

    # --- the envelope itself -------------------------------------------
    assert exit_code == 0
    assert document["command"] == "doctor"
    assert document["status"] == "ok"
    assert document["ok"] is True
    assert document["error"] is None
    assert document["schema_version"] == cli.ENVELOPE_SCHEMA_VERSION

    data = document["data"]

    # --- inventory: schema_version 1, real blocks, real tool facts -----
    inventory = data["host_inventory"]
    assert isinstance(inventory, dict)
    assert inventory["schema_version"] == 1
    assert INVENTORY_SCHEMA_VERSION == 1
    for block in REQUIRED_INVENTORY_BLOCKS:
        assert block in inventory, f"inventory is missing the {block!r} block"
    assert inventory["host"]["arch"]
    assert inventory["collector"]
    toolchain = inventory["toolchain"]
    assert toolchain["python"]["version"] == platform.python_version()
    assert isinstance(toolchain["python"].get("pytest"), str)
    assert "git" in toolchain

    # --- budget: machine-readable limits and usage ----------------------
    budget = data["budget"]
    assert budget["limits"]["per_hypothesis_max_experiments"] == 3
    assert budget["limits"]["same_failure_signature_max"] == 3
    # 0 means "Actions budget not authorized" -- an open item, not a default.
    assert budget["limits"]["max_actions_minutes_per_week"] == 0
    assert "usage" in budget and "remaining" in budget
    assert budget["exceeded"] == []

    # --- findings are machine-readable, not prose -----------------------
    for finding in data["findings"]:
        assert isinstance(finding, dict)
        assert isinstance(finding.get("code"), str) and finding["code"]
        assert isinstance(finding.get("detail"), str) and finding["detail"]

    # --- the machine definition is reported, not assumed ---------------
    assert data["machine"]["accel"] == "tcg"
    assert data["machine"]["config"] == MACHINE_CONFIG.as_posix()


def test_missing_qemu_is_a_finding_not_a_crash_and_not_success(tmp_path, monkeypatch):
    """PRD 6 M0: no QEMU binary -> finding; the command still exits 0 and
    ``ready_for_run`` stays ``False`` (a finding is not a pass)."""
    real_which = cli.shutil.which

    def _which_without_qemu(name, *args, **kwargs):
        if name == "qemu-system-aarch64":
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(cli.shutil, "which", _which_without_qemu)

    exit_code, document = _run_cli(["doctor", *_cli_paths(tmp_path)])

    # not a crash: the envelope is a success carrying findings
    assert exit_code == 0
    assert document["status"] == "ok"
    assert document["error"] is None

    data = document["data"]
    assert data["qemu"]["available"] is False
    assert data["qemu"]["path"] is None
    assert data["qemu"]["expected"] == "qemu-system-aarch64"

    codes = _finding_codes(document)
    assert "qemu_not_found" in codes
    detail = next(f["detail"] for f in data["findings"] if f["code"] == "qemu_not_found")
    assert "PATH" in detail

    # ... and not a success either: the host is explicitly not runnable.
    assert data["ready_for_run"] is False


def test_doctor_reports_a_non_tcg_machine_config_as_a_finding(tmp_path):
    """PRD 2.2-1: a non-TCG ``config/machine.json`` is reported, never used."""
    machine = read_json(MACHINE_CONFIG)
    machine["accel"] = "whpx"
    machine_path = tmp_path / "machine-whpx.json"
    machine_path.write_text(json.dumps(machine), encoding="utf-8")

    exit_code, document = _run_cli(
        ["doctor", *_cli_paths(tmp_path), "--machine-config", str(machine_path)]
    )

    assert exit_code == 0
    assert ErrorCode.ACCELERATOR_NOT_TCG in _finding_codes(document)
    assert document["data"]["machine"]["accel"] == "whpx"
    assert document["data"]["ready_for_run"] is False


def test_unfrozen_sources_lock_blocks_the_build_command(tmp_path):
    """PRD 6/8: an unfrozen lock is an open item that *stops* the paid path."""
    exit_code, document = _run_cli(
        [
            "build",
            "--stage",
            "M4",
            *_cli_paths(tmp_path),
        ]
    )

    assert exit_code == int(cli.ExitCode.BLOCKED)
    assert document["status"] == "blocked"
    assert document["error"]["code"] == ErrorCode.BLOCKED
    details = document["error"]["details"]
    assert details["present"] is True
    assert details["sources_lock"] == SOURCES_LOCK.as_posix()
    # nothing was built: the command stopped before the build capability.
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())


# ---------------------------------------------------------------------------
# config/sources.lock.json (PRD 8.1 inputs, 2.1 E04)
# ---------------------------------------------------------------------------


def test_sources_lock_is_present_and_records_frozen_false_as_an_open_item():
    """PRD 8.1/FR01: the lock exists; ``frozen: false`` is an explicit open item."""
    assert SOURCES_LOCK.is_file(), "config/sources.lock.json must exist (PRD 9)"
    lock = read_json(SOURCES_LOCK)

    assert lock["schema_version"] == 1
    assert lock["frozen"] is False

    # the open item is *recorded*, with the condition that closes it
    note = lock.get("frozen_note", "")
    assert "frozen=true" in note

    # M0 pinned the sources: the commits are real 40-hex SHAs now, not nulls.
    # A null here would mean the lock regressed to a moving branch.
    for block in (lock["unixnu"], lock["device_repo"]):
        commit = block["commit"]
        assert isinstance(commit, str) and len(commit) == 40, commit
        int(commit, 16)  # raises for anything that is not hexadecimal
        assert block["commit_note"]

    # Apple material is referenced by hash, never copied into the repository
    # (PRD 12): the lock records the IPSW digest, not the file.
    assert lock["ipsw"]["sha256"] is not None
    assert lock["ipsw"]["size"] > 0

    # tools pinned for M0.  Python is recorded from the live host; QEMU was
    # installed during the real-boot investigation, so it is no longer null --
    # a null here would mean the lock regressed to "we never measured it".
    assert lock["tools"]["python"]["version"] == platform.python_version()
    qemu = lock["tools"]["qemu"]
    assert qemu["accel"] == "tcg"
    assert qemu["version"], "QEMU is installed; the lock must record its version"
    assert _is_dotted_version(qemu["version"]), qemu["version"]
    # an identity claim needs a digest, not just a label
    digest = qemu.get("sha256")
    assert isinstance(digest, str) and len(digest) == 64, digest
    int(digest, 16)
    assert qemu["note"]


def test_sources_lock_carries_the_build_number_discrepancy_field():
    """PRD 2.1 E04: 21H161 vs 21H461 must stay visible, resolved or not."""
    lock = read_json(SOURCES_LOCK)
    identity = lock["system_identity"]

    assert identity["board"] == "iPad7,1"
    assert identity["system"] == "iPadOS 17.7.11"

    candidates = identity["build_candidates"]
    for build in BUILD_CANDIDATES:
        assert build in candidates

    # The history is never dropped: whichever field carries the answer, both
    # candidate spellings must still appear in it and it must name who decided.
    resolution = identity.get("resolution") or identity.get("discrepancy") or ""
    assert isinstance(resolution, str) and resolution.strip()
    for build in BUILD_CANDIDATES:
        assert build in resolution, "the resolution field must name both builds"
    assert "M0" in resolution or "ADR-0001" in resolution

    # Once resolved, the resolved value must be one of the candidates -- a
    # third spelling would mean the evidence was not checked.
    resolved = identity.get("resolved_to")
    if resolved is not None:
        assert resolved in candidates
        assert identity["build"] == resolved
        assert identity["resolution_evidence"], "a resolution needs citations"


# ---------------------------------------------------------------------------
# host-inventory.json at the repository root (PRD 9.2)
# ---------------------------------------------------------------------------


def test_repo_host_inventory_parses_and_matches_the_inventory_schema():
    """PRD 9.2: the checked-in inventory parses and matches schema_version 1."""
    assert HOST_INVENTORY.is_file(), "host-inventory.json must exist at the repo root"
    inventory = read_json(HOST_INVENTORY)

    assert inventory["schema_version"] == 1 == INVENTORY_SCHEMA_VERSION
    assert isinstance(inventory["collector"], str) and inventory["collector"]
    assert inventory["collected_at"]

    for block in REQUIRED_INVENTORY_BLOCKS:
        assert block in inventory, f"host-inventory.json is missing {block!r}"

    host = inventory["host"]
    assert host["os"]
    # This file describes the Intel Windows host of PRD 2.2 (x86_64, no ARM64).
    assert host["arch"] == "x86_64"
    assert host["kernel"]

    cpu = inventory["cpu"]
    assert cpu["model"]
    assert cpu["vendor"] == "GenuineIntel"
    assert isinstance(cpu["cores_physical"], int) and cpu["cores_physical"] > 0
    assert isinstance(cpu["threads_logical"], int) and cpu["threads_logical"] > 0
    assert cpu["vt_x"]
    assert isinstance(cpu["hypervisor_present"], bool)
    assert cpu["hypervisor_note"]

    memory = inventory["memory"]
    assert isinstance(memory["total_gb"], (int, float)) and memory["total_gb"] > 0
    assert memory["note"]

    gpu = inventory["gpu"]
    assert gpu["model"]
    assert isinstance(gpu["virtual_displays"], list)
    assert gpu["note"]

    disks = inventory["disks"]
    assert isinstance(disks, list) and disks
    for disk in disks:
        assert disk["device"]
        assert isinstance(disk["size_gb"], (int, float))
        assert isinstance(disk["free_gb"], (int, float))
    devices = {disk["device"] for disk in disks}
    assert "F:" in devices, "the repo lives on F:; the inventory must cover it"

    assert isinstance(inventory["physical_disks"], list) and inventory["physical_disks"]

    toolchain = inventory["toolchain"]
    assert toolchain["python"]["version"] == platform.python_version()
    assert toolchain["git"]

    implications = inventory["tcg_implications"]
    assert isinstance(implications, list) and implications
    assert any("TCG" in item for item in implications)

    # M0 open items are listed rather than silently dropped (PRD 6 M0).
    open_items = inventory["open_items"]
    assert isinstance(open_items, list) and open_items
    assert any("QEMU" in item for item in open_items)


# ---------------------------------------------------------------------------
# the live collector: schema match, and never a fabricated value (PRD 9.2)
# ---------------------------------------------------------------------------


def test_live_inventory_matches_the_schema_and_never_fabricates(tmp_path):
    """PRD 9.2: unmeasurable values are ``None`` plus a note, never invented."""
    inventory = collect_host_inventory(probe_cim=False)

    assert inventory["schema_version"] == 1
    for block in REQUIRED_INVENTORY_BLOCKS:
        assert block in inventory

    # the CIM probe was skipped, and that fact is recorded in the notes
    assert any("cim probe skipped" in note for note in inventory["notes"])

    # hypervisor state could not be measured: it stays None with a note
    # instead of being guessed either way (PRD 2.2-1 must not assume WHPX).
    assert inventory["hypervisor_present"] is None
    assert "could not be determined" in inventory["hypervisor_note"]
    assert any("unknown" in item for item in inventory["tcg_implications"])

    # CPU model is either measured or the documented fallback -- never empty
    assert inventory["cpu"]["model"]
    assert inventory["cpu"]["cores_physical"] >= 1
    assert inventory["cpu"]["threads_logical"] >= 1

    # ... and the document round-trips through the writer that PRD 9.2 names
    written = write_host_inventory(tmp_path / "host-inventory.json", inventory)
    reloaded = read_json(written)
    assert reloaded["schema_version"] == 1
    assert reloaded["collector"] == inventory["collector"]
