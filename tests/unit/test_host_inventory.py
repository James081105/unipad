"""Unit tests for :mod:`unipad.host.inventory` (PRD 4.1, 6 WP00, 9.2).

WP00 requires ``host-inventory.json`` -- CPU/RAM/disk/toolchain facts measured
on the Windows host -- and PRD 9.2 makes it part of the minimum runtime evidence
set.  The contract that matters here is not "the numbers look right" (they are
host-dependent and cannot be asserted) but:

- the document always declares ``schema_version`` 1 with the documented blocks
  (PRD 9.2 evidence shape);
- a probe that cannot run degrades to ``None`` plus a note instead of raising
  or inventing a value (PRD 6 WP00: "audit list", never a fabricated fact);
- every disk entry carries the keys a later space check needs.

All probes are injected, so the suite needs no PowerShell, no WMI and no real
disk: ``monkeypatch`` replaces the PowerShell probe and ``shutil.disk_usage``.
Nothing here is evidence about a real host -- it proves the collector's
behaviour (docs/testing-strategy.md D2/D3).
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.host import inventory as inv
from unipad.host.inventory import (
    INVENTORY_SCHEMA_VERSION,
    collect_host_inventory,
    write_host_inventory,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stable_toolchain(monkeypatch) -> None:
    """Make the toolchain block independent of what is installed on this box."""
    monkeypatch.setattr(inv, "_command_version", lambda command, timeout: f"{command}-1.0")


def _cim_facts(**overrides):
    """A Win32_* shaped CIM payload, as ``ConvertTo-Json`` would emit it."""
    facts = {
        "hypervisor_present": True,
        "system_manufacturer": "LAPTOP",
        "system_model": "TEST",
        "total_physical_memory": 16 * 1024 ** 3,
        "cpu_name": "Fake Intel Core i9-9999K",
        "cpu_vendor": "GenuineFake",
        "cpu_cores": 8,
        "cpu_threads": 16,
        "cpu_virtualization": True,
        "gpu": [{"Name": "Fake RTX 4090", "AdapterRAM": 24 * 1024 ** 3, "DriverVersion": "1.0"}],
        "physical_disks": [{"Model": "Fake NVMe SSD", "Size": 1024 ** 4}],
    }
    facts.update(overrides)
    return facts


def _probe_returning(monkeypatch, facts, note=None):
    """Replace the CIM probe with a canned answer."""
    monkeypatch.setattr(inv, "_probe_cim", lambda timeout: (facts, note))


def _disk_usage_for(monkeypatch, letter: str, total_bytes: int, free_bytes: int):
    """Make only ``<letter>:`` enumerable; every other drive raises."""
    real = inv.shutil.disk_usage

    def fake(root: str):
        if root.upper().startswith(letter.upper()):
            return SimpleNamespace(total=total_bytes, used=total_bytes - free_bytes, free=free_bytes)
        raise OSError("not ready")

    monkeypatch.setattr(inv.shutil, "disk_usage", fake)
    return real


# ---------------------------------------------------------------------------
# schema / shape
# ---------------------------------------------------------------------------


def test_collect_host_inventory_declares_schema_version_one():
    """PRD 9.2: the evidence document must carry the schema version."""
    inventory = collect_host_inventory(probe_cim=False)

    assert inventory["schema_version"] == 1
    assert INVENTORY_SCHEMA_VERSION == 1
    assert inventory["collector"] == "unipad.host.inventory"


def test_collect_host_inventory_has_the_documented_blocks():
    """PRD 9.2 / host-inventory.json: every documented block is present."""
    inventory = collect_host_inventory(probe_cim=False)

    for key in ("host", "cpu", "memory", "gpu", "disks", "physical_disks", "toolchain", "notes"):
        assert key in inventory, f"missing evidence block {key!r}"
    assert set(inventory["host"]) >= {"os", "kernel", "arch"}
    assert set(inventory["cpu"]) >= {
        "model",
        "cores_physical",
        "threads_logical",
        "vt_x",
        "hypervisor_present",
    }
    assert set(inventory["memory"]) >= {"total_gb", "free_gb_at_inventory"}
    assert isinstance(inventory["notes"], list)


def test_host_block_reports_the_real_architecture_without_inventing_it():
    """PRD 6 WP00: arch is measured from ``platform.machine()``, never guessed."""
    inventory = collect_host_inventory(probe_cim=False)

    # The convenience mirrors must agree with the canonical nested block.
    assert inventory["os"] == inventory["host"]["os"]
    assert inventory["arch"] == inventory["host"]["arch"]
    assert inventory["host"]["os"]


def test_toolchain_block_reports_the_running_python(monkeypatch):
    """PRD 6 WP00: python/pytest versions are part of the frozen baseline."""
    _stable_toolchain(monkeypatch)
    inventory = collect_host_inventory(probe_cim=False)

    python = inventory["toolchain"]["python"]
    assert python["version"]
    assert python["implementation"]
    assert "command" in python


# ---------------------------------------------------------------------------
# degrade-with-notes (PRD 6 WP00: audit, never fabricate)
# ---------------------------------------------------------------------------


def test_missing_powershell_probe_degrades_instead_of_raising(monkeypatch):
    """PRD 6 WP00: a probe that cannot run must not abort the inventory."""
    monkeypatch.setattr(
        inv,
        "_run_powershell",
        lambda script, timeout: (False, "", "simulated: powershell/pwsh not found on PATH"),
    )
    _stable_toolchain(monkeypatch)

    inventory = collect_host_inventory()  # must not raise

    assert any("cim probe degraded" in note for note in inventory["notes"])
    assert "powershell/pwsh not found" in inventory["hypervisor_note"]


def test_degraded_probe_never_fabricates_the_hypervisor_state(monkeypatch):
    """PRD 6 WP00: unknown must stay ``None``, not default to False."""
    _probe_returning(monkeypatch, {}, note="simulated: cim unavailable")
    _stable_toolchain(monkeypatch)

    inventory = collect_host_inventory()

    assert inventory["hypervisor_present"] is None
    assert inventory["cpu"]["hypervisor_present"] is None
    assert "unknown" in inventory["notes"][0].lower() or "degraded" in inventory["notes"][0].lower()


def test_degraded_probe_documents_where_cpu_counts_came_from(monkeypatch):
    """PRD 6 WP00: whatever filled the CPU counts is named in the notes.

    The value may come from psutil (installed here) or from the documented
    ``os.cpu_count()`` upper-bound fallback; either way the provenance is
    recorded, so a reader never has to guess whether it was measured.
    """
    _probe_returning(monkeypatch, {}, note="simulated: cim unavailable")
    _stable_toolchain(monkeypatch)

    inventory = collect_host_inventory()
    cpu = inventory["cpu"]

    assert cpu["threads_logical"] >= 1
    assert cpu["cores_physical"] >= 1
    assert any(
        "cpu.cores_physical" in note or "cpu.threads_logical" in note
        for note in inventory["notes"]
    ), f"cpu provenance undocumented: {inventory['notes']}"
    # vt_x must be reported as unknown rather than as enabled/disabled.
    assert cpu["vt_x"] == "unknown"


def test_degraded_probe_yields_empty_gpu_and_physical_disk_lists(monkeypatch):
    """PRD 9.2: an unmeasurable list is empty plus a note, never invented."""
    _probe_returning(monkeypatch, {}, note="simulated: cim unavailable")
    _stable_toolchain(monkeypatch)

    inventory = collect_host_inventory()

    assert inventory["gpu"]["adapters"] == []
    assert inventory["gpu"]["virtual_displays"] == []
    assert inventory["physical_disks"] == []
    assert any("gpu list unavailable" in note for note in inventory["notes"])


def test_tcg_implications_are_always_recorded(monkeypatch):
    """PRD 2.2-1: the inventory states the TCG-only consequence on every run."""
    _probe_returning(monkeypatch, {}, note="simulated: cim unavailable")
    _stable_toolchain(monkeypatch)

    inventory = collect_host_inventory()

    implications = " ".join(inventory["tcg_implications"])
    assert "accelerator_not_tcg" in implications
    assert "tcg" in implications.lower()


# ---------------------------------------------------------------------------
# cim enrichment
# ---------------------------------------------------------------------------


def test_cpu_block_prefers_measured_cim_values(monkeypatch):
    """PRD 6 WP00: measured CPU facts win over ``platform.processor()``."""
    _probe_returning(monkeypatch, _cim_facts())
    _stable_toolchain(monkeypatch)

    cpu = collect_host_inventory()["cpu"]

    assert cpu["model"] == "Fake Intel Core i9-9999K"
    assert cpu["vendor"] == "GenuineFake"
    assert cpu["cores_physical"] == 8
    assert cpu["threads_logical"] == 16
    assert cpu["vt_x"] == "enabled_in_firmware"


def test_vt_x_reports_disabled_in_firmware_when_measured(monkeypatch):
    """PRD 6 WP00: the measured firmware state is reported verbatim."""
    _probe_returning(monkeypatch, _cim_facts(cpu_virtualization=False))
    _stable_toolchain(monkeypatch)

    cpu = collect_host_inventory()["cpu"]

    assert cpu["vt_x"] == "present_disabled_in_firmware"


def test_hypervisor_present_true_is_reported_with_its_warning(monkeypatch):
    """PRD 6 WP00: an active hypervisor must reach the evidence, not be hidden."""
    _probe_returning(monkeypatch, _cim_facts(hypervisor_present=True))
    _stable_toolchain(monkeypatch)

    inventory = collect_host_inventory()

    assert inventory["hypervisor_present"] is True
    assert "Hyper-V" in inventory["hypervisor_note"]


def test_gpu_block_separates_virtual_display_adapters(monkeypatch):
    """PRD 4.2 / 4.3: virtual adapters are flagged, never used as the target."""
    _probe_returning(
        monkeypatch,
        _cim_facts(
            gpu=[
                {"Name": "Fake RTX 3060", "AdapterRAM": 6 * 1024 ** 3, "DriverVersion": "1.0"},
                {"Name": "OrayIddDriver Device", "AdapterRAM": None, "DriverVersion": "2.0"},
            ]
        ),
    )
    _stable_toolchain(monkeypatch)

    gpu = collect_host_inventory()["gpu"]

    assert gpu["model"] == "Fake RTX 3060"
    assert gpu["virtual_displays"] == ["OrayIddDriver Device"]


def test_physical_disks_come_from_the_disk_drive_probe(monkeypatch):
    """PRD 6 WP00: Win32_DiskDrive facts land in the evidence set."""
    _probe_returning(monkeypatch, _cim_facts())
    _stable_toolchain(monkeypatch)

    disks = collect_host_inventory()["physical_disks"]

    assert disks == [{"model": "Fake NVMe SSD", "size_gb": 1024.0}]


# ---------------------------------------------------------------------------
# disks
# ---------------------------------------------------------------------------


def test_disk_entries_carry_device_size_and_free(monkeypatch):
    """PRD 6 WP00: each drive reports device/size/free so space checks work."""
    _disk_usage_for(monkeypatch, "F", 600 * 1024 ** 3, 69 * 1024 ** 3)
    _stable_toolchain(monkeypatch)

    disks = collect_host_inventory(probe_cim=False)["disks"]

    assert len(disks) == 1
    entry = disks[0]
    assert entry["device"] == "F:"
    assert entry["size_gb"] == pytest.approx(600.0, abs=0.2)
    assert entry["free_gb"] == pytest.approx(69.0, abs=0.2)


def test_critically_low_free_space_is_flagged_per_drive(monkeypatch):
    """PRD 6 WP00: a drive that must not hold artifacts is flagged inline."""
    _disk_usage_for(monkeypatch, "C", 198 * 1024 ** 3, int(0.6 * 1024 ** 3))
    _stable_toolchain(monkeypatch)

    disks = collect_host_inventory(probe_cim=False)["disks"]

    assert "warning" in disks[0]
    assert "critically low" in disks[0]["warning"]


def test_no_enumerable_drive_is_a_note_not_an_exception(monkeypatch):
    """PRD 6 WP00: nothing to measure means an empty list plus a note."""

    def refuse(root: str):
        raise OSError("no volume")

    monkeypatch.setattr(inv.shutil, "disk_usage", refuse)
    _stable_toolchain(monkeypatch)

    inventory = collect_host_inventory(probe_cim=False)

    assert inventory["disks"] == []
    assert any("no drives could be enumerated" in note for note in inventory["notes"])


# ---------------------------------------------------------------------------
# determinism / writing
# ---------------------------------------------------------------------------


def test_collected_at_is_derived_from_the_injected_clock(monkeypatch):
    """Determinism: the injected clock, never the wall clock, stamps the doc."""
    epoch = 1_736_000_000.0
    expected = (
        dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%d")
    )
    _stable_toolchain(monkeypatch)

    first = collect_host_inventory(probe_cim=False, clock=lambda: epoch)
    second = collect_host_inventory(probe_cim=False, clock=lambda: epoch)

    assert first["collected_at"] == expected
    assert first["collected_at_epoch"] == int(epoch)
    # Same clock -> same stamp, so two runs of the same host diff cleanly.
    assert first["collected_at"] == second["collected_at"]


def test_write_host_inventory_round_trips_as_sorted_json(tmp_path):
    """PRD 9.2: the evidence file is machine-readable, key-sorted JSON."""
    target = tmp_path / "nested" / "host-inventory.json"

    written = write_host_inventory(target, collect_host_inventory(probe_cim=False))
    document = json.loads(target.read_text(encoding="utf-8"))

    assert written == target.resolve()
    assert document["schema_version"] == INVENTORY_SCHEMA_VERSION
    assert document["collector"] == "unipad.host.inventory"
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_write_host_inventory_rejects_a_document_without_the_schema_version(tmp_path):
    """PRD 7.4: an unlabelled inventory is not evidence and must be refused."""
    with pytest.raises(UniPadError) as excinfo:
        write_host_inventory(tmp_path / "bad.json", {"host": {}})

    assert excinfo.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID
    assert "schema_version" in excinfo.value.message


def test_write_host_inventory_rejects_a_non_mapping(tmp_path):
    """PRD 7.4: refuse rather than write a file that cannot be validated."""
    with pytest.raises(UniPadError) as excinfo:
        write_host_inventory(tmp_path / "bad.json", ["not", "a", "mapping"])

    assert excinfo.value.code == ErrorCode.ARTIFACT_MANIFEST_INVALID
