"""Print the QMP command names a QEMU build understands, one per line.

Used by scripts/ci/probe-qemu-hostmem.ps1 for the second gate question: does
this build have ``qqemu.PhyMemMode``?  That is the QMP pair which would let the
host read guest-PHYSICAL memory after the guest enables its MMU -- exactly what
the gdbstub cannot do (fact 194: once the MMU is on, ``m`` and ``M`` answer E14
at every guest-physical address while ``g`` still works, which pins the cause to
the stub addressing the CPU's current VIRTUAL space).

MEASURED on the pinned 11.1.0: 238 commands, none mentioning phymem.

This is Python rather than PowerShell because the PowerShell version of this
same client timed out on ``ReadLine()`` where this one reads the greeting
immediately -- and a client that reads nothing prints "no PhyMemMode", which is
indistinguishable from a build that really has none.  An instrument that cannot
distinguish "absent" from "not read" is worse than no instrument.

``qmp_capabilities`` is sent before anything else, deliberately: a client that
skips it has every command refused, which is how ``pmemsave`` once looked absent
for days on this project.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path


def free_port(start: int = 15999, limit: int = 16100) -> int:
    for port in range(start, limit):
        try:
            probe = socket.create_connection(("127.0.0.1", port), timeout=0.3)
        except OSError:
            return port
        probe.close()
    raise SystemExit(f"no free loopback port in {start}..{limit}")


class Qmp:
    def __init__(self, port: int, timeout: float = 15.0) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        self._read()  # greeting
        self.execute("qmp_capabilities")

    def _read(self) -> dict:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("the QMP socket closed")
            self.buf += chunk
        raw, _, self.buf = self.buf.partition(b"\n")
        return json.loads(raw.decode("utf-8"))

    def execute(self, command: str) -> dict:
        self.sock.sendall(json.dumps({"execute": command}).encode() + b"\n")
        for _ in range(50):
            reply = self._read()
            if "return" in reply or "error" in reply:
                return reply
        raise OSError("no reply arrived")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def main() -> int:
    exe = Path(sys.argv[1])
    port = free_port()
    process = subprocess.Popen(
        [str(exe), "-machine", "none", "-S", "-display", "none", "-monitor", "none",
         "-serial", "none", "-nodefaults",
         "-qmp", f"tcp:127.0.0.1:{port},server=on,wait=off"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        client = None
        while time.monotonic() < deadline and client is None:
            try:
                client = Qmp(port)
            except OSError:
                time.sleep(0.25)
        if client is None:
            print("::error::could not connect to the QMP socket", file=sys.stderr)
            return 2
        try:
            reply = client.execute("query-commands")
            names = [entry["name"] for entry in reply.get("return", [])]
            for name in sorted(names):
                print(name)
            return 0 if names else 2
        finally:
            client.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=20)


if __name__ == "__main__":
    sys.exit(main())
