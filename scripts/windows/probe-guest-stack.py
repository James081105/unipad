"""The call stack of the spinning guest CPU.

``probe_cpu.py`` showed the guest parked at ``_ml_get_timebase`` + 0x14 for
over a minute.  A PC names a function; the frame chain names the reason.
X29 is the frame pointer on this ABI, ``[x29]`` is the caller's frame and
``[x29 + 8]`` is the return address, so the chain can be walked from the
register dump and every return address symbolized against kernel.macho's
own symbol table.

Read-only: it reads QEMU memory over QMP and the kernel's Mach-O headers.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

# Usage: probe-guest-stack.py [SECONDS] [BUNDLE] [ADT] [WORKDIR]
ARGV = sys.argv[1:]
SECONDS = float(ARGV[0]) if len(ARGV) > 1 else 60.0
BUNDLE = Path(ARGV[1]) if len(ARGV) > 1 else Path("local-private/boot7/boot-inputs")
ADT = Path(ARGV[2]) if len(ARGV) > 2 else Path("local-private/boot6/kernel.macho.adt")
WORK = Path(ARGV[3]) if len(ARGV) > 3 else Path("local-private/boot7/stack")

from unipad.host.unixnu_boot import build_argv, read_board_payload, stage_esp  # noqa: E402

KERNEL = BUNDLE / "unixnu-out/qemu/kernel/kernel.macho"
DARWINBOOT = BUNDLE / "unixnu-out/qemu/tree/build/darwinboot-efi/DarwinBoot.efi"
UNIXNU = BUNDLE / "UniXNU.efi"
QEMU = Path(os.environ.get("UNIPAD_QEMU", "local-private/tools/qemu/qemu-system-aarch64.exe"))
FW = Path(os.environ.get("UNIPAD_FIRMWARE", "local-private/tools/qemu/share/edk2-aarch64-code.fd"))
DEVICE = Path("vendor/UniXNU-qemu")

PORT = 14566
FRAMES = 24


class Inputs:
    firmware = str(FW)
    kernel = str(KERNEL)
    devicetree = str(ADT)
    darwinboot = str(DARWINBOOT)
    unixnu = str(UNIXNU)
    payload_toml = None


WORK.mkdir(parents=True, exist_ok=True)
stage_esp(Inputs, WORK / "esp", read_board_payload(DEVICE))
argv = build_argv(QEMU, Inputs, WORK / "esp", WORK, qmp_port=PORT, memory_mib=6144, display="none")

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


def qmp(execute, **arguments):
    with socket.create_connection(("127.0.0.1", PORT), timeout=15) as sock:
        sock.settimeout(60)
        stream = sock.makefile("rwb", buffering=0)
        stream.readline()
        reply = {}
        for request in ({"execute": "qmp_capabilities"}, {"execute": execute, "arguments": arguments}):
            stream.write((json.dumps(request) + "\n").encode())
            while True:
                candidate = json.loads(stream.readline())
                if "return" in candidate or "error" in candidate:
                    reply = candidate
                    break
        return reply


def hmon(command):
    return qmp("human-monitor-command", **{"command-line": command})


start = time.monotonic()
while time.monotonic() - start < 30:
    if proc.poll() is not None:
        raise SystemExit(f"qemu exited early: {proc.returncode}")
    try:
        qmp("query-status")
        break
    except OSError:
        time.sleep(0.25)

while time.monotonic() - start < SECONDS and proc.poll() is None:
    time.sleep(5)

regs_text = hmon("info registers").get("return") or ""
fields = {}
for line in regs_text.splitlines():
    for token in line.replace("\n", " ").split():
        if "=" in token:
            key, _, value = token.partition("=")
            if key.startswith(("X", "SP", "PC")) and len(value) == 16:
                try:
                    fields[key] = int(value, 16)
                except ValueError:
                    pass

print("--- registers ---")
print(regs_text.strip()[:1200])

pc = fields.get("PC")
fp = fields.get("X29")
lr = fields.get("X30")
print(f"\nPC={pc:#x} X29(FP)={fp:#x} X30(LR)={lr:#x}")


def read_qword(address):
    """One 8-byte guest read, via the HMP's memory dump."""
    text = hmon(f"x/1gx {address:#x}").get("return") or ""
    for token in text.split():
        if token.startswith("0x") and len(token) == 18:
            try:
                return int(token, 16)
            except ValueError:
                continue
    return None


chain = []
seen = set()
frame = fp
for _ in range(FRAMES):
    if frame is None or frame in seen or frame < 0x1000:
        break
    seen.add(frame)
    nxt = read_qword(frame)
    ret = read_qword(frame + 8)
    chain.append((frame, ret))
    frame = nxt
    if frame is None:
        break

print("\n--- frame chain ---")
for index, (frame, ret) in enumerate(chain):
    print(f"  [{index:2d}] fp={frame:#014x} ret={ret if ret is None else hex(ret)}")

# --- symbolize -----------------------------------------------------------
blob = KERNEL.read_bytes()
magic, = struct.unpack_from("<I", blob, 0)
# MH_MAGIC_64 is 0xFEEDFACF in file order; a little-endian read of a
# little-endian file yields exactly that.  0xCFFAEDFE is the byte-swapped
# spelling, which is what a big-endian file would give.
if magic != 0xFEEDFACF:
    raise SystemExit(f"not a 64-bit Mach-O: {magic:#x}")
header = struct.unpack_from("<7I", blob, 0)
ncmds = header[4]
segments = []
symtab = None
at = 32
for _ in range(ncmds):
    cmd, cmdsize = struct.unpack_from("<II", blob, at)
    if cmd == 0x19:
        segname = blob[at + 8:at + 24].rstrip(b"\0").decode()
        vmaddr, vmsize, fileoff, filesize = struct.unpack_from("<4Q", blob, at + 24)
        segments.append((segname, vmaddr, vmsize, fileoff, filesize))
    elif cmd == 0x2:
        symtab = struct.unpack_from("<4I", blob, at + 8)
    at += cmdsize

symoff, nsyms, stroff, strsize = symtab
symbols = []
for index in range(nsyms):
    n_strx, n_type, n_sect, n_desc, n_value = struct.unpack_from("<IBBHQ", blob, symoff + index * 16)
    if n_strx and n_value:
        symbols.append((n_value, blob[stroff + n_strx: blob.index(b"\0", stroff + n_strx)].decode("utf-8", "replace")))
symbols.sort()
values = [v for v, _ in symbols]


def name_of(address):
    if address is None:
        return "(unreadable)"
    import bisect

    index = bisect.bisect_right(values, address) - 1
    if index < 0:
        return f"{address:#x} (no symbol)"
    base, name = symbols[index]
    return f"{name}+{address - base:#x}"


print("\n--- symbolized ---")
print(f"  PC   {name_of(pc)}")
print(f"  LR   {name_of(lr)}")
for frame, ret in chain:
    print(f"  ret  {name_of(ret)}   (fp {frame:#x})")

try:
    qmp("quit")
except OSError:
    proc.terminate()
try:
    proc.wait(timeout=30)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait(timeout=30)
print("\nqemu exit", proc.returncode)
