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

Plus the install-lifecycle surface PRD 3.2 P1 (3) asks for
(应用卸载、更新、安装历史和清晰的错误分类).  Those three are *not* a rewrite of
the PRD 9.1 list -- they read the same append-only ledger
:class:`unipad.ipa.lifecycle.InstallLedger` that ``install`` writes, which is
why they live in a second table (:data:`_LIFECYCLE_COMMAND_TABLE`) instead of
being folded into :data:`COMMANDS`::

    unipad history  --bundle <id> | --all
    unipad uninstall <bundle-id>
    unipad update   <path-to-ipa>

And the compatibility-matrix surface PRD 3.2 P1 (2) asks for
(更多自有或获授权的 IPA 兼容性测试).  One command, two actions, over the append-only
:class:`unipad.ipa.matrix.CompatibilityMatrix`::

    unipad matrix [report] [--bundle <id> | --key <version> <build> <config>]
    unipad matrix record <path-to-ipa> --package-version <v> --system-build <b>
        --run-config <c> --authorisation <a> --trust-category <t>

``matrix`` lives in a third table (:data:`_MATRIX_COMMAND_TABLE`) for the same
reason the lifecycle commands do: the matrix is a store with its own contract,
and "the controller implements exactly the PRD 9.1 list" has to stay a checkable
statement rather than a convention someone has to remember.

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
from typing import Any, Callable, Iterator, Mapping, Sequence

from .. import __version__
from ..acceptance.record import redact
from ..errors import ErrorCode, ExitCode, UniPadError, blocked
from ..host.paths import safe_location, scrub_account
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

#: Commands PRD 3.2 P1 (3) adds on top of the PRD 9.1 list.  Kept apart on
#: purpose: the nine are the documented controller contract, and a lifecycle
#: command that reads a ledger nobody has written yet is a different kind of
#: surface from a command that drives the machine.
LIFECYCLE_COMMANDS: tuple[str, ...] = (
    "history",
    "uninstall",
    "update",
)

#: Commands PRD 3.2 P1 (2) adds on top of the PRD 9.1 list, for the same reason
#: and in the same shape: the compatibility matrix is a store with its own
#: contract, and folding its command into either list above would make "the
#: controller implements exactly the PRD 9.1 list" untrue.
MATRIX_COMMANDS: tuple[str, ...] = ("matrix",)

#: The two actions of :data:`MATRIX_COMMANDS`.  ``report`` reads and is the
#: default; ``record`` appends.  Closed, like :data:`TRANSPORTS`: an action this
#: controller does not implement is a usage error, never a guess.
MATRIX_ACTIONS: tuple[str, ...] = ("report", "record")

#: Everything ``main`` accepts.  An unknown command is exit 2 against *this*
#: set, so adding a lifecycle or matrix command cannot silently change what
#: "unknown" means for the nine.
SUPPORTED_COMMANDS: tuple[str, ...] = COMMANDS + LIFECYCLE_COMMANDS + MATRIX_COMMANDS

# --- transfer stage: which route the package bytes are published on (ADR-0008)
#
# ``channel`` is today's route and stays the default: the bytes move on the
# guest channel, which is the only route with an implementation on both ends on
# this host.  ``https`` publishes the package through the self-controlled
# :class:`unipad.host.httpsvc.PackageServer` for exactly the transfer window.
#
# The default must stay ``channel`` and the reason is not laziness: this host
# has no guest-side package fetcher, so an install that defaulted to ``https``
# would take a route nothing has ever exercised and would surface as "the
# package is bad" instead of "the transport is wrong".

#: Package bytes keep moving on the guest channel (the default).
TRANSPORT_CHANNEL = "channel"

#: The package is published by the self-controlled HTTPS service.
TRANSPORT_HTTPS = "https"

#: Closed vocabulary.  Anything else is a usage error, never a guess.
TRANSPORTS: tuple[str, ...] = (TRANSPORT_CHANNEL, TRANSPORT_HTTPS)

#: What an operator gets who says nothing.
DEFAULT_TRANSPORT = TRANSPORT_CHANNEL

#: ``--transport`` help text, shared by ``install`` and ``update`` so the two
#: cannot drift into promising different defaults.
TRANSPORT_HELP = (
    "route the package bytes are published on: "
    f"{'|'.join(TRANSPORTS)} (default: {DEFAULT_TRANSPORT})"
)


def _package_service_port() -> int:
    """The port the transfer stage publishes on (ADR-0008).

    Read out of :mod:`unipad.host.httpsvc` rather than spelled here: the port,
    its validator and the URL builders all live in the module that owns the
    service, and a second copy of the number would be a second thing to keep
    true.  It is a *fixed* port, not an ephemeral one -- the guest is told this
    port, and a silent move to another would leave the recorded URL describing
    a service that is not the one that listened.
    """
    httpsvc = _httpsvc()
    if httpsvc is None:  # pragma: no cover - the module ships with this package
        raise blocked("the self-controlled package service is not installed")
    return int(httpsvc.DEFAULT_GUEST_PORT)

#: Where the append-only install ledger lives, relative to the runs root.
#:
#: Under the runs root, not the artifacts root: the artifacts root holds
#: *immutable* build output and checkpoints, while this file is appended to on
#: every install, update and uninstall.  Under the root rather than inside a
#: run directory because an install is a property of the *host*, not of one
#: experiment -- ``unipad install --experiment A`` and ``--experiment B`` must
#: land in one ledger, or ``installed()`` would answer per-experiment and be
#: wrong about the device.
#:
#: Overridable per command with ``--ledger``; the default is what an operator
#: who never passes the flag gets.
LEDGER_RELATIVE_PATH: Path = Path("lifecycle") / "install-ledger.jsonl"

#: ``--ledger`` help text, shared by every command that opens the ledger.  One
#: string per direction (read / write) so the four subparsers cannot drift into
#: promising different defaults.
LEDGER_HELP_READ: str = (
    f"install ledger to read (default: <runs-root>/{LEDGER_RELATIVE_PATH.as_posix()})"
)
LEDGER_HELP_WRITE: str = (
    f"install ledger to record in (default: <runs-root>/{LEDGER_RELATIVE_PATH.as_posix()})"
)

#: Where the append-only compatibility matrix lives, relative to the runs root.
#:
#: Beside the ledger, under ``lifecycle/``, for the same reason the ledger is
#: there: an observation is a property of the *host*, not of one experiment.
#: The two files are separate stores with separate contracts -- the ledger
#: records what this host did to a bundle, the matrix records what a
#: (package version, system build, run configuration) triple was observed to
#: do -- and nothing here merges them.
#:
#: Overridable with ``--matrix`` on the one command that reads it.
MATRIX_RELATIVE_PATH: Path = Path("lifecycle") / "compatibility-matrix.jsonl"

#: ``--matrix`` help text.  One string per direction so the subparsers cannot
#: drift into promising different defaults, the same split
#: :data:`LEDGER_HELP_READ`/:data:`LEDGER_HELP_WRITE` uses.  ``doctor`` only
#: reads; ``matrix`` reads and appends, so its string says both.
MATRIX_HELP_READ: str = (
    f"compatibility matrix to read (default: <runs-root>/{MATRIX_RELATIVE_PATH.as_posix()})"
)
MATRIX_HELP: str = (
    "compatibility matrix to read or append to "
    f"(default: <runs-root>/{MATRIX_RELATIVE_PATH.as_posix()})"
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
        "--matrix",
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

#: The module that owns the compatibility matrix, its closed vocabularies and
#: its two dataclasses.  Spelled once: the capability table, the module-level
#: probe below and the vocabularies' own error messages all have to name the
#: same thing, and a second literal would be a second thing to keep true.
_MATRIX_MODULE: str = "unipad.ipa.matrix"

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
    # guest channel factory is picked up without controller changes.  That is a
    # claim about the two lists agreeing, so it is kept true: ``GuestClient``
    # is the fifth entry there and the only one ``unipad.guest`` exports, so a
    # list that stopped short of it resolved to ``None`` and made every install
    # report ``guest_channel_missing`` about a host that ships the channel.
    "guest_channel": (
        ("unipad.guest", "create_install_channel"),
        ("unipad.guest", "default_channel"),
        ("unipad.guest", "connect"),
        ("unipad.guest", "GuestChannel"),
        ("unipad.guest", "GuestClient"),
    ),
    "collect_evidence": (("unipad.host.logs", "EvidenceBundle"),),
    "restore_image": (("unipad.host.images", "ImageManager"),),
    "launch": (("unipad.host.qemu", "QemuLauncher"),),
    "stop": (("unipad.host.qemu", "QemuLauncher"),),
    "status": (("unipad.host.qemu", "QemuLauncher"),),
    # PRD 3.2 P1 (3): the append-only install ledger.  A class, like the four
    # above: the command adapters construct it with the resolved path.
    "install_ledger": (("unipad.ipa.lifecycle", "InstallLedger"),),
    # PRD 3.2 P1 (2): the append-only compatibility matrix.  Read by ``doctor``
    # and by ``unipad matrix``, and written by ``unipad matrix record`` -- the
    # store now has a writer in the product, which is what makes its skipped
    # lines worth reporting anywhere at all.
    "compatibility_matrix": ((_MATRIX_MODULE, "CompatibilityMatrix"),),
    # PRD 3.2 P1 (2): the two readers ``matrix record`` derives its entry from.
    # Both are the same entry points the install chain's precheck stage uses, so
    # an observation the matrix records cannot disagree with the one the install
    # reported.  ``ipa_open`` is needed separately because the matrix reads the
    # §5.1 record (signing mode, embedded frameworks, requested permissions) off
    # the :class:`~unipad.ipa.package.PackageInfo`, which the precheck report does
    # not carry.
    "ipa_open": (("unipad.ipa.package", "open_ipa"),),
    "ipa_precheck_package": (("unipad.ipa.precheck", "run_precheck_package"),),
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

    def to_json(self, redact: Callable[[Any], Any] | None = None) -> str:
        """Return the envelope as one indented JSON object.

        ``redact`` walks the whole document before it is serialised.  It is a
        parameter rather than something ``_emit`` does to the string afterwards
        because a string-level pass cannot tell a path inside a JSON string from
        the JSON syntax around it, and would have to re-parse to put it back.
        """
        document = self.to_dict()
        if redact is not None:
            document = redact(document)
        return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)


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


def _redact_payload(value: Any) -> Any:
    """Walk ``value`` and redact every string in it (PRD 9.2).

    Applied once, here, at the single point every envelope is serialised -- not
    at each of the call sites that happen to carry a path.  The reason is
    measured: when this was added, SIX commands were leaking the absolute path
    (and with it the Windows account name, because the profile is
    ``C:\\Users\\<name>\\...``) into their envelopes -- ``doctor`` through
    ``usage.usage_file``, ``verify`` and ``stop`` through
    ``error.details.runs_root``, ``collect`` through ``error.details.path``,
    ``restore`` through ``error.details.searched`` and ``history`` through
    ``data.ledger``.  Patching six call sites fixes six call sites; the seventh
    ships.  Patching the choke point fixes the class, and the test that walks
    every command's envelope becomes the regression net for the next one.

    What is redacted is the *account* and the *drive*, never the whole path:
    :func:`unipad.acceptance.record.redact` turns ``C:/Users/<name>/AppData/...``
    into ``<redacted:user_path>/AppData/...``, so an operator can still tell two
    candidate files apart -- which is why ``safe_location`` keeps a final
    component everywhere else in this codebase.  The path redaction alone is not
    sufficient and that is measured, not assumed: an account name that appears a
    second time further along the same path survives it, and on this host those
    exist (the pytest fixture root is ``.../pytest-of-<name>/pytest-N/test_x0/``).
    :func:`unipad.host.paths.scrub_account` therefore runs after it and removes
    the name wherever it appears.
    """
    if isinstance(value, str):
        return scrub_account(redact(value))
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_payload(item) for item in value]
    return value


def _emit(outcome: CommandOutcome, stream: Any = None) -> int:
    """Print exactly one JSON object and return the process exit code."""
    handle = stream if stream is not None else sys.stdout
    handle.write(outcome.to_json(redact=_redact_payload) + "\n")
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


def _httpsvc() -> Any | None:
    """Import :mod:`unipad.host.httpsvc`, or ``None`` when it is not installed.

    The controller imports every host capability lazily (see the
    "Capability probing" note above); the package service is no exception, even
    though it ships in this package -- a controller that hard-imports it could
    not report a missing capability as a structured refusal.
    """
    return _import_module("unipad.host.httpsvc")


def _machine_config(args: argparse.Namespace) -> Any:
    """Load ``config/machine.json`` into a :class:`unipad.models.MachineConfig`.

    Raises:
        UniPadError: ``config_invalid`` when the file is missing, is not a
            JSON object, or names a key this controller does not model.

    The last case is the one that matters.  ``from_dict`` drops keys it does
    not model -- correct for a caller holding a superset dict, wrong for a
    caller reading the shipped config, where a dropped key is a typo
    (``cpu_modle``), a key from a newer schema, or a feature the operator
    believes is configured and is not.  In each of those the file describes one
    machine while the launch builds another, so the question
    :meth:`MachineConfig.unknown_keys` exists to ask is asked here and a
    non-empty answer refuses the config.  Only key *names* are reported: a
    config value may be anything, and PRD 9.2 keeps values out of output.
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
    unknown = MachineConfig.unknown_keys(raw)
    if unknown:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "machine config names keys this controller does not model: "
            + ", ".join(unknown),
            command="start",
            path=path.as_posix(),
            unknown_keys=list(unknown),
            hint="fix the spelling, or move the value under a modelled key; "
            "a dropped key would silently launch a different machine",
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


def _ledger_path(args: argparse.Namespace) -> Path:
    """Resolve the install-ledger path for this invocation.

    ``--ledger`` wins; otherwise the ledger sits under the runs root at
    :data:`LEDGER_RELATIVE_PATH`.  A missing file is *not* an error -- an empty
    history is a true answer, and :class:`InstallLedger` treats an absent file
    as an empty ledger rather than inventing one.
    """
    override = getattr(args, "ledger", None)
    if override:
        return Path(str(override))
    return Path(args.runs_root) / LEDGER_RELATIVE_PATH


def _ledger(args: argparse.Namespace) -> Any:
    """Open the append-only install ledger.

    Resolved through the capability table rather than imported directly, for
    the same reason every other sibling package is: a ``unipad.ipa`` that
    cannot be imported must surface as a structured ``blocked`` naming the
    probe, not as an import-time crash in the controller.
    """
    path = _ledger_path(args)
    # A path that exists but is not a file would otherwise read as an *empty*
    # ledger -- ``InstallLedger`` tests ``is_file()`` -- which would answer
    # "nothing is installed" about a ledger that is simply unreadable.  That is
    # the one wrong answer this surface must never give, so it is refused here
    # rather than left to surface as an OSError halfway through an append.
    if path.exists() and not path.is_file():
        raise blocked(
            "the install ledger path is not a file; refusing to read or append to it",
            command=str(getattr(args, "command", None) or "lifecycle"),
            blocked_reason="ledger_not_a_file",
            # PRD 9.2, same as the matrix opener below: the final component only.
            ledger=safe_location(path),
        )
    ledger_cls = _require_capability(
        str(getattr(args, "command", None) or "lifecycle"), "install_ledger"
    )
    return ledger_cls(path)


def _matrix_path(args: argparse.Namespace) -> Path:
    """Resolve the compatibility-matrix path for this invocation.

    ``--matrix`` wins; otherwise the matrix sits under the runs root at
    :data:`MATRIX_RELATIVE_PATH`.  A missing file is *not* an error -- a matrix
    nothing has written yet is a true answer, and
    :class:`unipad.ipa.matrix.CompatibilityMatrix` treats an absent file as an
    empty matrix rather than inventing one.
    """
    override = getattr(args, "matrix", None)
    if override:
        return Path(str(override))
    return Path(args.runs_root) / MATRIX_RELATIVE_PATH


def _matrix(args: argparse.Namespace) -> Any:
    """Open the append-only compatibility matrix.

    Resolved through the capability table, like the ledger: a ``unipad.ipa``
    that cannot be imported must surface as a structured refusal naming the
    probe, not as an import-time crash in the controller.

    ``doctor`` and ``unipad matrix`` both call this.  The split of skipped lines
    by cause is the reason it is worth opening the store in two places rather
    than one: a line the authorisation gate refused is somebody recording
    something they were not allowed to, and a command that only counted the total
    would let that hide inside a disk error.

    A path that exists but is not a file is refused rather than read as empty,
    for the same reason the ledger is: ``CompatibilityMatrix`` tests
    ``is_file()``, so a directory would otherwise answer "nothing is recorded"
    about a matrix that is simply unreadable.
    """
    path = _matrix_path(args)
    if path.exists() and not path.is_file():
        raise blocked(
            "the compatibility matrix path is not a file; refusing to read or append to it",
            command=str(getattr(args, "command", None) or "matrix"),
            blocked_reason="matrix_not_a_file",
            # PRD 9.2: the final component only.  ``path.as_posix()`` here put the
            # whole absolute path -- and with it the Windows account name, because
            # the profile is C:\Users\<name>\... -- into the blocked envelope and
            # therefore onto stdout, on the one command whose entire purpose is to
            # be run by an operator.  Every sibling store redacts the same way.
            matrix=safe_location(path),
        )
    matrix_cls = _require_capability(
        str(getattr(args, "command", None) or "matrix"), "compatibility_matrix"
    )
    return matrix_cls(path)


def _matrix_location(args: argparse.Namespace, path: Path) -> str:
    """Return ``path`` as the envelope may name it (PRD 9.2).

    Relative to the runs root when it is under it, and only the final component
    when the operator pointed ``--matrix`` somewhere outside it.  The absolute
    path is never emitted: on this host it embeds the Windows username, and an
    observation envelope is exactly the kind of thing that gets pasted into an
    issue.  A relative location still tells two candidate files apart, which is
    the only thing a location has to do here.
    """
    try:
        return path.relative_to(Path(args.runs_root)).as_posix()
    except ValueError:
        return safe_location(path)


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
            # ``sys.executable`` is an absolute path, and on this host that
            # means the profile directory -- i.e. the Windows username.  This
            # envelope is part of the PRD 9.2 minimum runtime evidence set, so
            # the account name would reach every evidence bundle the project
            # produces.  The interpreter is named by its final component, the
            # same way ``unipad.host.inventory``'s toolchain block already
            # does, and the note records what was withheld and why.
            "executable": safe_location(sys.executable),
            "executable_note": (
                "python.executable is the interpreter's final path component; "
                "the absolute path embeds the Windows username and PRD 9.2 "
                "keeps it out of the evidence set"
            ),
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

    data["matrix"] = _matrix_block(args)
    matrix_unreadable = data["matrix"].get("unreadable_lines") or 0
    if matrix_unreadable:
        # A skipped line is not a cosmetic problem: the matrix is the only place
        # a compatibility claim can live, and a line the loader refused is a
        # claim the operator believes is recorded and is not.  Reported here
        # because ``doctor`` is the one command that reads the store and is part
        # of the PRD 9.2 minimum runtime evidence set -- so the cost shows up in
        # the evidence rather than only on the disk.
        findings.append(
            {
                "code": "matrix_unreadable_lines",
                "detail": (
                    f"{matrix_unreadable} compatibility-matrix line(s) could not be read; "
                    "those observations are not counted"
                ),
                "corrupt_lines": data["matrix"].get("corrupt_lines"),
                "refused_lines": data["matrix"].get("refused_lines"),
            }
        )

    data["findings"] = findings
    data["ready_for_run"] = qemu_path is not None and configured_accel in (None, "tcg")
    data["budget"] = _tracker(args).report().to_dict()
    return _ok("doctor", data)


def _matrix_block(args: argparse.Namespace) -> dict[str, Any]:
    """Report the compatibility matrix's health, including what it could not read.

    The counter is split by *cause*, because the two causes call for different
    actions: a corrupt line is damage to repair, and a refused line is a line
    one of this module's own gates turned away -- the authorisation gate, the
    trust-category gate, or a verdict claiming more than it can back.  Summing
    them would hide the second kind inside the first, and the second kind is the
    one that means somebody tried to record something they were not allowed to.

    ``location`` is relative to the runs root, and only the final component when
    the operator pointed ``--matrix`` somewhere outside it.  The absolute path is
    never emitted: on this host it embeds the Windows username, and this block is
    in ``doctor``'s envelope, which is part of the PRD 9.2 minimum runtime
    evidence set.  A relative location still tells two candidate files apart,
    which is the only thing a location has to do here.

    A missing module or an unreadable path is reported rather than raised: the
    doctor's job is to say the host is not ready, never to crash.
    """
    path = _matrix_path(args)
    location = _matrix_location(args, path)
    block: dict[str, Any] = {
        "location": location,
        "overridden": bool(getattr(args, "matrix", None)),
        "present": path.is_file(),
        "entry_count": 0,
        "observation_count": 0,
        "unreadable_lines": 0,
        "corrupt_lines": 0,
        "refused_lines": 0,
    }
    try:
        matrix = _matrix(args)
    except (UniPadError, OSError) as exc:
        # An unreadable file is a host condition, not a controller fault: the
        # counters stay ``unknown`` (via the note) rather than being reported as
        # zero, which would answer "nothing was lost" about a file nobody could
        # read.  ``OSError`` is caught alongside the structured refusal because
        # the loader opens the file itself and a permission or sharing violation
        # arrives as one.
        block["note"] = (
            "the compatibility matrix could not be opened, so its skipped lines "
            "are unknown rather than zero"
        )
        block["error_code"] = getattr(exc, "code", None) or type(exc).__name__
        # ``blocked`` is a category, not a reason: a path that is not a file and
        # a module that will not import are both ``blocked``, and an operator
        # has to be able to tell them apart without re-running the command
        # under a different name.  The ``matrix`` subcommand reports the same
        # guard as ``blocked_reason``; naming it here too keeps the two answers
        # about one store in agreement.
        reason = getattr(exc, "details", {}).get("blocked_reason")
        if isinstance(reason, str) and reason:
            block["blocked_reason"] = reason
        return block
    block.update(
        {
            "entry_count": len(matrix.entries),
            "observation_count": len(matrix.observations),
            "unreadable_lines": matrix.unreadable_lines,
            "corrupt_lines": matrix.corrupt_lines,
            "refused_lines": matrix.refused_lines,
        }
    )
    return block


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

    # Caller input is validated before the host is probed: a missing or invalid
    # manifest is a usage-class error (artifact_missing / artifact_manifest_invalid)
    # and must not be masked by the environment report below.
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

    if shutil.which("qemu-system-aarch64") is None:
        raise blocked(
            "qemu-system-aarch64 is not on PATH; cannot start the guest",
            command="start",
            expected="qemu-system-aarch64",
        )

    store = _store(args)
    experiment_id = args.experiment
    budget = _tracker(args)
    budget_report = budget.check("M1", hypothesis=args.hypothesis, new_experiment=False)
    run_dir = store.dir_for(experiment_id) if experiment_id else Path(args.runs_root)

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

    # The run directory is the first thing this command writes, so it is
    # created only once the launch is known to be viable.  Creating it before
    # the image check left an empty directory behind on every refused start:
    # no journal, no launch record, nothing.  ``ExperimentStore.scan`` skips a
    # directory without ``experiment.json``, so ``_latest_run`` never sees it
    # and nothing removes it -- retrying with a different experiment id each
    # time fills the runs root with directories that describe nothing.
    run_dir.mkdir(parents=True, exist_ok=True)

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

    # The accelerator is read out of the launch, never spelled here.  ``launch``
    # judges the command line it was handed and records what *that* states, so
    # an argv naming another machine cannot be reported as TCG; a literal in
    # this file would be a second, independent description of the run, and the
    # two are then free to disagree -- which is the defect
    # ``QemuLauncher._launch_evidence`` was rewritten to remove.  It is never
    # ``None``: the same guard refuses an argv that states no accelerator.
    accel = launched.evidence.get("accel")

    # Everything from here to the outcome is inside the guard: the child is
    # allowed to outlive this command *only* if the command says it did.
    # ``succeeded`` is the one flag that says it did, and it is set on the
    # single path that reports a running guest.
    succeeded = False
    try:
        # The launch record is written while the child is alive, not after it
        # is ready.  A crash in the readiness wait used to leave QEMU running
        # with nothing on disk naming its pid, so ``unipad stop`` answered
        # ``no_launch_record`` about a guest that was demonstrably started --
        # and the process, started in its own group with no Job Object, then
        # survived this controller's death with nothing left able to reach it.
        # ``readiness`` is ``None`` here rather than a guess: "not established
        # yet" and "established as not ready" are different facts, and only the
        # second one has been observed.
        record = launched.to_dict()
        record["profile"] = args.profile
        record["accel"] = accel
        record["readiness"] = None
        record["manifest"] = manifest_path.as_posix()
        write_json(run_dir / "launch.json", record)

        readiness = launcher.wait_for_qmp(args.qmp_timeout)
        record["readiness"] = readiness.to_dict()
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
            "accel": accel,
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
            # A ``failed`` envelope says the guest did *not* come up, so the
            # child may not outlive this command.  A ``return`` from inside the
            # ``try`` runs the ``finally`` but never an ``except``, which is why
            # the cleanup below is a ``finally`` and not an exception handler.
            return _failure(
                "start",
                launched.error_code or readiness.error_code or ErrorCode.QEMU_LAUNCH_FAILED,
                launched.error_message or readiness.error_message or "guest did not become ready",
                data=data,
            )
        succeeded = True
        return _ok("start", data)
    finally:
        # ``KeyboardInterrupt`` included: ``main`` turns it into a JSON envelope
        # and a fixed exit code rather than a traceback, which is exactly how a
        # Ctrl-C during the readiness wait used to end *cleanly* with QEMU still
        # running.  Cleanup must never mask the original failure, so ``close`` is
        # guaranteed by the inner ``finally`` whatever ``stop`` reports, and the
        # original exception is re-raised after this block either way.
        #
        # ``stop`` is a no-op for a process the launcher never started and for
        # one that already exited, so running it on every unsuccessful way out
        # costs nothing and is never a guess about whether a child is there.
        if not succeeded:
            try:
                launcher.stop(graceful=True)
            finally:
                launcher.close()


def cmd_install(args: argparse.Namespace) -> CommandOutcome:
    """``unipad install <path-to-ipa>`` (PRD 9.1, FR06)."""
    return _import_ipa(args, command="install", action="install")


def cmd_update(args: argparse.Namespace) -> CommandOutcome:
    """``unipad update <path-to-ipa>``: re-import a package that is installed.

    PRD 3.2 P1 (3).  An update is a *transition*, not a second kind of install:
    the ledger entry it writes names the entry it replaces, so "what it was
    before" survives "what it is now" and a rollback stays definable.  That is
    only meaningful for a bundle the ledger has installed, so the command
    refuses anything else -- and refuses *before* transferring bytes, because a
    transfer the operator believes is a replacement is worse than a refusal.

    The guest-side work is identical to :func:`cmd_install` and is deliberately
    shared with it: two implementations of one chain would drift, and the
    drift would show up as an update that installs differently than an install.
    """
    return _import_ipa(
        args, command="update", action="update", require_installed=True, reason=args.reason or ""
    )


# ---------------------------------------------------------------------------
# transfer stage: the package source (ADR-0008)
# ---------------------------------------------------------------------------


def _transfer_transport(args: argparse.Namespace, *, command: str) -> str:
    """Return the validated transport for this import.

    ``argparse`` already restricts the operator surface to
    :data:`TRANSPORTS`; this is the same check for a caller that reaches
    :func:`_import_ipa` without going through the parser (a test, a script, a
    future command).  An unknown value is a usage error rather than a silent
    fallback to the default, because a transport the operator believes they
    asked for is not the transport they would get.
    """
    raw = getattr(args, "transport", DEFAULT_TRANSPORT)
    value = str(raw if raw is not None else DEFAULT_TRANSPORT).strip().lower()
    if value not in TRANSPORTS:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "unknown transfer transport",
            command=command,
            transport=value,
            known=sorted(TRANSPORTS),
        )
    return value


class _TransferWindow:
    """The package source the transfer stage publishes, and only for that window.

    ADR-0008 puts the self-controlled HTTPS package service
    (:class:`unipad.host.httpsvc.PackageServer`) in ``_import_ipa``'s transfer
    stage, as a context manager whose lifetime is exactly the window in which
    bytes must move.  It is *not* in ``start`` (which spawns QEMU and returns,
    so a server there would be a daemon thread on a process that exits) and not
    in ``stop`` (which structurally cannot stop a process it did not launch).

    ``channel`` publishes nothing: the object holds no service at all and the
    guest channel behaves exactly as it did before ADR-0008.  ``https`` starts
    the service over the IPA's **own** directory -- never the whole run
    directory -- bound to loopback with TLS on.  Both are ``PackageServer``'s
    defaults and nothing here passes an argument that would relax either.

    The port is released by :meth:`stop` whatever the body did, an exception
    included.  A port that is already bound is a **refusal**, not a retry on
    another port: the recorded URL has to describe the service that actually
    listened.
    """

    def __init__(
        self,
        *,
        command: str,
        ipa_path: Path,
        transport: str,
        package_sha256: str,
        bundle_id: str | None,
        log_path: Path,
        log_lines: list[str],
        persist_log: Any,
    ) -> None:
        self.command = command
        self.ipa_path = Path(ipa_path)
        self.transport = transport
        self.package_sha256 = package_sha256
        self.bundle_id = bundle_id
        self.log_path = Path(log_path)
        self._log_lines = log_lines
        self._persist_log = persist_log
        self._server: Any | None = None
        self._evidence: dict[str, Any] | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "_TransferWindow":
        """Publish the package source, or refuse without touching the transfer.

        A service that could not start is a host condition, not a transfer
        failure: reporting ``transfer_failed`` would tell the operator the
        bytes broke when in fact nothing ever listened.  So this raises a
        structured ``blocked`` result -- after writing the install log the
        envelope is about to name, because every other refusal in this chain
        does (PRD 9.1).
        """
        if self.transport != TRANSPORT_HTTPS:
            return self
        if self._server is not None:
            # A second start would orphan the first socket: the new server would
            # become the one recorded, and the first would keep the port with
            # nothing left to stop it -- a leak that outlives the window and
            # looks exactly like the "port in use" this stage refuses.
            self._refuse(
                "the package service is already running",
                service_error="already_running",
                blocked_reason="package_service_already_running",
            )
        httpsvc = _httpsvc()
        if httpsvc is None:  # pragma: no cover - the module ships with this package
            self._refuse(
                "the self-controlled package service is not installed",
                service_error="unavailable",
            )
        try:
            server = httpsvc.PackageServer(
                root=self.ipa_path.parent, port=_package_service_port()
            )
            server.start()
        except UniPadError as exc:
            self._refuse(
                "the self-controlled package service did not listen; refusing to "
                "transfer over a transport nothing is serving",
                service_error=exc.code,
            )
        # From here on the socket is bound, so the window owns it.  The
        # assignment is deliberately the first statement after the bind and the
        # bookkeeping after it is guarded: a failure between the two would
        # otherwise leave a listening socket that nothing references and nobody
        # stops -- which a *second* import would meet as "address in use", the
        # exact thing this stage refuses to paper over.
        self._server = server
        try:
            self._evidence = httpsvc.service_evidence(
                server, package_name=self.ipa_path.name, package_sha256=self.package_sha256
            )
            self._log_lines.append(
                f"  package_service=listening port={self._evidence['port']} "
                f"fingerprint={self._evidence['certificate_fingerprint']}"
            )
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        """Release the port.  Idempotent, and safe on the ``channel`` route."""
        server, self._server = self._server, None
        if server is None:
            return
        server.stop()

    def __enter__(self) -> "_TransferWindow":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()

    # -- what the rest of the chain reads -----------------------------------

    @property
    def evidence(self) -> dict[str, Any] | None:
        """The service evidence, or ``None`` when no service was published.

        ``None`` rather than an empty mapping: an absent service and a service
        that recorded nothing are different facts, and only one of them
        happened on the ``channel`` route.
        """
        return dict(self._evidence) if self._evidence is not None else None

    def request_fields(self) -> dict[str, Any]:
        """The optional install-request fields this window publishes.

        Empty on the ``channel`` route, so the request the guest receives is
        byte-identical to the one it received before ADR-0008.  On ``https`` it
        names the URL the guest would fetch and the fingerprint it would pin,
        which is what makes the protocol extension reachable from production
        instead of being a second contract with no caller.
        """
        if self._evidence is None:
            return {}
        return {
            "package_url": self._evidence["guest_url"],
            "cert_fingerprint": self._evidence["certificate_fingerprint"],
        }

    # -- internals ---------------------------------------------------------

    def _refuse(
        self, message: str, *, service_error: str, blocked_reason: str = "package_service_unavailable"
    ) -> None:
        """Write the log, then refuse: a service that did not listen."""
        self._log_lines.append(f"  package_service={service_error}")
        self._persist_log()
        raise blocked(
            message,
            command=self.command,
            package_sha256=self.package_sha256,
            stage="checked",
            blocked_reason=blocked_reason,
            service_error=service_error,
            # The route is named on the refusal as well as on the success path:
            # a reader seeing ``package_service_unavailable`` must not have to
            # infer that ``--transport https`` was in play (ADR-0008).
            transport=self.transport,
            data={
                "package_sha256": self.package_sha256,
                "bundle_id": self.bundle_id,
                "stage": "checked",
                "transport": self.transport,
                "trust_mode": None,
                "error_code": ErrorCode.BLOCKED,
                "error_message": message,
                "log_path": self.log_path.as_posix(),
            },
        )


@contextlib.contextmanager
def _transfer_window(window: _TransferWindow) -> Iterator[_TransferWindow]:
    """Open an already-built transfer window: publish the source, then release it.

    ``start`` runs *before* the body, so a refusal never enters the window and
    never transfers a byte.  ``stop`` runs in a ``finally``, so the port is
    released on success, on a guest failure, and on an exception nobody
    expected.
    """
    window.start()
    try:
        yield window
    finally:
        window.stop()


def _as_install_channel(channel: Any) -> Any:
    """Return ``channel`` as an :class:`~unipad.ipa.installer.InstallChannel`.

    The ``guest_channel`` capability resolves to :mod:`unipad.guest`, whose only
    channel is a :class:`~unipad.guest.client.GuestClient`: a JSON-Lines control
    client exposing ``install_package`` / ``challenge`` / ``status`` and **no
    byte path at all**.  The channel contract the installer drives is
    ``send_package`` + ``request_install``, and
    :class:`~unipad.ipa.installer.GuestClientChannel` is the module that owns
    the adapter joining the two.

    An object that already satisfies that contract -- a byte channel plus a
    request method -- is returned unchanged; only a bare control client is
    wrapped.  The wrapping happens **here**, before
    :class:`_PackageSourceChannel` goes on top, because that wrapper's contract
    is "forward untouched": forwarding to an object that does not have the
    members being forwarded is an ``AttributeError`` at runtime.  The https
    route did exactly that, because the wrapper's own ``send_package`` made
    :func:`unipad.ipa.installer._adapt_channel` believe the object it wrapped
    was already a channel, so the adaptation never happened and the wrapper
    forwarded to a client (callchain finding 2).

    The import is lazy for the same reason ``_import_ipa`` imports
    :mod:`unipad.ipa.lifecycle` lazily: the controller must start, and every
    other command must run, without the install chain being importable.
    """
    from ..ipa.installer import GuestClientChannel

    if callable(getattr(channel, "send_package", None)) and callable(
        getattr(channel, "request_install", None)
    ):
        return channel
    return GuestClientChannel(channel)


class _PackageSourceChannel:
    """Wrap the guest channel so the install request names the package source.

    Only the ``https`` route installs this wrapper, and it changes one thing:
    the metadata handed to ``request_install`` gains the ``package_url`` and
    ``cert_fingerprint`` the transfer window published.  Everything else is
    forwarded untouched -- including ``send_package``, because the byte channel
    is what actually moves bytes on this host and the service is a *source*
    record, not a second byte path.

    ``inner`` must therefore already be an
    :class:`~unipad.ipa.installer.InstallChannel`; :func:`_as_install_channel`
    is what makes that true, and this wrapper does not repeat its work.  A
    wrapper that adapted its own inner would be a second, independent answer to
    "does this object need adapting", free to disagree with the installer's.

    Without it the two optional protocol fields would have no production
    caller, which is the exact defect ADR-0008 exists to close.
    """

    def __init__(self, inner: Any, window: _TransferWindow) -> None:
        self._inner = inner
        self._window = window

    @property
    def inner(self) -> Any:
        """The wrapped channel."""
        return self._inner

    def send_package(self, name: str, data: bytes) -> Any:
        """Forward the byte delivery; this wrapper adds no byte path."""
        return self._inner.send_package(name, data)

    def request_install(self, bundle_meta: Mapping[str, Any]) -> Any:
        """Ask the guest to install, naming where the package was published."""
        payload = dict(bundle_meta)
        payload.update(self._window.request_fields())
        return self._inner.request_install(payload)


def _import_ipa(
    args: argparse.Namespace,
    *,
    command: str,
    action: str,
    require_installed: bool = False,
    reason: str = "",
) -> CommandOutcome:
    """Import an IPA at runtime: precheck -> transfer -> install -> register (FR06).

    The result always reports package hash, bundle id, furthest stage reached,
    trust mode, error type and log location (PRD 9.1).  A successful file
    transfer alone never reports ``installed``.

    ``install`` and ``update`` run this same chain; they differ only in the
    transition recorded.  Every outcome that *reaches* the chain is appended to
    the ledger, a failure included -- the failure's class is the part an
    operator acts on, so dropping it would make the history prettier and less
    useful.  A stage that never happened records nothing in the *ledger*: the
    ``blocked`` paths below raise before :func:`_finish_install`, which is the
    only ledger writer.  They do still write ``install.log``, because the
    envelope names that file -- so "records nothing" is about the ledger, never
    about the evidence trail, and every refusal below persists its log before it
    leaves the command.

    Args:
        command: Envelope command name (``install`` or ``update``).
        action: Ledger action to record -- ``install`` or ``update``.
        require_installed: Refuse unless the ledger already has this bundle
            installed.  Checked after the precheck, where the bundle id is
            known, and before the guest channel, where bytes start moving.
        reason: Free-text operator note stored on the ledger entry.  Never a
            guest message (PRD 9.2).
    """
    from ..ipa.lifecycle import LifecycleAction

    transition = LifecycleAction(str(action))
    # Derived from the argument, never spelled at a call site.  ``prior_install_check``
    # is evidence of a check this controller actually ran, so the plain
    # ``install`` path -- which has nothing to check against, that being the
    # whole point of a first install -- must not claim it performed one
    # (PRD 9.2: missing evidence is unverified, not filled in).  ``update``
    # does run the check, and a precheck rejection never reaches either.
    prior_install_check = (
        "performed_by_controller" if require_installed else "not_applicable_first_install"
    )
    ledger = _ledger(args)
    ipa_path = Path(args.ipa)
    # Resolved before anything else happens: an unknown transport is a usage
    # error about this invocation, not a discovery made halfway through moving
    # bytes (ADR-0008).
    transport = _transfer_transport(args, command=command)
    if not ipa_path.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "IPA file not found",
            command=command,
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
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} controller: "
        f"{command} requested",
        f"  ipa={ipa_path.as_posix()}",
        f"  package_sha256={package_sha256}",
        f"  experiment_id={experiment_id}",
        f"  transport={transport}",
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
            command,
            install,
            experiment_id=experiment_id,
            run_dir=run_dir,
            log_path=log_path,
            log_lines=log_lines,
            precheck=precheck,
            dropped=precheck_dropped,
            budget=budget_report,
            # Passed explicitly rather than left to the helper's default: the
            # field reports what the chain did, so it must be derived from the
            # same argument on every path that reaches the envelope builder.
            prior_install_check=prior_install_check,
            # Reported, with no ``transfer`` evidence: the precheck refused the
            # package before any byte moved, so no service ever listened.  The
            # transport is still named because it is what this invocation asked
            # for, and a reader comparing two runs must not have to guess.
            transport=transport,
            ledger=ledger,
            action=transition,
            reason=reason,
        )

    # 1b. An update replaces something, so the something has to exist.  The
    #     check lives here rather than at the top of the command because the
    #     bundle id only becomes known from the precheck -- and it lives here
    #     rather than after the transfer because a transfer the operator
    #     believes is a replacement is worse than a refusal.
    if require_installed:
        bundle_id = getattr(report, "bundle_id", None)
        if not bundle_id or ledger.installed(str(bundle_id)) is None:
            log_lines.append(f"  refused=not_installed bundle_id={bundle_id}")
            _persist_install_log()
            raise blocked(
                "this bundle is not recorded as installed; use `unipad install` "
                "for a first install",
                command=command,
                experiment_id=experiment_id,
                package_sha256=package_sha256,
                stage="checked",
                blocked_reason="not_installed",
                ledger=ledger.path.as_posix(),
                installed_bundle_ids=list(ledger.installed_bundle_ids()),
                data={
                    "package_sha256": package_sha256,
                    "bundle_id": bundle_id,
                    "stage": "checked",
                    "transport": transport,
                    "trust_mode": None,
                    "error_code": ErrorCode.BLOCKED,
                    "error_message": "this bundle is not recorded as installed; "
                    "use `unipad install` for a first install",
                    "log_path": log_path.as_posix(),
                },
            )

    # 2. Guest channel: the transfer target must exist (PRD 10, FR06).
    channel_factory = _resolve_capability("guest_channel")
    if channel_factory is None:
        log_lines.append("  guest_channel=missing")
        _persist_install_log()
        raise blocked(
            "no guest channel available; cannot transfer the package into the guest",
            command=command,
            experiment_id=experiment_id,
            package_sha256=package_sha256,
            stage="checked",
            blocked_reason="guest_channel_missing",
            probed=_probed("guest_channel"),
            data={
                "package_sha256": package_sha256,
                "bundle_id": getattr(report, "bundle_id", None),
                "stage": "checked",
                "transport": transport,
                "trust_mode": None,
                "error_code": ErrorCode.BLOCKED,
                "error_message": "no guest channel available; cannot transfer the package into the guest",
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
            command=command,
            experiment_id=experiment_id,
            package_sha256=package_sha256,
            stage="checked",
            blocked_reason="guest_channel_unavailable",
            # The probe list is reported here as well as on the missing path:
            # the factory was found, and an operator needs to see which one
            # before concluding the guest is unreachable.
            probed=_probed("guest_channel"),
            error=f"{type(exc).__name__}: {exc}",
            data={
                "package_sha256": package_sha256,
                "bundle_id": getattr(report, "bundle_id", None),
                "stage": "checked",
                "transport": transport,
                "trust_mode": None,
                "error_code": ErrorCode.BLOCKED,
                "error_message": f"guest channel could not be established: {type(exc).__name__}: {exc}",
                "log_path": log_path.as_posix(),
            },
        ) from exc

    # 2b. Transfer stage (ADR-0008): the package source this import publishes.
    #     Built here, before the installer, because the installer is the thing
    #     that asks the guest to install and the request it sends must be able
    #     to name the source.  Nothing listens yet: that happens in the window
    #     below, which is the only period the service exists for.
    transfer = _TransferWindow(
        command=command,
        ipa_path=ipa_path,
        transport=transport,
        package_sha256=package_sha256,
        bundle_id=getattr(report, "bundle_id", None),
        log_path=log_path,
        log_lines=log_lines,
        persist_log=_persist_install_log,
    )
    # On the default route the channel stays exactly the object the capability
    # returned: no wrapper, no extra fields, no behaviour change.
    if transport == TRANSPORT_HTTPS:
        # The wrapper is an InstallChannel, so what it wraps has to be one too:
        # a bare ``GuestClient`` is a control client with no byte path, and
        # forwarding ``send_package`` to it is an AttributeError rather than a
        # transfer.  Adapting here rather than inside the wrapper keeps one
        # spelling of "does this need adapting" -- the installer's.
        channel = _PackageSourceChannel(_as_install_channel(channel), transfer)

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
            command=command,
            experiment_id=experiment_id,
            package_sha256=package_sha256,
            stage="checked",
            blocked_reason=exc.details.get("reason") or exc.code,
            data={
                "package_sha256": package_sha256,
                "bundle_id": getattr(report, "bundle_id", None),
                "stage": "checked",
                "transport": transport,
                "trust_mode": None,
                "error_code": exc.code,
                "error_message": exc.message,
                "log_path": log_path.as_posix(),
            },
        ) from exc

    # 4. The transfer window itself.  The service exists for exactly as long as
    #    the installer is moving bytes, and ``_transfer_window`` releases the
    #    port whatever this body does.
    with _transfer_window(transfer):
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
                command,
                install,
                experiment_id=experiment_id,
                run_dir=run_dir,
                log_path=log_path,
                log_lines=log_lines,
                precheck=precheck,
                dropped=sorted({*precheck_dropped, *channel_dropped}),
                budget=budget_report,
                prior_install_check=prior_install_check,
                transport=transfer.transport,
                transfer=transfer.evidence,
                ledger=ledger,
                action=transition,
                reason=reason,
            )
        log_lines.append(f"  installer_stdout_bytes={len(install_stdout)}")
        try:
            install = _coerce_install_result(
                install,
                command=command,
                package_sha256=package_sha256,
                bundle_id=getattr(report, "bundle_id", None),
                log_path=log_path,
            )
        except UniPadError as exc:
            # A refusal is evidence too.  Every other blocked path in this chain
            # writes install.log before raising, because the envelope names that
            # file; this one used to raise before any writer, so the run directory
            # held only the journal the command created before the chain started
            # and nothing recorded that an install was attempted (PRD 9.1).
            log_lines.append(f"  refused={exc.code}: {exc.message}")
            _persist_install_log()
            raise
        if not install.bundle_id:
            install.bundle_id = getattr(report, "bundle_id", None)
        if not install.log_path:
            install.log_path = log_path.as_posix()
        return _finish_install(
            command,
            install,
            experiment_id=experiment_id,
            run_dir=run_dir,
            log_path=log_path,
            log_lines=log_lines,
            precheck=precheck,
            dropped=sorted({*precheck_dropped, *channel_dropped}),
            budget=budget_report,
            prior_install_check=prior_install_check,
            transport=transfer.transport,
            transfer=transfer.evidence,
            ledger=ledger,
            action=transition,
            reason=reason,
        )


def _coerce_install_result(
    raw: Any,
    *,
    command: str,
    package_sha256: str,
    bundle_id: str | None,
    log_path: Path,
) -> InstallResult:
    """Normalise an installer return value into :class:`InstallResult`.

    An unrecognised return value is a blocked outcome: the controller never
    synthesises an install stage it did not observe (PRD 9.1).

    ``command``, ``bundle_id`` and ``log_path`` are passed in rather than read
    from an enclosing scope: the first was once referenced as a bare name, so
    the refusal it builds raised :class:`NameError` and reached ``main``'s
    catch-all as ``internal_error`` -- a code outside the PRD 10 taxonomy that
    blames the controller for an installer that returned something else.  The
    other two let the refusal carry the same PRD 9.1 fields every other
    ``blocked`` install reports, including the log the caller has already been
    asked to write.
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
        command=command,
        package_sha256=package_sha256,
        stage="checked",
        blocked_reason="installer_result_unclassified",
        # Only the type: the value itself may be anything (PRD 9.2).
        returned_type=type(raw).__name__,
        data={
            "package_sha256": package_sha256,
            "bundle_id": bundle_id,
            "stage": "checked",
            "trust_mode": None,
            "error_code": ErrorCode.BLOCKED,
            "error_message": (
                "installer returned no structured result; refusing to report an install stage"
            ),
            "log_path": log_path.as_posix(),
        },
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
    transport: str | None = None,
    transfer: dict[str, Any] | None = None,
    ledger: Any = None,
    action: Any = None,
    reason: str = "",
) -> CommandOutcome:
    """Persist the install log/result and build the PRD 9.1 envelope.

    ``ledger`` is the append-only install ledger to record the transition in.
    When it is ``None`` nothing is written, which is what keeps the ledger a
    property of the chain rather than of this helper -- only
    :func:`_import_ipa` opens one.

    ``transport`` names the route the package bytes were published on and
    ``transfer`` the service that published them; both are ADR-0008 evidence,
    and both are omitted when the caller passes nothing.  ``transfer`` is
    present only when a service actually listened, so its absence is the
    statement "no package service ran" and never "a service ran and recorded
    nothing".

    The entry records the stage the chain actually reached, failure included:
    ``InstallLedger.installed`` derives presence from a stage in
    ``("installed", "registered")``, so a ``transferred`` or ``failed`` entry
    can never make a bundle look present (PRD 9.1).
    """
    log_lines.append(
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} controller: "
        f"stage={install.stage} error_code={install.error_code} bundle_id={install.bundle_id}"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(log_lines) + "\n")
    # The install result stays ``InstallResult``'s own document; the transport
    # evidence is added beside it rather than folded into it, so the model's
    # contract does not grow a field only the controller can fill.
    record = install.to_dict()
    if transport is not None:
        record["transport"] = transport
    if transfer:
        record["transfer"] = transfer

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
    # The transport is reported on every path that got this far, because an
    # operator reading the envelope must be able to tell which route the bytes
    # took without opening the run directory.  The service evidence is reported
    # only when a service ran (see the ``transfer`` note above).
    if transport is not None:
        data["transport"] = transport
    if transfer:
        data["transfer"] = transfer
    if ledger is not None:
        # The write is not wrapped in a try/except: a ledger that cannot be
        # appended is a host fault, and reporting the install as fine while its
        # history was lost would be the worse of the two errors.  ``main``
        # turns the resulting OSError into a JSON envelope, not a traceback.
        entry = ledger.record_install(install, action=action, reason=reason)
        data["lifecycle"] = {
            "ledger": ledger.path.as_posix(),
            "entry_id": entry.entry_id,
            "action": entry.action.value,
            "bundle_id": entry.bundle_id,
            "stage": entry.stage,
            "supersedes": entry.supersedes,
            "error_code": entry.error_code,
            "error_class": entry.error_class,
            "unreadable_lines": ledger.unreadable_lines,
        }
    # The run record is evidence, and the ledger is the state -- so the ledger
    # lands first.  The other order left a window in which a crash wrote
    # ``install.json`` and no history, after which ``history`` answered
    # "nothing" and ``update`` and ``uninstall`` refused with ``not_installed``
    # about a run whose own record says it installed.  Nothing reconciles the
    # two files, so the one that derives the other's answers has to be the one
    # that is already durable.  ``models.write_json`` is an atomic rename plus
    # fsync, so this ordering is about *which* fact is committed first, not
    # about a torn file.
    write_json(run_dir / "install.json", record)

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
    # The record is caller-writable data, so its ``pid`` is validated rather
    # than coerced.  ``int()`` on a string, a list or a mapping raised out of
    # this command into ``main``'s catch-all and came back as
    # ``internal_error`` -- an off-taxonomy code that says "the controller is
    # broken" when the truth is "this launch record is not one I can read".
    # ``bool`` is excluded on purpose: it is an ``int`` subclass, and
    # ``True`` is not a pid.
    raw_pid = record["pid"]
    if isinstance(raw_pid, bool) or not isinstance(raw_pid, int):
        raise UniPadError(
            ErrorCode.ARTIFACT_MANIFEST_INVALID,
            "launch record pid is not an integer",
            command="stop",
            run=run_id,
            path=record_path.as_posix(),
            pid_type=type(raw_pid).__name__,
            hint="launch.json is written by `unipad start`; a hand-edited or "
            "truncated record cannot name a process to stop",
        )
    pid = raw_pid
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


# ---------------------------------------------------------------------------
# install lifecycle (PRD 3.2 P1 (3))
# ---------------------------------------------------------------------------
def cmd_history(args: argparse.Namespace) -> CommandOutcome:
    """Print the install history of one bundle, or of every bundle (PRD 3.2 P1 (3)).

    A question with a known answer is not a failure.  A bundle nothing ever
    recorded, and a ledger that does not exist yet, both return an empty
    history with exit 0 -- the operator asked what happened and the honest
    answer is "nothing", which a non-zero exit code would misreport as a fault.

    ``unreadable_lines`` is reported whenever the ledger had to skip a line, so
    a truncated tail costs the last entry rather than the whole history, and
    the cost is visible instead of silent.

    ``ledger_growth_warning`` carries :attr:`InstallLedger.growth_warning` once
    the file passes the documented line bound.  The bound is a measurement, not
    a limit: nothing refuses to append and nothing refuses to read, so the only
    honest defence against a cost that is linear and unindexed is to say so
    while the file is still small enough to do something about it.  The key is
    absent below the bound rather than ``None``, so "no warning" is a shape a
    caller can test for without knowing the threshold.
    """
    if args.bundle and args.all_bundles:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "history takes either --bundle <id> or --all, not both",
            command="history",
        )
    if not args.bundle and not args.all_bundles:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "history requires --bundle <id> or --all",
            command="history",
            hint="--bundle answers for one app; --all answers for the whole host",
        )
    # A blank --bundle is a usage error, not a request for everything: silently
    # widening the scope would answer a different question than the one asked.
    bundle_id = (args.bundle or "").strip() or None
    if args.bundle and not bundle_id:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "history requires a non-empty --bundle id",
            command="history",
        )

    path = _ledger_path(args)
    ledger = _ledger(args)
    entries = ledger.history(bundle_id) if bundle_id else ledger.entries
    # One bundle, one question, one scan.  ``installed()`` reduces over
    # ``history()``, so calling it bare here re-walked the whole ledger to
    # answer something the pass above already knew -- and at the documented
    # 100 k bound that second pass is a second of the command's cost.
    # ``from_history`` is the same tuple this loop already holds.
    current = (
        ledger.installed(bundle_id, from_history=entries) if bundle_id else None
    )

    data: dict[str, Any] = {
        "ledger": path.as_posix(),
        "ledger_present": path.is_file(),
        "bundle_id": bundle_id,
        "all_bundles": bool(args.all_bundles),
        "entry_count": len(entries),
        # LifecycleEntry.to_dict() omits error_message by contract (PRD 9.2).
        "entries": [entry.to_dict() for entry in entries],
        "unreadable_lines": ledger.unreadable_lines,
        # Derived from the tail, never stored as a field.
        "installed": bool(current) if bundle_id else None,
        "current_stage": (current.stage if current is not None else None),
    }
    # Growth is a warning, never a refusal: the key is absent below the
    # documented bound so "no warning" and "no ledger" stay distinguishable
    # from a warning that happens to be empty.
    growth = ledger.growth_warning
    if growth is not None:
        data["ledger_growth_warning"] = growth
    if args.all_bundles:
        data["installed_bundle_ids"] = list(ledger.installed_bundle_ids())
    return _ok("history", data)


def cmd_uninstall(args: argparse.Namespace) -> CommandOutcome:
    """Record the removal of an installed bundle (PRD 3.2 P1 (3)).

    Two things this command refuses to do:

    * remove a bundle the ledger does not have installed.  ``record_uninstall``
      raises for exactly that case, because agreeing with a caller whose view of
      the device is wrong would hide the disagreement -- and the CLI turns the
      ``ValueError`` into a structured ``blocked`` envelope with the list of
      bundles that *are* installed, so the operator can see the mismatch;
    * claim anything about the guest.  The guest-side removal is not this host's
      to perform; what is recorded is the host-side transition, and the ledger
      is what every later ``history``/``update`` answers from.
    """
    bundle_id = (args.bundle_id or "").strip()
    if not bundle_id:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "uninstall requires a bundle id",
            command="uninstall",
            hint="unipad uninstall <bundle-id>, e.g. the CFBundleIdentifier",
        )

    path = _ledger_path(args)
    ledger = _ledger(args)
    current = ledger.installed(bundle_id)
    data: dict[str, Any] = {
        "ledger": path.as_posix(),
        "bundle_id": bundle_id,
        "installed_bundle_ids": list(ledger.installed_bundle_ids()),
        "entry_count_before": len(ledger.entries),
        "unreadable_lines": ledger.unreadable_lines,
    }
    if current is None:
        data["installed"] = False
        raise blocked(
            f"{bundle_id} is not recorded as installed; refusing to record a removal",
            command="uninstall",
            bundle_id=bundle_id,
            blocked_reason="not_installed",
            ledger=path.as_posix(),
            installed_bundle_ids=data["installed_bundle_ids"],
            data=data,
        )

    result = InstallResult(
        package_sha256=current.package_sha256,
        bundle_id=bundle_id,
        stage="removed",
    )
    try:
        entry = ledger.record_uninstall(result, reason=args.reason or "")
    except ValueError as exc:
        # Defensive: installed() said the bundle is there and record_uninstall
        # disagreed, which means the ledger changed under us.  Report the
        # disagreement rather than either answer.
        data["installed"] = False
        raise blocked(
            "the ledger disagrees that this bundle is installed; nothing was recorded",
            command="uninstall",
            bundle_id=bundle_id,
            blocked_reason="ledger_disagreement",
            error=str(exc),
            data=data,
        ) from exc

    data.update(
        {
            "installed": False,
            "entry_count_after": len(ledger.entries),
            "entry": entry.to_dict(),
            "stage": entry.stage,
            "package_sha256": entry.package_sha256,
            "supersedes": entry.supersedes,
            # Re-derived after the removal, not the value captured before it.
            # ``installed_bundle_ids`` is what an operator reads to decide what
            # to remove next, so a list that still names the bundle this same
            # envelope reports as ``removed`` contradicts the entry beside it --
            # and the next ``uninstall`` of that id is refused for a reason the
            # previous answer denied.
            "installed_bundle_ids": list(ledger.installed_bundle_ids()),
        }
    )
    return _ok("uninstall", data)


# ---------------------------------------------------------------------------
# PRD 3.2 P1 (2): the compatibility matrix (report / record)
# ---------------------------------------------------------------------------


def _matrix_module(command: str) -> Any:
    """Return :mod:`unipad.ipa.matrix` itself, or refuse naming the probe.

    The store's *class* resolves through :data:`_CAPABILITIES` like every other
    capability.  The module is needed alongside it because the closed
    vocabularies (:class:`~unipad.ipa.matrix.Authorisation`,
    :class:`~unipad.ipa.matrix.TrustCategory`,
    :class:`~unipad.ipa.matrix.Verdict`) and the two dataclasses
    (:class:`~unipad.ipa.matrix.MatrixKey`,
    :class:`~unipad.ipa.matrix.MatrixEntry`) live in it -- and a second copy of
    those spellings here would be exactly the drift the module's own docstring
    says must never exist, on the one surface where a stale copy would let an
    unauthorised package in under a near-miss spelling.
    """
    module = _import_module(_MATRIX_MODULE)
    if module is None:
        raise blocked(
            "capability unavailable on this host",
            command=command,
            capability="compatibility_matrix",
            probed=[f"{_MATRIX_MODULE}.CompatibilityMatrix"],
            data={"capability": "compatibility_matrix"},
        )
    return module


def _matrix_entry_view(entry: Any) -> dict[str, Any]:
    """Return one entry as an envelope may carry it.

    ``package_path`` is dropped and the reason is PRD 9.2: it is an absolute host
    path, so on this machine it embeds the Windows username, and an observation
    envelope is exactly the kind of thing that ends up pasted into an issue.  The
    file is where the path belongs -- the record is not useful without it, and
    stdout is not the place it is read from.  Everything else the entry carries
    is either read off the package's own bytes, declared by the operator, or
    derived by the precheck, and none of it is a secret.
    """
    view = entry.to_dict()
    view.pop("package_path", None)
    return view


def _matrix_key(args: argparse.Namespace, module: Any) -> Any:
    """Build the PRD 10 three-part key, or refuse an incomplete one.

    All three parts are required and none has a default.  A default is how "this
    app works" gets recorded as if it were a sample: a blank ``run_config``
    quietly widened to "any configuration" would make one observation answer a
    question it never asked, and nothing downstream could tell.
    """
    parts = {
        "package_version": str(getattr(args, "package_version", None) or "").strip(),
        "system_build": str(getattr(args, "system_build", None) or "").strip(),
        "run_config": str(getattr(args, "run_config", None) or "").strip(),
    }
    missing = sorted(name for name, value in parts.items() if not value)
    if missing:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "matrix record requires all three parts of the PRD 10 key",
            command="matrix",
            missing=missing,
            key=parts,
            hint=(
                "--package-version, --system-build and --run-config together name one "
                "sample under one run configuration; config/sources.lock.json carries "
                "the pinned system build"
            ),
        )
    try:
        return module.MatrixKey(**parts)
    except ValueError as exc:
        # Defensive: the strip above already caught a blank part, so this is a
        # non-string reaching the key.  Reported rather than rounded.
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            str(exc),
            command="matrix",
            key=parts,
        ) from exc


def _matrix_vocabulary(
    args: argparse.Namespace, module: Any, flag: str, vocabulary: Any
) -> Any:
    """Coerce one ``--`` flag against a closed vocabulary, or refuse the spelling.

    The allowed values are read out of the module that owns the vocabulary rather
    than spelled here, for the reason :func:`_matrix_module` gives.

    ``not-authorised`` is deliberately *not* refused here.  It is a legal member
    of the vocabulary, and the module's authorisation gate is what refuses it --
    so the refusal an operator sees comes from the gate, carrying the gate's own
    stable reason, instead of from argparse second-guessing policy.  A gate that
    only holds when the caller is a test is not a gate.
    """
    raw = getattr(args, flag, None)
    if raw is None or not str(raw).strip():
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            f"matrix record requires --{flag.replace('_', '-')}",
            command="matrix",
            allowed=[str(member) for member in vocabulary],
            hint=(
                "an entry that declares no "
                f"{flag.replace('_', ' ')} is not an entry at all (PRD 5.1 / 12)"
            ),
        )
    try:
        return vocabulary.coerce(str(raw).strip())
    except ValueError as exc:
        # A near-miss is not rounded to a neighbour: "compatible" must not become
        # "installs-and-runs", and "probably-fine" must not become "own-source".
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            str(exc),
            command="matrix",
            field=flag,
            value=str(raw),
            allowed=[str(member) for member in vocabulary],
        ) from exc


def _matrix_selector(args: argparse.Namespace, module: Any) -> dict[str, Any]:
    """Return the report selector as data, or refuse an ambiguous one."""
    bundle_id = str(getattr(args, "bundle", None) or "").strip()
    key_parts = [str(part).strip() for part in (getattr(args, "key", None) or ())]
    if bundle_id and key_parts:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "matrix report takes either --bundle or --key, not both",
            command="matrix",
            hint="--bundle answers for one app; --key answers for one PRD 10 triple",
        )
    if bundle_id:
        return {"kind": "bundle", "bundle_id": bundle_id, "key": None}
    if key_parts:
        try:
            key = module.MatrixKey(*key_parts)
        except (TypeError, ValueError) as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "--key takes exactly three parts: a package version, a system "
                "build and a run configuration",
                command="matrix",
                key=key_parts,
                error=str(exc),
            ) from exc
        return {"kind": "key", "bundle_id": None, "key": key.to_dict()}
    return {"kind": "all", "bundle_id": None, "key": None}


def cmd_matrix(args: argparse.Namespace) -> CommandOutcome:
    """Read or append to the compatibility matrix (PRD 3.2 P1 (2); docs/PRD.md:87).

    Two actions over one append-only store:

    ``report`` (the default) answers what the matrix holds.  A question with a
    known answer is not a failure, so an absent matrix and a selector nothing
    matched both return exit 0 with an empty list -- the operator asked and the
    honest answer is "nothing", which a non-zero exit code would misreport as a
    fault.  What it must never do is report zero where the truth is "unknown", so
    the lines the loader skipped are always carried, split by cause: a corrupt
    line is damage to repair, a refused line is one this module's own gate turned
    away, and summing the two would hide the second inside the first.

    ``record`` appends one observation about one package under one
    :class:`~unipad.ipa.matrix.MatrixKey`.  It derives the verdict rather than
    accepting one, because the only evidence a Windows host can produce about a
    package is the precheck the install chain itself runs: a report with faults
    is ``rejected`` and carries the first fault's stable code, a clean report is
    ``untested``.  There is no flag that turns a host-side check into a
    compatibility claim -- ``installs-only`` and ``installs-and-runs`` need an
    install stage, an install stage needs a guest, and this host has none, so
    they are unreachable through this command by construction rather than by
    good intentions.

    The authorisation and trust category are the operator's declaration and are
    required, because the module refuses an entry that declares neither.  The
    command does not soften that: it hands the declaration to the module and
    reports whatever the module says, so the refusal the operator reads carries
    the module's own stable reason.
    """
    action = str(getattr(args, "action", None) or "report")
    if action == "record":
        return _matrix_record(args)
    return _matrix_report(args)


def _matrix_report(args: argparse.Namespace) -> CommandOutcome:
    """Report what the compatibility matrix holds."""
    if getattr(args, "ipa", None):
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "matrix report takes no package; use `unipad matrix record <ipa>` to append",
            command="matrix",
            hint="report answers what the matrix holds; record adds to it",
        )
    module = _matrix_module("matrix")
    selector = _matrix_selector(args, module)
    path = _matrix_path(args)
    store = _matrix(args)

    if selector["kind"] == "bundle":
        entries = store.entries_for_bundle(str(selector["bundle_id"]))
    elif selector["kind"] == "key":
        current = store.entry_for(module.MatrixKey(**selector["key"]))
        entries = (current,) if current is not None else ()
    else:
        entries = store.entries

    # Every observation under the keys being reported, oldest first.  Without
    # ``--history`` only the count is carried: an operator asking "what is the
    # current answer" does not want the history, and one asking "what was ever
    # observed" asks for it explicitly.
    #
    # ``dict.fromkeys`` rather than a ``not in`` scan over a list: the list form
    # is O(k^2) in the number of distinct keys, and a matrix that has been
    # appended to for a while is exactly where that stops being free.  It keeps
    # first-seen order, which is what makes the answer deterministic.
    keys = list(dict.fromkeys(entry.key for entry in entries))
    observations = [observed for key in keys for observed in store.observations_for(key)]

    # Tallied over what is reported, so ``entry_count`` and the sum of ``counts``
    # cannot disagree -- with no selector those are the matrix's own current
    # entries, and the two answers are then the module's ``counts()``.
    counts = {str(verdict): 0 for verdict in module.VERDICTS}
    for entry in entries:
        counts[str(entry.verdict)] += 1

    data: dict[str, Any] = {
        "matrix": _matrix_location(args, path),
        "matrix_overridden": bool(getattr(args, "matrix", None)),
        "matrix_present": path.is_file(),
        "selector": selector,
        "history": bool(getattr(args, "history", False)),
        "entry_count": len(entries),
        "observation_count": len(observations),
        "entries": [_matrix_entry_view(entry) for entry in entries],
        "counts": counts,
        # Split by cause, and always carried: a skipped line is either damage to
        # repair or a line this module's gate refused, and neither may vanish.
        "unreadable_lines": store.unreadable_lines,
        "corrupt_lines": store.corrupt_lines,
        "refused_lines": store.refused_lines,
    }
    if data["history"]:
        data["observations"] = [_matrix_entry_view(observed) for observed in observations]
    return _ok("matrix", data)


def _matrix_redact_ipa_error(exc: UniPadError, ipa_path: Path) -> UniPadError:
    """Return ``exc`` with any absolute host path in its details replaced by a name.

    ``unipad.ipa.package.open_ipa`` reports a structurally broken IPA with
    ``path=<the path it was handed>``, and that path is absolute -- so on this
    machine it embeds the Windows username.  :func:`_from_error` serialises a
    :class:`UniPadError`'s ``details`` verbatim, which means an operator who
    typo'd a path would paste their username into an issue report.

    The install chain does not have this exposure because
    ``unipad.ipa.precheck.run_precheck`` catches the same error and keeps only
    its ``code``, ``reason`` and message, dropping the location.  ``record``
    calls ``open_ipa`` directly -- it needs the :class:`PackageInfo` for the
    PRD 5.1 support record the precheck report does not carry -- so the
    redaction has to happen here instead.

    Only the location is redacted.  The code, the message and every other
    detail survive untouched, because what an operator acts on is the stable
    ``reason`` and that is not what is being withheld.
    """
    details = dict(exc.details) if isinstance(exc.details, dict) else {}
    for key in ("path", "package_path", "ipa", "ipa_path"):
        value = details.get(key)
        if isinstance(value, str) and value:
            details[key] = safe_location(value)
    return UniPadError(exc.code, exc.message, **details)


def _matrix_record(args: argparse.Namespace) -> CommandOutcome:
    """Append one observation, derived from the package and its precheck."""
    raw_ipa = getattr(args, "ipa", None)
    if raw_ipa is None or not str(raw_ipa).strip():
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "matrix record requires the path to the .ipa being recorded",
            command="matrix",
            hint=(
                "unipad matrix record <path-to-ipa> --package-version <v> "
                "--system-build <b> --run-config <c> --authorisation <a> "
                "--trust-category <t>"
            ),
        )
    ipa_path = Path(str(raw_ipa).strip())
    note = str(getattr(args, "note", None) or "").strip()

    module = _matrix_module("matrix")
    key = _matrix_key(args, module)
    authorisation = _matrix_vocabulary(args, module, "authorisation", module.Authorisation)
    trust_category = _matrix_vocabulary(
        args, module, "trust_category", module.TrustCategory
    )

    # The store is opened before the package, so a matrix path that is not a file
    # is refused as such rather than as a missing IPA -- the operator pointed the
    # command at the wrong *store*, and that is the more useful of the two
    # answers to get first.
    path = _matrix_path(args)
    store = _matrix(args)

    if not ipa_path.is_file():
        raise UniPadError(
            ErrorCode.ARTIFACT_MISSING,
            "IPA file not found",
            command="matrix",
            # The name, not the path: an absolute path embeds the username, and
            # the name is enough to tell two candidates apart (PRD 9.2).
            path=safe_location(ipa_path),
        )

    # The same two readers the install chain's precheck stage uses, resolved
    # through the capability table.  ``open_ipa`` is needed separately from the
    # precheck because the matrix records the §5.1 support record -- signing
    # mode, embedded frameworks, requested permissions -- off the
    # PackageInfo, which the precheck report does not carry.
    open_ipa = _require_capability("matrix", "ipa_open")
    precheck_package = _require_capability("matrix", "ipa_precheck_package")

    # ``from_precheck`` derives the verdict and the conditions, and re-runs every
    # gate on the way through.  A UniPadError from here is either the package
    # reader refusing a structurally broken IPA or the module refusing this
    # entry, and the envelope carries the module's own stable code and reason,
    # which is what an operator acts on.  The one thing taken out is an absolute
    # host path -- see :func:`_matrix_redact_ipa_error`.
    try:
        package, _dropped_open, _open_stdout = _call_capability(
            open_ipa, path=ipa_path.as_posix()
        )
        report, _dropped_precheck, _precheck_stdout = _call_capability(
            precheck_package, package=package
        )
        entry = module.MatrixEntry.from_precheck(
            package,
            report,
            key=key,
            authorisation=authorisation,
            trust_category=trust_category,
            note=note,
        )
        stored = store.record(entry)
    except UniPadError as exc:
        raise _matrix_redact_ipa_error(exc, ipa_path) from exc

    data: dict[str, Any] = {
        "matrix": _matrix_location(args, path),
        "matrix_overridden": bool(getattr(args, "matrix", None)),
        "action": "record",
        # The name, not the path (PRD 9.2); the full path is on the stored entry.
        "ipa": ipa_path.name,
        "package_sha256": stored.package_sha256,
        "bundle_id": stored.bundle_id,
        "app_name": stored.app_name,
        "key": stored.key.to_dict(),
        "authorisation": str(stored.authorisation),
        "trust_category": str(stored.trust_category),
        "verdict": str(stored.verdict),
        "established_conditions": [str(c) for c in stored.established_conditions],
        "install_stage": stored.install_stage,
        "signing_mode": stored.signing_mode,
        "embedded_frameworks": list(stored.embedded_frameworks),
        "requested_permissions": list(stored.requested_permissions),
        "error_code": stored.error_code,
        "reason": stored.reason,
        "note": stored.note,
        "observed_at": stored.observed_at,
        # Why the verdict is what it is: the precheck is the only evidence behind
        # it, so the verdict without the report's own outcome is unreadable.
        "precheck_verdict": str(getattr(report, "verdict", "")),
        "precheck_fault_codes": [str(code) for code in getattr(report, "fault_codes", ())],
        "entry_count": len(store.entries),
        "observation_count": len(store.observations),
        "unreadable_lines": store.unreadable_lines,
        "corrupt_lines": store.corrupt_lines,
        "refused_lines": store.refused_lines,
    }
    return _ok("matrix", data)


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

#: The PRD 3.2 P1 (3) lifecycle commands, kept out of :data:`_COMMAND_TABLE` so
#: "the controller implements exactly the PRD 9.1 list" stays a checkable
#: statement rather than a convention someone has to remember.
_LIFECYCLE_COMMAND_TABLE: dict[str, Callable[[argparse.Namespace], CommandOutcome]] = {
    "history": cmd_history,
    "uninstall": cmd_uninstall,
    "update": cmd_update,
}

#: The PRD 3.2 P1 (2) compatibility-matrix command, kept out of both tables above
#: for the same reason: the matrix is a store with its own contract, and a third
#: table makes "the controller implements exactly the PRD 9.1 list" a statement
#: about one dict rather than a convention someone has to remember.
_MATRIX_COMMAND_TABLE: dict[str, Callable[[argparse.Namespace], CommandOutcome]] = {
    "matrix": cmd_matrix,
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
    """Build the argparse CLI (PRD 9.1 commands, the lifecycle and matrix surfaces)."""
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
        # The epilog is the operator-facing half of the same split the code
        # carries in COMMANDS / LIFECYCLE_COMMANDS / MATRIX_COMMANDS.  Thirteen
        # subcommands print under a description that still reads "(PRD 9.1)", so
        # without this an operator reading --help cannot tell the documented nine
        # from the three the install-lifecycle surface adds and the one the
        # compatibility-matrix surface adds.
        epilog=(
            "PRD 9.1 commands: " + ", ".join(COMMANDS) + ".  "
            "PRD 3.2 P1 (3) install lifecycle: " + ", ".join(LIFECYCLE_COMMANDS) + ".  "
            "PRD 3.2 P1 (2) compatibility matrix: " + ", ".join(MATRIX_COMMANDS) + "."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    doctor.add_argument("--matrix", default=None, help=MATRIX_HELP_READ)
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
    install.add_argument("--ledger", default=None, help=LEDGER_HELP_WRITE)
    install.add_argument("--transport", default=DEFAULT_TRANSPORT, choices=list(TRANSPORTS), help=TRANSPORT_HELP)
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

    # --- PRD 3.2 P1 (3): uninstall, update, install history -----------------
    # All three read the same ledger `install` appends to, so they take the
    # same --ledger override for the same reason.
    history = sub.add_parser(
        "history", parents=[common], help="print the install history of a bundle or of every bundle"
    )
    history.add_argument("--bundle", default=None, help="bundle id (CFBundleIdentifier) to report")
    history.add_argument(
        "--all", dest="all_bundles", action="store_true", help="report every bundle in the ledger"
    )
    history.add_argument("--ledger", default=None, help=LEDGER_HELP_READ)
    history.set_defaults(handler=cmd_history)

    uninstall = sub.add_parser(
        "uninstall", parents=[common], help="record the removal of an installed bundle"
    )
    uninstall.add_argument("bundle_id", help="bundle id (CFBundleIdentifier) to remove")
    uninstall.add_argument(
        "--reason", default="", help="operator note stored on the ledger entry"
    )
    uninstall.add_argument("--ledger", default=None, help=LEDGER_HELP_WRITE)
    uninstall.set_defaults(handler=cmd_uninstall)

    update = sub.add_parser(
        "update", parents=[common], help="re-import a package that is already installed"
    )
    update.add_argument("ipa", help="path to the replacement .ipa file")
    update.add_argument("--experiment", default=None, help="experiment id to attribute the update to")
    update.add_argument("--milestone", default="M10", help="milestone to charge the update to")
    update.add_argument("--reason", default="", help="operator note stored on the ledger entry")
    update.add_argument("--ledger", default=None, help=LEDGER_HELP_WRITE)
    update.add_argument("--transport", default=DEFAULT_TRANSPORT, choices=list(TRANSPORTS), help=TRANSPORT_HELP)
    update.set_defaults(handler=cmd_update)

    # --- PRD 3.2 P1 (2): read or append to the compatibility matrix -----------
    # One subcommand, two actions.  ``report`` is the default so the question
    # "what does the matrix hold" is the shortest thing to type; ``record`` takes
    # the package as a positional, the way ``install`` and ``update`` do.
    #
    # The PRD 10 key and the two required declarations have no argparse
    # ``choices`` on purpose.  The closed vocabularies live in
    # ``unipad.ipa.matrix``, which this controller does not import at module load
    # -- that is the whole point of :data:`_CAPABILITIES` -- and a second copy of
    # the spellings here would be a second thing to keep true.  The coercion
    # happens in the command, against the module that owns the vocabulary, and a
    # near-miss comes back as a structured ``config_invalid`` naming the allowed
    # values.  ``action`` does use ``choices``: it is this controller's own
    # surface, exactly as ``--transport`` is.
    matrix = sub.add_parser(
        "matrix", parents=[common], help="read or append to the compatibility matrix"
    )
    matrix.add_argument(
        "action",
        nargs="?",
        choices=list(MATRIX_ACTIONS),
        default="report",
        help="report what the matrix holds (default) or record one observation",
    )
    matrix.add_argument(
        "ipa",
        nargs="?",
        default=None,
        help="path to the .ipa to record (action=record only)",
    )
    matrix.add_argument(
        "--bundle", default=None, help="report: bundle id (CFBundleIdentifier) to report"
    )
    matrix.add_argument(
        "--key",
        nargs=3,
        default=None,
        metavar=("PACKAGE_VERSION", "SYSTEM_BUILD", "RUN_CONFIG"),
        help="report: the PRD 10 three-part key to report",
    )
    matrix.add_argument(
        "--history",
        action="store_true",
        help="report: include every observation under the reported keys, oldest first",
    )
    matrix.add_argument(
        "--package-version", default=None, help="record: the package version half of the key"
    )
    matrix.add_argument(
        "--system-build", default=None, help="record: the system build half of the key"
    )
    matrix.add_argument("--run-config", default=None, help="record: the run configuration half of the key")
    matrix.add_argument(
        "--authorisation",
        default=None,
        help="record: own-source or licensed-with-evidence (required; PRD 12)",
    )
    matrix.add_argument(
        "--trust-category",
        default=None,
        help="record: the support category this observation was made under (required; PRD 5.1)",
    )
    matrix.add_argument(
        "--note", default="", help="record: operator note stored on the entry (no secrets, PRD 9.2)"
    )
    matrix.add_argument("--matrix", default=None, help=MATRIX_HELP)
    matrix.set_defaults(handler=cmd_matrix)

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
    if command is not None and command not in SUPPORTED_COMMANDS:
        error = UniPadError(
            ErrorCode.UNSUPPORTED_COMMAND,
            "unknown command",
            command=command,
            known_commands=list(SUPPORTED_COMMANDS),
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
                # One list, the same one the unknown-command envelope reports.
                # Naming the nine here and the twelve there meant a caller that
                # learned the list from a typo was told a different set when it
                # simply forgot the command -- and neither answer said which
                # surface the nine belong to.  The help text carries that split;
                # both error envelopes answer "what do you accept" identically.
                known_commands=list(SUPPORTED_COMMANDS),
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
