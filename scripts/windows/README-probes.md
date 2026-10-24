# Windows-side boot probes

Four read-only tools that answer questions the serial log cannot.  They were
written during the 2026-10-24 boot of the runner's kernel on this Windows host,
and each one exists because a specific question came up and nothing else could
answer it.

None of them modifies the guest, the kernel, or the device tree.
`probe-adt-uart.py` writes a *variant* device tree beside the original and boots
that; the original is never touched.

| script | answers |
| --- | --- |
| `symbolize-kernel.py` | "which function is this guest address in?"  Maps a kernel VA to a file offset through the Mach-O segment load commands and to the nearest preceding symbol in `LC_SYMTAB`. |
| `probe-guest-cpu.py` | "is the guest running or wedged?"  Boots the same machine `unipad.host.unixnu_boot.boot()` builds (it calls `build_argv`, so the argv is not re-implemented) and samples `info registers` over QMP. |
| `probe-guest-stack.py` | "why is it there?"  Walks the frame-pointer chain (`X29` -> `[X29]` -> `[X29+8]`) and symbolizes every return address.  This is what proved the guest is in XNU's idle thread. |
| `probe-adt-uart.py` | "is the missing UART node why the serial is quiet?"  Adds one `arm,pl011` node to a copy of the tree and boots it.  Answer was no. |

## Usage

```
python scripts/windows/symbolize-kernel.py <kernel.macho> <va> [<va> ...]
python scripts/windows/probe-guest-cpu.py   [SECONDS] [BUNDLE] [ADT] [WORKDIR]
python scripts/windows/probe-guest-stack.py [SECONDS] [BUNDLE] [ADT] [WORKDIR]
python scripts/windows/probe-adt-uart.py    [SECONDS] [BUNDLE] [ADT] [WORKDIR]
```

`BUNDLE` is the runner's `boot-inputs` directory (it carries `UniXNU.efi`,
`kernel.macho` and `DarwinBoot.efi`); `ADT` is this host's generated device tree.
The defaults name the paths this machine used, so a fresh checkout has to pass
them explicitly.  `UNIPAD_QEMU` and `UNIPAD_FIRMWARE` override the QEMU binary
and the EDK II code volume.

## What they established

* `probe-guest-cpu.py` + `probe-guest-stack.py`: after the DarwinBoot hand-off
  the CPU sits at `_ml_get_timebase+0x14`, reached from `_idle_thread` via
  `_processor_idle`, with the PC unchanged across 50 seconds.  The kernel booted
  to the scheduler and is idling — not hung.
* `probe-adt-uart.py`: adding a PL011 node changed nothing on serial, so the
  device tree's lack of a UART node is **not** the reason the log goes quiet, and
  upstream's tree must not be "fixed" for it.

Both are recorded in `docs/boot-windows.md` §1.4 and §3.4.

## Why they are committed

`local-private/` is where these ran, and it is gitignored on purpose (PRD §12).
A probe that only exists in a gitignored directory cannot be re-run by anyone
else, and its result cannot be checked.  The paths default to this machine's
layout and are overridable, so the tools travel with the repository while the
Apple-derived inputs they read stay out of it.
