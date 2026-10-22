"""In-memory fake guest install service (PRD sections 5.2, 7, 9, 10).

``FakeGuestService`` is the backbone of every simulation/acceptance test that
does not have real hardware: it models the *guest side* of the IPA install
chain with the same state progression the real guest must exhibit --

    validate structure -> transfer check -> trust -> container -> icon/registration
    -> dependency resolution -> dyld entry readiness -> stage ``registered``

and it can inject each PRD section 10 failure at the matching point so the
host controller and verifier can be exercised against the real fault taxonomy
instead of a happy-path stub.

What it deliberately does **not** do: it never reports success it did not
earn.  ``install()`` returns ``stage='registered'`` only after the container
exists, the icon is registered and the trust decision was actually recorded.
``trust_mode`` is whatever the injected policy decided and is *recorded*,
never assumed.

Persistence (``persist`` / ``load``) simulates the persistent Data volume for
M9b: a brand-new service instance loading the same directory sees the same
installed apps and containers, exactly as a fresh QEMU process would.

Transports:

- ``FakeTransport``  -- in-process bridge, ``request(dict) -> dict``.
- ``FakeTcpPair``    -- a real loopback TCP server (127.0.0.1, ephemeral
  port) speaking the same JSON Lines protocol in a background thread, so
  host-side socket code is exercised for real.

Dependency direction: imports :mod:`unipad.errors` and
:mod:`unipad.guest.protocol` only.  It must never import :mod:`unipad.ipa`
or :mod:`unipad.host`.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.protocol import (
    FAILURE_CODES,
    MSG_CHALLENGE_REQUEST,
    MSG_CHALLENGE_RESPONSE,
    MSG_ERROR,
    MSG_INSTALL_REQUEST,
    MSG_INSTALL_RESPONSE,
    MSG_STATUS_REQUEST,
    MSG_STATUS_RESPONSE,
    ChallengeResponse,
    InstallProgress,
    InstallRequest,
    InstallResponse,
    Message,
    StatusResponse,
    error_message,
)

#: Trust mode the fake reports when the injected policy accepts a self-signed
#: package.  A recorded observation, not a claim about any real guest.
DEFAULT_TRUST_MODE = "self-signed-dev-trust"

#: Persisted state schema version (bump on incompatible layout changes).
STATE_SCHEMA_VERSION = 1

#: Failure points the install chain can be interrupted at, in execution order.
INSTALL_FAILURE_POINTS: tuple[str, ...] = (
    "structure",
    "transfer",
    "signature",
    "install_service",
    "registration",
    "dependency",
    "dyld",
)


def derive_challenge_value(bundle_id: str, nonce: str) -> str:
    """Deterministically derive an app's challenge value.

    The value is ``sha256(app_seed || nonce)`` where ``app_seed`` is derived
    from the bundle id.  It is a *simulation fixture*: reproducible so M11
    evidence can compare expected against actual, and it contains no real
    credential (no token, key or user document is stored anywhere).

    Args:
        bundle_id: the app the challenge is answered for.
        nonce: the per-run nonce supplied by the host.

    Returns:
        str: 64-char lowercase hex digest.
    """
    return hashlib.sha256(_app_seed(bundle_id) + nonce.encode("utf-8")).hexdigest()


def _app_seed(bundle_id: str) -> bytes:
    """Derive the per-app simulation seed (fixture data, not a secret)."""
    return hashlib.sha256(("unipad-fake-guest-app-seed:" + bundle_id).encode("utf-8")).digest()


class FakeGuestService:
    """In-memory guest that installs apps and answers status/challenge queries.

    Args:
        guest_os: OS version this fake guest reports (e.g. ``"17.7.11"``).
        guest_build: build string reported alongside the OS version.
        trust_policy: how packages are trusted.  Keys:
            ``accept_modes`` (list of accepted ``signature_mode`` values),
            ``trust_mode`` (recorded trust label), ``accept`` (master switch).
        failures: failure injection config -- ``{"fail_at": <point>,
            "mode": "always"|"once"}``.  ``fail_at`` must be one of
            :data:`INSTALL_FAILURE_POINTS` or a PRD 10 taxonomy name.
        device_id: opaque simulation device label (never a real identifier).
    """

    def __init__(
        self,
        *,
        guest_os: str = "17.7.11",
        guest_build: str = "21H161",
        trust_policy: Optional[Mapping[str, Any]] = None,
        failures: Optional[Mapping[str, Any]] = None,
        device_id: str = "ipad7,1-sim",
    ) -> None:
        self.guest_os = guest_os
        self.guest_build = guest_build
        self.device_id = device_id

        policy = dict(trust_policy or {})
        self.trust_accept_modes: list[str] = list(policy.get("accept_modes", ["self"]))
        self.trust_mode_label: str = str(policy.get("trust_mode", DEFAULT_TRUST_MODE))
        self.trust_accept: bool = bool(policy.get("accept", True))

        self._failures: dict[str, Any] = self._normalize_failures(failures)

        # --- guest state ---
        self.installed: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.challenge_log: list[dict[str, Any]] = []
        self.trust_log: list[dict[str, Any]] = []
        self.install_log: list[dict[str, Any]] = []
        self.progress_log: list[dict[str, Any]] = []
        self._staging: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # --- failure injection ---

    @staticmethod
    def _normalize_failures(failures: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        """Validate the injection config, rejecting unknown failure points."""
        if not failures:
            return {}
        cfg = dict(failures)
        point = cfg.get("fail_at")
        if point is None:
            return {}
        if point not in INSTALL_FAILURE_POINTS and point not in FAILURE_CODES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown failure injection point",
                fail_at=point,
                known_points=list(INSTALL_FAILURE_POINTS) + sorted(FAILURE_CODES),
            )
        mode = cfg.get("mode", "always")
        if mode not in ("always", "once"):
            raise UniPadError(ErrorCode.CONFIG_INVALID, "failure mode must be 'always' or 'once'", mode=mode)
        cfg["mode"] = mode
        return cfg

    def set_failures(self, failures: Optional[Mapping[str, Any]]) -> None:
        """Replace the active failure injection config (``None`` clears it)."""
        with self._lock:
            self._failures = self._normalize_failures(failures)

    def _injected(self, point: str) -> Optional[str]:
        """Return the stable error code to fail with, consuming a ``once`` failure."""
        cfg = self._failures
        if not cfg:
            return None
        if cfg.get("fail_at") != point:
            return None
        code = FAILURE_CODES.get(point)
        if code is None:
            # 'structure' and 'transfer' are precheck/transfer points.
            code = ErrorCode.INVALID_PACKAGE if point == "structure" else ErrorCode.TRANSFER_FAILED
        if cfg.get("mode") == "once":
            self._failures = {}
        return code

    # --- package staging (simulates the byte transfer channel) ---

    def stage_package(self, package_bytes: bytes, meta: Mapping[str, Any]) -> dict[str, Any]:
        """Deliver package bytes on the (simulated) byte channel.

        This stands in for ``unipad.ipa.transfer``: the bytes land in a guest
        staging area keyed by their digest.  ``install()`` picks them up.

        Raises:
            UniPadError: ``transfer_failed`` when the delivered bytes do not
                match the declared size or SHA-256.
        """
        request = InstallRequest.from_dict(meta)
        if not isinstance(package_bytes, (bytes, bytearray)):
            raise UniPadError(ErrorCode.TRANSFER_FAILED, "package bytes must be a bytes-like object")
        actual_hash = hashlib.sha256(package_bytes).hexdigest()
        if len(package_bytes) != request.size_bytes:
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "delivered package size does not match declared size_bytes",
                declared=request.size_bytes,
                actual=len(package_bytes),
            )
        if actual_hash != request.package_sha256.lower():
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "delivered package sha256 does not match declared package_sha256",
                declared=request.package_sha256,
                actual=actual_hash,
            )
        with self._lock:
            self._staging[actual_hash] = {
                "bytes": bytes(package_bytes),
                "meta": request.to_dict(),
            }
        return {"staged_sha256": actual_hash, "size_bytes": len(package_bytes)}

    # --- install chain ---

    def install(self, package_name: str, data: Optional[bytes], meta: Mapping[str, Any]) -> InstallResponse:
        """Run the install chain and return the furthest stage actually reached.

        Args:
            package_name: file name of the package (used to derive the bundle id
                when the metadata does not carry one).
            data: staged package bytes, or ``None`` when the caller did not
                stage any (structure validation is then skipped).
            meta: install metadata (:class:`~unipad.guest.protocol.InstallRequest`
                fields).

        Returns:
            InstallResponse: ``stage='registered'`` on full success, otherwise
            ``stage='failed'`` with the guest's verbatim taxonomy code.

        Invariant:
            a non-None ``error_code`` always comes with ``stage='failed'`` and
            the app is **not** left in :attr:`installed` -- a late-chain failure
            (``dependency``/``dyld``) withdraws the registration so a verifier's
            ``status()`` can never contradict a reported failure.  Use
            :meth:`launch` to model a launch failure of an app that installed
            successfully.
        """
        with self._lock:
            request = self._coerce_meta(meta, package_name)
            bundle_id = self._bundle_id_for(package_name, meta)

            self._progress("received", f"package {package_name}")
            self._progress("checked", f"bundle {bundle_id}")

            # 1. structure validation (basic, guest-side)
            code = self._injected("structure")
            if code is None and data is not None:
                code = self._check_structure(data)
            if code is not None:
                return self._fail(bundle_id, code, "package structure rejected")

            # 2. transfer verification against the declared digest
            code = self._injected("transfer")
            if code is None and data is not None:
                code = self._check_transfer(data, request)
            if code is not None:
                return self._fail(bundle_id, code, "package transfer verification failed")

            # 3. signature / trust policy
            code = self._injected("signature")
            if code is None:
                code = self._evaluate_trust(request, bundle_id)
            if code is not None:
                return self._fail(bundle_id, code, "package signature or policy rejected")

            # 4. install service availability (container creation)
            code = self._injected("install_service")
            if code is not None:
                return self._fail(bundle_id, code, "install service unavailable")

            container: dict[str, Any] = {"version": "1.0", "markers": {}, "files": []}

            # 5. registration: icon + container establishment
            code = self._injected("registration")
            if code is not None:
                return self._fail(bundle_id, code, "application registration failed")
            self.installed[bundle_id] = {
                "version": "1.0",
                "icon": True,
                "container": container,
                "package_name": package_name,
                "package_sha256": request.package_sha256,
                "size_bytes": request.size_bytes,
                "declared_min_os": request.declared_min_os,
                "signature_mode": request.signature_mode,
                "trust_mode": self.trust_mode_label,
            }
            self.containers[bundle_id] = container
            self._progress("installed", f"container for {bundle_id}")

            # 6. dependency resolution
            code = self._injected("dependency")
            if code is not None:
                return self._rollback(bundle_id, code, "required dependency missing")

            # 7. dyld entry readiness
            code = self._injected("dyld")
            if code is not None:
                return self._rollback(bundle_id, code, "entry point did not execute")

            self._progress("registered", f"icon registered for {bundle_id}")
            self.install_log.append({"bundle_id": bundle_id, "package_sha256": request.package_sha256, "stage": "registered"})
            return InstallResponse(
                stage="registered",
                bundle_id=bundle_id,
                trust_mode=self.trust_mode_label,
            )

    def launch(self, bundle_id: str) -> dict[str, Any]:
        """Check that an installed app can actually start (M11 precondition).

        Re-runs the dependency and dyld checks so a launch-time failure is
        distinguishable from an install-time one.

        Raises:
            UniPadError: ``missing_dependency`` / ``dyld_failed`` per injection,
                ``registration_failed`` when the app is not installed.
        """
        with self._lock:
            if bundle_id not in self.installed:
                raise UniPadError(
                    ErrorCode.REGISTRATION_FAILED,
                    "application is not installed",
                    bundle_id=bundle_id,
                )
            code = self._injected("dependency")
            if code is not None:
                raise UniPadError(code, "required dependency missing", bundle_id=bundle_id)
            code = self._injected("dyld")
            if code is not None:
                raise UniPadError(code, "entry point did not execute", bundle_id=bundle_id)
            return {
                "bundle_id": bundle_id,
                "stage": "running",
                "trust_mode": self.installed[bundle_id].get("trust_mode"),
            }

    def uninstall(self, bundle_id: str) -> dict[str, Any]:
        """Remove an app and its container (P1 feature, exercised by tests)."""
        with self._lock:
            if bundle_id not in self.installed:
                raise UniPadError(ErrorCode.REGISTRATION_FAILED, "application is not installed", bundle_id=bundle_id)
            del self.installed[bundle_id]
            self.containers.pop(bundle_id, None)
            return {"bundle_id": bundle_id, "stage": "uninstalled"}

    # --- data volume (M9b / M12) ---

    def set_data_marker(self, bundle_id: str, key: str, value: str) -> dict[str, Any]:
        """Write one data marker into an app container (simulates app data)."""
        with self._lock:
            container = self._require_container(bundle_id)
            container["markers"][key] = value
            container["files"].append({"name": key, "size_bytes": len(value.encode("utf-8"))})
            return {"bundle_id": bundle_id, "key": key, "stored": True}

    def get_data_marker(self, bundle_id: str, key: str) -> str:
        """Read one data marker back (simulates reopening the app)."""
        with self._lock:
            container = self._require_container(bundle_id)
            try:
                return str(container["markers"][key])
            except KeyError as exc:
                raise UniPadError(
                    ErrorCode.PERSISTENCE_FAILED,
                    "data marker not present in the app container",
                    bundle_id=bundle_id,
                    key=key,
                ) from exc

    def data_markers(self) -> list[str]:
        """All markers across all containers, as ``bundle_id:key`` strings."""
        with self._lock:
            out: list[str] = []
            for bundle_id in sorted(self.containers):
                for key in sorted(self.containers[bundle_id].get("markers", {})):
                    out.append(f"{bundle_id}:{key}")
            return out

    def status(self) -> StatusResponse:
        """Installed apps plus Data-volume markers (M9b/M12 verification)."""
        with self._lock:
            installed = [
                {
                    "bundle_id": bundle_id,
                    "version": entry.get("version", "1.0"),
                    "icon": bool(entry.get("icon", False)),
                    "trust_mode": entry.get("trust_mode"),
                }
                for bundle_id, entry in sorted(self.installed.items())
            ]
            return StatusResponse(installed=installed, data_markers=self.data_markers())

    # --- challenge (M11) ---

    def challenge(self, nonce: str, bundle_id: Optional[str] = None) -> ChallengeResponse:
        """Answer a challenge for one app, deterministically.

        ``bundle_id`` defaults to the most recently installed app so the
        message-level API (which carries only the nonce) has a stable target.
        """
        if not isinstance(nonce, str) or nonce == "":
            raise UniPadError(ErrorCode.CONFIG_INVALID, "challenge nonce must be a non-empty string")
        with self._lock:
            target = bundle_id or self._last_installed()
            value = derive_challenge_value(target, nonce)
            self.challenge_log.append({"bundle_id": target, "nonce": nonce, "value": value})
            return ChallengeResponse(nonce=nonce, value=value)

    def expected_challenge(self, bundle_id: str, nonce: str) -> str:
        """The value the host expects for ``(bundle_id, nonce)`` (M11 evidence)."""
        return derive_challenge_value(bundle_id, nonce)

    # --- persistence (M9b) ---

    def persist(self, directory: str | Path) -> dict[str, Any]:
        """Serialize guest state into ``directory`` (the persistent Data volume).

        Returns:
            dict: ``{path, schema_version, installed, containers, markers}``.

        Raises:
            UniPadError: ``persistence_failed`` when the state cannot be written.
        """
        target = Path(directory)
        try:
            target.mkdir(parents=True, exist_ok=True)
            with self._lock:
                state = {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "guest_os": self.guest_os,
                    "guest_build": self.guest_build,
                    "device_id": self.device_id,
                    "trust_policy": {
                        "accept_modes": self.trust_accept_modes,
                        "trust_mode": self.trust_mode_label,
                        "accept": self.trust_accept,
                    },
                    "installed": self.installed,
                    "containers": self.containers,
                    "challenge_log": self.challenge_log,
                    "trust_log": self.trust_log,
                    "install_log": self.install_log,
                }
            state_path = target / "guest-state.json"
            state_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise UniPadError(
                ErrorCode.PERSISTENCE_FAILED,
                "failed to persist guest state",
                path=str(target),
            ) from exc
        return {
            "path": str(state_path),
            "schema_version": STATE_SCHEMA_VERSION,
            "installed": len(state["installed"]),
            "containers": len(state["containers"]),
            "markers": len(self.data_markers()),
        }

    def load(self, directory: str | Path) -> dict[str, Any]:
        """Restore guest state written by :meth:`persist`.

        A fresh instance loading the same directory sees the same installed
        apps and containers -- the M9b "new QEMU process reads the data back"
        property.

        Raises:
            UniPadError: ``persistence_failed`` when the state is missing or
                cannot be parsed.
        """
        state_path = Path(directory) / "guest-state.json"
        if not state_path.is_file():
            raise UniPadError(
                ErrorCode.PERSISTENCE_FAILED,
                "persisted guest state not found",
                path=str(state_path),
            )
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UniPadError(
                ErrorCode.PERSISTENCE_FAILED,
                "persisted guest state is unreadable",
                path=str(state_path),
            ) from exc
        if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise UniPadError(
                ErrorCode.PERSISTENCE_FAILED,
                "persisted guest state has an incompatible schema",
                path=str(state_path),
                schema_version=state.get("schema_version") if isinstance(state, dict) else None,
            )
        with self._lock:
            self.guest_os = str(state.get("guest_os", self.guest_os))
            self.guest_build = str(state.get("guest_build", self.guest_build))
            self.device_id = str(state.get("device_id", self.device_id))
            policy = state.get("trust_policy") or {}
            self.trust_accept_modes = list(policy.get("accept_modes", self.trust_accept_modes))
            self.trust_mode_label = str(policy.get("trust_mode", self.trust_mode_label))
            self.trust_accept = bool(policy.get("accept", self.trust_accept))
            self.installed = dict(state.get("installed", {}))
            self.containers = dict(state.get("containers", {}))
            self.challenge_log = list(state.get("challenge_log", []))
            self.trust_log = list(state.get("trust_log", []))
            self.install_log = list(state.get("install_log", []))
        return {"installed": len(self.installed), "containers": len(self.containers)}

    # --- message dispatch (used by both transports) ---

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one message envelope and return the reply envelope as a dict.

        This is the seam both :class:`FakeTransport` and the TCP pair drive, so
        the two transports cannot diverge in behaviour.

        Raises:
            UniPadError: ``qmp_protocol_error`` for a non-conforming envelope.
        """
        envelope = message if isinstance(message, Message) else Message.from_dict(message)
        mtype = envelope.type
        try:
            if mtype == MSG_INSTALL_REQUEST:
                return self._reply(envelope, MSG_INSTALL_RESPONSE, self._handle_install(envelope))
            if mtype == MSG_CHALLENGE_REQUEST:
                return self._reply(envelope, MSG_CHALLENGE_RESPONSE, self._handle_challenge(envelope))
            if mtype == MSG_STATUS_REQUEST:
                return self._reply(envelope, MSG_STATUS_RESPONSE, self.status())
            return error_message(
                ErrorCode.UNSUPPORTED_COMMAND,
                f"unsupported guest message type '{mtype}'",
                message_id=envelope.id,
            ).to_dict()
        except UniPadError as exc:
            return error_message(exc.code, exc.message, message_id=envelope.id).to_dict()

    @staticmethod
    def _reply(envelope: Message, reply_type: str, payload: Any) -> dict[str, Any]:
        """Wrap a payload in a reply envelope echoing the request id."""
        body = payload.to_dict() if hasattr(payload, "to_dict") else dict(payload)
        return Message(type=reply_type, id=envelope.id, payload=body).to_dict()

    def _handle_install(self, envelope: Message) -> InstallResponse:
        request = InstallRequest.from_dict(envelope.payload)
        staged = self._staging.get(request.package_sha256.lower())
        data = staged["bytes"] if staged is not None else None
        response = self.install(request.package_name, data, request.to_dict())
        return response

    def _handle_challenge(self, envelope: Message) -> ChallengeResponse:
        nonce = envelope.payload.get("nonce")
        if not isinstance(nonce, str) or nonce == "":
            raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, "challenge request without a nonce")
        return self.challenge(nonce)

    # --- internals ---

    def _coerce_meta(self, meta: Mapping[str, Any], package_name: str) -> InstallRequest:
        if isinstance(meta, InstallRequest):
            return meta
        if not isinstance(meta, Mapping):
            raise UniPadError(ErrorCode.CONFIG_INVALID, "install metadata must be a mapping")
        data = dict(meta)
        data.setdefault("package_name", package_name)
        try:
            return InstallRequest.from_dict(data)
        except UniPadError as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "install metadata is incomplete or malformed",
                reason=exc.message,
            ) from exc

    @staticmethod
    def _bundle_id_for(package_name: str, meta: Mapping[str, Any] | InstallRequest) -> str:
        """Resolve the bundle id used for this install.

        A caller-supplied ``bundle_id`` wins.  ``unipad.ipa`` parses the real
        bundle id out of ``Info.plist``, and tests that drive the fake directly
        can pass it in ``meta`` -- using it keeps the guest registry, the M11
        challenge evidence and the M9b persistence keys aligned with the host's
        notion of the app.

        Note that the *message* path cannot carry it: :class:`InstallRequest`
        is a frozen five-field contract, so a request arriving over a transport
        has no ``bundle_id`` and falls back to the package file name
        (``Foo.ipa`` -> ``Foo``).  That fallback is a simulation stand-in; the
        authoritative bundle id stays with the host's ``ipa`` layer.
        """
        declared = meta.get("bundle_id") if isinstance(meta, Mapping) else None
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
        stem = (package_name or "unknown").strip()
        if stem.lower().endswith(".ipa"):
            stem = stem[:-4]
        return stem or "unknown.bundle"

    @staticmethod
    def _check_structure(data: bytes) -> Optional[str]:
        """Basic structure check: non-empty, and looks like an IPA/zip container."""
        if len(data) == 0:
            return ErrorCode.INVALID_PACKAGE
        if data[:2] != b"PK" and not data.startswith(b"PKG"):
            return ErrorCode.INVALID_PACKAGE
        return None

    @staticmethod
    def _check_transfer(data: bytes, request: InstallRequest) -> Optional[str]:
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != request.size_bytes or digest != request.package_sha256.lower():
            return ErrorCode.TRANSFER_FAILED
        return None

    def _evaluate_trust(self, request: InstallRequest, bundle_id: str) -> Optional[str]:
        """Apply the injected trust policy and record the decision.

        The recorded ``trust_mode`` is the outcome of this decision; it is
        never assumed to be any particular value.
        """
        accepted = self.trust_accept and request.signature_mode in self.trust_accept_modes
        record = {
            "bundle_id": bundle_id,
            "signature_mode": request.signature_mode,
            "accepted": accepted,
            "trust_mode": self.trust_mode_label if accepted else None,
            "declared_min_os": request.declared_min_os,
            "guest_os": self.guest_os,
        }
        self.trust_log.append(record)
        if not accepted:
            return ErrorCode.SIGNATURE_OR_POLICY_REJECTED
        return None

    def _require_container(self, bundle_id: str) -> dict[str, Any]:
        """Return an app's container, distinguishing "not installed" from "data lost".

        An unknown bundle id is a registration problem; an *installed* app whose
        container is missing means the Data volume lost the app's data, which is
        PRD section 10's ``persistence_failed`` -- not a registration fault.
        """
        if bundle_id not in self.installed:
            raise UniPadError(
                ErrorCode.REGISTRATION_FAILED,
                "application is not installed",
                bundle_id=bundle_id,
            )
        try:
            return self.containers[bundle_id]
        except KeyError as exc:
            raise UniPadError(
                ErrorCode.PERSISTENCE_FAILED,
                "application container is missing from the data volume",
                bundle_id=bundle_id,
            ) from exc

    def _last_installed(self) -> str:
        """Bundle id of the most recently installed app (dicts keep insertion order)."""
        if not self.installed:
            return "unipad.fake.noapp"
        return next(reversed(self.installed))

    def _progress(self, stage: str, detail: str) -> None:
        self.progress_log.append(InstallProgress(stage=stage, detail=detail).to_dict())

    def _fail(self, bundle_id: str, code: str, message: str) -> InstallResponse:
        """Record and return a failed install outcome (never fabricated)."""
        self.install_log.append({"bundle_id": bundle_id, "stage": "failed", "error_code": code})
        return InstallResponse(stage="failed", bundle_id=bundle_id, error_code=code, error_message=message)

    def _rollback(self, bundle_id: str, code: str, message: str) -> InstallResponse:
        """Fail a late-chain step and withdraw the registration.

        ``dependency`` and ``dyld`` sit at the end of the chain: the container
        and icon were already created when they fail.  Withdrawing them keeps
        the invariant that a non-None ``error_code`` always means the app is
        not left installed, so a verifier's ``status()`` can never contradict
        an install that reported a failure.  Use :meth:`launch` to model a
        launch-time failure of an app that *did* install successfully.
        """
        self.installed.pop(bundle_id, None)
        self.containers.pop(bundle_id, None)
        self.install_log.append({"bundle_id": bundle_id, "stage": "failed", "error_code": code, "rolled_back": True})
        return InstallResponse(stage="failed", bundle_id=bundle_id, error_code=code, error_message=message)


class FakeTransport:
    """In-process bridge: drives a :class:`FakeGuestService` over messages.

    Args:
        service: the guest service to drive.
        fail_times: number of leading calls that fail with ``OSError``
            (transport-level), for exercising the client's bounded retries.
    """

    def __init__(self, service: FakeGuestService, *, fail_times: int = 0) -> None:
        self._service = service
        self._fail_times = fail_times
        self.calls: list[dict[str, Any]] = []

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Forward one message envelope to the service and return its reply.

        Raises:
            OSError: while ``fail_times`` leading calls remain, to exercise
                the client's bounded retry path.
        """
        if self._fail_times > 0:
            self._fail_times -= 1
            raise OSError("simulated transport failure")
        self.calls.append(dict(message))
        return self._service.handle(message)

    def close(self) -> None:
        """Nothing to release; present for transport-interface symmetry."""


class _TcpLineClient:
    """Client half of :class:`FakeTcpPair`: JSON Lines over a real socket."""

    def __init__(self, sock: socket.socket, timeout_s: float) -> None:
        self._sock = sock
        self._sock.settimeout(timeout_s)
        self._buffer = b""

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send one message line and block until the reply line arrives.

        Raises:
            OSError: when the peer closes the connection or the socket fails.
        """
        line = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._sock.sendall(line + b"\n")
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise OSError("guest tcp peer closed the connection")
            self._buffer += chunk
        raw, _, self._buffer = self._buffer.partition(b"\n")
        return json.loads(raw.decode("utf-8"))

    def close(self) -> None:
        """Close the client socket (errors are ignored on teardown)."""
        try:
            self._sock.close()
        except OSError:
            pass


class FakeTcpPair:
    """A real loopback TCP server driving a :class:`FakeGuestService`.

    The server thread speaks the same JSON Lines protocol as the in-process
    transport, so host-side socket/QMP-style code is exercised for real.  The
    listener binds ``127.0.0.1`` on an ephemeral port; :meth:`close` tears down
    the client socket, the accepted connection, the listener and the thread.

    Attributes:
        address: ``(host, port)`` the listener is bound to.
        transport: client-side object exposing ``request(dict) -> dict``.
    """

    def __init__(self, service: FakeGuestService, *, host: str = "127.0.0.1", port: int = 0, timeout_s: float = 10.0) -> None:
        self._service = service
        self._timeout_s = timeout_s
        self._closed = False

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, port))
        self._listener.listen(1)
        self._listener.settimeout(timeout_s)
        self.address = self._listener.getsockname()

        self._client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._client_sock.settimeout(timeout_s)
        self._client_sock.connect(self.address)
        self._conn, _ = self._listener.accept()
        self._conn.settimeout(timeout_s)

        self.transport = _TcpLineClient(self._client_sock, timeout_s)
        self._thread = threading.Thread(target=self._serve, name="unipad-fake-guest-tcp", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        """Serve newline-delimited messages until the peer disconnects."""
        buffer = b""
        while not self._closed:
            try:
                chunk = self._conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, _, buffer = buffer.partition(b"\n")
                if not raw.strip():
                    continue
                try:
                    reply = self._service.handle(json.loads(raw.decode("utf-8")))
                except (UniPadError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    code = exc.code if isinstance(exc, UniPadError) else ErrorCode.QMP_PROTOCOL_ERROR
                    text = exc.message if isinstance(exc, UniPadError) else "malformed message line"
                    reply = error_message(code, text).to_dict()
                try:
                    self._conn.sendall(json.dumps(reply, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
                except OSError:
                    return

    def close(self) -> None:
        """Tear down the sockets and the server thread (idempotent)."""
        if self._closed:
            return
        self._closed = True
        for sock in (self._client_sock, self._conn, self._listener):
            try:
                sock.close()
            except OSError:
                pass
        self.transport.close()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "FakeTcpPair":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def start_tcp_server(
    service: FakeGuestService,
    port: int = 0,
    *,
    host: str = "127.0.0.1",
    timeout_s: float = 10.0,
) -> FakeTcpPair:
    """Start a loopback TCP server for ``service`` and return the pair.

    Args:
        service: the guest service to drive.
        port: ``0`` selects an ephemeral port.
        host: bind address; loopback only by design.
        timeout_s: socket timeout for both halves.

    Returns:
        FakeTcpPair: caller owns it and must call ``close()`` (or use it as a
        context manager).
    """
    return FakeTcpPair(service, host=host, port=port, timeout_s=timeout_s)


__all__ = [
    "DEFAULT_TRUST_MODE",
    "STATE_SCHEMA_VERSION",
    "INSTALL_FAILURE_POINTS",
    "derive_challenge_value",
    "FakeGuestService",
    "FakeTransport",
    "FakeTcpPair",
    "start_tcp_server",
]
