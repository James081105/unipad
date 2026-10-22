"""Guest-side protocol, client and in-memory fake guest.

Layout:

- :mod:`unipad.guest.protocol` -- JSON Lines message contracts (frozen shapes,
  ``to_dict``/``from_dict``, ``encode``/``decode``).
- :mod:`unipad.guest.client`   -- :class:`GuestClient`, the host-side client
  with bounded retries and verbatim error-code pass-through.
- :mod:`unipad.guest.fake`     -- :class:`FakeGuestService` and its transports,
  the backbone of every simulation/acceptance test without real hardware.

Import boundary: this package must never import :mod:`unipad.ipa` or
:mod:`unipad.host`.  Those packages may use ``unipad.guest`` lazily.  Only
:mod:`unipad.errors` and the standard library are imported here.
"""

from __future__ import annotations

from unipad.guest.protocol import (
    ChallengeRequest,
    ChallengeResponse,
    InstallProgress,
    InstallRequest,
    InstallResponse,
    Message,
    StatusRequest,
    StatusResponse,
    decode,
    encode,
    error_message,
    new_message_id,
)
from unipad.guest.client import GuestClient, Transport
from unipad.guest.fake import (
    FakeGuestService,
    FakeTcpPair,
    FakeTransport,
    derive_challenge_value,
    start_tcp_server,
)

__all__ = [
    # protocol
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
    "new_message_id",
    # client
    "GuestClient",
    "Transport",
    # fake
    "FakeGuestService",
    "FakeTransport",
    "FakeTcpPair",
    "start_tcp_server",
    "derive_challenge_value",
]
