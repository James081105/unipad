"""Host -> guest package transfer for the IPA install chain (PRD 9.1 / 10).

The transport boundary is a Protocol, not a concrete class: the guest side
owns the real channel (a virtio-serial link, a shared folder, or the fake
service used by Windows-side tests) and this package must not import it at
module import time.  Anything with a compatible ``send_package`` works.

Three implementations live here:

``GuestTransport``
    The Protocol: ``send_package(name, data) -> TransferReceipt``.

``LoopbackTransport``
    In-memory transport for unit tests and Windows simulation.  It can be
    told to corrupt the receiver hash or to fail, so the ``transfer_failed``
    path is exercised without a guest.

``HostSideTransfer``
    The host-side driver used by :class:`unipad.ipa.installer.IpaInstaller`.
    It reads the IPA in chunks, computes the SHA-256 while reading, sends the
    package, then verifies the receiver's hash against the local one.  A
    mismatch -- or a receiver that reports no hash at all -- raises
    ``transfer_failed``: an unverified transfer is not a transfer.

Resumable chunking
------------------
``HostSideTransfer`` reads with a fixed ``chunk_size`` and reports
``chunk_count``/``chunk_size`` in the receipt.  A transport that can resume
needs exactly those two numbers plus the package SHA-256: chunk ``i`` of a
file whose digest is ``D`` is retransmittable on its own, and a receiver can
discard a partial file whose digest does not match ``D``.  The default
``send_package`` channel is whole-file, so ``resumed_chunks`` is always 0
here; the field exists so a resuming transport can report it without
changing the receipt contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, runtime_checkable

from ..errors import ErrorCode, UniPadError

__all__ = [
    "TransferProgress",
    "TransferReceipt",
    "GuestTransport",
    "LoopbackTransport",
    "HostSideTransfer",
    "iter_chunks",
]

DEFAULT_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class TransferProgress:
    """Progress snapshot handed to a progress callback during a transfer."""

    name: str
    bytes_sent: int
    total_bytes: int
    chunks_sent: int
    chunks_total: int

    @property
    def fraction(self) -> float:
        """Fraction of the package already sent (0.0 - 1.0)."""
        if self.total_bytes <= 0:
            return 0.0
        return min(1.0, self.bytes_sent / self.total_bytes)

    def to_dict(self) -> dict[str, Any]:
        """Return the progress snapshot as a mapping."""
        return {
            "name": self.name,
            "bytes_sent": self.bytes_sent,
            "total_bytes": self.total_bytes,
            "chunks_sent": self.chunks_sent,
            "chunks_total": self.chunks_total,
            "fraction": self.fraction,
        }


@dataclass
class TransferReceipt:
    """Evidence that a package reached the guest (PRD 10, "主客体传输").

    ``sha256`` is the digest *the receiver* computed.  ``verified`` is true
    only when it matches the host-side digest; a transfer whose receipt is
    not verified must be treated as ``transfer_failed``.
    """

    name: str
    sha256: str
    size_bytes: int
    verified: bool = False
    chunk_count: int = 0
    chunk_size: int = 0
    resumed_chunks: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the receipt as a JSON-serialisable mapping."""
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "verified": self.verified,
            "chunk_count": self.chunk_count,
            "chunk_size": self.chunk_size,
            "resumed_chunks": self.resumed_chunks,
            "detail": self.detail,
        }


ProgressCallback = Callable[[TransferProgress], None]


@runtime_checkable
class GuestTransport(Protocol):
    """Channel able to move a package from the host into the guest.

    Implemented by the guest package (``unipad.guest``) and by
    :class:`LoopbackTransport` for tests.  Only ``send_package`` is required
    by the transfer layer; installers additionally look for an optional
    ``request_install``.
    """

    def send_package(self, name: str, data: bytes) -> TransferReceipt:
        """Send one package and return the receiver's evidence."""
        ...


def iter_chunks(path: str | Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
    """Yield the file at ``path`` in ``chunk_size`` byte chunks."""
    if chunk_size <= 0:
        raise UniPadError(
            ErrorCode.CONFIG_INVALID,
            "chunk_size must be positive",
            reason="invalid_chunk_size",
            chunk_size=chunk_size,
        )
    p = Path(path)
    try:
        with p.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    except OSError as exc:
        raise UniPadError(
            ErrorCode.INVALID_PACKAGE,
            f"cannot read package for transfer: {p}",
            reason="unreadable_file",
            path=str(p),
        ) from exc


class LoopbackTransport:
    """In-memory :class:`GuestTransport` for tests and Windows simulation.

    This is a *fake*: it proves the host-side logic (hashing, verification,
    failure mapping) and nothing about a real guest.  Its default install
    reply is deliberately ``install_service_unavailable`` so a test that
    forgets to configure a reply can never look like a successful install.
    """

    def __init__(
        self,
        *,
        install_reply: Mapping[str, Any] | None = None,
        corrupt_receiver_hash: bool = False,
        fail_transfer: bool = False,
    ) -> None:
        """Create an in-memory transport whose install reply is explicit."""
        self.received: dict[str, bytes] = {}
        self.install_requests: list[dict[str, Any]] = []
        self.install_reply: dict[str, Any] = (
            dict(install_reply)
            if install_reply is not None
            else {
                "status": "unavailable",
                "error_code": ErrorCode.INSTALL_SERVICE_UNAVAILABLE,
                "detail": "LoopbackTransport has no configured install reply",
            }
        )
        self.corrupt_receiver_hash = corrupt_receiver_hash
        self.fail_transfer = fail_transfer

    def send_package(self, name: str, data: bytes) -> TransferReceipt:
        """Store the package in memory and return its digest."""
        if self.fail_transfer:
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "loopback transport was asked to fail",
                reason="transport_error",
                name=name,
            )
        self.received[name] = bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        if self.corrupt_receiver_hash:
            digest = "0" * 64
        return TransferReceipt(
            name=name,
            sha256=digest,
            size_bytes=len(data),
            verified=not self.corrupt_receiver_hash,
            chunk_count=1,
            chunk_size=len(data),
            detail="loopback",
        )

    def request_install(self, bundle_meta: Mapping[str, Any]) -> dict[str, Any]:
        """Record the request and return the configured (never invented) reply."""
        self.install_requests.append(dict(bundle_meta))
        return dict(self.install_reply)

    def reset(self) -> None:
        """Drop everything received so far (test isolation)."""
        self.received.clear()
        self.install_requests.clear()


class HostSideTransfer:
    """Drive a package from the host file system into the guest channel.

    The driver owns three things the channel does not: chunked reading (so a
    large IPA is hashed without a second pass), progress reporting, and
    post-transfer verification.  Verification compares the receiver's digest
    with the locally computed one; a mismatch raises ``transfer_failed`` and
    a missing receiver digest is treated as a mismatch, because an unproven
    transfer must not be reported as success (PRD 7.4 / 9.2).
    """

    def __init__(
        self,
        channel: GuestTransport,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        """Bind a transfer driver to a guest channel."""
        if chunk_size <= 0:
            raise UniPadError(
                ErrorCode.CONFIG_INVALID,
                "chunk_size must be positive",
                reason="invalid_chunk_size",
                chunk_size=chunk_size,
            )
        self._channel = channel
        self.chunk_size = chunk_size

    @property
    def channel(self) -> GuestTransport:
        """The underlying transport."""
        return self._channel

    def send_file(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> TransferReceipt:
        """Send a file and verify the receiver's hash.

        Args:
            path: Package file to send.
            name: Name the guest should store it under (defaults to the
                file name).
            progress: Optional callback invoked once per chunk.

        Returns:
            TransferReceipt: verified receipt (``verified`` is always true
                when this returns; failures raise instead).

        Raises:
            UniPadError: ``transfer_failed`` when the transport fails or the
                receiver's digest does not match; ``invalid_package`` when
                the file cannot be read.
        """
        p = Path(path)
        package_name = name or p.name
        total = p.stat().st_size if p.is_file() else 0
        chunks_total = (total + self.chunk_size - 1) // self.chunk_size if total else 0

        digester = hashlib.sha256()
        buffer = bytearray()
        sent = 0
        chunks = 0
        for chunk in iter_chunks(p, self.chunk_size):
            digester.update(chunk)
            buffer.extend(chunk)
            sent += len(chunk)
            chunks += 1
            if progress is not None:
                progress(
                    TransferProgress(
                        name=package_name,
                        bytes_sent=sent,
                        total_bytes=total,
                        chunks_sent=chunks,
                        chunks_total=chunks_total,
                    )
                )
        local_sha = digester.hexdigest()
        data = bytes(buffer)
        del buffer

        try:
            receipt = self._channel.send_package(package_name, data)
        except UniPadError:
            raise
        except Exception as exc:  # transport bug / dropped link
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "package transport raised an unexpected error",
                reason="transport_error",
                name=package_name,
                error_type=type(exc).__name__,
            ) from exc

        return self._verify(package_name, local_sha, sent, chunks, self.chunk_size, receipt)

    def send_bytes(
        self,
        data: bytes,
        name: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> TransferReceipt:
        """Send an in-memory package and verify the receiver's hash."""
        local_sha = hashlib.sha256(data).hexdigest()
        total = len(data)
        chunks_total = (total + self.chunk_size - 1) // self.chunk_size if total else 0
        if progress is not None:
            progress(
                TransferProgress(
                    name=name,
                    bytes_sent=total,
                    total_bytes=total,
                    chunks_sent=chunks_total,
                    chunks_total=chunks_total,
                )
            )
        try:
            receipt = self._channel.send_package(name, data)
        except UniPadError:
            raise
        except Exception as exc:
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "package transport raised an unexpected error",
                reason="transport_error",
                name=name,
                error_type=type(exc).__name__,
            ) from exc
        return self._verify(name, local_sha, total, chunks_total, self.chunk_size, receipt)

    def _verify(
        self,
        name: str,
        local_sha: str,
        size_bytes: int,
        chunks: int,
        chunk_size: int,
        receipt: Any,
    ) -> TransferReceipt:
        """Compare the receiver's evidence with the local digest."""
        receiver_sha, receiver_size, detail = _coerce_receipt(receipt, name)
        if not receiver_sha:
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "receiver did not report a package hash",
                reason="receiver_hash_missing",
                name=name,
                local_sha256=local_sha,
            )
        if receiver_sha.lower() != local_sha:
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "receiver hash does not match the host hash",
                reason="hash_mismatch",
                name=name,
                local_sha256=local_sha,
                receiver_sha256=receiver_sha.lower(),
            )
        if receiver_size is not None and receiver_size != size_bytes:
            raise UniPadError(
                ErrorCode.TRANSFER_FAILED,
                "receiver size does not match the host size",
                reason="size_mismatch",
                name=name,
                local_size_bytes=size_bytes,
                receiver_size_bytes=receiver_size,
            )
        return TransferReceipt(
            name=name,
            sha256=local_sha,
            size_bytes=size_bytes,
            verified=True,
            chunk_count=chunks,
            chunk_size=chunk_size,
            resumed_chunks=0,
            detail=detail,
        )


def _coerce_receipt(receipt: Any, name: str) -> tuple[str | None, int | None, str]:
    """Normalise a channel reply into ``(sha256, size_bytes, detail)``.

    Accepts a :class:`TransferReceipt`, a mapping, or any object exposing
    ``sha256``/``hash`` and ``size_bytes``/``size`` attributes, so a guest
    implementation is not forced to import this module.
    """
    if receipt is None:
        return None, None, ""
    if isinstance(receipt, TransferReceipt):
        return receipt.sha256, receipt.size_bytes, receipt.detail
    if isinstance(receipt, Mapping):
        sha = receipt.get("sha256") or receipt.get("hash") or receipt.get("sha256_hex")
        size = receipt.get("size_bytes")
        if size is None:
            size = receipt.get("size")
        detail = str(receipt.get("detail") or receipt.get("status") or "")
        return (str(sha) if sha else None), (int(size) if isinstance(size, int) else None), detail
    sha = getattr(receipt, "sha256", None) or getattr(receipt, "hash", None)
    size = getattr(receipt, "size_bytes", None)
    if size is None:
        size = getattr(receipt, "size", None)
    detail = str(getattr(receipt, "detail", "") or "")
    return (str(sha) if sha else None), (int(size) if isinstance(size, int) else None), detail
