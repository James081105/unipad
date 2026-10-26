# Windows-side boot probes

Five tools that answer questions the serial log cannot.  They were
written during the 2026-10-24 boot of the runner's kernel on this Windows host,
and each one exists because a specific question came up and nothing else could
answer it.  A sixth, `local-private/firmware/verify-data-mount.py`, answers
whether the answers add up to success; it is described at the end and is not
committed, because the UUID it compares against is Apple-derived.

None of them modifies the guest, the kernel, or the device tree.
`probe-adt-uart.py` writes a *variant* device tree beside the original and boots
that; the original is never touched.  `probe-root-volume.py` is the one that
attaches a *second* block device to the machine -- the OS root volume, read-only
-- and reads the kernel's log out of guest memory while QEMU is still running;
it too modifies neither the guest, the kernel, nor the device tree, but unlike
`symbolize-kernel.py` it starts QEMU and writes a run directory under
`local-private/`, as `probe-guest-cpu.py`, `probe-guest-stack.py` and
`probe-adt-uart.py` already do.

| script | answers |
| --- | --- |
| `symbolize-kernel.py` | "which function is this guest address in?"  Maps a kernel VA to a file offset through the Mach-O segment load commands and to the nearest preceding symbol in `LC_SYMTAB`. |
| `probe-guest-cpu.py` | "is the guest running or wedged?"  Boots the same machine `unipad.host.unixnu_boot.boot()` builds (it calls `build_argv`, so the argv is not re-implemented) and samples `info registers` over QMP. |
| `probe-guest-stack.py` | "why is it there?"  Walks the frame-pointer chain (`X29` -> `[X29]` -> `[X29+8]`) and symbolizes every return address.  This is what proved the guest is in XNU's idle thread. |
| `probe-adt-uart.py` | "is the missing UART node why the serial is quiet?"  Adds one `arm,pl011` node to a copy of the tree and boots it.  Answer was no. |
| `probe-root-volume.py` | "does a `qemu_defconfig` kernel enumerate a second virtio-blk device at all, and does the root volume mount?"  Boots the same machine `unipad.host.unixnu_boot.boot()` builds -- it stages the ESP with `stage_esp` and calls `build_argv(..., root_image=root)`, so the argv is not re-implemented -- with the OS root volume attached as a second, read-only block device, and watches the live guest's kernel log over QMP.  Answer was no: `device=osroot inserted=True`, and the UUID the kernel waits for is in the image, but `CONFIG_XNU_NATIVE_VIRTIO_BLOCK` is absent from the `qemu_defconfig` kernel, so nothing ever enumerated it. |

## Usage

```
python scripts/windows/symbolize-kernel.py <kernel.macho> <va> [<va> ...]
python scripts/windows/probe-guest-cpu.py   [SECONDS] [BUNDLE] [ADT] [WORKDIR]
python scripts/windows/probe-guest-stack.py [SECONDS] [BUNDLE] [ADT] [WORKDIR]
python scripts/windows/probe-adt-uart.py    [SECONDS] [BUNDLE] [ADT] [WORKDIR]
python scripts/windows/probe-root-volume.py --root-image PATH [--seconds N] [--settle S] [--run DIR] [--decode-udif]
```

`BUNDLE` is the runner's `boot-inputs` directory (it carries `UniXNU.efi`,
`kernel.macho` and `DarwinBoot.efi`); `ADT` is this host's generated device tree.
The defaults name the paths this machine used, so a fresh checkout has to pass
them explicitly.  `UNIPAD_QEMU` and `UNIPAD_FIRMWARE` override the QEMU binary
and the EDK II code volume.

`probe-root-volume.py`'s `--root-image` is the OS root volume as a raw disk.  A
UDIF container is decoded first, and that is detected by sniffing the `koly`
trailer rather than by the `.dmg` suffix -- `044-76034-038.dmg` is raw APFS with
no trailer at all, while `044-76040-038.dmg` is a UDIF container -- so
`--decode-udif` only forces what the sniff already found.  Its exit codes are
load-bearing: 0 = a mountpoint marker appeared, 2 = a refusal before QEMU
starts, 3 = the UDIF decode was refused, 4 = the guest never reached the kernel
hand-off, 5 = the deadline expired with no marker.  An earlier version returned
0 when it gave up, which made a failure indistinguishable from a success.

## What they established

* `probe-guest-cpu.py` + `probe-guest-stack.py`: after the DarwinBoot hand-off
  the CPU sits at `_ml_get_timebase+0x14`, reached from `_idle_thread` via
  `_processor_idle`, with the PC unchanged across 50 seconds.  The kernel booted
  to the scheduler and is idling — not hung.
* `probe-adt-uart.py`: adding a PL011 node changed nothing on serial, so the
  device tree's lack of a UART node is **not** the reason the log goes quiet, and
  upstream's tree must not be "fixed" for it.
* `probe-root-volume.py`: the OS root volume's UUID is the one the kernel blocks
  on, and the attachment side is code-complete and verified -- QMP `query-block`
  reports `device=osroot inserted=True` while `esp` stays a separate device.  The
  kernel still could not mount it: `vendor/UniXNU-qemu/configs/qemu_defconfig`
  has no `CONFIG_XNU_NATIVE_VIRTIO_BLOCK`, so the second disk was never
  enumerated as an `IOMedia` at all.  That is a missing kernel **configuration**,
  not a missing image, and launchd as PID 1 was therefore never observed.

The first two are recorded in `docs/boot-windows.md` §1.4 and §3.4; the
root-volume measurement is in §6.

## The one that only reads: `verify-data-mount.py`

None of the five above answers the question the whole boot path is now waiting
on.  They report what the guest printed; `local-private/firmware/
verify-data-mount.py` reports whether that adds up to *success*.  It takes a run
directory and answers one question -- did the Data volume mount, and is the
original launchd running as PID 1 -- against an explicit rule, with a non-zero
exit when the answer is no:

```
python local-private/firmware/verify-data-mount.py --run DIR [--dry-run | --live]
```

It boots nothing.  `--dry-run` (the default) opens no socket and reads only what
the run directory already holds: `summary.json` for the guest globals and
`kernel.log` for the console ring, or `verify-data-mount.json` if a `--live` run
wrote one.  `--live` connects to the QMP port the run's own `command.txt` names
and reads the guest through `unipad.host.guestobserve` -- the same reader
`read-owner-state.py` uses, so there is no second memory reader -- waiting on
that script's exit condition (`_native_owner_data_stage != 0`, or a 90-second
quiet ring) rather than on the `No POST boot-arg set.` marker, which is printed
twice and breaks on the first one a guest still loading kexts.

Proved on `local-private/boot25/run-real`, the boot that carried everything
except a real Data seed: `--dry-run` reports it **not verified** with
`_native_owner_data_stage 0` and `_native_owner_data_status 2` (ENOENT), and
exits 1.  A harness that reported that boot as anything else would be broken.

The rule itself is tested in `tests/unit/test_verify_data_mount_rule.py`, which
loads the script across the `local-private/` boundary and exercises it in both
directions: a synthetic fixture that mounted the volume verifies, and eighteen
mutations each break exactly the criterion that names them.  That test is why
the script can live in a gitignored directory and still be checked.

## Why they are committed

`local-private/` is where these ran, and it is gitignored on purpose (PRD §12).
A probe that only exists in a gitignored directory cannot be re-run by anyone
else, and its result cannot be checked.  The paths default to this machine's
layout and are overridable, so the tools travel with the repository while the
Apple-derived inputs they read stay out of it.
