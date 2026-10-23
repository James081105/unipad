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

:func:`create_install_channel` is the package's zero-argument channel factory:
the one seam the controller's ``guest_channel`` capability resolves to and calls
with no arguments.  It lives here, beside the re-exports, because it is the
package's answer to "build the default install channel" -- it wires a transport
into :class:`GuestClient` when a real guest provides one, and refuses cleanly
when this host has no guest to reach.
"""

from __future__ import annotations

from typing import Optional

from unipad.errors import ErrorCode, UniPadError
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


def create_install_channel(transport: Optional[Transport] = None) -> GuestClient:
    """Build the default guest install channel (ADR-0003, ADR-0008).

    This is the factory the controller's ``guest_channel`` capability resolves
    to.  ``cli._call_capability`` invokes whatever it finds as a **zero-argument**
    factory, so this is the single seam that knows how to turn a message
    transport into a :class:`GuestClient` -- the one channel :mod:`unipad.guest`
    exports whose constructor takes a ``transport`` the caller cannot supply.

    Given a ``transport`` it returns a ready client: that is how a host that
    *has* a real guest wires one up.  Given nothing -- the only call the
    controller makes -- it refuses rather than guessing.  :mod:`unipad.guest`
    ships the client, the protocol and in-memory fakes, but no transport to a
    real guest; building one from a fake would report an install that never
    happened (PRD 9.1 / appendix A).  The refusal is a stable
    ``install_service_unavailable`` so the controller surfaces
    ``guest_channel_unavailable`` -- "a channel that exists but cannot be
    constructed without a transport this host has no guest to provide" --
    instead of the bare ``TypeError`` a zero-argument ``GuestClient()`` used to
    raise (recorded in ADR-0008 and deferred to the owner of this module).

    Args:
        transport: object exposing ``request(message: dict) -> dict``.  ``None``
            (the default, and the controller's call) means no real guest is
            reachable.

    Returns:
        GuestClient: bound to ``transport``.

    Raises:
        UniPadError: ``install_service_unavailable`` with reason
            ``no_guest_transport`` when called with no transport.
    """
    if transport is None:
        raise UniPadError(
            ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
            "no guest transport is available to build an install channel",
            reason="no_guest_transport",
        )
    return GuestClient(transport)

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
    "create_install_channel",
    # fake
    "FakeGuestService",
    "FakeTransport",
    "FakeTcpPair",
    "start_tcp_server",
    "derive_challenge_value",
]
