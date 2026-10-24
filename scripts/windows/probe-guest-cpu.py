"""Where is the guest CPU after the DarwinBoot hand-off?

The serial log stops at "DARWINBOOT: hand-off to the kernel" and the
framebuffer never changes, so there are two possibilities that look
identical from the outside: the kernel is running and silent, or it is
wedged.  QMP can tell them apart -- `info registers` and a disassembly at
the program counter show whether the CPU is executing or spinning.

It reuses this repository's own argv builder so the machine under test is
byte-for-byte the one `unipad.host.unixnu_boot.boot()` starts; nothing here
re-implements the QEMU command line.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

# Usage: probe-guest-cpu.py [SECONDS] [BUNDLE] [ADT] [WORKDIR]
#   BUNDLE  the runner's boot-inputs directory (UniXNU.efi, kernel.macho,
#           DarwinBoot.efi); ADT is this host's generated device tree.
ARGV = sys.argv[1:]
SECONDS = float(ARGV[0]) if ARGV else 90.0
BUNDLE = Path(ARGV[1]) if len(ARGV) > 1 else Path("local-private/boot7/boot-inputs")
ADT = Path(ARGV[2]) if len(ARGV) > 2 else Path("local-private/boot6/kernel.macho.adt")
WORK = Path(ARGV[3]) if len(ARGV) > 3 else Path("local-private/boot7/probe")

from unipad.host.unixnu_boot import build_argv  # noqa: E402
from unipad.host.adt import read_board_adt  # noqa: E402
from unipad.host.unixnu_boot import read_board_payload  # noqa: E402

KERNEL = BUNDLE / "unixnu-out/qemu/kernel/kernel.macho"
DARWINBOOT = BUNDLE / "unixnu-out/qemu/tree/build/darwinboot-efi/DarwinBoot.efi"
UNIXNU = BUNDLE / "UniXNU.efi"
QEMU = Path(os.environ.get("UNIPAD_QEMU", "local-private/tools/qemu/qemu-system-aarch64.exe"))
FW = Path(os.environ.get("UNIPAD_FIRMWARE", "local-private/tools/qemu/share/edk2-aarch64-code.fd"))
DEVICE = REPO / "vendor/UniXNU-qemu"


class Inputs:
    firmware = str(FW)
    kernel = str(KERNEL)
    devicetree = str(ADT)
    darwinboot = str(DARWINBOOT)
    unixnu = str(UNIXNU)
    payload_toml = None


WORK.mkdir(parents=True, exist_ok=True)

# The ESP, staged by this repository's own code so the probe runs the same
# volume boot() would.  build_argv only names the directory.
from unipad.host.unixnu_boot import stage_esp  # noqa: E402

stage_esp(Inputs, WORK / "esp", read_board_payload(DEVICE))

PORT = 14555

WORK.mkdir(parents=True, exist_ok=True)


argv = build_argv(
    QEMU,
    Inputs,
    WORK / "esp",
    WORK,
    qmp_port=PORT,
    memory_mib=6144,
    display="none",
)
# The module's watcher is not used here; this probe drives QMP itself.
argv = [a.replace(f"tcp:127.0.0.1:{PORT}", f"tcp:127.0.0.1:{PORT}") for a in argv]

serial = WORK / "serial.log"
# The writable vars flash: boot() sizes it to the code flash and leaves an
# existing one alone, because it holds the variables of a previous boot.
code_size = FW.stat().st_size
vars_path = WORK / "vars.fd"
if not vars_path.is_file() or vars_path.stat().st_size < code_size:
    with vars_path.open("wb") as handle:
        handle.truncate(code_size)

proc = subprocess.Popen(
    [str(QEMU), *argv],
    cwd=str(WORK),
    stdin=subprocess.DEVNULL,
    stdout=(WORK / "qemu-stdout.log").open("wb"),
    stderr=(WORK / "qemu-stderr.log").open("wb"),
)
print("pid", proc.pid)


def qmp(execute, **arguments):
    with socket.create_connection(("127.0.0.1", PORT), timeout=15) as sock:
        sock.settimeout(30)
        stream = sock.makefile("rwb", buffering=0)
        stream.readline()
        reply = {}
        for request in ({"execute": "qmp_capabilities"}, {"execute": execute, "arguments": arguments}):
            stream.write((json.dumps(request) + "\n").encode())
            while True:
                candidate = json.loads(stream.readline())
                if "return" in candidate or "error" in candidate:
                    # break, not return: returning here answers with the
                    # capabilities reply and never sends the command -- the exact
                    # bug fixed in unipad.host.unixnu_boot._qmp.
                    reply = candidate
                    break
        return reply


def hmon(command):
    return qmp("human-monitor-command", **{"command-line": command})


start = time.monotonic()
ready = None
while time.monotonic() - start < 30:
    if proc.poll() is not None:
        print("qemu exited early", proc.returncode)
        sys.exit(1)
    try:
        ready = qmp("query-status")
        break
    except OSError:
        time.sleep(0.25)
print("ready", ready, f"{time.monotonic() - start:.1f}s")

deadline = start + SECONDS
samples = []
while time.monotonic() < deadline and proc.poll() is None:
    time.sleep(10)
    if proc.poll() is not None:
        break
    status = qmp("query-status").get("return", {})
    regs = hmon("info registers")
    pc = ""
    for line in (regs.get("return") or "").splitlines():
        if line.strip().startswith("PC=") or " PC " in line:
            pc = line.strip()
            break
    elapsed = time.monotonic() - start
    samples.append((round(elapsed, 1), status.get("status"), pc))
    print(f"t={elapsed:6.1f}s status={status.get('status')} {pc}")

# A disassembly at the program counter, and a stack-ish look at the fault state.
print("--- info registers ---")
print(hmon("info registers").get("return"))
print("--- x/8i $pc ---")
print(hmon("x/8i $pc").get("return"))
print("--- info mtree (first 20 lines) ---")
print("\n".join((hmon("info mtree").get("return") or "").splitlines()[:20]))

try:
    qmp("quit")
except OSError:
    proc.terminate()
try:
    proc.wait(timeout=30)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait(timeout=30)
print("qemu exit", proc.returncode)
print("serial bytes", serial.stat().st_size if serial.is_file() else 0)
