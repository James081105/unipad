"""Which kernel function is the CPU spinning in?

The probe showed the guest parked at 0xfffffff0073f1b94 reading
``cntvct_el0`` -- a timer-based wait -- for over 40 seconds.  A PC alone
says nothing; a function name says a great deal.  kernel.macho is a Mach-O
with a symbol table, so the VA can be mapped to a file offset through the
segment load commands and then to the nearest preceding symbol.

This is read-only: nothing here modifies the kernel.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# Usage: symbolize-kernel.py [KERNEL] [VA ...]
KERNEL = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("0x")     else Path("local-private/boot7/boot-inputs/unixnu-out/qemu/kernel/kernel.macho")
WANT = [int(a, 0) for a in sys.argv[2:]] or [0xfffffff0073f1b94]

blob = KERNEL.read_bytes()
magic, = struct.unpack_from(">I", blob, 0)
if magic not in (0xFEEDFACF, 0xCFFAEDFE):
    raise SystemExit(f"not a Mach-O 64-bit image: magic {magic:#x}")
little = magic == 0xCFFAEDFE
end = "<" if little else ">"
(magic_le,) = struct.unpack_from("<I", blob, 0)

header = struct.unpack_from(end + "7I", blob, 0)
ncmds, sizeofcmds = header[4], header[5]
print(f"Mach-O: {len(blob):,} bytes, {ncmds} load commands")

segments = []
symtab = None
at = 32
for _ in range(ncmds):
    cmd, cmdsize = struct.unpack_from(end + "II", blob, at)
    if cmd == 0x19:  # LC_SEGMENT_64
        segname = blob[at + 8:at + 24].rstrip(b"\0").decode()
        vmaddr, vmsize, fileoff, filesize = struct.unpack_from(end + "4Q", blob, at + 24)
        segments.append((segname, vmaddr, vmsize, fileoff, filesize))
    elif cmd == 0x2:  # LC_SYMTAB
        symoff, nsyms, stroff, strsize = struct.unpack_from(end + "4I", blob, at + 8)
        symtab = (symoff, nsyms, stroff, strsize)
    at += cmdsize

for name, vmaddr, vmsize, fileoff, filesize in segments:
    print(f"  segment {name:16s} vmaddr {vmaddr:#014x} size {vmsize:#x} fileoff {fileoff:#x}")

if symtab is None:
    raise SystemExit("no LC_SYMTAB: the image carries no symbol table")

symoff, nsyms, stroff, strsize = symtab
print(f"symtab: {nsyms} symbols, strtab {strsize:,} bytes at {stroff:#x}")


def to_offset(vmaddr):
    for name, base, size, fileoff, filesize in segments:
        if base <= vmaddr < base + size:
            inside = vmaddr - base
            if inside < filesize:
                return fileoff + inside
    return None


def cstr(off):
    end_at = blob.index(b"\0", off)
    return blob[off:end_at].decode("utf-8", "replace")


symbols = []
for index in range(nsyms):
    n_strx, n_type, n_sect, n_desc, n_value = struct.unpack_from(end + "IBBHQ", blob, symoff + index * 16)
    if n_strx == 0 or n_value == 0:
        continue
    symbols.append((n_value, cstr(stroff + n_strx)))
symbols.sort()
print(f"{len(symbols)} valued symbols")

for want in WANT:
    print(f"\n=== VA {want:#x} ===")
    off = to_offset(want)
    print(f"  file offset {off if off is None else hex(off)}")
    best = None
    for value, name in symbols:
        if value <= want:
            best = (value, name)
        else:
            break
    if best:
        print(f"  symbol: {best[1]}  (at {best[0]:#x}, +{want - best[0]:#x})")
    else:
        print("  no symbol at or below that address")
    # A few neighbours, so the call target of the loop is identifiable too.
    after = [(v, n) for v, n in symbols if v > want][:6]
    for value, name in after:
        print(f"    next: {value:#x} {name}")
