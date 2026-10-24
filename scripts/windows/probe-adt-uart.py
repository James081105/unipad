"""Does a UART node in the device tree give XNU a serial console?

Established first: the guest CPU is in ``_idle_thread`` ->
``_processor_idle`` -> ``_ml_get_timebase`` -- XNU's idle loop.  The kernel
booted to the scheduler, so the hand-off is complete and the kernel is
healthy.  What is missing is *observation*: no serial output and no
framebuffer change after the hand-off, because the device tree this
project generates carries no UART node at all (44 nodes, none of them
``uart``/``pl011``), so XNU has nothing to bind a serial console to.
QEMU virt's own DTB has one at 0x09000000 -- the very port the firmware and
UniXNU wrote to before the hand-off.

This is the experiment: take the verified tree, add one ``pl011`` node
under ``arm-io``, and boot with it.  Either the serial log fills with
XNU's boot messages or it does not; both outcomes are evidence, and the
second would mean the console is elsewhere.

It does NOT change what this project generates.  ``unipad.host.adt`` still
produces upstream's tree; this writes a variant beside it and boots that.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

# Usage: probe-adt-uart.py [SECONDS] [BUNDLE] [ADT] [WORKDIR]
ARGV = sys.argv[1:]
SECONDS = float(ARGV[0]) if len(ARGV) > 1 else 60.0
BUNDLE = Path(ARGV[1]) if len(ARGV) > 1 else Path("local-private/boot7/boot-inputs")
ADT = Path(ARGV[2]) if len(ARGV) > 2 else Path("local-private/boot6/kernel.macho.adt")
WORK = Path(ARGV[3]) if len(ARGV) > 3 else Path("local-private/boot7/uart")

from unipad.host.unixnu_boot import build_argv, read_board_payload, stage_esp  # noqa: E402
from unipad.host.adt import _parse  # noqa: E402

KERNEL = BUNDLE / "unixnu-out/qemu/kernel/kernel.macho"
DARWINBOOT = BUNDLE / "unixnu-out/qemu/tree/build/darwinboot-efi/DarwinBoot.efi"
UNIXNU = BUNDLE / "UniXNU.efi"
QEMU = Path(os.environ.get("UNIPAD_QEMU", "local-private/tools/qemu/qemu-system-aarch64.exe"))
FW = Path(os.environ.get("UNIPAD_FIRMWARE", "local-private/tools/qemu/share/edk2-aarch64-code.fd"))
DEVICE = Path("vendor/UniXNU-qemu")

PORT = 14577

# The PL011 of the QEMU virt machine: 0x09000000, SPI 1.  The interrupt is
# written the way this tree spells it (cells, not a plain int) -- see arm-io's
# #address-cells / #size-cells and intc's #interrupt-cells.
UART_NAME = "uart"
UART_PROPERTIES = [
    ("name", b"uart\0"),
    ("device_type", b"uart\0"),
    ("compatible", b"arm,pl011\0"),
    ("reg", struct.pack("<4Q", 0, 0x09000000, 0, 0x1000)),
    ("interrupts", struct.pack("<3I", 0, 1, 4)),
    ("clock-frequency", struct.pack("<I", 24000000)),
]


def serialize(node):
    _, properties, children = node
    result = struct.pack("<II", len(properties), len(children))
    for key, data in properties:
        result += struct.pack("<32sI", key.encode() if isinstance(key, str) else key, len(data))
        result += data + bytes(-len(data) % 4)
    return result + b"".join(serialize(child) for child in children)


def find(node, name):
    if node[0] == name:
        return node
    for child in node[2]:
        found = find(child, name)
        if found is not None:
            return found
    return None


blob = ADT.read_bytes()
root, _ = _parse(blob, 0)
arm_io = find(root, "arm-io")
if arm_io is None:
    raise SystemExit("no arm-io node in the device tree")
existing = [child[0] for child in arm_io[2]]
print("arm-io children:", existing)

if UART_NAME in existing:
    print("a uart node is already there")
else:
    arm_io[2].append([UART_NAME, UART_PROPERTIES, []])
    print("added:", UART_NAME, UART_PROPERTIES)

variant = WORK / "kernel.macho.uart.adt"
WORK.mkdir(parents=True, exist_ok=True)
variant.write_bytes(serialize(root))
print(f"wrote {variant} ({variant.stat().st_size} bytes; original {len(blob)} bytes)")


class Inputs:
    firmware = str(FW)
    kernel = str(KERNEL)
    devicetree = str(variant)
    darwinboot = str(DARWINBOOT)
    unixnu = str(UNIXNU)
    payload_toml = None


stage_esp(Inputs, WORK / "esp", read_board_payload(DEVICE))
argv = build_argv(QEMU, Inputs, WORK / "esp", WORK, qmp_port=PORT, memory_mib=6144, display="none")

code_size = FW.stat().st_size
vars_path = WORK / "vars.fd"
if not vars_path.is_file() or vars_path.stat().st_size < code_size:
    with vars_path.open("wb") as handle:
        handle.truncate(code_size)

serial = WORK / "serial.log"
proc = subprocess.Popen(
    [str(QEMU), *argv],
    cwd=str(WORK),
    stdin=subprocess.DEVNULL,
    stdout=(WORK / "qemu-stdout.log").open("wb"),
    stderr=(WORK / "qemu-stderr.log").open("wb"),
)
print("pid", proc.pid)

deadline = time.monotonic() + SECONDS
sizes = []
while time.monotonic() < deadline and proc.poll() is None:
    time.sleep(5)
    if proc.poll() is not None:
        break
    sizes.append((round(time.monotonic() - (deadline - SECONDS), 1), serial.stat().st_size if serial.is_file() else 0))

for at, size in sizes:
    marker = "  <== growing" if size > (sizes[0][1] if sizes else 0) else ""
    print(f"t={at:6.1f}s serial={size}{marker}")

if proc.poll() is None:
    subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)

print("qemu exit", proc.returncode)
text = serial.read_bytes().decode("utf-8", "replace") if serial.is_file() else ""
print(f"serial {len(text)} bytes, {len(text.splitlines())} lines")
print("--- after the hand-off ---")
for line in text.splitlines():
    if any(k in line for k in ("Darwin Kernel", "kernel_version", "vm_page", "DarwinBoot", "UNIXNU", "panic", "Kernel")):
        print("   ", line[:160])
