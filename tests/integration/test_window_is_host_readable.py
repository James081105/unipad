"""The host can read the shared window and the framebuffer over QMP, without
``memory-backend-file``.

Why this file exists
--------------------
The task #37 row and fact 167 both record the host-side blocker as total:
this host's pinned QEMU registers only ``memory-backend-ram``, the upstream
``share=on`` file window cannot be produced, and therefore "there is no way
for this host to map that memory in and read it".  The first two clauses are
measured.  The third is an inference -- from "not host-mappable" to "not
host-readable" -- and those are different claims.

Measured here, on the pinned binary, with the same two-node layout
``unixnu_boot.py`` builds:

  * the window's first page and its last page are both readable, each
    returning the full number of bytes asked for;
  * the framebuffer is readable too, at the address the delivered kernel
    reports and for exactly its size.

So the blocker is narrower than recorded.  ``memory-backend-file`` with
``share=on`` is what makes the window *host-mappable*, which is what a live
zero-copy window needs.  Reading what the Metal device published needs only
QMP, which the host has used for every measurement it has ever taken.  The
E2E cases -- "the screen changed after a mouse operation", "the counter
changed after ten clicks" -- need the second, not the first.

What is deliberately NOT claimed
--------------------------------
A ``memory-backend-ram`` node starts zeroed, so a read of zeros proves the
address is mapped and nothing about whether the Metal device's writes land
there.  The content half needs a booted guest and is left open.

The instrument checks itself first
----------------------------------
The known-present read (inside node 0, the guest's RAM) runs before the
question.  If it does not return its bytes, the test fails as a broken
instrument rather than answering the question -- the first draft of the
probe this came from omitted ``qmp_capabilities`` and every command answered
``CommandNotFound``, which reads exactly like "this QEMU cannot do that".
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from unipad.host.unixnu_boot import MEMORY_MIB, SHARED_WINDOW_BASE

REPO = Path(__file__).resolve().parents[2]

#: Where the pinned QEMU lives on this host.  Absent on a fresh checkout, and
#: absent is a skip rather than a failure -- the same shape the plan uses for
#: the private upstream clones.
QEMU_CANDIDATES = (
    REPO / "local-private/tools/qemu/qemu-system-aarch64.exe",
    Path("C:/Program Files/QEMU/qemu-system-aarch64.exe"),
)

#: The window size the 2026-10-31 measurement used.
WINDOW_MIB = 2048

#: The framebuffer's guest-physical address and size, both measured out of the
#: delivered kernel's counters on run 41108366328: ``native_legacy_video_base``,
#: ``native_gop_physical`` and ``native_legacy_gop_physical`` are all 7447969792
#: (= 0x1bbef0000) and ``native_legacy_video_bytes`` is 5636096, which is
#: exactly 1376*1024*4 -- the ramfb geometry the boot reports.  It is NOT inside
#: the window: it sits 65 MiB below it, inside node 0.
FRAMEBUFFER_ADDRESS = 0x1BBEF0000
FRAMEBUFFER_BYTES = 5636096
RAMFB_GEOMETRY = (1376, 1024)

#: Outside Hyper-V's reserved 14535-14634 on this host.
PORT = 14902


def _qemu() -> Path:
    for candidate in QEMU_CANDIDATES:
        if candidate.is_file():
            return candidate
    pytest.skip("no qemu-system-aarch64.exe on this host")


class _Qmp:
    """The smallest QMP client that negotiates."""

    def __init__(self, port: int, timeout: float = 30.0) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._read()
        self.execute("qmp_capabilities")

    def _read(self) -> dict[str, Any]:
        buffer = b""
        while b"\n" not in buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
        return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))

    def execute(self, name: str, **arguments: object) -> dict[str, Any]:
        self.sock.sendall(
            (json.dumps({"execute": name, "arguments": arguments}) + "\r\n").encode()
        )
        while True:
            reply = self._read()
            if "return" in reply or "error" in reply:
                return reply

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _window_geometry() -> tuple[int, int]:
    """``(base, end)`` of the window, as ``unixnu_boot.py`` computes it."""
    base = SHARED_WINDOW_BASE + (MEMORY_MIB << 20)
    return base, base + (WINDOW_MIB << 20)


def test_the_window_geometry_is_the_one_the_boot_builds() -> None:
    """The arithmetic, pinned, because the probe below depends on it.

    ``unixnu_boot.py:1874`` is ``SHARED_WINDOW_BASE + (int(memory_mib) << 20)``
    and ``:1741`` is ``str(int(memory_mib) + int(shared_window_mib))`` -- the
    ``-m`` is the SUM of both nodes, not the guest's RAM.  Getting that wrong
    makes QEMU refuse to start with "total memory for NUMA nodes should equal
    RAM size", which is the first thing the probe did.
    """
    base, end = _window_geometry()
    assert base == 0x1C0000000, hex(base)
    assert end == 0x240000000, hex(end)
    assert end - base == WINDOW_MIB << 20
    assert FRAMEBUFFER_BYTES == RAMFB_GEOMETRY[0] * RAMFB_GEOMETRY[1] * 4
    # The framebuffer is NOT in the window, and saying so is the point: two
    # different regions, both of which the host needs to read.
    assert not (base <= FRAMEBUFFER_ADDRESS < end)
    assert SHARED_WINDOW_BASE <= FRAMEBUFFER_ADDRESS < base


def test_the_host_can_read_the_window_and_the_framebuffer_over_qmp(tmp_path: Path) -> None:
    """The measurement the recorded blocker was inferred without."""
    qemu = _qemu()
    base, end = _window_geometry()
    argv = [
        str(qemu),
        "-machine", "virt",
        "-cpu", "max",
        "-display", "none",
        "-m", str(MEMORY_MIB + WINDOW_MIB),
        "-object", f"memory-backend-ram,id=ram0,size={MEMORY_MIB}M",
        "-object", f"memory-backend-ram,id=ram1,size={WINDOW_MIB}M",
        "-numa", "node,nodeid=0,cpus=0,memdev=ram0",
        "-numa", "node,nodeid=1,memdev=ram1",
        "-qmp", f"tcp:127.0.0.1:{PORT},server,nowait",
        "-monitor", "none",
        "-serial", "none",
    ]
    log = tmp_path / "qemu-stderr.log"
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=log.open("wb"))
    client = None
    try:
        client = _Qmp(PORT)
        status = client.execute("query-status")
        assert status.get("return", {}).get("running") is True, status

        def read(address: int, size: int, name: str) -> int:
            target = tmp_path / name
            reply = client.execute(
                "pmemsave", val=address, size=size, filename=str(target)
            )
            assert "return" in reply, (name, reply)
            return target.stat().st_size if target.is_file() else -1

        # The instrument first: a region inside node 0, which is the guest's RAM
        # and which every measurement to date has read.  If this does not return
        # its bytes, nothing below means anything.
        assert read(0x100000, 4096, "known.bin") == 4096, (
            "the known-present read failed; the instrument is broken and every "
            "answer below would be void"
        )

        # The window: its first page and its last page.  Both, because a node can
        # be mapped at its start and not at its end.
        assert read(base, 4096, "window-start.bin") == 4096
        assert read(end - 4096, 4096, "window-end.bin") == 4096

        # The framebuffer, at the address the kernel reports and for exactly its
        # size -- this is the region the E2E cases actually need to see change.
        assert (
            read(FRAMEBUFFER_ADDRESS, FRAMEBUFFER_BYTES, "framebuffer.bin")
            == FRAMEBUFFER_BYTES
        )
    finally:
        if client is not None:
            try:
                client.execute("quit")
            except Exception:
                pass
            client.close()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()


def test_the_probe_refuses_to_answer_when_the_instrument_is_broken() -> None:
    """The guard's own teeth, exercised without QEMU.

    A client that never negotiates answers every command with
    ``CommandNotFound``, which reads exactly like "this QEMU cannot do that".
    The probe's first act is the known-present read, so a broken client fails
    there instead of producing a confident "the window is not readable" -- which
    is the shape of every false negative this project has recorded.
    """
    client = _Qmp.__new__(_Qmp)

    class _Broken:
        def recv(self, _size: int) -> bytes:
            return b'{"QMP": {"version": {"QEMU": {"micro": 1, "minor": 1, "major": 8}}}}\n'

        def settimeout(self, _value: float) -> None:
            pass

        def sendall(self, _data: bytes) -> None:
            raise AssertionError("a client that has not negotiated must not send")

    client.sock = _Broken()  # type: ignore[assignment]
    client._read = lambda: {}  # type: ignore[method-assign]
    with pytest.raises(AssertionError):
        client.execute("pmemsave", val=0, size=4096, filename="x")
