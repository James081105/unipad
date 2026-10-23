"""Install orchestration for the IPA chain (PRD 9.1 / 10).

``IpaInstaller`` drives one package through the PRD 9.1 stages:

``received`` -> ``checked`` -> ``transferred`` -> ``installed`` -> ``registered``

and returns a :class:`unipad.models.InstallResult` whose ``stage`` is the
furthest stage *actually completed*.  Every stage transition persists an
``install-record.json`` (``InstallResult.to_dict()``) into the run directory
plus an append-only ``install-events.jsonl`` audit trail.

Hard rule (PRD 9.1): **file transfer success never reports ``installed``.**
Only the guest install service may move the stage past ``transferred``; this
module therefore refuses to invent that transition even when the transport
verified the package perfectly.  See :meth:`IpaInstaller._stage_transferred`.

``trust_mode`` is whatever the guest reports.  It is never assumed, defaulted
or inferred: an absent value stays ``None`` in the record (PRD 9.1 requires
the install interface to report "信任方式" as a fact, not a guess).

The guest channel is duck-typed (``send_package`` + ``request_install``) so
this module does not import :mod:`unipad.guest` at import time.  A default
channel may be discovered lazily from that package; when it is unavailable
the installer reports ``blocked`` instead of pretending a channel exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from ..errors import ErrorCode, UniPadError
from ..models import InstallResult
from .faults import InstallStage, classify, fault_codes
from .package import PackageInfo, open_ipa
from .precheck import PrecheckReport, run_precheck_package
from .transfer import GuestTransport, HostSideTransfer, TransferReceipt
__all__ = [
    "InstallChannel",
    "InstallStore",
    "GuestClientChannel",
    "GuestReply",
    "IpaInstaller",
    "load_guest_module",
]

RECORD_NAME = "install-record.json"
EVENTS_NAME = "install-events.jsonl"

#: Guest reply statuses that mean "the install service finished the job".
_SUCCESS_STATUSES = frozenset({"installed", "registered", "ok", "success", "done", "complete", "completed"})
_REGISTERED_STATUSES = frozenset({"registered", "icon_registered"})

#: Guest-reported error identifiers mapped onto the stable taxonomy (PRD 10).
_GUEST_ERROR_CODES: dict[str, str] = {
    "signature_or_policy_rejected": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "signature_rejected": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "policy_rejected": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "untrusted": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "install_service_unavailable": ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
    "registration_failed": ErrorCode.REGISTRATION_FAILED,
    "missing_dependency": ErrorCode.MISSING_DEPENDENCY,
    "dyld_failed": ErrorCode.DYLD_FAILED,
    "transfer_failed": ErrorCode.TRANSFER_FAILED,
    "invalid_package": ErrorCode.INVALID_PACKAGE,
    "unsupported_platform": ErrorCode.UNSUPPORTED_PLATFORM,
    "unsupported_arch": ErrorCode.UNSUPPORTED_ARCH,
    "render_failed": ErrorCode.RENDER_FAILED,
    "input_failed": ErrorCode.INPUT_FAILED,
    "blocked": ErrorCode.BLOCKED,
}

#: Guest reply statuses mapped onto a fault code when no code is given.
_STATUS_ERROR_CODES: dict[str, str] = {
    "unavailable": ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
    "no_service": ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
    "timeout": ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
    "rejected": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "denied": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "forbidden": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "registration_failed": ErrorCode.REGISTRATION_FAILED,
    "missing_dependency": ErrorCode.MISSING_DEPENDENCY,
    "dyld_failed": ErrorCode.DYLD_FAILED,
    "blocked": ErrorCode.BLOCKED,
}


@runtime_checkable
class InstallChannel(Protocol):
    """Guest-side channel used by the installer.

    ``send_package`` is required (see :class:`unipad.ipa.transfer.GuestTransport`).
    ``request_install`` is what actually installs and registers the package;
    it may raise, and it may return a mapping, an object, or a JSON string.
    """

    def send_package(self, name: str, data: bytes) -> Any:
        """Move one package into the guest and return receiver evidence."""
        ...

    def request_install(self, bundle_meta: Mapping[str, Any]) -> Any:
        """Ask the guest install service to install and register a package."""
        ...


class GuestClientChannel:
    """Adapt :class:`unipad.guest.GuestClient` to :class:`InstallChannel`.

    The guest package deliberately splits the two channels (PRD 9.1): package
    *bytes* travel on a byte channel owned by :mod:`unipad.ipa.transfer`, while
    ``install``/``status``/``challenge`` are JSON-Line control messages.  This
    adapter joins them so the installer can drive the real guest client with
    one object:

    * ``send_package`` forwards to the byte channel (a socket-like transport,
      :class:`unipad.guest.fake.FakeTcpPair`, or any
      :class:`unipad.ipa.transfer.GuestTransport`) and keeps the bytes;
    * ``request_install`` replays those bytes into ``install_package`` for the
      client's delivery digest, and returns the guest's
      ``InstallResponse`` -- whose ``stage`` is already the PRD 9.1 stage, so
      no re-interpretation happens here.

    Without a byte channel the adapter reports an *unverified* receipt, which
    :class:`unipad.ipa.transfer.HostSideTransfer` rejects as
    ``transfer_failed``: a control message alone can never stand in for a
    verified package delivery.
    """

    def __init__(self, client: Any, byte_channel: GuestTransport | None = None) -> None:
        """Wrap a guest client together with the byte channel that feeds it."""
        self._client = client
        self._byte_channel = byte_channel
        self._delivered: bytes | None = None

    @property
    def client(self) -> Any:
        """The wrapped guest client."""
        return self._client

    @property
    def byte_channel(self) -> GuestTransport | None:
        """The byte channel used for package delivery."""
        return self._byte_channel

    @byte_channel.setter
    def byte_channel(self, value: GuestTransport | None) -> None:
        """Replace the byte channel (and drop any remembered package bytes)."""
        self._byte_channel = value
        self._delivered = None

    def send_package(self, name: str, data: bytes) -> TransferReceipt:
        """Deliver package bytes and remember them for the install request."""
        self._delivered = bytes(data)
        if self._byte_channel is None:
            return TransferReceipt(name=name, sha256="", size_bytes=len(data), verified=False)
        return self._byte_channel.send_package(name, data)

    def request_install(self, bundle_meta: Mapping[str, Any]) -> Any:
        """Ask the guest install service, replaying the delivered bytes."""
        if self._delivered is None:
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "no package bytes were delivered before the install request",
                reason="package_not_delivered",
            )
        install_package = getattr(self._client, "install_package", None)
        if not callable(install_package):
            raise UniPadError(
                ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
                "guest client does not expose install_package",
                reason="guest_client_incompatible",
            )
        return install_package(self._delivered, dict(bundle_meta))


@runtime_checkable
class InstallStore(Protocol):
    """Minimal store surface the installer needs (PRD 7.3 experiment journal).

    ``unipad.controller`` owns the real implementation; anything exposing
    ``run_dir`` and/or ``record_install``/``record`` works, and the installer
    degrades to writing into the resolved run directory when a store does not
    implement them.
    """

    run_dir: Any

    def record_install(self, result: InstallResult) -> None:
        """Persist an install stage transition into the experiment journal."""
        ...


@dataclass
class GuestReply:
    """Normalised guest install reply.

    ``status`` is the raw status string the guest used; the booleans are the
    normalised interpretation.  ``error_code`` is a stable taxonomy code and
    ``raw`` keeps the original mapping for evidence.
    """

    status: str = ""
    installed: bool = False
    registered: bool = False
    trust_mode: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """True when the guest reported an install or a registration."""
        return self.installed or self.registered

    def to_dict(self) -> dict[str, Any]:
        """Return the normalised reply as a mapping (never the guest's raw payload)."""
        return {
            "status": self.status,
            "installed": self.installed,
            "registered": self.registered,
            "trust_mode": self.trust_mode,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


class IpaInstaller:
    """Run one IPA through the PRD 9.1 install chain.

    Args:
        channel: Guest channel (``send_package`` + ``request_install``), or a
            :class:`unipad.guest.GuestClient` (auto-wrapped by
            :class:`GuestClientChannel`).  When omitted, a channel is
            discovered lazily from :mod:`unipad.guest`; if that package is
            missing the installer raises ``blocked``.
        store: Optional experiment store (duck-typed).  Used for the run
            directory and for journal recording.
        run_id: Experiment/run identifier used to resolve ``runs/<run_id>``.
        run_dir: Explicit run directory (wins over ``store.run_dir``).
        byte_channel: Byte transport used when ``channel`` is a
            ``GuestClient`` (see :class:`GuestClientChannel`).
        chunk_size: Transfer chunk size in bytes.
    """

    def __init__(
        self,
        channel: InstallChannel | None = None,
        store: InstallStore | None = None,
        run_id: str | None = None,
        run_dir: str | Path | None = None,
        *,
        byte_channel: GuestTransport | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        """Create an installer bound to one guest channel."""
        self._channel = _adapt_channel(channel, byte_channel)
        self._store = store
        self._run_id = run_id
        self._explicit_run_dir = Path(run_dir) if run_dir is not None else None
        self._transfer = HostSideTransfer(self._channel, chunk_size=chunk_size)
        self._run_dir = self._resolve_run_dir()

    # -- public API -------------------------------------------------------

    @property
    def channel(self) -> InstallChannel:
        """The guest channel in use."""
        return self._channel

    @property
    def run_dir(self) -> Path | None:
        """Directory that receives ``install-record.json`` (may be None)."""
        return self._run_dir

    def install(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        precheck: PrecheckReport | None = None,
        signature_mode: str | None = None,
    ) -> InstallResult:
        """Install one IPA and report the furthest completed stage.

        Args:
            path: ``.ipa`` file (read-only).
            name: Guest-side package name (defaults to the file name).
            precheck: Optional already-computed report, so a caller that
                prechecked does not hash and unzip the package twice.
            signature_mode: What the host *declares* about the package's
                signature.  Defaults to what the Mach-O actually shows
                (``code-signature-present`` / ``unsigned``).  This is a
                declaration, not a trust decision: how the guest trusts the
                package is reported back separately as ``trust_mode``.

        Returns:
            InstallResult: with ``stage`` in
            ``received|checked|transferred|installed|registered|failed``.
            A rejected package, a failed transfer and a guest-side failure
            all come back as ``stage='failed'`` with a stable ``error_code``.

        Raises:
            UniPadError: ``blocked`` when no guest channel could be resolved,
                or ``config_invalid`` for a nonsensical ``chunk_size``.
        """
        package_path = Path(path)
        result = InstallResult(package_sha256="", stage=InstallStage.RECEIVED.value)
        self._emit(result)

        package = self._open_package(package_path, result)
        if package is None:
            return result
        result.package_sha256 = package.sha256
        result.bundle_id = package.bundle_id
        self._emit(result)

        report = precheck if precheck is not None else run_precheck_package(package)
        if report.verdict != "accept":
            codes = fault_codes(report.faults)
            result.stage = InstallStage.FAILED.value
            result.error_code = codes[0] if codes else ErrorCode.INVALID_PACKAGE
            result.error_message = "precheck rejected: " + "; ".join(
                f.label for f in report.faults
            )
            self._emit(result)
            return result
        result.stage = InstallStage.CHECKED.value
        self._emit(result)

        receipt = self._transfer_package(package_path, package, name, result)
        if receipt is None:
            return result
        result.stage = InstallStage.TRANSFERRED.value
        self._emit(result)

        return self._install_in_guest(package, receipt, result, signature_mode)

    # -- stages -----------------------------------------------------------

    def _open_package(self, package_path: Path, result: InstallResult) -> PackageInfo | None:
        """Stage ``received``: open the package and record its hash."""
        try:
            package = open_ipa(package_path)
        except UniPadError as exc:
            result.stage = InstallStage.FAILED.value
            result.error_code = exc.code
            result.error_message = exc.message
            self._emit(result)
            return None
        except Exception as exc:  # defensive: never leak a raw traceback as a verdict
            result.stage = InstallStage.FAILED.value
            result.error_code = classify(exc)
            result.error_message = f"package could not be read: {type(exc).__name__}"
            self._emit(result)
            return None
        return package

    def _transfer_package(
        self,
        package_path: Path,
        package: PackageInfo,
        name: str | None,
        result: InstallResult,
    ) -> TransferReceipt | None:
        """Stage ``transferred``: send the package and verify its hash."""
        try:
            return self._transfer.send_file(package_path, name=name)
        except UniPadError as exc:
            result.stage = InstallStage.FAILED.value
            result.error_code = exc.code
            result.error_message = exc.message
            self._emit(result)
            return None

    def _install_in_guest(
        self,
        package: PackageInfo,
        receipt: TransferReceipt,
        result: InstallResult,
        signature_mode: str | None = None,
    ) -> InstallResult:
        """Stages ``installed`` / ``registered``: ask the guest install service.

        The guest owns these stages.  Whatever it reports is recorded
        verbatim; an unrecognised reply is a failure, never a success.
        """
        meta = _bundle_meta(package, receipt, signature_mode)
        try:
            reply = self._channel.request_install(meta)
        except UniPadError as exc:
            result.stage = InstallStage.FAILED.value
            result.error_code = exc.code
            result.error_message = exc.message
            self._emit(result)
            return result
        except ImportError as exc:
            result.stage = InstallStage.FAILED.value
            result.error_code = ErrorCode.BLOCKED
            result.error_message = f"guest install service is unavailable: {exc}"
            self._emit(result)
            return result
        except Exception as exc:
            result.stage = InstallStage.FAILED.value
            result.error_code = classify(exc, default=ErrorCode.INSTALL_SERVICE_UNAVAILABLE)
            result.error_message = (
                f"guest install service raised {type(exc).__name__}"
            )
            self._emit(result)
            return result

        normalised = _normalise_reply(reply)
        result.trust_mode = normalised.trust_mode
        if normalised.registered:
            result.stage = InstallStage.REGISTERED.value
        elif normalised.installed:
            result.stage = InstallStage.INSTALLED.value
        else:
            result.stage = InstallStage.FAILED.value
            result.error_code = normalised.error_code or ErrorCode.BLOCKED
            result.error_message = normalised.error_message or (
                f"guest did not confirm an install (status={normalised.status or 'none'})"
            )
        self._emit(result)
        return result

    # -- persistence ------------------------------------------------------

    def _emit(self, result: InstallResult) -> None:
        """Persist one stage transition (record file + journal hook)."""
        self._record_in_store(result)
        if self._run_dir is None:
            return
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
            record_path = self._run_dir / RECORD_NAME
            record_path.write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            result.log_path = str(record_path)
            with (self._run_dir / EVENTS_NAME).open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": _utc_now(),
                            "stage": result.stage,
                            "bundle_id": result.bundle_id,
                            "package_sha256": result.package_sha256,
                            "error_code": result.error_code,
                            "error_message": result.error_message,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
        except OSError:
            # Evidence persistence is best-effort: the in-memory result and
            # the returned contract stay authoritative.  Never fail an
            # install because a log file could not be written.
            return

    def _record_in_store(self, result: InstallResult) -> None:
        """Hand the transition to the experiment store, if it accepts one.

        A *snapshot* is passed, not the live result: the installer advances one
        mutable ``InstallResult`` through every stage, so handing the object
        itself would leave a journal store with N references to the final
        state and no record of the transitions (PRD 7.3).

        A store that raises is *not* swallowed.  The install itself has already
        happened by the time this runs, so failing the whole run would report a
        success as a failure; but discarding the record would mean the history
        PRD 3.2 P1 (3) asks for silently loses an entry, and nothing anywhere
        would say so.  The failure is therefore recorded on the result, where
        the evidence bundle and the verifier both read it.
        """
        store = self._store
        if store is None:
            return
        snapshot = replace(result)
        for method_name in ("record_install", "record"):
            method = getattr(store, method_name, None)
            if callable(method):
                try:
                    method(snapshot)
                except Exception as exc:
                    # Only the first failure is recorded: a store that raises
                    # once will raise again, and one note is the signal.
                    if result.error_code is None:
                        result.error_code = ErrorCode.INSTALL_SERVICE_UNAVAILABLE
                        result.error_message = (
                            f"install history could not be recorded: {type(exc).__name__}"
                        )
                return

    def _resolve_run_dir(self) -> Path | None:
        """Resolve where install evidence is written (may be None)."""
        if self._explicit_run_dir is not None:
            return self._explicit_run_dir
        store_dir = getattr(self._store, "run_dir", None)
        if store_dir:
            try:
                return Path(str(store_dir))
            except TypeError:
                return None
        if self._run_id:
            root = _repo_root()
            if root is not None:
                return root / "runs" / self._run_id
            return Path("runs") / self._run_id
        return None


# --------------------------------------------------------------------------
# module-level helpers
# --------------------------------------------------------------------------


def load_guest_module() -> Any:
    """Import and return :mod:`unipad.guest` lazily.

    The guest package is a sibling of this one and is optional at import
    time (the host must run without a guest present).

    Returns:
        module: the imported ``unipad.guest`` module.

    Raises:
        UniPadError: ``blocked`` when the guest package cannot be imported.
    """
    try:
        import unipad.guest as guest  # lazy on purpose: see module docstring
    except ImportError as exc:
        # Constructed directly (not via ``errors.blocked``) so the detail key
        # stays ``reason`` like every other fault in this package.
        raise UniPadError(
            ErrorCode.BLOCKED,
            "guest package is unavailable",
            reason="guest_import_failed",
            detail=str(exc),
        ) from exc
    return guest


def _default_channel() -> InstallChannel:
    """Discover a guest install channel from :mod:`unipad.guest`.

    Raises:
        UniPadError: ``blocked`` when the guest package is missing or exposes
            no channel factory.
    """
    guest = load_guest_module()
    searched = [
        "create_install_channel",
        "default_channel",
        "connect",
        "GuestChannel",
        "GuestClient",
    ]
    for attribute in searched:
        candidate = getattr(guest, attribute, None)
        if candidate is None:
            continue
        if callable(candidate):
            try:
                return candidate()
            except TypeError:
                continue
        return candidate
    raise UniPadError(
        ErrorCode.BLOCKED,
        "guest package exposes no install channel factory",
        reason="guest_channel_missing",
        searched=searched,
    )


def _bundle_meta(
    package: PackageInfo,
    receipt: TransferReceipt,
    signature_mode: str | None = None,
) -> dict[str, Any]:
    """Build the bundle metadata handed to the guest install service.

    The first five keys are exactly :class:`unipad.guest.protocol.InstallRequest`,
    so the mapping can be passed straight to ``GuestClient.install_package``.
    The remaining keys are diagnostics for channels that want them.
    """
    return {
        # --- InstallRequest contract (unipad.guest.protocol) ---
        "package_name": Path(package.path).name,
        "package_sha256": package.sha256,
        "size_bytes": package.size_bytes,
        "declared_min_os": package.min_os or "unspecified",
        "signature_mode": signature_mode or _declared_signature_mode(package),
        # --- diagnostics (ignored by InstallRequest.from_dict) ---
        "bundle_id": package.bundle_id,
        "app_name": package.app_name,
        "app_dir": package.app_dir,
        "executable": package.executable,
        "package_size_bytes": package.size_bytes,
        "min_os": package.min_os,
        "supported_platforms": list(package.supported_platforms),
        "device_family": list(package.device_family),
        "required_capabilities": list(package.required_capabilities),
        "privacy_keys": list(package.privacy_keys),
        "frameworks": list(package.frameworks),
        "linked_dylibs": list(package.linked_dylibs),
        "embedded_dylibs": list(package.dylibs),
        "rpaths": list(package.rpaths),
        "transfer": receipt.to_dict(),
    }


def _declared_signature_mode(package: PackageInfo) -> str:
    """Report what the Mach-O *shows* about signing -- never a trust claim.

    The presence of ``LC_CODE_SIGNATURE`` is observable; its validity is not,
    so this value describes the artifact only.  How the guest trusts the
    package comes back separately as ``InstallResult.trust_mode``.
    """
    slices = package.macho.slices if package.macho is not None else []
    signed = any(s.has_code_signature for s in slices)
    return "code-signature-present" if signed else "unsigned"


def _adapt_channel(
    channel: InstallChannel | None,
    byte_channel: GuestTransport | None,
) -> InstallChannel:
    """Wrap a ``GuestClient``-shaped object, or discover a channel lazily."""
    if channel is None:
        return _default_channel()
    if hasattr(channel, "install_package") and not hasattr(channel, "send_package"):
        return GuestClientChannel(channel, byte_channel)
    return channel


def _normalise_reply(reply: Any) -> GuestReply:
    """Normalise a guest reply into a :class:`GuestReply`.

    Accepts a mapping, a JSON string, or any object exposing the same
    attributes.  Ambiguity resolves to failure: only an explicit
    ``installed``/``registered``/``ok`` marker counts as success, and
    ``trust_mode`` is passed through untouched (never invented).
    """
    data: dict[str, Any] = {}
    if isinstance(reply, Mapping):
        data = {str(k): v for k, v in reply.items()}
    elif isinstance(reply, (str, bytes)):
        try:
            parsed = json.loads(reply)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, Mapping):
            data = {str(k): v for k, v in parsed.items()}
    elif reply is not None:
        data = {
            key: getattr(reply, key)
            for key in (
                "status",
                "stage",
                "installed",
                "registered",
                "trust_mode",
                "error_code",
                "error",
                "message",
                "reason",
                "detail",
                "ok",
            )
            if hasattr(reply, key)
        }

    if not data:
        return GuestReply(
            status="",
            error_code=ErrorCode.BLOCKED,
            error_message="guest install service returned no recognisable reply",
        )

    status = str(data.get("status") or data.get("stage") or "").strip().lower()
    installed = _as_bool(data.get("installed"))
    registered = _as_bool(data.get("registered"))
    ok = _as_bool(data.get("ok"))
    if ok and not installed and not registered:
        installed = True
    if status in _REGISTERED_STATUSES:
        registered = True
    if status in _SUCCESS_STATUSES and not installed and not registered:
        installed = True

    trust_mode = data.get("trust_mode")
    error_code = data.get("error_code") or data.get("error")
    message = data.get("message") or data.get("detail") or data.get("reason")

    normalised = GuestReply(
        status=status,
        installed=installed,
        registered=registered,
        trust_mode=str(trust_mode) if isinstance(trust_mode, str) and trust_mode.strip() else None,
        error_code=None,
        error_message=str(message) if message else None,
        raw=data,
    )
    if normalised.succeeded:
        return normalised

    mapped = _map_guest_error(error_code, status)
    normalised.error_code = mapped or ErrorCode.BLOCKED
    if not normalised.error_message:
        normalised.error_message = (
            f"guest reported status={status or 'none'}"
            + (f", code={error_code}" if error_code else "")
        )
    return normalised


def _map_guest_error(error_code: Any, status: str) -> str | None:
    """Map a guest-reported error identifier onto the stable taxonomy."""
    if isinstance(error_code, str) and error_code.strip():
        key = error_code.strip().lower()
        if key in _GUEST_ERROR_CODES:
            return _GUEST_ERROR_CODES[key]
        if key in _STATUS_ERROR_CODES:
            return _STATUS_ERROR_CODES[key]
        # An unknown guest code is recorded as blocked rather than guessed.
        return ErrorCode.BLOCKED
    if status and status in _STATUS_ERROR_CODES:
        return _STATUS_ERROR_CODES[status]
    return None


def _as_bool(value: Any) -> bool:
    """Interpret a reply flag as a boolean without inventing truth."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "ok", "installed", "registered")
    if isinstance(value, int):
        return value != 0
    return False


def _utc_now() -> str:
    """Current UTC timestamp in ISO-8601 (evidence only, never a verdict)."""
    return datetime.now(timezone.utc).isoformat()


def _repo_root() -> Path | None:
    """Locate the repository root from this file (``src/unipad/ipa/``)."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "docs" / "PRD.md").is_file():
            return candidate
    return None
