"""A guest agent that answers the install channel over a real socket (ADR-0011).

``FakeGuestService`` is the only implementation of the guest protocol that
exists, and it is an *in-process* double: ``FakeTransport`` calls it directly
and ``FakeTcpPair`` puts a server thread behind it whose readiness, connection
count and request discipline are whatever the test happened to want.  Neither
one can be asked the question ADR-0011 exists to ask -- *what happens when the
guest service is not there yet?* -- because neither models a guest that
accepts a connection and then says nothing.

:class:`GuestAgentStub` is that peer.  It is a **server**: it binds a loopback
port, the host's :class:`~unipad.guest.agent_channel.AgentChannelTransport`
connects to it, and the same JSON Lines contract is spoken on a real socket
with the same correlation rules.  So ``GuestClient`` + ``AgentSession`` can be
driven end-to-end against something that behaves like the chardev rather than
like a function call.

What it adds over the in-memory fake
------------------------------------
1. **A readiness gate.**  The stub accepts the TCP connection *before* it can
   answer anything, because that is what QEMU does: accepting TCP is the
   chardev's job and the virtio-serial device exists whether or not the guest
   has a driver.  A stub that only listens once it is ready would hide the one
   failure mode this host has actually measured (``state == 'silent'``).
2. **A policy for a request that arrives before readiness.**  See
   :data:`STALE_DISCARD` / :data:`STALE_ANSWER` below -- this is the decision
   a real guest-side service has to make, and getting it wrong silently
   poisons the stream for the next question.
3. **One connection per boot.**  The chardev serves exactly one connection for
   the life of the QEMU process (measured, ``chardev-connection-count.py``);
   the stub closes its listener after the first accept so a test that spends
   the connection sees ``refused`` rather than a second, working one.
4. **Fault injection keyed by the ADR-0003 stage**, not by the PRD 10 chain
   point.  ``FakeGuestService`` injects at ``structure`` / ``transfer`` /
   ``signature`` / ... because it models the seven sub-problems for the
   verifier.  This stub models the *reporting* contract
   (``received|checked|transferred|installed|registered``), so its injection
   vocabulary is that one, and the two are deliberately separate models over a
   shared protocol.
5. **A closed code vocabulary.**  A failure may only be reported with a code
   from :data:`INJECTABLE_CODES`.  Injecting ``"banana"`` is a
   ``config_invalid`` rather than a stub that quietly teaches the host to
   accept codes outside PRD 10's taxonomy.

What it does not do
-------------------
It does not parse the IPA.  Package-structure precheck is the host's
``unipad.ipa`` responsibility (ADR-0003's evidence table lists the precheck
report as a host artifact); the stub's ``checked`` stage validates the
*declared metadata* it was sent, which is the part only the guest can check.
It does not send ``install.progress``: see the note on
:meth:`GuestAgentStub._dispatch`.

Dependency direction: imports :mod:`unipad.errors`, :mod:`unipad.guest.protocol`
and :mod:`unipad.guest.fake` (for :func:`~unipad.guest.fake.derive_challenge_value`,
so an M11 challenge answered by this stub is the same value the in-memory fake
would answer).  It must never import :mod:`unipad.ipa` or :mod:`unipad.host`.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from pathlib import Path
from typing import Any, Mapping, Optional

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.fake import derive_challenge_value
from unipad.guest.protocol import (
    KNOWN_TYPES,
    MSG_CHALLENGE_REQUEST,
    MSG_CHALLENGE_RESPONSE,
    MSG_ERROR,
    MSG_INSTALL_REQUEST,
    MSG_INSTALL_RESPONSE,
    MSG_LAUNCH_REQUEST,
    MSG_LAUNCH_RESPONSE,
    MSG_PERSIST_REQUEST,
    MSG_PERSIST_RESPONSE,
    MSG_STATUS_REQUEST,
    MSG_STATUS_RESPONSE,
    ChallengeResponse,
    InstallProgress,
    InstallRequest,
    InstallResponse,
    LaunchRequest,
    LaunchResponse,
    Message,
    PersistRequest,
    PersistResponse,
    StatusResponse,
    error_message,
)

#: How the stub treats a request line that arrives before :meth:`make_ready`.
#: ``discard`` (the default) reads it, records it in :attr:`discarded`, and
#: sends nothing.  ``answer`` queues it and answers it once ready.
STALE_DISCARD = "discard"
STALE_ANSWER = "answer"

#: Persisted-state schema version written by :meth:`GuestAgentStub.persist`.
STATE_SCHEMA_VERSION = 1

#: The ADR-0003 install stages plus the two terminal words of the launch and
#: persist vocabularies.  A failure may be injected *at* any of these; the
#: reported stage is then ``failed`` and the stages before it are the only
#: ones the stub claims to have reached.
INJECTABLE_STAGES: tuple[str, ...] = (
    "received",
    "checked",
    "transferred",
    "installed",
    "registered",
    "launched",
    "persisted",
)

#: The closed set of codes this stub may put on the wire.  Every one is a PRD
#: 10 taxonomy code already named by ADR-0003 / ADR-0010; the stub's own
#: verdicts (a digest that does not match, a policy that refuses a signature
#: mode) are drawn from here too, so "never invented" holds in both
#: directions -- an injected code passes through verbatim, and a self-derived
#: code is a real member of the taxonomy rather than a fresh string.
INJECTABLE_CODES: frozenset[str] = frozenset(
    {
        ErrorCode.INVALID_PACKAGE,
        ErrorCode.TRANSFER_FAILED,
        ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
        ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
        ErrorCode.REGISTRATION_FAILED,
        ErrorCode.MISSING_DEPENDENCY,
        ErrorCode.DYLD_FAILED,
        ErrorCode.PERSISTENCE_FAILED,
    }
)

#: Trust label the stub records when its policy accepts a package.  A recorded
#: observation, never a claim about any real guest.
DEFAULT_TRUST_MODE = "self-signed-dev-trust"

#: How often the stub's own socket loop wakes up to notice ``close()``.  The
#: stub's recv budget is *not* the host client's: the client's per-request
#: budget is the transport's ``timeout_s``, and conflating the two is how a
#: stub ends up answering inside a budget the host thought it had set.
POLL_S = 0.2


class GuestAgentStub:
    """A loopback guest agent implementing the protocol's server half.

    Args:
        host: bind address; loopback only by design.
        port: ``0`` selects an ephemeral port.  Read it back from
            :attr:`address` after construction -- never assume it.
        ready: whether the service can answer yet.  ``False`` (the default)
            models a guest whose install service has not come up.
        stale_policy: :data:`STALE_DISCARD` or :data:`STALE_ANSWER`; what
            happens to a request that arrives before :attr:`ready`.
        one_connection: serve exactly one connection and then close the
            listener, mirroring the measured chardev behaviour.  ``False``
            serves connections one at a time for a test that wants that.
        failures: initial fault injection, ``{stage: code}`` -- see
            :meth:`inject_failure`.
        trust_policy: ``accept_modes`` / ``trust_mode`` / ``accept``, the same
            shape :class:`~unipad.guest.fake.FakeGuestService` takes.
        poll_s: the stub's own socket poll interval (see :data:`POLL_S`).

    Attributes:
        address: ``(host, port)`` the listener is bound to.
        ready: whether the service answers.  Flip it with :meth:`make_ready`.
        installed: ``bundle_id -> entry`` the stub actually holds.
        connections_served: connections the stub answered on.  One, unless
            ``one_connection=False``.
        answered: ``[{"type", "id"}, ...]`` for every request answered.
        discarded: ``[{"type", "id"}, ...]`` for every pre-readiness request
            dropped under :data:`STALE_DISCARD`.
        install_log: one record per install attempt, including the stages it
            actually reached -- the evidence a test asserts against.
        progress_log: :class:`~unipad.guest.protocol.InstallProgress` records
            the stub emitted for itself.  Deliberately **not** on the wire.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        ready: bool = False,
        stale_policy: str = STALE_DISCARD,
        one_connection: bool = True,
        failures: Optional[Mapping[str, str]] = None,
        trust_policy: Optional[Mapping[str, Any]] = None,
        poll_s: float = POLL_S,
    ) -> None:
        if stale_policy not in (STALE_DISCARD, STALE_ANSWER):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "stale_policy must be 'discard' or 'answer'",
                stale_policy=stale_policy,
            )
        if poll_s <= 0:
            raise UniPadError(ErrorCode.CONFIG_INVALID, "poll_s must be positive", poll_s=poll_s)

        self._poll_s = float(poll_s)
        self._stale_policy = stale_policy
        self._one_connection = bool(one_connection)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = bool(ready)
        self._failures: dict[str, Any] = {}
        self._pending: list[bytes] = []
        self._staging: dict[str, dict[str, Any]] = {}
        self._conn: Optional[socket.socket] = None

        policy = dict(trust_policy or {})
        self.trust_accept_modes: list[str] = list(policy.get("accept_modes", ["self"]))
        self.trust_mode_label: str = str(policy.get("trust_mode", DEFAULT_TRUST_MODE))
        self.trust_accept: bool = bool(policy.get("accept", True))

        # --- guest state the stub actually holds ---
        self.installed: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.challenge_log: list[dict[str, Any]] = []
        self.trust_log: list[dict[str, Any]] = []
        self.install_log: list[dict[str, Any]] = []
        self.progress_log: list[dict[str, Any]] = []

        # --- observation counters a test asserts against ---
        self.connections_served = 0
        self.answered: list[dict[str, Any]] = []
        self.discarded: list[dict[str, Any]] = []
        #: The state file the last successful :meth:`persist` wrote, so a test
        #: can measure it rather than trust the reported byte count.
        self.last_state_path: Optional[Path] = None

        # Bind and listen BEFORE the port is published, so a client that is
        # handed self.address can never race an accept that has not started:
        # the kernel queues the connection and the serve loop picks it up.
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, int(port)))
        self._listener.listen(4)
        self._listener.settimeout(self._poll_s)
        self.address = self._listener.getsockname()

        if failures:
            for stage, code in dict(failures).items():
                self.inject_failure(stage, code)

        self._thread = threading.Thread(
            target=self._run, name="unipad-agent-stub", daemon=True
        )
        self._thread.start()

    # --- lifecycle ---

    @property
    def port(self) -> int:
        """The port the listener is bound to (``self.address[1]``)."""
        return int(self.address[1])

    @property
    def ready(self) -> bool:
        """Whether the service answers.  Not a claim about the socket."""
        return self._ready

    def make_ready(self) -> None:
        """Let the service answer.  The connection is *not* re-opened.

        This is the whole point of the readiness gate: the host's transport
        holds its socket across a timeout (``agent_channel`` records why --
        the chardev serves one connection per boot), so a guest that comes up
        late is answered on the socket it already has.
        """
        with self._lock:
            self._ready = True

    def close(self) -> None:
        """Tear down the listener, the connection and the thread.  Idempotent."""
        if self._stop.is_set():
            return
        self._stop.set()
        self._close_listener()
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            self._hang_up(conn)
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "GuestAgentStub":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- fault injection ---

    def inject_failure(self, stage: str, code: str, *, mode: str = "always") -> None:
        """Fail at ``stage`` with ``code``, verbatim.

        Args:
            stage: one of :data:`INJECTABLE_STAGES` -- the stage the guest was
                working toward when it refused.  The stages *before* it are the
                only ones reported as reached.
            code: the guest's own error code.  Must be a member of
                :data:`INJECTABLE_CODES`; anything else is ``config_invalid``,
                because a stub that accepts an unknown code would teach the
                host to accept one.
            mode: ``always`` (default) or ``once`` -- ``once`` clears itself
                after firing, which is how a transient guest failure is
                modelled so the client's bounded retry has something to retry.

        Raises:
            UniPadError: ``config_invalid`` for a stage outside the vocabulary,
                a code outside the taxonomy, or an unknown mode.  An injection
                that validates cleanly and then never fires is worse than no
                injection at all.
        """
        if stage not in INJECTABLE_STAGES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "unknown failure injection stage",
                stage=stage,
                known_stages=list(INJECTABLE_STAGES),
            )
        if code not in INJECTABLE_CODES:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "failure code is not a member of the PRD 10 taxonomy this stub may report",
                injected_code=code,
                known_codes=sorted(INJECTABLE_CODES),
            )
        if mode not in ("always", "once"):
            raise UniPadError(
                ErrorCode.CONFIG_INVALID, "failure mode must be 'always' or 'once'", mode=mode
            )
        with self._lock:
            self._failures = {"stage": stage, "code": code, "mode": mode}

    def clear_failures(self) -> None:
        """Drop the active injection (``None`` never needs to be spelled)."""
        with self._lock:
            self._failures = {}

    def _injected(self, stage: str) -> Optional[str]:
        """The code to fail ``stage`` with, consuming a ``once`` failure."""
        with self._lock:
            cfg = self._failures
            if not cfg or cfg.get("stage") != stage:
                return None
            if cfg.get("mode") == "once":
                self._failures = {}
            return str(cfg["code"])

    # --- byte channel (stands in for unipad.ipa.transfer) ---

    def stage_package(self, package_bytes: bytes, meta: Mapping[str, Any]) -> dict[str, Any]:
        """Deliver package bytes on the (simulated) byte channel.

        The control channel carries metadata only, so the bytes a guest would
        verify against the declared digest have to arrive some other way; on a
        real guest that is ``unipad.ipa.transfer``.  Keyed by the digest the
        bytes actually hash to, so an install request naming a different
        digest finds nothing and cannot report ``transferred``.

        Raises:
            UniPadError: ``transfer_failed`` when the bytes do not match the
                declared size or digest.
        """
        request = InstallRequest.from_dict(dict(meta))
        if not isinstance(package_bytes, (bytes, bytearray)):
            raise UniPadError(ErrorCode.TRANSFER_FAILED, "package bytes must be a bytes-like object")
        actual = hashlib.sha256(package_bytes).hexdigest()
        if len(package_bytes) != request.size_bytes:
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "delivered package size does not match declared size_bytes",
                declared=request.size_bytes,
                actual=len(package_bytes),
            )
        if actual != request.package_sha256.strip().lower():
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "delivered package sha256 does not match declared package_sha256",
                declared=request.package_sha256,
                actual=actual,
            )
        with self._lock:
            self._staging[actual] = {"bytes": bytes(package_bytes), "meta": request.to_dict()}
        return {"staged_sha256": actual, "size_bytes": len(package_bytes)}

    # --- the verbs ---

    def status(self) -> StatusResponse:
        """The installed set this stub actually holds, plus its data markers."""
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

    def install(self, request: InstallRequest) -> InstallResponse:
        """Walk the ADR-0003 stages and report the furthest one actually reached.

        Every stage is earned by something this stub really did: ``checked``
        validates the declared metadata, ``transferred`` verifies bytes that
        were actually delivered against the declared digest, ``installed``
        records a trust decision and creates the container, ``registered``
        records the registration.  A failure at stage *X* leaves
        ``stages_reached`` holding exactly the stages before *X*, and a failure
        at ``registered`` withdraws the container so :meth:`status` can never
        contradict an install that reported a failure.
        """
        package_name = request.package_name
        bundle_id = self._bundle_id_for(package_name)
        reached: list[str] = []

        self._progress("received", f"package {package_name}")
        code = self._injected("received")
        if code is not None:
            return self._fail(bundle_id, reached, code, "the guest refused the request on arrival")
        reached.append("received")

        self._progress("checked", f"bundle {bundle_id}")
        code = self._injected("checked")
        if code is None:
            code = self._check_metadata(request)
        if code is not None:
            return self._fail(bundle_id, reached, code, "the guest precheck rejected the package")
        reached.append("checked")

        self._progress("transferred", f"sha256 {request.package_sha256}")
        code = self._injected("transferred")
        if code is None:
            code = self._check_transfer(request)
        if code is not None:
            return self._fail(bundle_id, reached, code, "the guest could not verify the transfer")
        reached.append("transferred")

        self._progress("installed", f"container for {bundle_id}")
        code = self._injected("installed")
        if code is None:
            code = self._evaluate_trust(request, bundle_id)
        if code is not None:
            return self._fail(bundle_id, reached, code, "the guest refused to trust the package")
        with self._lock:
            self.installed[bundle_id] = {
                "version": "1.0",
                "icon": False,
                "container": {"markers": {}, "files": []},
                "package_name": package_name,
                "package_sha256": request.package_sha256,
                "size_bytes": request.size_bytes,
                "declared_min_os": request.declared_min_os,
                "signature_mode": request.signature_mode,
                "trust_mode": self.trust_mode_label,
            }
            self.containers[bundle_id] = self.installed[bundle_id]["container"]
        reached.append("installed")

        self._progress("registered", f"registration record for {bundle_id}")
        code = self._injected("registered")
        if code is not None:
            return self._rollback(bundle_id, reached, code, "the guest could not register the app")
        with self._lock:
            self.installed[bundle_id]["icon"] = True
        reached.append("registered")

        self.install_log.append(
            {"bundle_id": bundle_id, "stage": "registered", "stages_reached": list(reached), "error_code": None}
        )
        return InstallResponse(
            stage="registered",
            bundle_id=bundle_id,
            trust_mode=self.trust_mode_label,
        )

    def launch(self, bundle_id: str) -> LaunchResponse:
        """Start one already-installed app, or refuse with the guest's own code.

        The not-installed check comes *first*: there is no app to fail to
        start, so a ``dyld`` fault cannot be injected for a bundle the stub
        does not hold.  An app that installed cleanly and then failed to start
        stays installed -- a launch failure is not an install failure
        (ADR-0010 decision 2).
        """
        request_ok = isinstance(bundle_id, str) and bundle_id != ""
        with self._lock:
            known = request_ok and bundle_id in self.installed
            trust_mode = self.installed[bundle_id].get("trust_mode") if known else None
        if not request_ok:
            return LaunchResponse(
                stage="failed",
                error_code=ErrorCode.REGISTRATION_FAILED,
                error_message="a launch request must name a bundle id",
            )
        if not known:
            return LaunchResponse(
                stage="failed",
                bundle_id=bundle_id,
                error_code=ErrorCode.REGISTRATION_FAILED,
                error_message="application is not installed",
            )
        code = self._injected("launched")
        if code is not None:
            return LaunchResponse(
                stage="failed",
                bundle_id=bundle_id,
                error_code=code,
                error_message="the guest refused to start the application",
            )
        return LaunchResponse(stage="launched", bundle_id=bundle_id, trust_mode=trust_mode)

    def persist(self, path: str) -> PersistResponse:
        """Flush the data volume the host named and report what was written.

        ``markers_written`` is read back from :meth:`data_markers` *after* the
        flush -- the guest's own inventory, never the host's.  ``bytes_written``
        is the measured size of the file that was actually written; a constant
        here would look like a measurement and not be one.

        ``path`` is interpreted as the directory to write into, which is the
        only interpretation this host can exercise (ADR-0010 decision 4).
        """
        if not isinstance(path, str) or path == "":
            return PersistResponse(
                stage="failed",
                error_code=ErrorCode.PERSISTENCE_FAILED,
                error_message="a persist request must name a volume path",
            )
        code = self._injected("persisted")
        if code is not None:
            return PersistResponse(
                stage="failed",
                error_code=code,
                error_message="the guest refused to flush the data volume",
            )
        try:
            state_path = self._write_state(path)
            bytes_written = state_path.stat().st_size
        except OSError:
            # The OS error text can name the path it failed on, and a path is
            # not something PRD 9.2 lets onto the wire.  The code carries the
            # fact; the message stays generic.
            return PersistResponse(
                stage="failed",
                error_code=ErrorCode.PERSISTENCE_FAILED,
                error_message="the guest could not write the data volume it was asked to flush",
            )
        return PersistResponse(
            stage="persisted",
            markers_written=self.data_markers(),
            bytes_written=bytes_written,
        )

    def set_data_marker(self, bundle_id: str, key: str, value: str) -> dict[str, Any]:
        """Write one data marker into an app's container (simulates app data)."""
        with self._lock:
            container = self._require_container(bundle_id)
            container["markers"][key] = value
            container["files"].append({"name": key, "size_bytes": len(value.encode("utf-8"))})
            return {"bundle_id": bundle_id, "key": key, "stored": True}

    def data_markers(self) -> list[str]:
        """Every marker across every container, as ``bundle_id:key`` strings."""
        with self._lock:
            out: list[str] = []
            for bundle_id in sorted(self.containers):
                for key in sorted(self.containers[bundle_id].get("markers", {})):
                    out.append(f"{bundle_id}:{key}")
            return out

    def challenge(self, nonce: str) -> ChallengeResponse:
        """Answer a challenge for the most recently installed app.

        ``challenge.request`` carries only the nonce, so -- exactly as in
        :class:`~unipad.guest.fake.FakeGuestService` -- the target is the app
        the guest would show.  With nothing installed there is nothing to
        answer for, and saying so beats deriving a value for a phantom app.
        """
        if not isinstance(nonce, str) or nonce == "":
            raise UniPadError(ErrorCode.CONFIG_INVALID, "challenge nonce must be a non-empty string")
        with self._lock:
            if not self.installed:
                raise UniPadError(
                    ErrorCode.REGISTRATION_FAILED,
                    "no application is installed to answer a challenge for",
                )
            target = next(reversed(self.installed))
            value = derive_challenge_value(target, nonce)
            self.challenge_log.append({"bundle_id": target, "nonce": nonce, "value": value})
            return ChallengeResponse(nonce=nonce, value=value)

    def expected_challenge(self, bundle_id: str, nonce: str) -> str:
        """The value the host should expect for ``(bundle_id, nonce)`` (M11)."""
        return derive_challenge_value(bundle_id, nonce)

    # --- message dispatch ---

    def _dispatch(self, envelope: Message) -> dict[str, Any]:
        """Answer one envelope, echoing its id in the reply envelope."""
        mtype = envelope.type
        try:
            if mtype == MSG_STATUS_REQUEST:
                return self._reply(envelope, MSG_STATUS_RESPONSE, self.status())
            if mtype == MSG_INSTALL_REQUEST:
                return self._reply(envelope, MSG_INSTALL_RESPONSE, self.install(InstallRequest.from_dict(envelope.payload)))
            if mtype == MSG_LAUNCH_REQUEST:
                request = LaunchRequest.from_dict(envelope.payload)
                return self._reply(envelope, MSG_LAUNCH_RESPONSE, self.launch(request.bundle_id))
            if mtype == MSG_PERSIST_REQUEST:
                request = PersistRequest.from_dict(envelope.payload)
                return self._reply(envelope, MSG_PERSIST_RESPONSE, self.persist(request.path))
            if mtype == MSG_CHALLENGE_REQUEST:
                return self._reply(envelope, MSG_CHALLENGE_RESPONSE, self.challenge(envelope.payload.get("nonce")))
            return error_message(
                ErrorCode.UNSUPPORTED_COMMAND,
                f"unsupported guest message type '{mtype}'",
                message_id=envelope.id,
            ).to_dict()
        except UniPadError as exc:
            # The code passes through verbatim; the stub never re-maps it.
            return error_message(exc.code, exc.message, message_id=envelope.id).to_dict()

    @staticmethod
    def _reply(envelope: Message, reply_type: str, payload: Any) -> dict[str, Any]:
        """Wrap a payload in a reply envelope carrying the request's own id.

        Correlating the id is not politeness: ``GuestClient._validate_reply``
        rejects a reply whose ``id`` is not the request's, and a stub that
        minted a fresh id would make every "successful" test fail with
        ``guest reply id does not match the request id``.
        """
        body = payload.to_dict() if hasattr(payload, "to_dict") else dict(payload)
        return Message(type=reply_type, id=envelope.id, payload=body).to_dict()

    def _answer(self, line: bytes) -> Optional[dict[str, Any]]:
        """Turn one request line into the reply envelope to send back."""
        try:
            envelope = Message.from_dict(json.loads(line))
        except (UniPadError, ValueError, UnicodeDecodeError) as exc:
            code = exc.code if isinstance(exc, UniPadError) else ErrorCode.QMP_PROTOCOL_ERROR
            text = exc.message if isinstance(exc, UniPadError) else "malformed message line"
            return error_message(code, text).to_dict()
        if envelope.type not in KNOWN_TYPES:
            # The wire vocabulary is closed in both directions; the id is kept
            # so the host can attribute the refusal to its own request.
            return error_message(
                ErrorCode.UNSUPPORTED_COMMAND,
                f"unsupported guest message type '{envelope.type}'",
                message_id=envelope.id,
            ).to_dict()
        reply = self._dispatch(envelope)
        self.answered.append({"type": envelope.type, "id": envelope.id})
        return reply

    # --- the socket half ---

    def _run(self) -> None:
        """Accept and serve connections until stopped.

        With ``one_connection`` the listener is closed the moment the first
        connection is accepted, so a second ``connect()`` is refused by the
        kernel rather than served -- the measured chardev behaviour, and the
        trap ``agent_channel`` documents.  There is deliberately no counter of
        "refused" connections: the refusal happens in the kernel, below this
        loop, so such a counter would only ever read zero and would be a
        check that looked like coverage.  What a test asserts instead is the
        transport's own ``state == 'refused'``.

        Without ``one_connection``, connections are served one at a time, which
        is enough for a test that wants more than one.
        """
        while not self._stop.is_set():
            try:
                conn, _peer = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.connections_served += 1
            if self._one_connection:
                self._close_listener()
            with self._lock:
                self._conn = conn
            try:
                self._serve(conn)
            finally:
                self._hang_up(conn)
                with self._lock:
                    if self._conn is conn:
                        self._conn = None
            if self._one_connection:
                return

    def _serve(self, conn: socket.socket) -> None:
        """Read newline-delimited requests and answer them until the peer goes."""
        conn.settimeout(self._poll_s)
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, _, buffer = buffer.partition(b"\n")
                line = raw.strip()
                if not line:
                    continue
                if not self._handle_line(line, conn):
                    return

    def _handle_line(self, line: bytes, conn: socket.socket) -> bool:
        """Apply the readiness policy to one request line.

        Returns ``False`` when the connection must be dropped.
        """
        if not self._ready:
            if self._stale_policy == STALE_ANSWER:
                self._pending.append(line)
            else:
                self.discarded.append(self._identify(line))
            return True
        # Anything an 'answer' policy queued before readiness goes out first,
        # in the order it arrived, so the model is the stream and not a queue
        # the host cannot see.
        queued, self._pending = self._pending, []
        for stale in queued:
            if not self._send(conn, self._answer(stale)):
                return False
        return self._send(conn, self._answer(line))

    @staticmethod
    def _identify(line: bytes) -> dict[str, Any]:
        """``{"type", "id"}`` for a dropped request, for a test to assert on.

        Only the envelope's identity is recorded -- never the payload -- so a
        discard log cannot become a place where package metadata accumulates.
        """
        try:
            envelope = Message.from_dict(json.loads(line))
        except (UniPadError, ValueError, UnicodeDecodeError):
            return {"type": None, "id": None}
        return {"type": envelope.type, "id": envelope.id}

    def _send(self, conn: socket.socket, reply: Optional[dict[str, Any]]) -> bool:
        """Write one reply line.  ``False`` means the peer is gone."""
        if reply is None:
            return True
        try:
            conn.sendall(
                json.dumps(reply, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
        except OSError:
            return False
        return True

    def _close_listener(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass

    @staticmethod
    def _hang_up(conn: socket.socket) -> None:
        try:
            conn.close()
        except OSError:
            pass

    # --- internals: the checks that earn each stage ---

    @staticmethod
    def _check_metadata(request: InstallRequest) -> Optional[str]:
        """Guest-side precheck of the *declared* metadata.

        Package structure is the host's ``unipad.ipa`` job; what only the guest
        can check is whether the request it was sent describes a package at
        all.  An empty or malformed digest names nothing to verify against.
        """
        digest = request.package_sha256.strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return ErrorCode.INVALID_PACKAGE
        if request.size_bytes <= 0:
            return ErrorCode.INVALID_PACKAGE
        return None

    def _check_transfer(self, request: InstallRequest) -> Optional[str]:
        """Verify the declared digest against bytes that actually arrived.

        A request naming a digest the stub never received cannot be verified,
        and a guest that never received the bytes must not report a
        registration -- PRD 9.1 forbids claiming a stage the transfer did not
        earn, and PRD 10 assigns this fault to the transfer sub-problem.
        """
        digest = request.package_sha256.strip().lower()
        with self._lock:
            staged = self._staging.get(digest)
        if staged is None:
            return ErrorCode.TRANSFER_FAILED
        if len(staged["bytes"]) != request.size_bytes:
            return ErrorCode.TRANSFER_FAILED
        return None

    def _evaluate_trust(self, request: InstallRequest, bundle_id: str) -> Optional[str]:
        """Apply the trust policy and record the decision it made."""
        accepted = self.trust_accept and request.signature_mode in self.trust_accept_modes
        self.trust_log.append(
            {
                "bundle_id": bundle_id,
                "signature_mode": request.signature_mode,
                "accepted": accepted,
                "trust_mode": self.trust_mode_label if accepted else None,
                "declared_min_os": request.declared_min_os,
            }
        )
        if not accepted:
            return ErrorCode.SIGNATURE_OR_POLICY_REJECTED
        return None

    def _require_container(self, bundle_id: str) -> dict[str, Any]:
        """An app's container, distinguishing "not installed" from "data lost"."""
        with self._lock:
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

    @staticmethod
    def _bundle_id_for(package_name: str) -> str:
        """Derive the registry key from the package file name.

        ``InstallRequest`` is a five-field contract plus the optional
        package-source fields, so a request arriving over the wire carries no
        ``bundle_id`` and the file name is the only thing to key on.  The
        authoritative bundle id belongs to the host's ``unipad.ipa`` layer; a
        test that needs it reads it back off the install response.
        """
        stem = (package_name or "unknown").strip()
        if stem.lower().endswith(".ipa"):
            stem = stem[:-4]
        return stem or "unknown.bundle"

    def _write_state(self, path: str) -> Path:
        """Serialize guest state into ``path`` and return the file written."""
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        with self._lock:
            state = {
                "schema_version": STATE_SCHEMA_VERSION,
                "installed": self.installed,
                "containers": self.containers,
                "trust_mode": self.trust_mode_label,
            }
        state_path = target / "agent-state.json"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        with self._lock:
            self.last_state_path = state_path
        return state_path

    def _progress(self, stage: str, detail: str) -> None:
        """Record a stage for the stub's own log.  Never sent on the wire.

        ``install.progress`` is defined as guest -> host, and it has no safe
        place on this channel as ``GuestClient`` is written: ``_exchange``
        reads exactly one line per request, so a progress envelope interleaved
        into an install would be read as the reply and rejected with
        ``guest reply type does not match the request``.  A guest that wants to
        report progress needs either a second connection -- which the
        one-connection rule forbids -- or a protocol revision recorded as its
        own ADR.  Until then the stub logs progress instead of emitting it.
        """
        self.progress_log.append(InstallProgress(stage=stage, detail=detail).to_dict())

    def _fail(self, bundle_id: str, reached: list[str], code: str, message: str) -> InstallResponse:
        """Report a failed install, claiming only the stages actually reached."""
        self.install_log.append(
            {
                "bundle_id": bundle_id,
                "stage": "failed",
                "stages_reached": list(reached),
                "error_code": code,
            }
        )
        return InstallResponse(
            stage="failed", bundle_id=bundle_id, error_code=code, error_message=message
        )

    def _rollback(self, bundle_id: str, reached: list[str], code: str, message: str) -> InstallResponse:
        """Fail the last stage and withdraw the container and registration.

        ``installed`` was reached before ``registered`` failed, so the
        container really did exist -- and leaving it there would let a
        verifier's ``status()`` contradict an install that reported a failure.
        The stages *reached* still record that it got that far; what is
        withdrawn is the state, not the history.
        """
        with self._lock:
            self.installed.pop(bundle_id, None)
            self.containers.pop(bundle_id, None)
        self.install_log.append(
            {
                "bundle_id": bundle_id,
                "stage": "failed",
                "stages_reached": list(reached),
                "error_code": code,
                "rolled_back": True,
            }
        )
        return InstallResponse(
            stage="failed", bundle_id=bundle_id, error_code=code, error_message=message
        )


def start_agent_stub(**kwargs: Any) -> GuestAgentStub:
    """Start a :class:`GuestAgentStub` and return it.

    The caller owns it and must call :meth:`GuestAgentStub.close` -- or use it
    as a context manager, which is what every test does, because a stub left
    listening is a leaked thread holding a port.
    """
    return GuestAgentStub(**kwargs)


__all__ = [
    "DEFAULT_TRUST_MODE",
    "INJECTABLE_CODES",
    "INJECTABLE_STAGES",
    "STATE_SCHEMA_VERSION",
    "STALE_ANSWER",
    "STALE_DISCARD",
    "GuestAgentStub",
    "start_agent_stub",
]
