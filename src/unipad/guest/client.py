"""Host-side client for the guest install service (PRD sections 9.1, 9.10).

``GuestClient`` speaks the JSON Lines contract defined in
:mod:`unipad.guest.protocol` over a duck-typed transport::

    class Transport:
        def request(self, message: dict) -> dict: ...

The transport only moves *messages*.  Package bytes travel on a separate
channel owned by ``unipad.ipa.transfer``; this client never puts bytes in a
JSON message (no secrets, no bulk payloads on the control channel).

Guarantees implemented here:

- Bounded retries.  Every attempt is a fresh message id; a retry never
  reuses a correlation handle.
- Transport failure -> :data:`unipad.errors.ErrorCode.INSTALL_SERVICE_UNAVAILABLE`
  (timeouts carry ``detail='timeout'``).  A guest that never answers is
  never reported as ``installed``.
- Guest-reported codes pass through **verbatim** -- they are already stable
  taxonomy codes (PRD section 10) and must not be re-mapped or invented.
- No fabricated success: a missing, id-mismatched or ambiguous reply is an
  error, never a success stage.

Dependency direction: imports :mod:`unipad.errors` and
:mod:`unipad.guest.protocol` only.  It must never import :mod:`unipad.ipa`
or :mod:`unipad.host`.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from unipad.errors import ErrorCode, UniPadError
from unipad.guest.protocol import (
    MSG_CHALLENGE_REQUEST,
    MSG_CHALLENGE_RESPONSE,
    MSG_ERROR,
    MSG_INSTALL_REQUEST,
    MSG_INSTALL_RESPONSE,
    MSG_STATUS_REQUEST,
    MSG_STATUS_RESPONSE,
    ChallengeRequest,
    ChallengeResponse,
    InstallRequest,
    InstallResponse,
    Message,
    StatusResponse,
    new_message_id,
)

DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_S = 0.05


@runtime_checkable
class Transport(Protocol):
    """Message-level transport contract (distinct from ``ipa.transfer``'s byte protocol)."""

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send one message envelope and return the reply envelope as a dict.

        Raises:
            TimeoutError: when the peer does not answer in budget.
            OSError: when the channel itself fails.
        """
        ...


class GuestClient:
    """Client for the guest install/status/challenge service.

    Args:
        transport: object exposing ``request(message: dict) -> dict``.
        attempts: total attempts per call (>= 1); retries are bounded.
        backoff_s: sleep between attempts (0 disables sleeping).
        sleeper: injectable sleep for tests; defaults to :func:`time.sleep`.
        timeout_s: optional per-call budget, recorded in error details only.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_s: float = DEFAULT_BACKOFF_S,
        sleeper: Callable[[float], None] = time.sleep,
        timeout_s: Optional[float] = None,
    ) -> None:
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise UniPadError(ErrorCode.CONFIG_INVALID, "attempts must be an integer >= 1", attempts=attempts)
        if backoff_s < 0:
            raise UniPadError(ErrorCode.CONFIG_INVALID, "backoff_s must be >= 0", backoff_s=backoff_s)
        self._transport = transport
        self._attempts = attempts
        self._backoff_s = float(backoff_s)
        self._sleep = sleeper
        self._timeout_s = timeout_s
        self.last_error: Optional[UniPadError] = None
        self.last_delivery: Optional[dict[str, Any]] = None

    # --- public API ---

    def install_package(
        self,
        package_bytes: bytes,
        meta: Mapping[str, Any] | InstallRequest,
    ) -> InstallResponse:
        """Install one package already delivered on the byte channel.

        ``package_bytes`` is never serialized into a message -- the control
        channel carries metadata only.  Its digest is recorded as evidence
        (``client.last_delivery``); the authoritative transfer check belongs to
        ``unipad.ipa.transfer``.

        Returns:
            InstallResponse: the guest's answer.  On an injected guest-side
            failure the response carries ``stage='failed'`` and the guest's
            verbatim ``error_code``; it is returned, not raised, so callers
            can log it as evidence.

        Raises:
            UniPadError: ``install_service_unavailable`` when the guest never
                answers, ``config_invalid`` for malformed metadata.
        """
        request = self._coerce_request(meta)
        self.last_delivery = self._record_delivery(package_bytes, request)

        reply = self._exchange(MSG_INSTALL_REQUEST, MSG_INSTALL_RESPONSE, request.to_dict())

        if reply.type == MSG_ERROR:
            return self._install_response_from_error(reply)

        payload = reply.payload
        try:
            response = InstallResponse.from_dict(payload)
        except UniPadError as exc:
            raise self._protocol_error("install response payload is not conforming", reply=reply) from exc

        self._reject_fabricated_success(response, reply)
        return response

    def challenge(self, nonce: str) -> ChallengeResponse:
        """Ask the guest to derive this run's challenge value (PRD 5.2 step 5).

        The reply's ``nonce`` must echo the request; a mismatch means the
        answer cannot be attributed to this run and is an error, never
        evidence.

        Raises:
            UniPadError: verbatim guest code for a guest-reported failure;
                ``install_service_unavailable`` when the guest never answers.
        """
        if not isinstance(nonce, str) or nonce == "":
            raise UniPadError(ErrorCode.CONFIG_INVALID, "challenge nonce must be a non-empty string")
        request = ChallengeRequest(nonce=nonce)
        reply = self._exchange(MSG_CHALLENGE_REQUEST, MSG_CHALLENGE_RESPONSE, request.to_dict())
        self._raise_if_error(reply)
        try:
            response = ChallengeResponse.from_dict(reply.payload)
        except UniPadError as exc:
            raise self._protocol_error("challenge response payload is not conforming", reply=reply) from exc
        if response.nonce != nonce:
            raise self._protocol_error(
                "challenge response nonce does not match the request",
                reply=reply,
            )
        return response

    def status(self) -> StatusResponse:
        """Query installed apps and Data-volume markers (M9b/M12 verification).

        Raises:
            UniPadError: verbatim guest code for a guest-reported failure;
                ``install_service_unavailable`` when the guest never answers.
        """
        reply = self._exchange(MSG_STATUS_REQUEST, MSG_STATUS_RESPONSE, {})
        self._raise_if_error(reply)
        try:
            response = StatusResponse.from_dict(reply.payload)
        except UniPadError as exc:
            raise self._protocol_error("status response payload is not conforming", reply=reply) from exc
        for app in response.installed:
            if not app.get("bundle_id"):
                raise self._protocol_error("status response entry without bundle_id", reply=reply)
        return response

    # --- internals ---

    def _coerce_request(self, meta: Mapping[str, Any] | InstallRequest) -> InstallRequest:
        """Validate caller-supplied metadata into an :class:`InstallRequest`."""
        if isinstance(meta, InstallRequest):
            return meta
        if not isinstance(meta, Mapping):
            raise UniPadError(ErrorCode.CONFIG_INVALID, "install metadata must be a mapping")
        try:
            return InstallRequest.from_dict(meta)
        except UniPadError as exc:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "install metadata is incomplete or malformed",
                reason=exc.message,
            ) from exc

    @staticmethod
    def _record_delivery(package_bytes: bytes, request: InstallRequest) -> dict[str, Any]:
        """Record what the host believes it delivered, for evidence only.

        The authoritative transfer check (byte channel integrity, hash
        comparison, size) belongs to ``unipad.ipa.transfer``; this client only
        moves messages.  Mismatches are therefore *recorded*, never rejected
        here, so the guest's own verdict remains the single source of truth.

        Returns:
            dict: ``{size_bytes, sha256, declared_sha256, declared_size_bytes,
            matches_declared}`` -- safe to attach to evidence (digests only).
        """
        if isinstance(package_bytes, (bytes, bytearray)):
            actual_size = len(package_bytes)
            actual_hash = hashlib.sha256(package_bytes).hexdigest()
        else:
            actual_size = -1
            actual_hash = ""
        return {
            "size_bytes": actual_size,
            "sha256": actual_hash,
            "declared_sha256": request.package_sha256,
            "declared_size_bytes": request.size_bytes,
            "matches_declared": actual_hash == request.package_sha256.lower() and actual_size == request.size_bytes,
        }

    def _exchange(self, request_type: str, expected_type: str, payload: dict[str, Any]) -> Message:
        """Send one request and return the validated reply envelope.

        Retries are bounded by ``attempts``.  Every failure to obtain a valid
        answer is retried; a definitive guest answer (success or guest-reported
        error) is never retried.

        Raises:
            UniPadError: ``install_service_unavailable`` when no valid answer
                was obtained within the attempt budget.
        """
        last: Optional[UniPadError] = None
        for attempt in range(1, self._attempts + 1):
            message_id = new_message_id()
            message = Message(type=request_type, id=message_id, payload=dict(payload))
            try:
                raw = self._transport.request(message.to_dict())
            except TimeoutError as exc:
                last = UniPadError(
                    ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
                    "guest service did not answer within the timeout budget",
                    detail="timeout",
                    request_type=request_type,
                    attempt=attempt,
                )
                last.__cause__ = exc
            except UniPadError:
                raise
            except Exception as exc:  # OSError and any transport-level failure
                last = UniPadError(
                    ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
                    "guest service transport failed",
                    detail=type(exc).__name__,
                    request_type=request_type,
                    attempt=attempt,
                )
                last.__cause__ = exc
            else:
                try:
                    reply = self._validate_reply(raw, expected_type, message_id)
                except UniPadError as exc:
                    last = exc
                else:
                    self.last_error = None
                    return reply

            if attempt < self._attempts and self._backoff_s > 0:
                self._sleep(self._backoff_s)

        assert last is not None  # loop always sets last before exiting
        last.details.setdefault("attempts", self._attempts)
        if self._timeout_s is not None:
            last.details.setdefault("timeout_s", self._timeout_s)
        self.last_error = last
        raise last

    @staticmethod
    def _validate_reply(raw: Any, expected_type: str, message_id: str) -> Message:
        """Structural validation of a reply envelope (never trusts blindly)."""
        if not isinstance(raw, Mapping):
            raise UniPadError(
                ErrorCode.QMP_PROTOCOL_ERROR,
                "guest reply is not a JSON object",
                expected_type=expected_type,
            )
        try:
            reply = Message.from_dict(raw)
        except UniPadError as exc:
            exc.details.setdefault("expected_type", expected_type)
            raise
        if reply.type == MSG_ERROR:
            # A guest-reported error envelope is a definitive answer.
            return reply
        if reply.type != expected_type:
            raise UniPadError(
                ErrorCode.QMP_PROTOCOL_ERROR,
                "guest reply type does not match the request",
                expected_type=expected_type,
                actual_type=reply.type,
            )
        if reply.id != message_id:
            raise UniPadError(
                ErrorCode.QMP_PROTOCOL_ERROR,
                "guest reply id does not match the request id",
                expected_id=message_id,
                actual_id=reply.id,
            )
        return reply

    @staticmethod
    def _protocol_error(message: str, *, reply: Message) -> UniPadError:
        return UniPadError(
            ErrorCode.QMP_PROTOCOL_ERROR,
            message,
            reply_type=reply.type,
            reply_id=reply.id,
        )

    @staticmethod
    def _raise_if_error(reply: Message) -> None:
        """Re-raise a guest-reported error envelope with its code verbatim."""
        if reply.type != MSG_ERROR:
            return
        code = reply.payload.get("code")
        text = reply.payload.get("message")
        if not isinstance(code, str) or code == "":
            raise UniPadError(
                ErrorCode.QMP_PROTOCOL_ERROR,
                "guest error envelope without a stable code",
                reply_id=reply.id,
            )
        raise UniPadError(code, text if isinstance(text, str) else "guest reported an error", reply_id=reply.id)

    @staticmethod
    def _install_response_from_error(reply: Message) -> InstallResponse:
        """Convert a guest error envelope into a failed install response."""
        code = reply.payload.get("code")
        text = reply.payload.get("message")
        return InstallResponse(
            stage="failed",
            error_code=code if isinstance(code, str) and code else ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
            error_message=text if isinstance(text, str) else "guest reported an error",
        )

    @staticmethod
    def _reject_fabricated_success(response: InstallResponse, reply: Message) -> None:
        """Guard against a reply that claims success without the required fields.

        A success stage must name the bundle that was registered; a reply that
        claims ``installed``/``registered`` without one, or that carries both a
        success stage and an error code, is ambiguous and rejected.
        """
        if response.error_code is not None and response.stage in ("installed", "registered"):
            raise GuestClient._protocol_error("install reply claims success and an error at once", reply=reply)
        if response.stage in ("installed", "registered") and not response.bundle_id:
            raise GuestClient._protocol_error("install reply claims success without bundle_id", reply=reply)


__all__ = ["Transport", "GuestClient", "DEFAULT_ATTEMPTS", "DEFAULT_BACKOFF_S"]
