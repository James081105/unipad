# AArch64 boot stub — a real guest for the host-side chain

`boot-stub.elf` is a **real AArch64 bare-metal kernel** that boots on the QEMU
`virt` machine and writes three markers to the PL011 UART at `0x09000000`, then
parks the CPU in `wfe`.

## Why it exists

UniPad's launcher, QMP readiness, serial capture, screendump and stop path were
only ever exercised against `tests/fakes/fake_qemu.py`. That proves the host
side handles a well-behaved child, but it cannot prove that the argv QEMU is
given actually starts a guest — and on this host the *real* boot target
(`kernel.macho`, a patched XNU) cannot be produced at all
(`docs/implementation-plan.md`, "Tier-1 启动关键路径现状").

This stub closes that gap for the host side. It is **not** iPadOS, XNU, or any
part of UniXNU, and no claim about a real device boot may be derived from it.

## What is proven by booting it

- the exact argv UniPad builds is accepted by the real QEMU;
- real ARM64 instructions execute (it is cross-compiled, not emulated by a
  script);
- the serial log the launcher captures carries guest output;
- QMP readiness, `screendump` and a graceful `quit` all work against a live
  guest rather than a fake.

## Reproducing the binary

`boot-stub.elf` is committed so the real-boot test is hermetic and does not
need a cross-compiler. To rebuild it from `boot-stub.S`:

```sh
CLANG=<msys64>/ucrt64/bin/clang.exe
LD=<msys64>/ucrt64/bin/ld.lld.exe
"$CLANG" --target=aarch64-none-elf -c boot-stub.S -o boot-stub.o
"$LD" -m aarch64elf -T boot-stub.ld -o boot-stub.elf boot-stub.o
```

Verified against QEMU 11.1.0 with
`-M virt -cpu cortex-a710 -m 512 -smp 1 -accel tcg -kernel boot-stub.elf`.
