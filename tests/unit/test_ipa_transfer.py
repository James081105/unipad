"""Host -> guest transfer tests (PRD 9.1, 10 "主客体传输").

PRD 10 requires the package to reach a controlled guest location **and** the
hash to be verified; PRD 9.1 requires the install interface to report the
package hash and forbids calling a verified file transfer a finished install.
``unipad.ipa.transfer`` owns that boundary: it reads the IPA in chunks, hashes
while reading, hands the bytes to a duck-typed channel and then compares the
receiver's digest with its own.

Contracts pinned here:

* :class:`LoopbackTransport` round-trips a package and returns a **verified**
  receipt whose digest is the true SHA-256 of the bytes;
* :class:`HostSideTransfer` computes the same digest as
  ``unipad.ipa.package.sha256_file`` / ``open_ipa`` for the same file (the
  number PRD 5.2 step 3 asks the run to record);
* a receiver that reports a wrong digest -- or no digest, or a wrong size --
  is ``transfer_failed``: an unverified transfer is not a transfer;
* the progress callback fires once per chunk with monotonic byte counts.

No sockets, no threads, no sleeps: everything runs from ``tmp_path`` files and
in-memory doubles (docs/testing-strategy.md D3).
"""

from __future__ import annotations

import hashlib

import pytest

from unipad.errors import ErrorCode, UniPadError
from unipad.ipa.package import open_ipa, sha256_file
from unipad.ipa.transfer import (
    DEFAULT_CHUNK_SIZE,
    GuestTransport,
    HostSideTransfer,
    LoopbackTransport,
    TransferProgress,
    TransferReceipt,
    iter_chunks,
)
from tests.fixtures.ipa_builder import hello_ipa

#: A chunk size small enough that the fixture IPA spans several chunks.
SMALL_CHUNK = 64


def _chunk_count(size: int, chunk_size: int) -> int:
    """Chunk count the driver should report for ``size`` bytes."""
    return (size + chunk_size - 1) // chunk_size if size else 0


# --------------------------------------------------------------------------
# loopback round-trip
# --------------------------------------------------------------------------


def test_loopback_transport_round_trip_returns_a_verified_receipt(tmp_path):
    """The in-memory channel proves the host-side logic, nothing about a guest."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    data = ipa.read_bytes()
    transport = LoopbackTransport()

    # act
    receipt = transport.send_package(ipa.name, data)

    # assert
    assert isinstance(receipt, TransferReceipt)
    assert receipt.verified is True
    assert receipt.name == ipa.name
    assert receipt.size_bytes == len(data)
    assert receipt.sha256 == hashlib.sha256(data).hexdigest()
    assert transport.received[ipa.name] == data


def test_loopback_transport_satisfies_the_transport_protocol():
    """The channel boundary is a Protocol, so the fake must satisfy it."""
    # act / assert
    assert isinstance(LoopbackTransport(), GuestTransport)


def test_loopback_transport_default_reply_is_not_a_success():
    """A test that forgets to configure a reply can never look installed."""
    # arrange / act
    reply = LoopbackTransport().request_install({"bundle_id": "com.unipad.hello"})

    # assert
    assert reply["error_code"] == ErrorCode.INSTALL_SERVICE_UNAVAILABLE
    assert reply["status"] == "unavailable"


# --------------------------------------------------------------------------
# host-side digest (PRD 5.2 step 3 / 10)
# --------------------------------------------------------------------------


def test_host_side_transfer_computes_the_package_sha256(tmp_path):
    """The receipt digest is the package digest the run must record."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    package = open_ipa(ipa)

    # act
    receipt = HostSideTransfer(LoopbackTransport(), chunk_size=SMALL_CHUNK).send_file(ipa)

    # assert
    assert receipt.verified is True
    assert receipt.sha256 == package.sha256
    assert receipt.sha256 == sha256_file(ipa)
    assert receipt.sha256 == hashlib.sha256(ipa.read_bytes()).hexdigest()
    assert receipt.size_bytes == ipa.stat().st_size


def test_host_side_transfer_reports_the_chunking_it_used(tmp_path):
    """A resuming transport needs chunk_count/chunk_size (module contract)."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    size = ipa.stat().st_size

    # act
    receipt = HostSideTransfer(LoopbackTransport(), chunk_size=SMALL_CHUNK).send_file(ipa)

    # assert
    assert receipt.chunk_size == SMALL_CHUNK
    assert receipt.chunk_count == _chunk_count(size, SMALL_CHUNK)
    assert receipt.chunk_count > 1                      # the file really spans chunks
    assert receipt.resumed_chunks == 0                  # whole-file channel


def test_host_side_transfer_sends_the_whole_file(tmp_path):
    """Every byte of the package must reach the channel exactly once."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    data = ipa.read_bytes()
    transport = LoopbackTransport()

    # act
    HostSideTransfer(transport, chunk_size=SMALL_CHUNK).send_file(ipa, name="guest-name.ipa")

    # assert
    assert list(transport.received) == ["guest-name.ipa"]
    assert transport.received["guest-name.ipa"] == data


def test_send_bytes_round_trip(tmp_path):
    """An in-memory package is verified the same way as a file."""
    # arrange
    payload = b"in-memory package bytes" * 10
    transport = LoopbackTransport()

    # act
    receipt = HostSideTransfer(transport, chunk_size=16).send_bytes(payload, "mem.ipa")

    # assert
    assert receipt.verified is True
    assert receipt.sha256 == hashlib.sha256(payload).hexdigest()
    assert receipt.size_bytes == len(payload)
    assert receipt.chunk_count == _chunk_count(len(payload), 16)
    assert transport.received["mem.ipa"] == payload


def test_iter_chunks_reads_the_whole_file(tmp_path):
    """The chunk generator is the single reader used by the driver."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    data = ipa.read_bytes()

    # act
    chunks = list(iter_chunks(ipa, SMALL_CHUNK))

    # assert
    assert b"".join(chunks) == data
    assert [len(chunk) for chunk in chunks[:-1]] == [SMALL_CHUNK] * (len(chunks) - 1)


def test_iter_chunks_rejects_a_non_positive_chunk_size(tmp_path):
    """A zero/negative chunk size is a configuration error, not a no-op."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        list(iter_chunks(ipa, 0))
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID
    assert excinfo.value.details.get("reason") == "invalid_chunk_size"


def test_transfer_driver_rejects_a_non_positive_chunk_size():
    """The driver validates its own chunk size up front (PRD 7.4: crash early)."""
    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        HostSideTransfer(LoopbackTransport(), chunk_size=-1)
    assert excinfo.value.code == ErrorCode.CONFIG_INVALID


# --------------------------------------------------------------------------
# verification failures (PRD 10 ``transfer_failed``)
# --------------------------------------------------------------------------


def test_tampered_receiver_hash_gives_transfer_failed(tmp_path):
    """A receiver reporting the wrong digest is a failed transfer."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    local = sha256_file(ipa)
    tampered = LoopbackTransport(corrupt_receiver_hash=True)

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        HostSideTransfer(tampered).send_file(ipa)
    assert excinfo.value.code == ErrorCode.TRANSFER_FAILED
    assert excinfo.value.details.get("reason") == "hash_mismatch"
    assert excinfo.value.details.get("local_sha256") == local
    assert excinfo.value.details.get("receiver_sha256") == "0" * 64
    # The bytes still travelled; only the *evidence* was wrong.
    assert tampered.received[ipa.name] == ipa.read_bytes()


def test_transport_error_gives_transfer_failed(tmp_path):
    """A channel that raises is a transport fault, not a silent success."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    transport = LoopbackTransport(fail_transfer=True)

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        HostSideTransfer(transport).send_file(ipa)
    assert excinfo.value.code == ErrorCode.TRANSFER_FAILED
    assert excinfo.value.details.get("reason") == "transport_error"
    assert transport.received == {}


def test_receiver_without_a_hash_gives_transfer_failed(tmp_path):
    """PRD 7.4/9.2: no digest means no proof, so the transfer cannot pass."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")

    class HashlessChannel:
        def send_package(self, name, data):
            return {"size_bytes": len(data)}

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        HostSideTransfer(HashlessChannel()).send_file(ipa)
    assert excinfo.value.code == ErrorCode.TRANSFER_FAILED
    assert excinfo.value.details.get("reason") == "receiver_hash_missing"


def test_receiver_size_mismatch_gives_transfer_failed(tmp_path):
    """A right digest with a wrong size is still unverified evidence."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")

    class WrongSizeChannel:
        def send_package(self, name, data):
            return {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data) - 1,
            }

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        HostSideTransfer(WrongSizeChannel()).send_file(ipa)
    assert excinfo.value.code == ErrorCode.TRANSFER_FAILED
    assert excinfo.value.details.get("reason") == "size_mismatch"


def test_channel_crash_is_mapped_to_transfer_failed(tmp_path):
    """An unexpected channel bug must not surface as a raw traceback."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")

    class CrashingChannel:
        def send_package(self, name, data):
            raise RuntimeError("link dropped")

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        HostSideTransfer(CrashingChannel()).send_file(ipa)
    assert excinfo.value.code == ErrorCode.TRANSFER_FAILED
    assert excinfo.value.details.get("reason") == "transport_error"
    assert excinfo.value.details.get("error_type") == "RuntimeError"


def test_object_shaped_receipt_is_accepted(tmp_path):
    """A guest need not import this module to report a receipt."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    data = ipa.read_bytes()

    class ObjectReceiptChannel:
        def send_package(self, name, payload):
            class _Receipt:
                pass

            receipt = _Receipt()
            receipt.sha256 = hashlib.sha256(payload).hexdigest()
            receipt.size_bytes = len(payload)
            receipt.detail = "object-shaped"
            return receipt

    # act
    receipt = HostSideTransfer(ObjectReceiptChannel()).send_file(ipa)

    # assert
    assert receipt.verified is True
    assert receipt.detail == "object-shaped"
    assert receipt.sha256 == hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# progress reporting
# --------------------------------------------------------------------------


def test_progress_callback_fires_once_per_chunk(tmp_path):
    """Progress is reported per chunk with monotonic, bounded counters."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")
    size = ipa.stat().st_size
    seen: list[TransferProgress] = []

    # act
    HostSideTransfer(LoopbackTransport(), chunk_size=SMALL_CHUNK).send_file(
        ipa, progress=seen.append
    )

    # assert
    assert len(seen) == _chunk_count(size, SMALL_CHUNK)
    assert all(snapshot.name == ipa.name for snapshot in seen)
    assert all(snapshot.total_bytes == size for snapshot in seen)
    assert all(snapshot.chunks_total == len(seen) for snapshot in seen)
    assert [snapshot.bytes_sent for snapshot in seen] == sorted(
        snapshot.bytes_sent for snapshot in seen
    )
    assert seen[-1].bytes_sent == size
    assert seen[-1].fraction == 1.0
    assert 0.0 < seen[0].fraction < 1.0
    assert [snapshot.chunks_sent for snapshot in seen] == list(range(1, len(seen) + 1))


def test_progress_callback_is_optional(tmp_path):
    """A caller that does not want progress still gets a verified transfer."""
    # arrange
    ipa = hello_ipa(tmp_path / "HelloUniPad.ipa")

    # act
    receipt = HostSideTransfer(LoopbackTransport(), chunk_size=SMALL_CHUNK).send_file(ipa)

    # assert
    assert receipt.verified is True


def test_transfer_progress_fraction_is_bounded():
    """A snapshot's fraction is clamped to 0.0-1.0 and is JSON-friendly."""
    # arrange / act
    empty = TransferProgress(name="p", bytes_sent=0, total_bytes=0, chunks_sent=0, chunks_total=0)
    partial = TransferProgress(name="p", bytes_sent=5, total_bytes=10, chunks_sent=1, chunks_total=2)
    overshoot = TransferProgress(name="p", bytes_sent=20, total_bytes=10, chunks_sent=3, chunks_total=2)

    # assert
    assert empty.fraction == 0.0
    assert partial.fraction == 0.5
    assert overshoot.fraction == 1.0
    assert overshoot.to_dict()["fraction"] == 1.0


# --------------------------------------------------------------------------
# receipt contract (PRD 9.1 machine-readable result)
# --------------------------------------------------------------------------


def test_receipt_to_dict_carries_the_verification_evidence():
    """PRD 9.1: the install result must expose the hash and its verification."""
    # arrange / act
    receipt = TransferReceipt(
        name="p.ipa",
        sha256="a" * 64,
        size_bytes=10,
        verified=True,
        chunk_count=2,
        chunk_size=5,
        detail="loopback",
    )

    # assert
    payload = receipt.to_dict()
    assert payload == {
        "name": "p.ipa",
        "sha256": "a" * 64,
        "size_bytes": 10,
        "verified": True,
        "chunk_count": 2,
        "chunk_size": 5,
        "resumed_chunks": 0,
        "detail": "loopback",
    }


def test_default_chunk_size_is_one_mebibyte():
    """The documented default is 1 MiB (a large IPA is hashed in one pass)."""
    # assert
    assert DEFAULT_CHUNK_SIZE == 1024 * 1024


def test_unreadable_package_is_invalid_package(tmp_path):
    """A package that disappears mid-transfer is an invalid package."""
    # arrange
    missing = tmp_path / "gone.ipa"

    # act / assert
    with pytest.raises(UniPadError) as excinfo:
        HostSideTransfer(LoopbackTransport()).send_file(missing)
    assert excinfo.value.code == ErrorCode.INVALID_PACKAGE
    assert excinfo.value.details.get("reason") == "unreadable_file"
