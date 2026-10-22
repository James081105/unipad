"""Duck-typed guest install channel double (PRD 9.1 / 10, testing-strategy D3).

:class:`unipad.ipa.installer.IpaInstaller` never imports
:mod:`unipad.guest`; it only needs an object with ``send_package`` and
``request_install``.  That is exactly the shape :class:`RecordingGuestChannel`
provides, so the installer's stage machine can be driven on Windows without a
guest, a socket or a real install service.

The double is deliberately *honest*:

* ``send_package`` returns the true SHA-256 of the bytes it was handed, so the
  host-side verification in :class:`unipad.ipa.transfer.HostSideTransfer` is
  exercised for real (a fake that always reports the right hash would hide a
  broken verifier).
* ``request_install`` returns exactly the scripted reply.  It never invents a
  success: the default reply is a non-confirming status, so a test that forgets
  to configure one fails loudly instead of looking like a completed install.
* every call is recorded, so a test can prove that a precheck rejection
  short-circuited *before* any bytes were transferred (PRD 10, "包预检").

Determinism (testing-strategy D3): no clock, no randomness, no host state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from unipad.ipa.transfer import TransferReceipt

__all__ = ["RecordingGuestChannel"]


class RecordingGuestChannel:
    """A scripted, recording guest install channel for unit tests.

    Args:
        reply: Mapping the guest install service returns.  ``None`` (the
            default) yields a non-confirming status, which the installer must
            treat as "the guest did not confirm an install".
        reply_json: JSON string the guest returns instead of ``reply`` (the
            real guest speaks JSON Lines, so the normalisation path is worth
            exercising too).
        raise_exception: Exception ``request_install`` raises instead of
            replying (a guest-side crash / dropped link).
        receiver_hash: Digest ``send_package`` reports.  ``None`` computes the
            true one; pass a wrong value to simulate a corrupt receiver.
    """

    def __init__(
        self,
        reply: Mapping[str, Any] | None = None,
        *,
        reply_json: str | None = None,
        raise_exception: BaseException | None = None,
        receiver_hash: str | None = None,
    ) -> None:
        self.reply: Mapping[str, Any] | None = reply
        self.reply_json = reply_json
        self.raise_exception = raise_exception
        self.receiver_hash = receiver_hash
        self.sent_packages: list[dict[str, Any]] = []
        self.install_requests: list[dict[str, Any]] = []

    # -- transport side ----------------------------------------------------

    def send_package(self, name: str, data: bytes) -> TransferReceipt:
        """Receive the package bytes and report their real digest."""
        digest = self.receiver_hash or hashlib.sha256(data).hexdigest()
        self.sent_packages.append(
            {
                "name": name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "reported_sha256": digest,
            }
        )
        return TransferReceipt(
            name=name,
            sha256=digest,
            size_bytes=len(data),
            verified=True,
            chunk_count=1,
            chunk_size=len(data),
            detail="fake-guest-channel",
        )

    # -- install side ------------------------------------------------------

    def request_install(self, bundle_meta: Mapping[str, Any]) -> Any:
        """Record the request and return the scripted (never invented) reply."""
        self.install_requests.append(dict(bundle_meta))
        if self.raise_exception is not None:
            raise self.raise_exception
        if self.reply_json is not None:
            return self.reply_json
        if self.reply is not None:
            return dict(self.reply)
        # No configured reply: the guest says nothing an installer may read as
        # an install.  A JSON string keeps the reply shape identical to the
        # real guest protocol.
        return json.dumps({"status": "unknown_state", "detail": "no reply configured"})

    # -- introspection -----------------------------------------------------

    @property
    def transferred_bytes(self) -> int:
        """Total number of package bytes this channel received."""
        return sum(entry["size_bytes"] for entry in self.sent_packages)

    def reset(self) -> None:
        """Drop everything recorded so far (test isolation)."""
        self.sent_packages.clear()
        self.install_requests.clear()
