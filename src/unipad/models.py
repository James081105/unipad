"""Shared data models for UniPad.

Field names follow the PRD contracts verbatim:

- ``ExperimentRecord``  -> PRD 7.3 experiment journal format
- ``EvidenceSet``       -> PRD 9.2 minimum runtime evidence set
- ``MachineConfig``     -> PRD 9 ``config/machine.json``
- ``ArtifactManifest``  -> PRD 8.1 build artifact contract
- ``InstallResult``     -> PRD 9.1 install interface return contract
- ``GateResult``        -> PRD 6 milestone gate outcome

Models are intentionally dependency-free and JSON-round-trippable so the
controller, the verifier, and tests share one vocabulary.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# --- Milestones (PRD section 6).  Strings, not an enum: new sub-gates appear upstream. ---
M0 = "M0"
M1 = "M1"
M2 = "M2"
M3 = "M3"
M4 = "M4"
M5 = "M5"
M6 = "M6"
M7 = "M7"
M8 = "M8"
M9 = "M9"
M9A = "M9a"
M9B = "M9b"
M10 = "M10"
M10A = "M10a"
M11 = "M11"
M12 = "M12"
M13 = "M13"
M14 = "M14"
M15 = "M15"

IPA_MVP_GATES = (M0, M1, M2, M3, M4, M5, M6, M7, M8, M9, M9A, M9B, M10, M10A, M11, M12)


class ExperimentStatus:
    PLANNED = "planned"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class ExperimentRecord:
    """One experiment journal entry (PRD 7.3).  ``experiment_id`` is caller-assigned."""

    experiment_id: str
    milestone: str
    status: str = ExperimentStatus.PLANNED
    hypothesis: str = ""
    base_commit: Optional[str] = None
    candidate_commit: Optional[str] = None
    changes: list[str] = field(default_factory=list)
    source_lock_sha256: Optional[str] = None
    build: dict[str, Any] = field(default_factory=lambda: {"run_id": None, "artifact_manifest_sha256": None})
    runtime: dict[str, Any] = field(default_factory=lambda: {"host_inventory": None, "qemu_version": None, "accel": "tcg"})
    evidence: list[str] = field(default_factory=list)
    result: Optional[str] = None
    next_action: Optional[str] = None
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = self.schema_version
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExperimentRecord":
        d = dict(d)
        d.pop("schema_version", None)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)


@dataclass
class ArtifactEntry:
    """One deliverable file in a build manifest (PRD 8.1)."""

    name: str
    sha256: str
    size_bytes: int
    arch: Optional[str] = None      # e.g. "aarch64"; None for metadata files
    role: Optional[str] = None      # firmware / bootloader / kernel / devicetree / auxiliary
    path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactManifest:
    """Machine-readable build output contract (PRD 8.1)."""

    run_id: Optional[str]
    artifacts: list[ArtifactEntry] = field(default_factory=list)
    target: str = "tcg"             # payload target; HVF payloads must never be fetched to Windows
    unixnu_commit: Optional[str] = None
    device_repo_commit: Optional[str] = None
    toolchain: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["artifacts"] = [a.to_dict() for a in self.artifacts]
        return d

    def by_name(self, name: str) -> Optional[ArtifactEntry]:
        for a in self.artifacts:
            if a.name == name:
                return a
        return None


@dataclass
class EvidenceSet:
    """Minimum runtime evidence set (PRD 9.2).

    ``missing`` lists required-but-absent items; a verifier must never pass
    a gate while ``missing`` is non-empty (evidence_incomplete).
    """

    run_id: str
    experiment_json: Optional[str] = None
    host_inventory: Optional[str] = None
    artifact_manifest: Optional[str] = None
    qemu_args: Optional[str] = None
    serial_log: Optional[str] = None
    guest_log: Optional[str] = None
    qmp_log: Optional[str] = None
    screenshots: list[str] = field(default_factory=list)
    verification_json: Optional[str] = None
    gpu_bridge_log: Optional[str] = None
    missing: list[str] = field(default_factory=list)

    REQUIRED = (
        "experiment_json",
        "host_inventory",
        "artifact_manifest",
        "qemu_args",
        "serial_log",
        "verification_json",
    )

    def check_complete(self) -> list[str]:
        """Return the list of missing required evidence items (empty == complete)."""
        missing = [name for name in self.REQUIRED if getattr(self, name) in (None, "")]
        self.missing = missing
        return missing

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MachineConfig:
    """``config/machine.json``: virtual machine definition (PRD 9).

    ``accel`` must be ``tcg`` on the Intel Windows host; anything else is
    rejected at launch (accelerator_not_tcg).
    """

    name: str = "ipad-tcg"
    accel: str = "tcg"
    # The defaults describe the board UniPad actually boots (the pinned
    # UniXNU-qemu board.toml), not a generic AArch64 virt machine: a default
    # that disagrees with config/machine.json is a second, silently different
    # machine for any caller that builds one without reading the file.
    cpu_model: str = "cortex-a710"    # run-qemu.py:155
    cores: int = 1                    # board.toml:143 cpu_regs="0" -> make-qemu-adt.py:78
    memory_mb: int = 6144             # board.toml:92 dram_size="0x180000000"
    machine_type: str = "virt,virtualization=off,secure=off,gic-version=3"  # run-qemu.py:154
    clock: dict[str, Any] = field(default_factory=lambda: {"rtc": "utc"})
    display: dict[str, Any] = field(default_factory=lambda: {"width": 1376, "height": 1024, "format": "ramfb"})
    #: ``firmware.attach`` is here rather than left empty because the default
    #: decides which of two mutually exclusive QEMU spellings is used: an empty
    #: default silently means ``-bios``, which the board we boot cannot use.
    devices: dict[str, Any] = field(default_factory=lambda: {"firmware": {"attach": "pflash"}})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MachineConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class InstallResult:
    """Install interface return contract (PRD 9.1).

    ``stage`` is the furthest stage actually completed; a successful file
    transfer alone must never report ``installed``.
    """

    package_sha256: str
    bundle_id: Optional[str] = None
    stage: str = "received"          # received|checked|transferred|installed|registered|failed
    trust_mode: Optional[str] = None # how the guest trusts the package (recorded, never assumed)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    log_path: Optional[str] = None

    TERMINAL_STAGES = ("installed", "registered", "failed")

    #: Terminal stages that report success.  ``failed`` is terminal but is not
    #: a success; a gate asking "did the install complete" must use this.
    SUCCESS_STAGES = ("installed", "registered")

    @property
    def ok(self) -> bool:
        return self.stage in ("installed", "registered") and self.error_code is None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GateResult:
    """Milestone gate outcome (PRD 6/7.4)."""

    milestone: str
    passed: bool
    checked_at: str                          # ISO-8601, caller-stamped
    evidence: list[str] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)
    verifier_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
