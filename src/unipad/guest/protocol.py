"""Guest-side JSON message contracts (PRD sections 9.1, 9.10).

Every message exchanged between the host controller and the guest install
service is one JSON object on one line (JSON Lines / newline-delimited JSON).
The envelope is ``{"type": ..., "id": ..., "payload": {...}}``; the payload
shapes are the frozen dataclasses below.

Design rules enforced here:

- JSON-serializable in both directions (``to_dict`` / ``from_dict``); the
  encode/decode helpers never raise bare ``ValueError`` for guest data -- a
  malformed reply is surfaced as :class:`unipad.errors.UniPadError` with a
  stable code by the caller (``unipad.guest.client``).
- The wire vocabulary is closed (:data:`KNOWN_TYPES`): :func:`decode` reports
  an unknown message type as ``unsupported_command`` instead of dropping it.
- No secrets in any message (PRD 9.2): package bytes, hashes and derived
  challenge values only -- never tokens, keys or user documents.
- Guest-reported ``error_code`` values are already stable taxonomy codes
  (PRD section 10); they are passed through verbatim, never invented here.

Dependency direction: this module imports only :mod:`unipad.errors` and the
standard library.  It must never import :mod:`unipad.ipa` or
:mod:`unipad.host`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from unipad.errors import ErrorCode, UniPadError

SCHEMA_VERSION = 1

# --- Message type identifiers (wire contract) ---
MSG_INSTALL_REQUEST = "install.request"
MSG_INSTALL_PROGRESS = "install.progress"
MSG_INSTALL_RESPONSE = "install.response"
MSG_CHALLENGE_REQUEST = "challenge.request"
MSG_CHALLENGE_RESPONSE = "challenge.response"
MSG_STATUS_REQUEST = "status.request"
MSG_STATUS_RESPONSE = "status.response"
MSG_ERROR = "error"

#: Stage vocabulary shared with :class:`unipad.models.InstallResult`.
#: ``registered`` is the furthest stage a successful install reaches.
STAGES = ("received", "checked", "transferred", "installed", "registered", "failed")

#: Closed vocabulary of message types on the wire.  Anything else is not part
#: of the contract and must be reported, never silently dropped (PRD 9.1).
KNOWN_TYPES: frozenset[str] = frozenset(
    {
        MSG_INSTALL_REQUEST,
        MSG_INSTALL_PROGRESS,
        MSG_INSTALL_RESPONSE,
        MSG_CHALLENGE_REQUEST,
        MSG_CHALLENGE_RESPONSE,
        MSG_STATUS_REQUEST,
        MSG_STATUS_RESPONSE,
        MSG_ERROR,
    }
)

#: Injected failure point (PRD section 10 taxonomy) -> stable error code.
FAILURE_CODES: dict[str, str] = {
    "signature": ErrorCode.SIGNATURE_OR_POLICY_REJECTED,
    "install_service": ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
    "registration": ErrorCode.REGISTRATION_FAILED,
    "dyld": ErrorCode.DYLD_FAILED,
    "dependency": ErrorCode.MISSING_DEPENDENCY,
    "transfer": ErrorCode.TRANSFER_FAILED,
    "invalid_package": ErrorCode.INVALID_PACKAGE,
}


def new_message_id() -> str:
    """Return a fresh message id (UUID4 hex, no separators).

    Ids are request/response correlation handles only.  They are not secrets
    and carry no meaning beyond one exchange.
    """
    return uuid.uuid4().hex


class DictView:
    """Read-only mapping access over a dataclass with ``to_dict``.

    The protocol types are returned as real dataclasses (typed APIs), while
    callers and tests may still subscript them as ``obj["field"]`` or compare
    them to plain dicts.  Nothing here mutates state.
    """

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def __getitem__(self, key: str) -> Any:
        try:
            return self.to_dict()[key]
        except KeyError as exc:
            raise KeyError(key) from exc

    def keys(self) -> Any:
        return self.to_dict().keys()

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DictView):
            return self.to_dict() == other.to_dict()
        if isinstance(other, Mapping):
            return self.to_dict() == dict(other)
        return NotImplemented

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.to_dict()!r})"

    __hash__ = object.__hash__


@dataclass(eq=False)
class Message(DictView):
    """JSON Lines envelope: one type, one correlation id, one payload object."""

    type: str
    id: str = field(default_factory=new_message_id)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "payload": dict(self.payload)}

    def to_json(self) -> str:
        """Serialize without newlines so one message is exactly one line."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Message":
        """Decode an envelope, rejecting non-conforming shapes.

        Raises:
            UniPadError: with ``qmp_protocol_error`` when the envelope is not
                a JSON object with a string ``type``, a string ``id`` and an
                object ``payload``.
        """
        if not isinstance(data, Mapping):
            raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, "message envelope is not a JSON object")
        mtype = data.get("type")
        mid = data.get("id")
        payload = data.get("payload", {})
        if not isinstance(mtype, str) or not mtype:
            raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, "message envelope missing string 'type'")
        if not isinstance(mid, str) or not mid:
            raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, "message envelope missing string 'id'")
        if not isinstance(payload, dict):
            raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, "message envelope 'payload' is not an object")
        return cls(type=mtype, id=mid, payload=dict(payload))


@dataclass(eq=False)
class InstallRequest(DictView):
    """Host -> guest install request (package metadata only; bytes go via transfer)."""

    package_name: str
    package_sha256: str
    size_bytes: int
    declared_min_os: str
    signature_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_name": self.package_name,
            "package_sha256": self.package_sha256,
            "size_bytes": self.size_bytes,
            "declared_min_os": self.declared_min_os,
            "signature_mode": self.signature_mode,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InstallRequest":
        return cls(
            package_name=_req_str(data, "package_name"),
            package_sha256=_req_str(data, "package_sha256"),
            size_bytes=_req_int(data, "size_bytes"),
            declared_min_os=_req_str(data, "declared_min_os"),
            signature_mode=_req_str(data, "signature_mode"),
        )


@dataclass(eq=False)
class InstallProgress(DictView):
    """Guest -> host progress notification (informational; never a success claim)."""

    stage: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InstallProgress":
        return cls(stage=_req_str(data, "stage"), detail=str(data.get("detail", "")))


@dataclass(eq=False)
class InstallResponse(DictView):
    """Guest -> host install outcome (PRD 9.1 install return contract).

    ``stage`` is the furthest stage actually completed; a successful file
    transfer alone must never report ``installed``/``registered``.
    ``trust_mode`` records how the guest trusts the package and is never
    assumed by the host.
    """

    stage: str
    bundle_id: Optional[str] = None
    trust_mode: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "bundle_id": self.bundle_id,
            "trust_mode": self.trust_mode,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InstallResponse":
        return cls(
            stage=_req_str(data, "stage"),
            bundle_id=_opt_str(data.get("bundle_id")),
            trust_mode=_opt_str(data.get("trust_mode")),
            error_code=_opt_str(data.get("error_code")),
            error_message=_opt_str(data.get("error_message")),
        )

    @property
    def ok(self) -> bool:
        """True only when a terminal success stage was reached with no error."""
        return self.stage in ("installed", "registered") and self.error_code is None


@dataclass(eq=False)
class ChallengeRequest(DictView):
    """Host -> guest challenge request (PRD 5.2 step 5: show this run's value)."""

    nonce: str

    def to_dict(self) -> dict[str, Any]:
        return {"nonce": self.nonce}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChallengeRequest":
        return cls(nonce=_req_str(data, "nonce"))


@dataclass(eq=False)
class ChallengeResponse(DictView):
    """Guest -> host challenge answer (deterministic derivation over the nonce)."""

    nonce: str
    value: str

    def to_dict(self) -> dict[str, Any]:
        return {"nonce": self.nonce, "value": self.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChallengeResponse":
        return ChallengeResponse(nonce=_req_str(data, "nonce"), value=_req_str(data, "value"))


@dataclass(eq=False)
class StatusRequest(DictView):
    """Host -> guest status query (M9b/M12 persistence verification)."""

    def to_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StatusRequest":
        return cls()


@dataclass(eq=False)
class StatusResponse(DictView):
    """Guest -> host installed apps and data volume markers."""

    installed: list[dict[str, Any]] = field(default_factory=list)
    data_markers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"installed": [dict(a) for a in self.installed], "data_markers": list(self.data_markers)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StatusResponse":
        installed = data.get("installed", [])
        markers = data.get("data_markers", [])
        if not isinstance(installed, list) or not all(isinstance(a, dict) for a in installed):
            raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, "status response 'installed' is not a list of objects")
        if not isinstance(markers, list) or not all(isinstance(m, str) for m in markers):
            raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, "status response 'data_markers' is not a list of strings")
        return cls(installed=[dict(a) for a in installed], data_markers=list(markers))


# --- small helpers: strict, non-fabricating field extraction ---

def _req_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value == "":
        raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, f"missing or non-string field '{key}'")
    return value


def _req_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, f"missing or non-integer field '{key}'")
    return value


def _opt_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


# --- JSON Lines encode / decode ---

def encode(message: Message) -> bytes:
    """Encode one message as a single UTF-8 JSON line (no trailing newline)."""
    return message.to_json().encode("utf-8")


def decode(line: bytes | str) -> Message:
    """Decode one JSON line into a :class:`Message`.

    The wire vocabulary is closed (:data:`KNOWN_TYPES`): an envelope naming any
    other type is rejected here so a guest or a stale peer cannot inject a
    message the host would otherwise ignore without noticing.

    Raises:
        UniPadError: ``qmp_protocol_error`` for malformed JSON or a
            non-conforming envelope; ``unsupported_command`` for an envelope
            whose ``type`` is outside the contract.
    """
    message = Message.from_dict(_decode_object(line))
    if message.type not in KNOWN_TYPES:
        raise UniPadError(
            ErrorCode.UNSUPPORTED_COMMAND,
            f"unknown guest message type '{message.type}'",
            message_type=message.type,
            known_types=sorted(KNOWN_TYPES),
        )
    return message


def _decode_object(line: bytes | str) -> Any:
    """Parse one JSON line into the object it carries (envelope-agnostic)."""
    if isinstance(line, bytes | bytearray):
        try:
            text = bytes(line).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, "message line is not valid UTF-8") from exc
    else:
        text = str(line)
    text = text.strip()
    if not text:
        raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, "empty message line")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UniPadError(ErrorCode.QMP_PROTOCOL_ERROR, f"message line is not valid JSON: {exc}") from exc


def error_message(code: str, message: str, message_id: Optional[str] = None) -> Message:
    """Build a guest-side error envelope carrying a stable code."""
    return Message(type=MSG_ERROR, id=message_id or new_message_id(), payload={"code": code, "message": message})


__all__ = [
    "SCHEMA_VERSION",
    "MSG_INSTALL_REQUEST",
    "MSG_INSTALL_PROGRESS",
    "MSG_INSTALL_RESPONSE",
    "MSG_CHALLENGE_REQUEST",
    "MSG_CHALLENGE_RESPONSE",
    "MSG_STATUS_REQUEST",
    "MSG_STATUS_RESPONSE",
    "MSG_ERROR",
    "STAGES",
    "KNOWN_TYPES",
    "FAILURE_CODES",
    "new_message_id",
    "DictView",
    "Message",
    "InstallRequest",
    "InstallProgress",
    "InstallResponse",
    "ChallengeRequest",
    "ChallengeResponse",
    "StatusRequest",
    "StatusResponse",
    "encode",
    "decode",
    "error_message",
]
