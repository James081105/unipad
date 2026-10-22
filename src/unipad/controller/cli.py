"""Command-line controller (PRD section 9.1).

Commands (PRD 9.1, exactly these)::

    unipad doctor
    unipad build   --stage M4 --experiment <id>
    unipad fetch   --run-id <github-run-id>
    unipad start   --profile tcg --artifact <manifest>
    unipad install <path-to-ipa>
    unipad verify  --suite ipa-mvp [--run <id>]
    unipad stop    [--graceful|--force]
    unipad collect --run <id>
    unipad restore --checkpoint <id>

Contract
--------
* every command prints **exactly one** JSON object to stdout and returns a
  fixed :class:`unipad.errors.ExitCode`;
* the object always carries ``command``/``status``/``exit_code``/``data``/
  ``error`` so scripts branch on fields, never on prose;
* nothing is claimed without evidence: when the underlying capability cannot
  run on this host (no QEMU binary, no artifacts, no guest channel) the
  command returns a structured ``blocked`` result with a concrete reason --
  never a faked success and never a fixed countdown (PRD FR08);
* no secrets are printed (PRD 9.2): stdout produced by a capability is
  captured and reported as a byte count only, so child output can never leak
  into the envelope.

Capability probing
------------------
``unipad.host`` / ``unipad.ipa`` / ``unipad.guest`` own the real Windows,
QEMU and guest-side work.  The controller only orchestrates, so each command
imports its capability **lazily** and resolves it through
:data:`_CAPABILITIES`.  When nothing resolves the command reports
``blocked`` with the probe list -- it does not invent a result.  Function
capabilities are invoked with keyword arguments filtered to the ones they
accept (adapter contract); class capabilities
(:class:`unipad.ipa.installer.IpaInstaller`,
:class:`unipad.host.logs.EvidenceBundle`,
:class:`unipad.host.images.ImageManager`,
:class:`unipad.host.qemu.QemuLauncher`) are constructed by the command
adapters below.  Either way a keyword dropped for that reason is reported
instead of being silently guessed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import shutil
import string
import sys
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .. import __version__
from ..errors import ErrorCode, ExitCode, UniPadError, blocked
from ..models import InstallResult, write_json
from .budget import BudgetTracker
from .experiments import ExperimentStore
from .state_machine import describe as describe_state_machine
from .verifier import Verifier, load_facts

__all__ = ["main", "CommandOutcome", "COMMANDS", "build_parser"]

#: Envelope schema version of the machine-readable command result.
ENVELOPE_SCHEMA_VERSION = 1

#: Not part of the PRD 10 fault taxonomy: a controller-side catch-all for
#: unexpected exceptions.  Promoting it to ``errors.ErrorCode`` requires a
#: decision record (docs/decisions/); it must never mask a known fault.
INTERNAL_ERROR_CODE = "internal_error"

#: Commands of PRD 9.1.  Anything else is ``unsupported_command``.
COMMANDS: tuple[str, ...] = (
    "doctor",
    "build",
    "fetch",
    "start",
    "install",
    "verify",
    "stop",
    "collect",
    "restore",
)

#: Global options that take a value (used to find the command token).
_VALUE_OPTIONS = frozenset(
    {
        "--runs-root",
        "--budget-config",
        "--budget-usage",
        "--sources-lock",
        "--machine-config",
        "--artifacts-root",
    }
)

#: Evidence file names mapped onto :class:`unipad.models.EvidenceSet` fields.
_EVIDENCE_FILES: dict[str, tuple[str, ...]] = {
    "experiment_json": ("experiment.json",),
    "host_inventory": ("host-inventory.json",),
    "artifact_manifest": ("artifact-manifest.json",),
    "qemu_args": ("qemu-args.txt", "qemu-args.json"),
    "serial_log": ("serial.log", "serial.txt"),
    "guest_log": ("guest.log", "guest-log.txt"),
    "qmp_log": ("qmp.log",),
    "gpu_bridge_log": ("gpu-bridge.log",),
    "verification_json": ("verification.json",),
}

#: Lazy capability probe lists: ``(module, attribute)`` candidates per capability.
#:
#: Only entry points that really exist are listed.  A capability whose tuple is
#: empty has **no implementation on this host yet**; every command that needs
#: one then returns a structured ``blocked`` result naming the capability, so a
#: missing implementation can never look like a passing run (PRD FR08).
#:
#: Class-shaped capabilities (``IpaInstaller``, ``EvidenceBundle``,
#: ``ImageManager``, ``QemuLauncher``) resolve through the same table; the
#: command adapters below construct them instead of calling them.
_CAPABILITIES: dict[str, tuple[tuple[str, str], ...]] = {
    "host_inventory": (("unipad.host.inventory", "collect_host_inventory"),),
    # PRD 6/8 builds run on a macOS runner; no build stage module exists in
    # unipad.host yet, so `build` reports blocked rather than inventing one.
    "build_stage": (("unipad.host.build", "build_stage"),),
    "fetch_artifacts": (("unipad.host.artifacts", "fetch_run"),),
    "ipa_precheck": (("unipad.ipa.precheck", "run_precheck"),),
    "ipa_install": (("unipad.ipa.installer", "IpaInstaller"),),
    # Mirrors unipad.ipa.installer._default_channel's probe order so a future
    # guest channel factory is picked up without controller changes.
    "guest_channel": (
        ("unipad.guest", "create_install_channel"),
        ("unipad.guest", "default_channel"),
        ("unipad.guest", "connect"),
        ("unipad.guest", "GuestChannel"),
    ),
    "collect_evidence": (("unipad.host.logs", "EvidenceBundle"),),
    "restore_image": (("unipad.host.images", "ImageManager"),),
    "launch": (("unipad.host.qemu", "QemuLauncher"),),
    "stop": (("unipad.host.qemu", "QemuLauncher"),),
    "status": (("unipad.host.qemu", "QemuLauncher"),),
}

#: Keys an artifact manifest may use for the three launch images.  The first
#: key that is present wins, so a manifest written by an older or newer fetch
#: step still resolves.
_IMAGE_KEYS: dict[str, tuple[str, ...]] = {
    "firmware": ("firmware", "bios", "bootloader"),
    "system_image": ("system_image", "system", "rootfs"),
    "data_image": ("data_image", "data"),
}


# ---------------------------------------------------------------------------
# machine-readable envelope
# ---------------------------------------------------------------------------
@dataclass
class CommandOutcome:
    """One machine-readable command result (PRD 9.1)."""

    command: str
    status: str                       # ok | failed | blocked
    exit_code: int
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def ok(self) -> bool:
        """``True`` only when the exit code is :data:`ExitCode.OK`."""
        return self.exit_code == int(ExitCode.OK)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable envelope."""
        return {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "unipad_version": __version__,
            "command": self.command,
            "status": self.status,
            "ok": self.ok,
            "exit_code": int(self.exit_code),
            "data": _jsonable(self.data),
            "error": _jsonable(self.error) if self.error else None,
            "generated_at": self.generated_at,
        }

    def to_json(self) -> str:
        """Return the envelope as one indented JSON object."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of ``value`` into JSON-serialisable data.

    The controller must always be able to emit a single JSON object, so
    arbitrary objects coming back from sibling packages degrade to strings
    instead of raising.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        with contextlib.suppress(Exception):
            return _jsonable(to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        with contextlib.suppress(Exception):
            return _jsonable(asdict(value))
    return f"<{type(value).__name__}>"


def _status_for(exit_code: ExitCode) -> str:
    """Map an exit code onto the envelope status vocabulary."""
    if exit_code == ExitCode.OK:
        return "ok"
    if exit_code == ExitCode.BLOCKED:
        return "blocked"
    return "failed"


def _ok(command: str, data: dict[str, Any] | None = None) -> CommandOutcome:
    """Build a successful outcome."""
    return CommandOutcome(
        command=command, status="ok", exit_code=int(ExitCode.OK), data=dict(data or {})
    )


def _failure(
    command: str,
    code: str,
    message: str,
    *,
    data: dict[str, Any] | None = None,
    **details: Any,
) -> CommandOutcome:
    """Build a failed outcome carrying a stable error code and the evidence."""
    error = UniPadError(code, message, **details)
    return CommandOutcome(
        command=command,
        status=_status_for(error.exit_code),
        exit_code=int(error.exit_code),
        data=dict(data or {}),
        error=error.to_dict()["error"],
    )


def _from_error(command: str, error: UniPadError) -> CommandOutcome:
    """Build an outcome from a :class:`UniPadError`."""
    return CommandOutcome(
        command=command,
        status=_status_for(error.exit_code),
        exit_code=int(error.exit_code),
        data=dict(error.details.get("data", {})) if isinstance(error.details, dict) else {},
        error=error.to_dict()["error"],
    )


def _emit(outcome: CommandOutcome, stream: Any = None) -> int:
    """Print exactly one JSON object and return the process exit code."""
    handle = stream if stream is not None else sys.stdout
    handle.write(outcome.to_json() + "\n")
    handle.flush()
    return int(outcome.exit_code)


# ---------------------------------------------------------------------------
# capability plumbing
# ---------------------------------------------------------------------------
def _import_module(dotted: str) -> Any | None:
    """Import ``dotted`` or return ``None`` when it is not installed.

    A module that exists but fails to import (syntax error, broken transitive
    dependency) is *not* silently ignored: it is re-raised as a blocked error
    so a broken sibling package stays visible instead of looking like
    "capability absent".
    """
    import importlib

    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - surface broken siblings, never hide them
        raise blocked(
            "capability module failed to import",
            module=dotted,
            error=f"{type(exc).__name__}: {exc}",
        ) from exc


def _resolve_capability(name: str) -> Callable[..., Any] | None:
    """Return the callable for ``name``, or ``None`` when nothing resolves."""
    for dotted, attribute in _CAPABILITIES.get(name, ()):
        module = _import_module(dotted)
        if module is None:
            continue
        candidate = getattr(module, attribute, None)
        if callable(candidate):
            return candidate
    return None


def _require_capability(command: str, name: str) -> Callable[..., Any]:
    """Return the callable for ``name`` or raise a structured blocked error."""
    capability = _resolve_capability(name)
    if capability is None:
        raise blocked(
            "capability unavailable on this host",
            command=command,
            capability=name,
            probed=[f"{module}.{attribute}" for module, attribute in _CAPABILITIES.get(name, ())],
            data={"capability": name},
        )
    return capability


def _accepted_keywords(fn: Callable[..., Any], kwargs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Filter ``kwargs`` to the parameters ``fn`` accepts.

    Returns the accepted mapping plus the sorted list of dropped names so the
    controller can report an adapter mismatch instead of guessing.
    """
    import inspect

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return dict(kwargs), []
    parameters = signature.parameters
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return dict(kwargs), []
    accepted = {key: value for key, value in kwargs.items() if key in parameters}
    dropped = sorted(set(kwargs) - set(accepted))
    return accepted, dropped


@contextlib.contextmanager
def _capture_stdout() -> Iterator[io.StringIO]:
    """Capture child stdout so the envelope stays the only thing printed."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


def _call_capability(
    fn: Callable[..., Any], **kwargs: Any
) -> tuple[Any, list[str], str]:
    """Call ``fn`` with filtered kwargs, capturing its stdout.

    Returns ``(result, dropped_keywords, captured_stdout)``.
    """
    accepted, dropped = _accepted_keywords(fn, kwargs)
    with _capture_stdout() as buffer:
        result = fn(**accepted)
    return result, dropped, buffer.getvalue()


def _as_data(value: Any) -> Any:
    """Convert a capability result into JSON-friendly data."""
    return _jsonable(value)


def _probed(name: str) -> list[str]:
    """Return the ``module.attribute`` probe list for a capability."""
    return [f"{module}.{attribute}" for module, attribute in _CAPABILITIES.get(name, ())]


def _machine_config(args: argparse.Namespace) -> Any:
    """Load ``config/machine.json`` into a :class:`unipad.models.MachineConfig`.

    Raises:
        UniPadError: ``config_invalid`` when the file is missing or is not a
            JSON object.
    """
    from ..models import MachineConfig

    path = Path(args.machine_config)
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine config not found or not a JSON object",
            command="start",
            path=path.as_posix(),
        )
    return MachineConfig.from_dict(raw)


def _launcher(args: argparse.Namespace) -> Any:
    """Build a :class:`unipad.host.qemu.QemuLauncher` for this host.

    Construction is where the TCG-only guard lives (PRD 2.2-1 / 8.3), so a
    non-TCG ``config/machine.json`` fails here with
    ``accelerator_not_tcg`` before any argument is built.
    """
    launcher_cls = _require_capability("start", "launch")
    return launcher_cls(_machine_config(args), qmp_host="127.0.0.1")


def _image_manager(args: argparse.Namespace) -> Any:
    """Build the :class:`unipad.host.images.ImageManager` for the artifact root."""
    manager_cls = _require_capability("restore", "restore_image")
    return manager_cls(Path(args.artifacts_root))


def _evidence_bundle(run_dir: Path, run_id: str) -> Any:
    """Build the :class:`unipad.host.logs.EvidenceBundle` for one run."""
    bundle_cls = _require_capability("collect", "collect_evidence")
    return bundle_cls(run_dir, run_id)


def _pid_alive(pid: int) -> bool:
    """Return whether ``pid`` names a live process (no signal is sent)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _launch_images(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Path | None]:
    """Resolve the launch images from the manifest, then machine config.

    The artifact manifest wins over ``config/machine.json`` because it names
    the exact bytes a fetched run produced.  A value of ``None`` means "not
    configured" -- for ``data_image`` that is the PRD 4.2 RAM-disk-only
    diagnostic run, for the others it is a hard stop reported by the caller.
    """
    disks = _read_json(Path(args.machine_config))
    disks = disks.get("disks") if isinstance(disks, dict) else None
    disks = disks if isinstance(disks, dict) else {}

    def resolve(keys: tuple[str, ...]) -> Path | None:
        for key in keys:
            for source in (manifest, disks):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return Path(value)
        return None

    return {name: resolve(keys) for name, keys in _IMAGE_KEYS.items()}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 of ``path`` (streamed; used for IPA and image hashes)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    """Read JSON from ``path``; missing file -> ``None``; malformed -> CONFIG_INVALID."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "configuration file is not valid JSON",
            path=path.as_posix(),
            error=str(exc),
        ) from exc


def _tracker(args: argparse.Namespace) -> BudgetTracker:
    """Build the budget tracker from the resolved CLI paths."""
    usage = args.budget_usage
    if usage in (None, ""):
        usage = str(Path(args.runs_root) / "budget-usage.json")
    return BudgetTracker(config_path=args.budget_config, usage_path=usage)


def _store(args: argparse.Namespace) -> ExperimentStore:
    """Build the experiment store from the resolved runs root."""
    return ExperimentStore(args.runs_root)


def _disk_free() -> list[dict[str, Any]]:
    """Return free/total bytes per existing drive (Windows-correct, no /dev)."""
    drives: list[dict[str, Any]] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:/")
        try:
            if not root.exists():
                continue
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        drives.append(
            {
                "drive": f"{letter}:",
                "total_bytes": usage.total,
                "free_bytes": usage.free,
                "free_gb": round(usage.free / (1024**3), 2),
                "low_free_space": usage.free < 1024**3,
            }
        )
    return drives


def _evidence_from_run_dir(run_dir: Path) -> Any:
    """Build an :class:`unipad.models.EvidenceSet` from a run directory."""
    from ..models import EvidenceSet

    evidence = EvidenceSet(run_id=run_dir.name)
    for attribute, names in _EVIDENCE_FILES.items():
        for name in names:
            candidate = run_dir / name
            if candidate.is_file():
                setattr(evidence, attribute, name)
                break
    screenshots_dir = run_dir / "screenshots"
    if screenshots_dir.is_dir():
        evidence.screenshots = sorted(
            path.relative_to(run_dir).as_posix()
            for path in screenshots_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        )
    return evidence


def _latest_run(store: ExperimentStore) -> str | None:
    """Return the most recently modified run id, or ``None``.

    Deterministic tie-break on the id so reports never depend on directory
    enumeration order.
    """
    scan = store.scan()
    if not scan.records:
        return None
    best: tuple[float, str] | None = None
    for record in scan.records:
        journal = store.path_for(record.experiment_id)
        try:
            modified = journal.stat().st_mtime
        except OSError:
            modified = 0.0
        key = (modified, record.experiment_id)
        if best is None or key > best:
            best = key
    return best[1] if best else None


def _bundle_manifest(run_dir: Path, run_id: str) -> dict[str, Any]:
    """Hash every file in ``run_dir`` and return the evidence bundle manifest."""
    files: list[dict[str, Any]] = []
    total = 0
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        files.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "sha256": _sha256_file(path),
                "size_bytes": size,
            }
        )
    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "file_count": len(files),
        "total_bytes": total,
        "files": files,
    }


def _machine_accel(args: argparse.Namespace) -> str | None:
    """Return the configured accelerator from ``config/machine.json``."""
    machine = _read_json(Path(args.machine_config))
    if isinstance(machine, dict):
        accel = machine.get("accel")
        if isinstance(accel, str):
            return accel
    return None


def _sources_frozen(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    """Return whether ``config/sources.lock.json`` is frozen, plus the lock data."""
    lock = _read_json(Path(args.sources_lock))
    if not isinstance(lock, dict):
        return False, {"sources_lock": Path(args.sources_lock).as_posix(), "present": False}
    return bool(lock.get("frozen")), {
        "sources_lock": Path(args.sources_lock).as_posix(),
        "present": True,
        "unixnu_commit": (lock.get("unixnu") or {}).get("commit"),
        "device_repo_commit": (lock.get("device_repo") or {}).get("commit"),
    }


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> CommandOutcome:
    """Inventory the host, toolchain and disks (WP00 re-check, PRD 6).

    A missing QEMU binary is reported as a *finding*, never a crash: the
    command still succeeds and says the host is not ready to run.
    """
    findings: list[dict[str, Any]] = []
    data: dict[str, Any] = {
        "unipad_version": __version__,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "state_machine": describe_state_machine(),
    }

    inventory_fn = _resolve_capability("host_inventory")
    if inventory_fn is None:
        findings.append(
            {
                "code": "capability_unavailable",
                "detail": "unipad.host.inventory.collect_host_inventory is not importable",
            }
        )
        data["host_inventory"] = None
    else:
        try:
            raw, _dropped, captured = _call_capability(inventory_fn)
            data["host_inventory"] = _as_data(raw)
        except Exception as exc:  # noqa: BLE001 - report, never crash the doctor
            findings.append(
                {
                    "code": "host_inventory_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            data["host_inventory"] = None
        else:
            data["child_stdout_bytes"] = len(captured)

    qemu_path = shutil.which("qemu-system-aarch64")
    data["qemu"] = {
        "expected": "qemu-system-aarch64",
        "path": qemu_path,
        "available": qemu_path is not None,
        "note": "TCG is mandatory on this Intel host; the launcher refuses any other accelerator.",
    }
    if qemu_path is None:
        findings.append(
            {
                "code": "qemu_not_found",
                "detail": "qemu-system-aarch64 is not on PATH; start/build stay blocked (M0 open item)",
            }
        )

    disks = _disk_free()
    data["disks"] = disks
    for disk in disks:
        if disk["low_free_space"]:
            findings.append(
                {
                    "code": "low_disk_space",
                    "detail": f"{disk['drive']} has {disk['free_gb']} GB free",
                    "drive": disk["drive"],
                }
            )

    configured_accel = _machine_accel(args)
    data["machine"] = {
        "config": Path(args.machine_config).as_posix(),
        "accel": configured_accel,
    }
    if configured_accel is not None and configured_accel != "tcg":
        findings.append(
            {
                "code": ErrorCode.ACCELERATOR_NOT_TCG,
                "detail": f"config/machine.json requests accel={configured_accel!r}; TCG is mandatory",
            }
        )

    data["findings"] = findings
    data["ready_for_run"] = qemu_path is not None and configured_accel in (None, "tcg")
    data["budget"] = _tracker(args).report().to_dict()
    return _ok("doctor", data)


def cmd_build(args: argparse.Namespace) -> CommandOutcome:
    """Build one milestone stage (PRD 6 / 8); real builds need the macOS runner."""
    store = _store(args)
    experiment_id = args.experiment
    if experiment_id and not store.exists(experiment_id):
        if not args.hypothesis:
            raise blocked(
                "experiment record not found; pass --hypothesis to create it",
                command="build",
                experiment_id=experiment_id,
            )
        store.create(experiment_id, args.stage, args.hypothesis)

    budget = _tracker(args)
    report = budget.check(
        args.stage,
        hypothesis=args.hypothesis,
        actions_minutes=args.actions_minutes,
        new_experiment=True,
    )

    frozen, lock_info = _sources_frozen(args)
    if not frozen:
        raise blocked(
            "sources.lock.json is not frozen; PRD 6/8 forbid building from a moving branch",
            command="build",
            stage=args.stage,
            **lock_info,
        )

    build_fn = _require_capability("build", "build_stage")
    run_dir = store.dir_for(experiment_id) if experiment_id else Path(args.runs_root)
    result, dropped, captured = _call_capability(
        build_fn,
        stage=args.stage,
        milestone=args.stage,
        experiment_id=experiment_id,
        run_dir=run_dir.as_posix(),
        sources_lock=Path(args.sources_lock).as_posix(),
        actions_minutes=args.actions_minutes,
    )
    data = {
        "stage": args.stage,
        "experiment_id": experiment_id,
        "budget": report.to_dict(),
        "sources": lock_info,
        "result": _as_data(result),
        "dropped_keywords": dropped,
        "child_stdout_bytes": len(captured),
    }
    return _ok("build", data)


def cmd_fetch(args: argparse.Namespace) -> CommandOutcome:
    """Download and verify build artifacts for a GitHub run id (PRD 8.2)."""
    if not args.run_id:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "fetch requires --run-id",
            command="fetch",
        )
    budget = _tracker(args)
    report = budget.check("M0", actions_minutes=args.actions_minutes, new_experiment=False)
    frozen, lock_info = _sources_frozen(args)
    fetch_fn = _require_capability("fetch", "fetch_artifacts")
    result, dropped, captured = _call_capability(
        fetch_fn,
        run_id=args.run_id,
        destination=Path(args.artifacts_root).as_posix(),
        sources_lock=Path(args.sources_lock).as_posix(),
    )
    payload = _as_data(result)
    manifest = payload.get("manifest") if isinstance(payload, dict) else None
    target = manifest.get("target") if isinstance(manifest, dict) else None
    if target is not None and target != "tcg":
        raise blocked(
            "payload target is not tcg; PRD 8.3 forbids using an HVF payload on this host",
            command="fetch",
            run_id=args.run_id,
            target=target,
        )
    data = {
        "run_id": args.run_id,
        "budget": report.to_dict(),
        "sources_frozen": frozen,
        "result": payload,
        "dropped_keywords": dropped,
        "child_stdout_bytes": len(captured),
    }
    return _ok("fetch", data)


def cmd_start(args: argparse.Namespace) -> CommandOutcome:
    """Start QEMU with an explicit TCG profile (PRD FR02/FR08)."""
    if args.profile != "tcg":
        raise UniPadError(
            ErrorCode.ACCELERATOR_NOT_TCG,
            "only the tcg profile is supported on this Intel Windows host",
            command="start",
            profile=args.profile,
            expected="tcg",
        )
    # The launcher owns the TCG guard: constructing it refuses any non-TCG
    # accelerator from config/machine.json before a single argument is built.
    launcher = _launcher(args)
    configured = _machine_accel(args)
    if configured is not None and configured != "tcg":
        raise UniPadError(
            ErrorCode.ACCELERATOR_NOT_TCG,
            "config/machine.json requests a non-TCG accelerator",
            command="start",
            accel=configured,
        )
    if shutil.which("qemu-system-aarch64") is None:
        raise blocked(
            "qemu-system-aarch64 is not on PATH; cannot start the guest",
            command="start",
            expected="qemu-system-aarch64",
        )

    manifest_path = Path(args.artifact)
    if not manifest_path.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "artifact manifest not found",
            command="start",
            path=manifest_path.as_posix(),
        )
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "artifact manifest is not a JSON object",
            command="start",
            path=manifest_path.as_posix(),
        )
    if manifest.get("target") not in (None, "tcg"):
        raise blocked(
            "artifact manifest targets a non-TCG payload; PRD 8.3 forbids running it here",
            command="start",
            target=manifest.get("target"),
        )

    store = _store(args)
    experiment_id = args.experiment
    budget = _tracker(args)
    budget_report = budget.check("M1", hypothesis=args.hypothesis, new_experiment=False)
    run_dir = store.dir_for(experiment_id) if experiment_id else Path(args.runs_root)
    run_dir.mkdir(parents=True, exist_ok=True)

    images = _launch_images(args, manifest)
    missing_images = [
        name
        for name, path in images.items()
        if (name != "data_image" or path is not None) and (path is None or not path.is_file())
    ]
    if missing_images or images["firmware"] is None or images["system_image"] is None:
        raise UniPadError(
            ErrorCode.IMAGE_NOT_FOUND,
            "launch images are not present on this host; cannot start the guest",
            command="start",
            missing=missing_images,
            resolved={
                name: (path.as_posix() if path is not None else None)
                for name, path in images.items()
            },
        )

    serial_log = run_dir / "serial.log"
    argv = launcher.build_args(
        images["firmware"],
        images["system_image"],
        images["data_image"],
        run_dir,
        args.qmp_port,
        headless=args.headless,
        serial_log=serial_log,
    )
    launched = launcher.launch("qemu-system-aarch64", argv, workdir=run_dir)
    readiness = launcher.wait_for_qmp(args.qmp_timeout)

    record = launched.to_dict()
    record["profile"] = args.profile
    record["accel"] = "tcg"
    record["readiness"] = readiness.to_dict()
    record["manifest"] = manifest_path.as_posix()
    write_json(run_dir / "launch.json", record)
    if experiment_id:
        for name in ("qemu-args.txt", "serial.log"):
            if (run_dir / name).is_file():
                store.append_evidence(experiment_id, name)
        store.append_change(
            experiment_id,
            f"start: qemu pid={launched.pid} qmp_port={launched.qmp_port} "
            f"ready={readiness.ready}",
        )

    data = {
        "profile": args.profile,
        "accel": "tcg",
        "artifact": manifest_path.as_posix(),
        "experiment_id": experiment_id,
        "run_dir": run_dir.as_posix(),
        "images": {
            name: (path.as_posix() if path is not None else None)
            for name, path in images.items()
        },
        "qemu_args": list(argv),
        "budget": budget_report.to_dict(),
        "wait_strategy": (
            "evidence-driven: QMP greeting + capabilities + query-status, then serial "
            "progress; no fixed countdown (PRD FR08)"
        ),
        "launch": _as_data(launched),
        "readiness": _as_data(readiness),
    }
    if not launched.ok or not readiness.ready:
        return _failure(
            "start",
            launched.error_code or readiness.error_code or ErrorCode.QEMU_LAUNCH_FAILED,
            launched.error_message or readiness.error_message or "guest did not become ready",
            data=data,
        )
    return _ok("start", data)


def cmd_install(args: argparse.Namespace) -> CommandOutcome:
    """Import an IPA at runtime: precheck -> transfer -> install -> register (FR06).

    The result always reports package hash, bundle id, furthest stage reached,
    trust mode, error type and log location (PRD 9.1).  A successful file
    transfer alone never reports ``installed``.
    """
    ipa_path = Path(args.ipa)
    if not ipa_path.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "IPA file not found",
            command="install",
            path=ipa_path.as_posix(),
        )
    package_sha256 = _sha256_file(ipa_path)

    budget = _tracker(args)
    budget_report = budget.check(args.milestone, new_experiment=False)

    store = _store(args)
    experiment_id = args.experiment or f"exp-install-{package_sha256[:12]}"
    if not store.exists(experiment_id):
        store.create(
            experiment_id,
            args.milestone,
            f"runtime IPA import of {ipa_path.name} (sha256 {package_sha256[:16]}…)",
        )
    run_dir = store.dir_for(experiment_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "install.log"
    log_lines: list[str] = [
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} controller: install requested",
        f"  ipa={ipa_path.as_posix()}",
        f"  package_sha256={package_sha256}",
        f"  experiment_id={experiment_id}",
    ]

    def _persist_install_log() -> None:
        """Append the install log so every reported ``log_path`` exists.

        A blocked install still has to leave the evidence behind: the envelope
        names this file, so writing it must not depend on reaching the end of
        the chain (PRD 9.1).
        """
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(log_lines) + "\n")

    # 1. Package precheck (PRD 10): platform, arch, minimum OS, encryption.
    #    unipad.ipa.precheck never raises for a rejectable package, so a
    #    structurally broken IPA comes back as a fault list instead.
    precheck_fn = _require_capability("install", "ipa_precheck")
    report, precheck_dropped, precheck_stdout = _call_capability(
        precheck_fn, path=ipa_path.as_posix()
    )
    precheck = _as_data(report)
    log_lines.append(f"  precheck={json.dumps(precheck, sort_keys=True, ensure_ascii=False)}")
    if precheck_stdout:
        log_lines.append(f"  precheck_stdout_bytes={len(precheck_stdout)}")

    fault_codes = (
        [str(code) for code in report.fault_codes] if hasattr(report, "fault_codes") else []
    )
    if fault_codes:
        install = InstallResult(
            package_sha256=package_sha256,
            bundle_id=getattr(report, "bundle_id", None),
            stage="failed",
            error_code=fault_codes[0],
            error_message="precheck rejected: " + "; ".join(
                str(getattr(fault, "label", "") or getattr(fault, "code", ""))
                for fault in getattr(report, "faults", [])
            ),
            log_path=log_path.as_posix(),
        )
        return _finish_install(
            "install",
            install,
            experiment_id=experiment_id,
            run_dir=run_dir,
            log_path=log_path,
            log_lines=log_lines,
            precheck=precheck,
            dropped=precheck_dropped,
            budget=budget_report,
        )

    # 2. Guest channel: the transfer target must exist (PRD 10, FR06).
    channel_factory = _resolve_capability("guest_channel")
    if channel_factory is None:
        log_lines.append("  guest_channel=missing")
        _persist_install_log()
        raise blocked(
            "no guest channel available; cannot transfer the package into the guest",
            command="install",
            experiment_id=experiment_id,
            package_sha256=package_sha256,
            stage="checked",
            blocked_reason="guest_channel_missing",
            probed=_probed("guest_channel"),
            data={
                "package_sha256": package_sha256,
                "bundle_id": getattr(report, "bundle_id", None),
                "stage": "checked",
                "trust_mode": None,
                "error_code": ErrorCode.BLOCKED,
                "log_path": log_path.as_posix(),
            },
        )
    try:
        channel, channel_dropped, _channel_stdout = _call_capability(channel_factory)
    except Exception as exc:  # noqa: BLE001 - a dead channel is a blocked host, not a crash
        log_lines.append(f"  guest_channel={type(exc).__name__}: {exc}")
        _persist_install_log()
        raise blocked(
            "guest channel could not be established",
            command="install",
            experiment_id=experiment_id,
            package_sha256=package_sha256,
            stage="checked",
            blocked_reason="guest_channel_unavailable",
            error=f"{type(exc).__name__}: {exc}",
            data={
                "package_sha256": package_sha256,
                "bundle_id": getattr(report, "bundle_id", None),
                "stage": "checked",
                "trust_mode": None,
                "error_code": ErrorCode.BLOCKED,
                "log_path": log_path.as_posix(),
            },
        ) from exc

    # 3. Installer: transfer + install service + registration.  The precheck
    #    report is handed over so the package is not hashed and unzipped twice.
    installer_cls = _require_capability("install", "ipa_install")
    try:
        installer = installer_cls(
            channel=channel,
            run_id=experiment_id,
            run_dir=run_dir,
        )
    except UniPadError as exc:
        log_lines.append(f"  installer={exc.code}: {exc.message}")
        _persist_install_log()
        raise blocked(
            exc.message,
            command="install",
            experiment_id=experiment_id,
            package_sha256=package_sha256,
            stage="checked",
            blocked_reason=exc.details.get("reason") or exc.code,
            data={
                "package_sha256": package_sha256,
                "bundle_id": getattr(report, "bundle_id", None),
                "stage": "checked",
                "trust_mode": None,
                "error_code": exc.code,
                "log_path": log_path.as_posix(),
            },
        ) from exc

    try:
        with _capture_stdout() as buffer:
            install = installer.install(ipa_path, precheck=report)
        install_stdout = buffer.getvalue()
    except UniPadError as exc:
        log_lines.append(f"  installer={exc.code}: {exc.message}")
        install = InstallResult(
            package_sha256=package_sha256,
            bundle_id=getattr(report, "bundle_id", None),
            stage="failed",
            error_code=exc.code,
            error_message=exc.message,
            log_path=log_path.as_posix(),
        )
        return _finish_install(
            "install",
            install,
            experiment_id=experiment_id,
            run_dir=run_dir,
            log_path=log_path,
            log_lines=log_lines,
            precheck=precheck,
            dropped=sorted({*precheck_dropped, *channel_dropped}),
            budget=budget_report,
            prior_install_check="performed_by_controller",
        )
    log_lines.append(f"  installer_stdout_bytes={len(install_stdout)}")
    install = _coerce_install_result(install, package_sha256=package_sha256)
    if not install.bundle_id:
        install.bundle_id = getattr(report, "bundle_id", None)
    if not install.log_path:
        install.log_path = log_path.as_posix()
    return _finish_install(
        "install",
        install,
        experiment_id=experiment_id,
        run_dir=run_dir,
        log_path=log_path,
        log_lines=log_lines,
        precheck=precheck,
        dropped=sorted({*precheck_dropped, *channel_dropped}),
        budget=budget_report,
        prior_install_check="performed_by_controller",
    )


def _coerce_install_result(raw: Any, *, package_sha256: str) -> InstallResult:
    """Normalise an installer return value into :class:`InstallResult`.

    An unrecognised return value is a blocked outcome: the controller never
    synthesises an install stage it did not observe (PRD 9.1).
    """
    if isinstance(raw, InstallResult):
        if not raw.package_sha256:
            raw.package_sha256 = package_sha256
        return raw
    if isinstance(raw, dict):
        return InstallResult(
            package_sha256=str(
                raw.get("package_sha256") or raw.get("sha256") or raw.get("hash") or package_sha256
            ),
            bundle_id=raw.get("bundle_id"),
            stage=str(raw.get("stage") or "received"),
            trust_mode=raw.get("trust_mode"),
            error_code=raw.get("error_code") or raw.get("error"),
            error_message=raw.get("error_message"),
            log_path=raw.get("log_path"),
        )
    raise blocked(
        "installer returned no structured result; refusing to report an install stage",
        command="install",
        returned_type=type(raw).__name__,
    )


def _finish_install(
    command: str,
    install: InstallResult,
    *,
    experiment_id: str,
    run_dir: Path,
    log_path: Path,
    log_lines: list[str],
    precheck: Any,
    dropped: list[str],
    budget: Any,
    prior_install_check: str = "not_performed",
) -> CommandOutcome:
    """Persist the install log/result and build the PRD 9.1 envelope."""
    log_lines.append(
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} controller: "
        f"stage={install.stage} error_code={install.error_code} bundle_id={install.bundle_id}"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(log_lines) + "\n")
    write_json(run_dir / "install.json", install.to_dict())

    data = {
        "experiment_id": experiment_id,
        "run_dir": run_dir.as_posix(),
        "package_sha256": install.package_sha256,
        "bundle_id": install.bundle_id,
        "stage": install.stage,
        "trust_mode": install.trust_mode,
        "error_code": install.error_code,
        "error_message": install.error_message,
        "log_path": install.log_path or log_path.as_posix(),
        "precheck": precheck,
        "prior_install_check": prior_install_check,
        "dropped_keywords": dropped,
        "budget": budget.to_dict() if hasattr(budget, "to_dict") else budget,
    }
    if install.ok:
        return _ok(command, data)
    return _failure(
        command,
        install.error_code or ErrorCode.VERIFICATION_FAILED,
        install.error_message or f"install stopped at stage {install.stage}",
        data=data,
    )


def cmd_verify(args: argparse.Namespace) -> CommandOutcome:
    """Run the fixed-condition verifier for a suite or milestone (PRD 7.4)."""
    verifier = Verifier()
    if args.suite not in verifier.known_gates():
        raise UniPadError(
            ErrorCode.UNSUPPORTED_COMMAND,
            "unknown verification suite",
            command="verify",
            suite=args.suite,
            known_gates=list(verifier.known_gates()),
        )
    store = _store(args)
    run_id = args.run or _latest_run(store)
    if not run_id:
        raise blocked(
            "no experiment run found to verify",
            command="verify",
            runs_root=Path(args.runs_root).as_posix(),
        )
    run_dir = store.dir_for(run_id)
    record = store.load(run_id)
    evidence = _evidence_from_run_dir(run_dir)
    facts = load_facts(run_dir / "install.json", run_dir / "persistence.json")
    outcome = verifier.evaluate_detailed(
        args.suite,
        record.milestone,
        evidence,
        run_dir=run_dir,
        facts=facts,
    )
    store.append_evidence(run_id, "verification.json")

    data = {
        "run": run_id,
        "suite": args.suite,
        "milestone": record.milestone,
        "gate": outcome.result.to_dict(),
        "code": outcome.code,
        "missing": outcome.missing,
        "conditions": outcome.conditions,
        "verification_path": (
            outcome.verification_path.as_posix() if outcome.verification_path else None
        ),
        "evidence": evidence.to_dict(),
        "facts_present": sorted(facts),
        "experiment": {
            "experiment_id": record.experiment_id,
            "status": record.status,
            "hypothesis": record.hypothesis,
        },
    }
    if outcome.result.passed:
        return _ok("verify", data)
    return _failure(
        "verify",
        outcome.code or ErrorCode.VERIFICATION_FAILED,
        "gate did not pass",
        data=data,
    )


def cmd_stop(args: argparse.Namespace) -> CommandOutcome:
    """Stop the guest recorded by ``start`` without faking a countdown (FR08).

    ``unipad.host.qemu.QemuLauncher`` owns the child process object, so the
    graceful QMP ``quit`` -> ``terminate`` -> ``kill`` sequence can only be
    driven by the process that launched it.  A *second* CLI invocation can
    therefore prove two things and only two: the recorded pid is gone (the
    guest is not running), or it is alive and this controller does not own it.
    The second case is reported as ``blocked`` instead of killing a pid the
    host package never handed to us.
    """
    mode = "force" if args.force else "graceful"
    store = _store(args)
    run_id = args.run or _latest_run(store)
    if not run_id:
        raise blocked(
            "no experiment run found; nothing records a guest to stop",
            command="stop",
            runs_root=Path(args.runs_root).as_posix(),
        )
    run_dir = store.dir_for(run_id)
    record_path = run_dir / "launch.json"
    record = _read_json(record_path)
    if not isinstance(record, dict) or not record.get("pid"):
        raise blocked(
            "no launch record for this run; refusing to stop a guest that was never started",
            command="stop",
            run=run_id,
            expected=record_path.as_posix(),
            blocked_reason="no_launch_record",
        )
    pid = int(record["pid"])
    alive = _pid_alive(pid)
    data = {
        "mode": mode,
        "run": run_id,
        "run_dir": run_dir.as_posix(),
        "pid": pid,
        "pid_alive": alive,
        "qmp_port": record.get("qmp_port"),
        "wait_strategy": (
            "evidence-driven: QMP quit, then process exit observed by the launcher; "
            "no fixed countdown (PRD FR08)"
        ),
    }
    if not alive:
        data["stopped_by_controller"] = False
        data["method"] = "already-exited"
        data["evidence"] = {"note": "the recorded qemu pid is no longer alive", "pid": pid}
        return _ok("stop", data)
    raise blocked(
        "a qemu process recorded for this run is still alive but is not owned by this "
        "controller invocation; the graceful QMP stop belongs to unipad.host.qemu",
        command="stop",
        run=run_id,
        pid=pid,
        blocked_reason="no_owned_instance",
        probed=_probed("stop"),
        data=data,
    )


def cmd_collect(args: argparse.Namespace) -> CommandOutcome:
    """Assemble the PRD 9.2 evidence bundle for a run (FR09).

    The bundle is built by :class:`unipad.host.logs.EvidenceBundle` from what
    is actually on disk.  A missing required item makes the run *unverified*:
    the command reports ``evidence_incomplete`` with the missing names and
    never claims a complete bundle (PRD 9.2).
    """
    if not args.run:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "collect requires --run <id>",
            command="collect",
        )
    store = _store(args)
    run_dir = store.dir_for(args.run)
    record = store.load(args.run)

    bundle = _evidence_bundle(run_dir, args.run)
    collected: list[str] = []
    notes: list[str] = []

    journal = run_dir / "experiment.json"
    if journal.is_file():
        bundle.set_experiment_json(journal)
        collected.append("experiment.json")
    for name in _EVIDENCE_FILES["host_inventory"]:
        if (run_dir / name).is_file():
            bundle.set_host_inventory(run_dir / name)
            collected.append(name)
            break
    for name in _EVIDENCE_FILES["artifact_manifest"]:
        if (run_dir / name).is_file():
            bundle.set_artifact_manifest(run_dir / name)
            collected.append(name)
            break
    for name in _EVIDENCE_FILES["qemu_args"]:
        target = run_dir / name
        if not target.is_file():
            continue
        argv = [line for line in target.read_text(encoding="utf-8", errors="replace").splitlines() if line]
        if argv:
            bundle.add_qemu_args(argv)
            collected.append(name)
        else:
            notes.append(f"{name} is empty; an empty command line is not evidence of a run")
        break
    # Serial and guest logs are separate evidence items (PRD 7.4: serial text
    # alone is not proof), so each gets its own loop.  copy=False because the
    # logs already live inside the run directory.
    for field_name, method in (("serial_log", "add_serial_log"), ("guest_log", "add_guest_log")):
        for name in _EVIDENCE_FILES[field_name]:
            target = run_dir / name
            if not target.is_file():
                continue
            getattr(bundle, method)(target, copy=False)
            collected.append(name)
            break
    qmp_log = run_dir / "qmp.log"
    if qmp_log.is_file():
        bundle.add_qmp_log(qmp_log.read_text(encoding="utf-8", errors="replace"))
        collected.append("qmp.log")
    for name in _EVIDENCE_FILES["gpu_bridge_log"]:
        target = run_dir / name
        if target.is_file():
            bundle.set_gpu_bridge_log(target)
            collected.append(name)
            break
    verification = _read_json(run_dir / "verification.json")
    if isinstance(verification, dict) and verification:
        bundle.set_verification(verification)
        collected.append("verification.json")
    screenshots_dir = run_dir / "screenshots"
    if screenshots_dir.is_dir():
        for shot in sorted(screenshots_dir.iterdir()):
            if not shot.is_file() or shot.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            try:
                bundle.add_screenshot(shot)
            except UniPadError as exc:
                notes.append(f"screenshot {shot.name}: {exc.code} {exc.message}")
            else:
                collected.append(f"screenshots/{shot.name}")
    for note in notes:
        bundle.note(note)

    evidence = bundle.build(strict=False)
    missing = list(bundle.missing)

    manifest = _bundle_manifest(run_dir, args.run)
    bundle_path = run_dir / "evidence-bundle.json"
    write_json(bundle_path, manifest)

    data = {
        "run": args.run,
        "experiment_id": record.experiment_id,
        "milestone": record.milestone,
        "bundle": (run_dir / "evidence.json").as_posix(),
        "manifest": bundle_path.as_posix(),
        "collected": collected,
        "notes": notes,
        "missing": missing,
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "evidence": evidence.to_dict(),
        "bundle_state": bundle.to_dict(),
    }
    store.append_evidence(args.run, "evidence.json")
    if missing:
        return _failure(
            "collect",
            ErrorCode.EVIDENCE_INCOMPLETE,
            "runtime evidence set is incomplete; the run stays unverified",
            data=data,
            missing=missing,
        )
    return _ok("collect", data)


def cmd_restore(args: argparse.Namespace) -> CommandOutcome:
    """Roll the machine image back to a verified checkpoint (PRD FR10).

    A checkpoint is a *directory* produced by
    :meth:`unipad.host.images.ImageManager.backup`: the image copies plus a
    ``manifest.json`` whose ``entries`` carry ``path``/``backup_path``/
    ``sha256``/``size_bytes``.  Every entry is re-hashed before anything is
    copied, and the restore target is always a fresh directory -- PRD 7.2
    forbids overwriting the user's working image in place.
    """
    if not args.checkpoint:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "restore requires --checkpoint <id>",
            command="restore",
        )
    artifacts_root = Path(args.artifacts_root)
    candidates = [
        artifacts_root / args.checkpoint,
        artifacts_root / "checkpoints" / args.checkpoint,
        artifacts_root / "backups" / args.checkpoint,
    ]
    checkpoint_dir = next((path for path in candidates if path.is_dir()), None)
    if checkpoint_dir is None:
        raise blocked(
            "checkpoint directory not found; refusing to restore an unverified version",
            command="restore",
            checkpoint=args.checkpoint,
            searched=[path.as_posix() for path in candidates],
        )
    manifest_path = checkpoint_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "checkpoint manifest is not a JSON object",
            command="restore",
            path=manifest_path.as_posix(),
        )

    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise blocked(
            "checkpoint manifest lists no images",
            command="restore",
            checkpoint=args.checkpoint,
            path=manifest_path.as_posix(),
        )

    verified: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise blocked(
                "checkpoint manifest entry is not an object",
                command="restore",
                checkpoint=args.checkpoint,
                entry_index=index,
                entry_type=type(entry).__name__,
            )
        backup = entry.get("backup_path") or entry.get("path")
        expected = entry.get("sha256")
        if not backup or not expected:
            raise blocked(
                "checkpoint manifest entry lacks a backup_path or sha256",
                command="restore",
                checkpoint=args.checkpoint,
                entry_index=index,
                entry=entry,
            )
        raw_backup = Path(str(backup))
        if raw_backup.is_absolute():
            backup_path = raw_backup
        else:
            # ImageManager.backup records whatever path it was handed, so a
            # relative backup_path may resolve against the checkpoint directory
            # or against the working directory.  Prefer the one that exists and
            # keep the first candidate for the error message otherwise.
            options = [checkpoint_dir / raw_backup, Path.cwd() / raw_backup, raw_backup]
            backup_path = next((option for option in options if option.is_file()), options[0])
        if not backup_path.is_file():
            raise UniPadError(
                ErrorCode.IMAGE_NOT_FOUND,
                "checkpoint image not found",
                command="restore",
                checkpoint=args.checkpoint,
                path=backup_path.as_posix(),
            )
        actual = _sha256_file(backup_path)
        if actual != str(expected).lower():
            raise UniPadError(
                ErrorCode.IMAGE_HASH_MISMATCH,
                "checkpoint image hash mismatch; refusing to restore",
                command="restore",
                checkpoint=args.checkpoint,
                path=backup_path.as_posix(),
                expected_sha256=str(expected),
                actual_sha256=actual,
            )
        verified.append(
            {
                "name": backup_path.name,
                "path": backup_path.as_posix(),
                "source": str(entry.get("path") or ""),
                "sha256": actual,
                "size_bytes": backup_path.stat().st_size,
            }
        )

    manager = _image_manager(args)
    target_dir = artifacts_root / "restored" / args.checkpoint
    result = manager.restore_checkpoint(checkpoint_dir, target_dir=target_dir)
    payload = _as_data(result)

    data = {
        "checkpoint": args.checkpoint,
        "checkpoint_dir": checkpoint_dir.as_posix(),
        "manifest": manifest_path.as_posix(),
        "verified_artifacts": verified,
        "target_dir": target_dir.as_posix(),
        "result": payload,
    }
    store = _store(args)
    run_id = args.run or _latest_run(store)
    if run_id and store.exists(run_id):
        restore_path = store.dir_for(run_id) / "restore.json"
        write_json(restore_path, data)
        store.append_evidence(run_id, "restore.json")
        store.append_change(
            run_id,
            f"restore: checkpoint {args.checkpoint} -> {target_dir.as_posix()} "
            f"({len(verified)} image(s) hash-verified)",
        )
        data["run"] = run_id
    if payload.get("verified") is not True:
        return _failure(
            "restore",
            ErrorCode.RESTORE_FAILED,
            "restore did not verify every image against the checkpoint manifest",
            data=data,
        )
    return _ok("restore", data)


_COMMAND_TABLE: dict[str, Callable[[argparse.Namespace], CommandOutcome]] = {
    "doctor": cmd_doctor,
    "build": cmd_build,
    "fetch": cmd_fetch,
    "start": cmd_start,
    "install": cmd_install,
    "verify": cmd_verify,
    "stop": cmd_stop,
    "collect": cmd_collect,
    "restore": cmd_restore,
}


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
class _HelpRequested(Exception):
    """Raised instead of ``SystemExit`` when ``--help`` is used."""

    def __init__(self, text: str) -> None:
        super().__init__("help requested")
        self.text = text


class _JsonArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that reports errors as structured UniPadErrors.

    ``print_help`` is suppressed: argparse writes it straight to stdout before
    raising, which would put a second document on the command's stdout.  The
    help text travels inside the JSON envelope instead (PRD 9.1: exactly one
    JSON object per command).
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            message,
            usage=self.format_usage().strip(),
        )

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        if message:
            raise UniPadError(ErrorCode.CONFIG_INVALID, message.strip())
        raise _HelpRequested(self.format_help())

    def print_help(self, file: Any = None) -> None:  # type: ignore[override]
        """Never write help to stdout; :meth:`exit` carries it in the envelope."""
        return None

    def print_usage(self, file: Any = None) -> None:  # type: ignore[override]
        """Never write usage to stdout; it is part of the error details."""
        return None


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse CLI (PRD 9.1 commands plus controller-side options)."""
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS keeps the subparser from clobbering a value given before the
    # subcommand, so both `unipad --runs-root X doctor` and
    # `unipad doctor --runs-root X` work.
    common.add_argument("--runs-root", default=argparse.SUPPRESS)
    common.add_argument("--budget-config", default=argparse.SUPPRESS)
    common.add_argument("--budget-usage", default=argparse.SUPPRESS)
    common.add_argument("--sources-lock", default=argparse.SUPPRESS)
    common.add_argument("--machine-config", default=argparse.SUPPRESS)
    common.add_argument("--artifacts-root", default=argparse.SUPPRESS)

    parser = _JsonArgumentParser(
        prog="unipad",
        description="UniPad controller: experiment state machine, budget, verifier and CLI (PRD 9.1).",
    )
    parser.add_argument("--runs-root", default="runs", help="experiment/run root (default: runs)")
    parser.add_argument(
        "--budget-config", default="config/budget.json", help="budget limits (PRD 7.5)"
    )
    parser.add_argument(
        "--budget-usage",
        default=None,
        help="cumulative usage file (default: <runs-root>/budget-usage.json)",
    )
    parser.add_argument(
        "--sources-lock", default="config/sources.lock.json", help="pinned source versions"
    )
    parser.add_argument("--machine-config", default="config/machine.json", help="machine definition")
    parser.add_argument(
        "--artifacts-root", default="artifacts", help="local build artifacts and checkpoints"
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor = sub.add_parser(
        "doctor", parents=[common], help="inventory host, toolchain and disks (WP00 re-check)"
    )
    doctor.set_defaults(handler=cmd_doctor)

    build = sub.add_parser("build", parents=[common], help="build one milestone stage")
    build.add_argument("--stage", default="M4", help="milestone stage to build (default: M4)")
    build.add_argument("--experiment", default=None, help="experiment id to attribute the build to")
    build.add_argument("--hypothesis", default=None, help="hypothesis when creating the record")
    build.add_argument(
        "--actions-minutes",
        type=float,
        default=0.0,
        help="paid GitHub Actions minutes this build will consume (PRD 7.5/8.4)",
    )
    build.set_defaults(handler=cmd_build)

    fetch = sub.add_parser("fetch", parents=[common], help="download and verify build artifacts")
    fetch.add_argument("--run-id", default=None, help="GitHub Actions run id")
    fetch.add_argument(
        "--actions-minutes",
        type=float,
        default=0.0,
        help="paid Actions minutes accounted for this fetch",
    )
    fetch.set_defaults(handler=cmd_fetch)

    start = sub.add_parser("start", parents=[common], help="start QEMU with an explicit TCG profile")
    start.add_argument("--profile", default="tcg", help="accelerator profile; only tcg is supported")
    start.add_argument("--artifact", required=True, help="artifact manifest to run")
    start.add_argument("--experiment", default=None, help="experiment id for this run")
    start.add_argument("--hypothesis", default=None, help="hypothesis for budget attribution")
    start.add_argument(
        "--qmp-port", type=int, default=4444, help="TCP port for the QMP control channel (loopback)"
    )
    start.add_argument(
        "--qmp-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for the QMP greeting before reporting not-ready",
    )
    start.add_argument(
        "--headless", action="store_true", help="add -display none (no guest window)"
    )
    start.set_defaults(handler=cmd_start)

    install = sub.add_parser("install", parents=[common], help="import an IPA at runtime (FR06)")
    install.add_argument("ipa", help="path to the .ipa file")
    install.add_argument("--experiment", default=None, help="experiment id to attribute the install to")
    install.add_argument("--milestone", default="M10", help="milestone to charge the install to")
    install.set_defaults(handler=cmd_install)

    verify = sub.add_parser("verify", parents=[common], help="run the fixed-condition verifier")
    verify.add_argument("--suite", default="ipa-mvp", help="gate id or milestone (default: ipa-mvp)")
    verify.add_argument("--run", default=None, help="run id to verify (default: latest)")
    verify.set_defaults(handler=cmd_verify)

    stop = sub.add_parser("stop", parents=[common], help="stop the running guest")
    stop.add_argument("--graceful", action="store_true", help="ask the guest to shut down cleanly")
    stop.add_argument("--force", action="store_true", help="terminate immediately")
    stop.add_argument("--run", default=None, help="run id to stop (default: latest)")
    stop.set_defaults(handler=cmd_stop)

    collect = sub.add_parser("collect", parents=[common], help="assemble the evidence bundle")
    collect.add_argument("--run", required=True, help="run id to collect")
    collect.set_defaults(handler=cmd_collect)

    restore = sub.add_parser("restore", parents=[common], help="roll back to a verified checkpoint")
    restore.add_argument("--checkpoint", required=True, help="checkpoint id under the artifacts root")
    restore.add_argument("--run", default=None, help="run id to record the rollback in")
    restore.set_defaults(handler=cmd_restore)

    return parser


def _command_token(argv: Sequence[str]) -> str | None:
    """Return the first non-option token (the subcommand), or ``None``."""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token.startswith("-") and token != "-":
            if "=" not in token and token in _VALUE_OPTIONS:
                index += 2
                continue
            index += 1
            continue
        return token
    return None


def main(argv: Sequence[str] | None = None, stream: Any = None) -> int:
    """Run the CLI and return a fixed :class:`unipad.errors.ExitCode`.

    Always prints exactly one JSON object.  Unknown commands map to
    ``unsupported_command`` (exit 2); missing material, credentials or budget
    map to ``blocked`` (exit 3).
    """
    argv_list = list(sys.argv[1:] if argv is None else argv)
    command = _command_token(argv_list)
    if command is not None and command not in COMMANDS:
        error = UniPadError(
            ErrorCode.UNSUPPORTED_COMMAND,
            "unknown command",
            command=command,
            known_commands=list(COMMANDS),
        )
        return _emit(_from_error(command, error), stream)

    parser = build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv_list)
        if getattr(args, "command", None) is None:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "no command given",
                known_commands=list(COMMANDS),
            )
        outcome = args.handler(args)
    except _HelpRequested as exc:
        name = getattr(args, "command", None) or command or "<help>"
        outcome = _ok(str(name), {"help": exc.text})
    except UniPadError as exc:
        name = getattr(args, "command", None) or command or "<none>"
        outcome = _from_error(str(name), exc)
    except KeyboardInterrupt:
        outcome = _failure("<interrupted>", INTERNAL_ERROR_CODE, "interrupted")
    except Exception as exc:  # noqa: BLE001 - never leak a traceback instead of JSON
        name = getattr(args, "command", None) or command or "<none>"
        outcome = _failure(
            str(name),
            INTERNAL_ERROR_CODE,
            f"unexpected controller error: {type(exc).__name__}: {exc}",
            exception_type=type(exc).__name__,
        )
    return _emit(outcome, stream)


if __name__ == "__main__":  # pragma: no cover - console entry
    raise SystemExit(main())
